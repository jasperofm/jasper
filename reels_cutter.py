#!/usr/bin/env python3
"""
reels_cutter.py - turn one long video you own into several different vertical Reels.

For each Reel it picks a different moment of the video, a different length
(spread across --min..--max seconds), shows its own hook text for the first
few seconds, and burns in word-by-word captions from a Whisper transcript.

Usage:
    python reels_cutter.py my_video.mp4 --count 5
    python reels_cutter.py my_video.mp4 --count 6 --min 20 --max 60 --layout crop
    python reels_cutter.py my_video.mp4 --hooks hooks.txt     # your own hook lines, one per line

Requires: ffmpeg on PATH, and `pip install faster-whisper`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

W, H = 1080, 1920


def find_ffmpeg():
    """System ffmpeg if installed, otherwise the one bundled with `pip install imageio-ffmpeg`."""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit("ffmpeg not found. Run: pip3 install imageio-ffmpeg")


FFMPEG = None

HOOK_WORDS = {
    "secret", "mistake", "mistakes", "never", "always", "stop", "why", "how",
    "best", "worst", "truth", "nobody", "everyone", "actually", "real", "wrong",
    "biggest", "easiest", "fastest", "simple", "crazy", "insane", "important",
    "need", "must", "don't", "dont", "what", "you", "your", "here's", "heres",
}


# ---------------------------------------------------------------- transcription
def transcribe(video, model_name, language, cache_path):
    if cache_path.exists():
        print(f"Using cached transcript {cache_path}")
        return json.loads(cache_path.read_text())
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed. Run: pip install faster-whisper")
    print(f"Transcribing with Whisper '{model_name}' (first run downloads the model)...")
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    # Decode audio with ffmpeg ourselves: newer PyAV releases break faster-whisper's own decoder.
    import numpy as np
    pcm = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segments, info = model.transcribe(
        audio, language=language, word_timestamps=True, vad_filter=True
    )
    out = []
    for s in segments:
        words = [
            {"start": w.start, "end": w.end, "word": w.word.strip()}
            for w in (s.words or []) if w.word.strip()
        ]
        if words:
            out.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words})
        print(f"  [{s.start:7.1f}s] {s.text.strip()[:70]}")
    cache_path.write_text(json.dumps(out, indent=1))
    return out


def video_duration(video):
    import av  # installed with faster-whisper
    with av.open(str(video)) as c:
        return c.duration / 1_000_000


# ---------------------------------------------------------------- picking moments
def hook_score(text):
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if not words:
        return 0.0
    s = sum(1 for w in words if w in HOOK_WORDS) / len(words) * 4
    if "?" in text:
        s += 1.0
    if re.search(r"\d", text):
        s += 0.7
    if 4 <= len(words) <= 16:
        s += 0.5
    return s


def candidates(segs, lo, hi):
    """Every run of consecutive transcript segments whose span is lo..hi seconds."""
    out = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            dur = segs[j]["end"] - segs[i]["start"]
            if dur > hi:
                break
            if dur < lo:
                continue
            words = [w for s in segs[i:j + 1] for w in s["words"]]
            speech = sum(w["end"] - w["start"] for w in words)
            density = speech / dur                       # little dead air
            ends_clean = segs[j]["text"].rstrip()[-1:] in ".!?"
            score = density * 2 + hook_score(segs[i]["text"]) + (0.8 if ends_clean else 0)
            out.append({"i": i, "j": j, "start": segs[i]["start"], "end": segs[j]["end"],
                        "dur": dur, "score": score})
    return out


def pick_clips(segs, count, lo, hi, gap=2.0, used=()):
    """Choose `count` non-overlapping clips, each aiming at a different length."""
    if count == 1:
        targets = [(lo + hi) / 2]
    else:
        targets = [lo + (hi - lo) * k / (count - 1) for k in range(count)]
    chosen = []
    blocked = [{"start": u[0], "end": u[1]} for u in used]   # moments used in earlier runs
    used_hooks = set()
    for t in sorted(targets, reverse=True):       # longest first: they are hardest to fit
        band = max(5.0, (hi - lo) / max(count, 1) / 1.5)
        best = None
        for widen in (1, 2, 4, 100):
            pool = [c for c in candidates(segs, max(lo, t - band * widen), min(hi, t + band * widen))
                    if all(c["end"] + gap <= p["start"] or c["start"] >= p["end"] + gap for p in chosen + blocked)]
            if pool:
                best = max(pool, key=lambda c: c["score"] - abs(c["dur"] - t) / 30
                           - (3 if segs[c["i"]]["text"].lower() in used_hooks else 0))
                break
        if best is None:
            print(f"  Not enough unused footage for another {lo:.0f}-{hi:.0f}s clip; stopping at {len(chosen)}.")
            break
        chosen.append(best)
        used_hooks.add(segs[best["i"]]["text"].lower())
    return sorted(chosen, key=lambda c: c["start"])


def make_hook(text, max_words=9):
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0]
    words = first.split()
    hook = " ".join(words[:max_words]).rstrip(",;:")
    if len(words) > max_words:
        hook += "..."
    return hook.upper()


# ---------------------------------------------------------------- AI picking (optional)
def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    f = Path.home() / ".reels_cutter_key"
    if not key and f.exists():
        key = f.read_text().strip()
    return key


def ai_pick(segs, count, lo, hi, used, api_key):
    """Ask Claude for the most engaging moments + hooks + post captions. Returns [] on any failure."""
    try:
        import anthropic
    except ImportError:
        print("  (AI picking skipped: run  pip3 install anthropic)")
        return []
    lines = [f"[{i}] {s['start']:.1f}-{s['end']:.1f}s: {s['text']}" for i, s in enumerate(segs)]
    used_txt = ", ".join(f"{a:.0f}-{b:.0f}s" for a, b in used) or "none"
    prompt = f"""Here is a timestamped transcript of a long video, one numbered segment per line.

