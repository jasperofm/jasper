"""AI analysis pipeline: runs locally, one video at a time, results cached in SQLite.

Levels
  fast      scenes, black frames, silence, loudness (one low-res FFmpeg pass)
  standard  fast + transcript with word timestamps (local Whisper, if a model is installed)
  advanced  standard + face positions (for auto-reframe) + picture quality (brightness / sharpness)
  custom    any combination of the modules above
"""
import json
import subprocess
import threading
import time

from . import db, media, paths
from .media import NO_WINDOW

MODULES = {
    "scenes_audio": "Scenes, black frames, silence and loudness",
    "transcript": "Transcript with word timestamps (local speech recognition)",
    "faces": "Face positions for smart reframing",
    "quality": "Picture quality (brightness, sharpness)",
}
LEVELS = {
    "fast": ["scenes_audio"],
    "standard": ["scenes_audio", "transcript"],
    "advanced": ["scenes_audio", "transcript", "faces", "quality"],
}


LIGHT = ("SELECT id, name, duration, status, analysis_level, analysis IS NOT NULL has_a, "
         "instr(analysis,'\"faces\"')>0 has_f, instr(analysis,'\"quality\"')>0 has_q, transcript IS NOT NULL has_t "
         "FROM videos")


def done_modules(v):
    """Modules already analyzed. Accepts a LIGHT row (cheap) or a full videos row."""
    keys = v.keys()
    if "has_a" in keys:
        have = set()
        if v["has_a"]:
            have.add("scenes_audio")
        if v["has_t"]:
            have.add("transcript")
        if v["has_f"]:
            have.add("faces")
        if v["has_q"]:
            have.add("quality")
        return have
    a = json.loads(v["analysis"]) if v["analysis"] else {}
    have = set()
    if a.get("scenes") is not None and "rms" in a:
        have.add("scenes_audio")
    if v["transcript"] is not None:
        have.add("transcript")
    if a.get("faces") is not None:
        have.add("faces")
    if a.get("quality") is not None:
        have.add("quality")
    return have


def estimate_seconds(durations, modules):
    """Estimated wall time from this Mac's benchmark; None when there's no benchmark yet."""
    from . import hardware
    rep = hardware.last_report() or {}
    res = rep.get("results", {})
    total = sum(durations)
    secs = 0.0
    known = True
    if "scenes_audio" in modules:
        if res.get("analysis"):
            secs += total / res["analysis"]
        else:
            known = False
    if "transcript" in modules:
        if res.get("whisper_tiny"):
            factor = {"tiny": 1, "base": 1.8, "small": 5, "medium": 14}.get(db.setting("whisper_model"), 2)
            secs += total / res["whisper_tiny"] * factor
        else:
            known = False
    if "faces" in modules or "quality" in modules:
        if res.get("analysis"):
            secs += total / (res["analysis"] * 3)
        else:
            known = False
    return secs if known else None


