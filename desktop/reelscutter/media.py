"""Reading video files: metadata, fingerprints, thumbnails and content analysis (scenes, silence, loudness)."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import paths

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".ts", ".wmv", ".flv", ".mpg",
              ".mpeg", ".3gp"}

NO_WINDOW = {}
if os.name == "nt":  # don't flash console windows on Windows
    NO_WINDOW = {"creationflags": 0x08000000}


def is_video(path) -> bool:
    p = Path(path)
    return p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("._")


def fingerprint(path) -> str:
    """Fast content fingerprint for duplicate detection: size + first/middle/last 1 MB."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(p, "rb") as f:
        for off in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
            f.seek(off)
            h.update(f.read(1048576))
    return h.hexdigest()


def probe(path) -> dict:
    """Duration, size, codecs and frame rate. Raises if the file isn't a readable video."""
    import av
    with av.open(str(path), metadata_errors="ignore") if _av_has_metadata_errors() else av.open(str(path)) as c:
        v = next((s for s in c.streams if s.type == "video"), None)
        a = next((s for s in c.streams if s.type == "audio"), None)
        if v is None:
            raise ValueError("no video stream")
        duration = None
        if c.duration:
            duration = c.duration / 1_000_000
        elif v.duration and v.time_base:
            duration = float(v.duration * v.time_base)
        fps = None
        rate = v.average_rate or v.guessed_rate or v.base_rate
        if rate:
            fps = float(rate)
        return {
            "duration": duration or 0.0,
            "width": v.codec_context.width,
            "height": v.codec_context.height,
            "fps": round(fps, 3) if fps else None,
            "vcodec": v.codec_context.name,
            "acodec": a.codec_context.name if a else None,
            "fmt": Path(path).suffix.lower().lstrip("."),
        }


_meta_kw = None


def _av_has_metadata_errors():
    global _meta_kw
    if _meta_kw is None:
        import av
        try:
            import inspect
            _meta_kw = "metadata_errors" in inspect.signature(av.open).parameters
        except (TypeError, ValueError):
            _meta_kw = False
    return _meta_kw


def make_thumbnail(path, duration, video_id) -> str:
    out = paths.data_dir() / "thumbs" / f"{video_id}.jpg"
    t = max(0.0, min(duration * 0.1, max(0.0, duration - 0.5))) if duration else 0
    cmd = [paths.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
           "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(out)]
    subprocess.run(cmd, check=True, timeout=60, capture_output=True, **NO_WINDOW)
    return str(out)


def extract_frame(path, t, out, width=640):
    cmd = [paths.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(path),
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)]
    subprocess.run(cmd, check=True, timeout=60, capture_output=True, **NO_WINDOW)
    return out


def analyze(path, has_audio=True, cancel=None) -> dict:
    """One low-resolution pass over the file: scene changes, black frames, silence and loudness per second.

    Streams the file through FFmpeg at 2 fps / 160 px wide, so memory use stays tiny even for multi-hour videos.
    """
    from .hardware import temp_dir
    tmp = Path(tempfile.mkdtemp(dir=temp_dir()))
    scenes_f, rms_f = tmp / "scenes.txt", tmp / "rms.txt"
    vf = (f"fps=2,scale=160:-2,blackdetect=d=1:pix_th=0.10,select='gt(scene\\,0.32)',"
          f"metadata=print:file={_ff_path(scenes_f)}")
    cmd = [paths.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
           "-map", "0:v:0", "-vf", vf, "-f", "null", "-"]
    if has_audio:
        af = (f"aresample=8000,silencedetect=n=-38dB:d=0.8,asetnsamples=n=8000,astats=metadata=1:reset=1,"
              f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={_ff_path(rms_f)}")
        cmd += ["-map", "0:a:0", "-af", af, "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            errors="replace", **NO_WINDOW)
    err_lines = []
    for line in proc.stderr:
        err_lines.append(line)
        if cancel and cancel():
            proc.kill()
            raise InterruptedError("canceled")
    proc.wait()
    err = "".join(err_lines)
    if proc.returncode != 0:
        raise RuntimeError("analysis failed: " + err.strip().splitlines()[-1] if err.strip() else "analysis failed")

    scenes = []
    if scenes_f.exists():
        for m in re.finditer(r"pts_time:([\d.]+)", scenes_f.read_text(errors="replace")):
            scenes.append(round(float(m.group(1)), 2))
    rms = []
    if rms_f.exists():
        for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", rms_f.read_text(errors="replace")):
            v = m.group(1)
            rms.append(-90.0 if v == "-inf" else max(-90.0, float(v)))
    silence = _pairs(err, r"silence_start: (-?[\d.]+)", r"silence_end: ([\d.]+)")
    black = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    for f in (scenes_f, rms_f):
        f.unlink(missing_ok=True)
    tmp.rmdir()
    return {"scenes": scenes, "rms": [round(x, 1) for x in rms], "silence": silence, "black": black}


def _pairs(text, start_re, end_re):
    starts = [max(0.0, float(x)) for x in re.findall(start_re, text)]
    ends = [float(x) for x in re.findall(end_re, text)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append((round(s, 2), round(e, 2) if e is not None else None))
    return out


def _ff_path(p):
    """Escape a path for use inside an FFmpeg filter argument."""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\\\:").replace("'", "\\\\'").replace(",", "\\\\,")


def dumps(a):
    return json.dumps(a, separators=(",", ":"))