<transcript>
{chr(10).join(lines)}
</transcript>

Pick the {count} most engaging, self-contained moments to post as Instagram Reels. Each moment is a run of consecutive segments (start_seg to end_seg, inclusive) lasting between {lo:.0f} and {hi:.0f} seconds, and should make sense to someone who hasn't seen the rest of the video: it opens strong and ends on a complete thought. Prefer a mix of lengths. Moments must not overlap each other or these already-used time ranges: {used_txt}.

For each moment also write:
- hook: a short on-screen title (max 8 words) that makes people stop scrolling, true to what is said
- caption: an Instagram post caption (1-3 short sentences, may end with a question to drive comments)
- hashtags: 5 relevant hashtags without the # sign

Reply with only JSON in this shape:
{{"clips": [{{"start_seg": 0, "end_seg": 5, "hook": "...", "caption": "...", "hashtags": ["..."]}}]}}"""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default", "output_config": {"effort": "medium"}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            print("  (AI picking declined; using built-in picker)")
            return []
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as e:
        print(f"  (AI picking failed: {e}; using built-in picker)")
        return []
    chosen, blocked = [], [{"start": a, "end": b} for a, b in used]
    for c in data.get("clips", []):
        try:
            i, j = int(c["start_seg"]), int(c["end_seg"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (0 <= i <= j < len(segs)):
            continue
        st, en = segs[i]["start"], segs[j]["end"]
        if not (lo - 1 <= en - st <= hi + 3):
            continue
        if any(not (en + 1 <= p["start"] or st >= p["end"] + 1) for p in chosen + blocked):
            continue
        tags = [t.strip().lstrip("#").replace(" ", "") for t in c.get("hashtags", []) if str(t).strip()]
        chosen.append({"i": i, "j": j, "start": st, "end": en, "dur": en - st, "score": 99,
                       "hook": str(c.get("hook", "")).strip().upper()[:70],
                       "caption": str(c.get("caption", "")).strip(), "hashtags": tags[:8]})
        if len(chosen) == count:
            break
    return chosen


STOPWORDS = set("""the and that this with have from your what when they them then there their about would could should
just like really because which while where these those into over than also some more most very been were will being
going gonna wanna thing things people know think want make right okay yeah well here even only other after before""".split())


def post_caption(hook, text, tags=None):
    """Instagram caption + hashtags. Uses the AI's when given, else builds one from the transcript."""
    if not tags:
        words = [w for w in re.findall(r"[a-zA-Z]{5,}", text.lower()) if w not in STOPWORDS]
        top = sorted(set(words), key=lambda w: -words.count(w))[:4]
        tags = top + ["reels", "explore"]
    first = re.split(r"(?<=[.!?])\s+", text.strip())[:2]
    body = text if len(" ".join(first)) < 20 else " ".join(first)
    return f"{body}\n\n" + " ".join("#" + t.lower() for t in tags)