def _frames_pass(path, duration, want_faces, want_quality, cancel=None, every=2.0):
    """Stream small grayscale frames (one every `every` s) through OpenCV. Memory stays constant."""
    import numpy as np
    import cv2
    H = 360
    W_probe = media.probe(path)
    w = int(round(W_probe["width"] * H / max(1, W_probe["height"]) / 2) * 2)
    cmd = [paths.ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:v:0", "-vf",
           f"fps=1/{every},scale={w}:{H},format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **NO_WINDOW)
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml") if want_faces \
        else None
    faces, bright, sharp = [], [], []
    size = w * H
    i = 0
    try:
        while True:
            if cancel and cancel():
                proc.kill()
                raise InterruptedError("canceled")
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            img = np.frombuffer(buf, np.uint8).reshape(H, w)
            t = round(i * every, 2)
            if det is not None:
                found = det.detectMultiScale(img, 1.1, 5, minSize=(28, 28))
                if len(found):
                    fs = sorted(found, key=lambda f: -f[2] * f[3])[:3]
                    faces.append([t] + [round((x + fw / 2) / w, 3) for x, y, fw, fh in fs] + [len(found)])
            if want_quality:
                bright.append(float(img.mean()))
                sharp.append(float(cv2.Laplacian(img, cv2.CV_64F).var()))
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()
    out = {}
    if want_faces:
        out["faces"] = faces          # [t, x_biggest, x2?, x3?, count]
    if want_quality:
        n = max(1, len(bright))
        out["quality"] = dict(brightness=round(sum(bright) / n, 1), sharpness=round(sorted(sharp)[len(sharp) // 2], 1)
                              if sharp else 0, dark_pct=round(100 * sum(b < 25 for b in bright) / n, 1),
                              blurry_pct=round(100 * sum(s < 40 for s in sharp) / n, 1))
    return out


def analyze_video(vid, modules, cancel=None, progress=lambda msg: None):
    from . import subtitles
    v = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        raise ValueError("video not found")
    a = json.loads(v["analysis"]) if v["analysis"] else {}
    if "scenes_audio" in modules:
        progress("scenes and audio")
        a.update(media.analyze(v["path"], has_audio=bool(v["acodec"]), cancel=cancel))
        db.run("UPDATE videos SET analysis=? WHERE id=?", (media.dumps(a), vid))
    if ("faces" in modules or "quality" in modules):
        progress("faces and picture quality")
        a.update(_frames_pass(v["path"], v["duration"], "faces" in modules, "quality" in modules, cancel))
        db.run("UPDATE videos SET analysis=? WHERE id=?", (media.dumps(a), vid))
    if "transcript" in modules and v["acodec"]:
        if not subtitles.available() or not subtitles.current_model_name():
            raise RuntimeError("Transcript needs a speech model. Install one in AI Analysis → Models.")
        progress("transcript")
        words = []
        step = 600.0                    # 10-minute chunks keep memory bounded on long videos
        t = 0.0
        while t < v["duration"]:
            if cancel and cancel():
                raise InterruptedError("canceled")
            words += subtitles.transcribe_file(v["path"], t, min(v["duration"], t + step))
            t += step
            progress(f"transcript {min(100, int(100 * t / v['duration']))}%")
        db.run("UPDATE videos SET transcript=? WHERE id=?", (json.dumps(words, separators=(",", ":")), vid))
        subtitles.unload() if db.setting("model_idle_minutes", "5") == "0" else None
    level = ("advanced" if {"faces", "quality"} <= set(modules) | done_modules(v) else
             "standard" if "transcript" in set(modules) | done_modules(v) else "fast")
    db.run("UPDATE videos SET analysis_level=? WHERE id=?", (level, vid))


class AnalysisRunner:
    """Background analysis queue (bounded: one video at a time on Safe/Balanced)."""

    def __init__(self):
        self.pending = []         # (vid, modules)
        self.current = None
        self.status = ""
        self.errors = {}
        self.stop_flag = False
        self.lock = threading.Lock()
        self.thread = None

    def add(self, vids, modules):
        with self.lock:
            have = {p[0] for p in self.pending}
            for v in vids:
                if v not in have and v != self.current:
                    self.pending.append((v, list(modules)))
        if not self.thread or not self.thread.is_alive():
            self.stop_flag = False
            self.thread = threading.Thread(target=self._loop, daemon=True, name="analysis")
            self.thread.start()

    def cancel_all(self):
        with self.lock:
            self.pending.clear()
        self.stop_flag = True

    def _loop(self):
        from .jobs import events
        while True:
            with self.lock:
                if not self.pending:
                    self.current = None
                    self.status = ""
                    break
                vid, modules = self.pending.pop(0)
                self.current = vid
            self.stop_flag = False
            prev = db.q1("SELECT status FROM videos WHERE id=?", (vid,))
            try:
                db.run("UPDATE videos SET status='analyzing' WHERE id=?", (vid,))
                events.emit("analysis_changed")
                t0 = time.time()
                analyze_video(vid, modules, cancel=lambda: self.stop_flag,
                              progress=lambda m: (setattr(self, "status", m), events.emit("analysis_changed")))
                self.errors.pop(vid, None)
                db.log("info", f"Analyzed video {vid} ({', '.join(modules)}) in {time.time() - t0:.0f}s")
            except InterruptedError:
                pass
            except Exception as e:
                self.errors[vid] = str(e)
                db.log("error", f"Analysis failed for video {vid}: {e}")
            finally:
                st = prev["status"] if prev and prev["status"] != "analyzing" else "new"
                db.run("UPDATE videos SET status=? WHERE id=? AND status='analyzing'", (st, vid))
                events.emit("analysis_changed")


runner = AnalysisRunner()


def search_transcripts(text, limit=200):
    """Find words/phrases in cached transcripts. Returns [(video_id, name, t, context)]."""
    text = text.strip().lower()
    if not text:
        return []
    out = []
    terms = text.split()
    for r in db.q("SELECT id,name,transcript FROM videos WHERE transcript IS NOT NULL AND transcript LIKE ?",
                  (f"%{terms[0]}%",)):
        words = json.loads(r["transcript"])
        low = [w[2].lower().strip(".,!?\"'") for w in words]
        n = len(terms)
        for i in range(len(words) - n + 1):
            if all(low[i + k].startswith(terms[k]) if k == n - 1 else low[i + k] == terms[k] for k in range(n)):
                ctx = " ".join(w[2] for w in words[max(0, i - 6): i + n + 8])
                out.append((r["id"], r["name"], words[i][0], ctx))
                if len(out) >= limit:
                    return out
    return out