# ---------------------------------------------------------------- face tracking (optional)
def face_center(video, start, end):
    """Median horizontal face position (0..1) in this part of the video, or None."""
    try:
        return _face_center(video, start, end)
    except Exception as e:
        print(f"  (face tracking unavailable: {e})")
        return None


def _face_center(video, start, end):
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(video))
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    xs = []
    steps = max(1, min(40, int((end - start) / 0.5)))
    for k in range(steps):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + (end - start) * k / steps) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (w * 360 // h, 360))
        faces = det.detectMultiScale(small, 1.1, 5, minSize=(30, 30))
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + fw / 2) / small.shape[1])
    cap.release()
    if len(xs) < max(2, steps // 4):
        return None
    xs.sort()
    return xs[len(xs) // 2]


# ---------------------------------------------------------------- jump cuts
def keep_ranges(words, clip_start, clip_end, max_pause=0.35, pad=0.08):
    """Spans (relative to clip start) that contain speech; pauses longer than max_pause are cut."""
    ranges = []
    for w in words:
        a = max(0.0, w["start"] - pad - clip_start)
        b = min(clip_end - clip_start, w["end"] + pad - clip_start)
        if ranges and a - ranges[-1][1] <= max_pause:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    return [(a, b) for a, b in ranges if b - a > 0.05]


def remap(t, ranges):
    """Time in the original clip -> time in the jump-cut clip."""
    acc = 0.0
    for a, b in ranges:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


# ---------------------------------------------------------------- captions (ASS)
def ass_time(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# name: (fontsize, text colour, highlight colour, outline colour, back colour, border style, outline, shadow, marginV)
STYLES = {
    "classic": (84, "&H00FFFFFF", "&H0000FFFF", "&H00000000", "&H64000000", 1, 6, 2, 520),
    "bold":    (104, "&H00FFFFFF", "&H0000FF00", "&H00000000", "&H00000000", 1, 9, 0, 560),
    "boxed":   (76, "&H00FFFFFF", "&H0000D7FF", "&H00000000", "&H50000000", 3, 14, 0, 520),
    "minimal": (64, "&H00FFFFFF", "&H00FFFFFF", "&H00000000", "&H80000000", 1, 2, 1, 380),
}


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def write_ass(path, words, clip_start, clip_end, hook, hook_secs, font, chunk=3, ranges=None, style="classic"):
    fs, col, hi_col, out_col, back, border, outl, shad, mv = STYLES.get(style, STYLES["classic"])
    if ranges is None:
        ranges = [(0.0, clip_end - clip_start)]
    rt = lambda t: remap(t - clip_start, ranges)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{fs},{col},{hi_col},{out_col},{back},-1,0,0,0,100,100,0,0,{border},{outl},{shad},2,80,80,{mv},1
Style: Hook,{font},78,&H00000000,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,24,0,8,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(hook_secs)},Hook,,0,0,0,,{{\\fad(150,250)}}{ass_escape(hook)}")
    ws = [w for w in words if w["end"] > clip_start and w["start"] < clip_end]
    groups, cur = [], []                                     # <= chunk words, never across a sentence end
    for w in ws:
        cur.append(w)
        if len(cur) == chunk or w["word"][-1:] in ".!?,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    for gi, group in enumerate(groups):
        g_end = group[-1]["end"]
        nxt = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else g_end + 0.4
        g_end = min(max(g_end, nxt), g_end + 0.6)          # bridge tiny gaps, not long pauses
        for idx, w in enumerate(group):                      # one event per word -> highlight
            st = w["start"]
            en = group[idx + 1]["start"] if idx + 1 < len(group) else g_end
            txt = " ".join(
                ("{\\c" + hi_col + "&}" + ass_escape(x["word"].upper()) + "{\\c" + col + "&}") if n == idx
                else ass_escape(x["word"].upper())
                for n, x in enumerate(group)
            )
            if rt(en) - rt(st) > 0.01:
                lines.append(f"Dialogue: 0,{ass_time(rt(st))},{ass_time(rt(en))},Cap,,0,0,0,,{txt}")
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- rendering
LOOKS = {
    "none": "",
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.06,eq=saturation=1.1",
    "cool": "colorbalance=rs=-0.05:gs=0:bs=0.06,eq=saturation=1.05",
    "punchy": "eq=contrast=1.12:saturation=1.25:brightness=0.01",
    "bw": "hue=s=0,eq=contrast=1.12",
}


def render(video, out_dir, name, start, end, ass_name, layout, crf, ranges=None, look="none", enhance=True,
           title="", description="", face_x=None, music=None):
    pre = "fps=30"
    apre = "anull"
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11"   # Instagram-friendly loudness, consistent between Reels
    if ranges:
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)
        pre += f",select='{expr}',setpts=N/30/TB"
        apre = f"aselect='{expr}',asetpts=N/SR/TB"
    post = []
    if enhance:
        post.append("hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.6:5:5:0")
    if LOOKS.get(look):
        post.append(LOOKS[look])
    post.append(f"setsar=1,ass={ass_name}")
    post = ",".join(post)
    if layout == "crop":
        cx = "(iw-ow)/2" if face_x is None else f"max(0\\,min(iw-ow\\,iw*{face_x:.3f}-ow/2))"
        vf = f"[0:v]{pre},scale=-2:{H}:flags=lanczos,crop={W}:{H}:{cx}:0,{post}[v]"
    else:  # blur: whole frame fitted, blurred copy fills the background
        vf = (f"[0:v]{pre},split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=30:3[bg];[b]scale={W}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,{post}[v]")
    inputs = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(Path(video).resolve())]
    if music:
        # music loops under the voice and ducks whenever someone talks
        inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        vf += (f";[0:a]{apre},{norm},asplit[v1][v2];[1:a]aresample=48000,volume=0.22[m];"
               f"[m][v2]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[duck];"
               f"[v1][duck]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        vf += f";[0:a]{apre},{norm}[a]"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *inputs,
        "-filter_complex", vf, "-map", "[v]", "-map", "[a]", "-r", "30",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-level", "4.2", "-x264-params", "aq-mode=3:deblock=-1,-1",
        "-maxrate", "25M", "-bufsize", "50M", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
        "-metadata", f"title={title}", "-metadata", f"comment={description}",
        "-metadata", f"description={description}", "-movflags", "+faststart", name,
    ]
    subprocess.run(cmd, cwd=out_dir, check=True)


# ---------------------------------------------------------------- gallery
def write_gallery(library):
    """One HTML page that shows every Reel in the library folder."""
    import html
    cards = []
    for manifest in sorted(library.glob("*/reels.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            reels = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        folder = manifest.parent.name
        for r in reels:
            if not (manifest.parent / r["file"]).exists():
                continue
            src = f"{folder}/{r['file']}"
            cards.append(f"""<figure><video src="{html.escape(src)}" controls preload="metadata" playsinline></video>
<figcaption><b>{html.escape(r['hook'])}</b><span>{html.escape(folder)} · {r['length']:.0f}s · from {r['source_start']:.0f}s</span>
<details><summary>Post caption</summary><textarea readonly>{html.escape(r.get('post_caption', ''))}</textarea>
<button onclick="navigator.clipboard.writeText(this.previousElementSibling.value);this.textContent='Copied'">Copy caption</button></details>
<details><summary>Transcript</summary><p>{html.escape(r['transcript'])}</p></details>
<a href="{html.escape(src)}" download>Download</a></figcaption></figure>""")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Reels</title><style>
body{{margin:0;background:#111;color:#eee;font:15px -apple-system,Helvetica,sans-serif;padding:24px}}
h1{{margin:0 0 20px}}main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:20px}}
figure{{margin:0;background:#1c1c1c;border-radius:14px;overflow:hidden}}video{{width:100%;aspect-ratio:9/16;background:#000;display:block}}
figcaption{{padding:12px;display:flex;flex-direction:column;gap:6px}}span{{color:#999;font-size:13px}}
a{{color:#7cf}}details p{{color:#bbb;font-size:13px}}summary{{cursor:pointer}}
textarea{{width:100%;box-sizing:border-box;height:110px;background:#111;color:#eee;border:1px solid #333;border-radius:8px;padding:8px;font:13px -apple-system,sans-serif;margin-top:6px}}
button{{margin-top:6px;background:#7cf;color:#000;border:0;border-radius:8px;padding:6px 10px;font-weight:600;cursor:pointer}}
</style></head><body><h1>My Reels ({len(cards)})</h1><main>{''.join(cards) or '<p>No Reels yet.</p>'}</main></body></html>"""
    path = library / "gallery.html"
    path.write_text(page, encoding="utf-8")
    return path


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Cut one long video into several different vertical Reels.")
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="output folder (default: ~/Movies/Reels Cutter/<video name>)")
    ap.add_argument("--library", default=str(Path.home() / "Movies" / "Reels Cutter"),
                    help="folder that holds all your Reels and gallery.html")
    ap.add_argument("--open", action="store_true", help="open the gallery when done")
    ap.add_argument("--count", type=int, default=5, help="how many Reels (default 5)")
    ap.add_argument("--min", type=float, default=15, help="shortest Reel in seconds (default 15)")
    ap.add_argument("--max", type=float, default=90, help="longest Reel in seconds (default 90)")
    ap.add_argument("--layout", choices=["blur", "crop"], default="blur",
                    help="blur = full frame on blurred background (default); crop = center crop to fill")
    ap.add_argument("--model", default="small", help="Whisper model: tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default=None, help="e.g. en, nl (default: auto-detect)")
    ap.add_argument("--hooks", default=None, help="text file with your own hook lines, one per Reel")
    ap.add_argument("--hook-seconds", type=float, default=3.0)
    ap.add_argument("--font", default="Arial")
    ap.add_argument("--crf", type=int, default=16, help="quality, lower = better/bigger (default 16, very high)")
    ap.add_argument("--look", choices=list(LOOKS), default="none", help="color look: none/warm/cool/punchy/bw")
    ap.add_argument("--no-jumpcuts", action="store_true", help="keep pauses instead of cutting them out")
    ap.add_argument("--no-enhance", action="store_true", help="skip denoise + sharpen")
    ap.add_argument("--style", choices=list(STYLES), default="classic", help="caption style")
    ap.add_argument("--music", default=None,
                    help="music file or folder (default: <library>/music if it exists); 'none' to turn off")
    ap.add_argument("--no-ai", action="store_true", help="don't use Claude to pick moments even if a key is set")
    ap.add_argument("--no-face", action="store_true", help="center the crop instead of following faces")
    ap.add_argument("--dry-run", action="store_true", help="only print the chosen moments")
    a = ap.parse_args()

    global FFMPEG
    FFMPEG = find_ffmpeg()
    video = Path(a.video)
    if not video.exists():
        sys.exit(f"Not found: {video}")
    if not 5 <= a.min <= a.max:
        sys.exit("--min must be >= 5 and <= --max")
    library = Path(a.library).expanduser()
    out = Path(a.out) if a.out else library / video.stem
    out.mkdir(parents=True, exist_ok=True)

    segs = transcribe(video, a.model, a.language, out / "transcript.json")
    if not segs:
        sys.exit("No speech found in the video, so there is nothing to caption.")
    total = video_duration(video)
    if total < a.min:
        sys.exit(f"The video is only {total:.0f}s long, shorter than --min.")

    old = []
    if (out / "reels.json").exists():
        old = json.loads((out / "reels.json").read_text(encoding="utf-8"))
    used = [(r["source_start"], r["source_end"]) for r in old]
    if used:
        print(f"{len(used)} Reels already made from this video; picking new moments.")
    clips = []
    key = "" if a.no_ai else load_api_key()
    if key:
        print("Asking Claude to pick the best moments...")
        clips = ai_pick(segs, a.count, a.min, a.max, used, key)
        print(f"  Claude picked {len(clips)} moments.")
    if len(clips) < a.count:
        more = pick_clips(segs, a.count - len(clips), a.min, a.max,
                          used=used + [(c["start"], c["end"]) for c in clips])
        clips = sorted(clips + more, key=lambda c: c["start"])

    tracks = []
    music_src = a.music if a.music else str(library / "music")
    if music_src.lower() != "none" and Path(music_src).expanduser().exists():
        mp = Path(music_src).expanduser()
        tracks = sorted(mp.glob("*")) if mp.is_dir() else [mp]
        tracks = [t for t in tracks if t.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg")]
        if tracks:
            print(f"Background music: {len(tracks)} track(s) from {mp}")
    if not clips:
        print("No unused moments left in this video. Try a lower --min, or a new video.")
    custom_hooks = []
    if a.hooks:
        custom_hooks = [l.strip() for l in Path(a.hooks).read_text(encoding="utf-8").splitlines() if l.strip()]

    manifest = list(old)
    first = len(old) + 1
    for n, c in enumerate(clips, first):
        clip_segs = segs[c["i"]:c["j"] + 1]
        words = [w for s in clip_segs for w in s["words"]]
        start = max(0.0, c["start"] - 0.15)
        end = min(total, c["end"] + 0.35)
        hook = (custom_hooks[n - first] if n - first < len(custom_hooks)
                else c.get("hook") or make_hook(clip_segs[0]["text"]))
        ranges = None if a.no_jumpcuts else keep_ranges(words, start, end)
        final_len = sum(b - x for x, b in ranges) if ranges else end - start
        name = f"reel_{n:02d}_{int(final_len)}s.mp4"
        ass_name = f"reel_{n:02d}.ass"
        cut_note = f", {end - start - final_len:.1f}s of pauses cut" if ranges else ""
        print(f"\nReel {n}: {start:.1f}s -> {end:.1f}s ({final_len:.0f}s{cut_note})  hook: {hook}")
        manifest.append({
            "file": name, "source_start": round(start, 2), "source_end": round(end, 2),
            "length": round(final_len, 1), "hook": hook,
            "transcript": " ".join(s["text"] for s in clip_segs),
            "picked_by": "claude" if c.get("score") == 99 else "built-in",
        })
        if c.get("caption"):
            manifest[-1]["post_caption"] = post_caption(hook, c["caption"], c.get("hashtags"))
        else:
            manifest[-1]["post_caption"] = post_caption(hook, manifest[-1]["transcript"])
        if a.dry_run:
            continue
        face_x = None
        if a.layout == "crop" and not a.no_face:
            face_x = face_center(video, start, end)
            print("  following face" if face_x is not None else "  no face found, centered crop")
        music = tracks[(n - 1) % len(tracks)] if tracks else None
        write_ass(out / ass_name, words, start, end, hook, a.hook_seconds, a.font, ranges=ranges, style=a.style)
        render(video, out, name, start, end, ass_name, a.layout, a.crf,
               ranges=ranges, look=a.look, enhance=not a.no_enhance,
               title=hook.title(), description=manifest[-1]["transcript"][:250],
               face_x=face_x, music=music)
        (out / name.replace(".mp4", "_caption.txt")).write_text(manifest[-1]["post_caption"], encoding="utf-8")
        print(f"  wrote {out / name}")

    if not a.dry_run:
        (out / "reels.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"\nDone: {len(clips)} new Reels in {out}")
    if out.parent == library:
        gallery = write_gallery(library)
        print(f"Gallery: {gallery}")
        if a.open:
            subprocess.run(["open", str(gallery)])


if __name__ == "__main__":
    main()
