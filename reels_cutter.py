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
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
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
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


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


def pick_clips(segs, count, lo, hi, gap=2.0):
    """Choose `count` non-overlapping clips, each aiming at a different length."""
    if count == 1:
        targets = [(lo + hi) / 2]
    else:
        targets = [lo + (hi - lo) * k / (count - 1) for k in range(count)]
    chosen = []
    used_hooks = set()
    for t in sorted(targets, reverse=True):       # longest first: they are hardest to fit
        band = max(5.0, (hi - lo) / max(count, 1) / 1.5)
        best = None
        for widen in (1, 2, 4, 100):
            pool = [c for c in candidates(segs, max(lo, t - band * widen), min(hi, t + band * widen))
                    if all(c["end"] + gap <= p["start"] or c["start"] >= p["end"] + gap for p in chosen)]
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


# ---------------------------------------------------------------- captions (ASS)
def ass_time(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def write_ass(path, words, clip_start, clip_end, hook, hook_secs, font, chunk=3):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},84,&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,520,1
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
                ("{\\c&H0000FFFF&}" + ass_escape(x["word"].upper()) + "{\\c&H00FFFFFF&}") if n == idx
                else ass_escape(x["word"].upper())
                for n, x in enumerate(group)
            )
            lines.append(f"Dialogue: 0,{ass_time(st - clip_start)},{ass_time(en - clip_start)},Cap,,0,0,0,,{txt}")
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- rendering
def render(video, out_dir, name, start, end, ass_name, layout, crf):
    if layout == "crop":
        vf = f"scale=-2:{H},crop={W}:{H},setsar=1,ass={ass_name}"
    else:  # blur: whole frame fitted, blurred copy fills the background
        vf = (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=30:3[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass={ass_name}")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(Path(video).resolve()),
        "-filter_complex", vf, "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", name,
    ]
    subprocess.run(cmd, cwd=out_dir, check=True)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Cut one long video into several different vertical Reels.")
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="output folder (default: <video>_reels)")
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
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="only print the chosen moments")
    a = ap.parse_args()

    import shutil
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is not installed. On a Mac run: brew install ffmpeg  (get Homebrew at https://brew.sh first if 'brew' is not found)")
    video = Path(a.video)
    if not video.exists():
        sys.exit(f"Not found: {video}")
    if not 5 <= a.min <= a.max:
        sys.exit("--min must be >= 5 and <= --max")
    out = Path(a.out or f"{video.with_suffix('')}_reels")
    out.mkdir(parents=True, exist_ok=True)

    segs = transcribe(video, a.model, a.language, out / "transcript.json")
    if not segs:
        sys.exit("No speech found in the video, so there is nothing to caption.")
    total = video_duration(video)
    if total < a.min:
        sys.exit(f"The video is only {total:.0f}s long, shorter than --min.")

    clips = pick_clips(segs, a.count, a.min, a.max)
    custom_hooks = []
    if a.hooks:
        custom_hooks = [l.strip() for l in Path(a.hooks).read_text(encoding="utf-8").splitlines() if l.strip()]

    manifest = []
    for n, c in enumerate(clips, 1):
        clip_segs = segs[c["i"]:c["j"] + 1]
        words = [w for s in clip_segs for w in s["words"]]
        start = max(0.0, c["start"] - 0.15)
        end = min(total, c["end"] + 0.35)
        hook = custom_hooks[n - 1] if n - 1 < len(custom_hooks) else make_hook(clip_segs[0]["text"])
        name = f"reel_{n:02d}_{int(end - start)}s.mp4"
        ass_name = f"reel_{n:02d}.ass"
        print(f"\nReel {n}: {start:.1f}s -> {end:.1f}s ({end - start:.0f}s)  hook: {hook}")
        manifest.append({
            "file": name, "source_start": round(start, 2), "source_end": round(end, 2),
            "length": round(end - start, 1), "hook": hook,
            "transcript": " ".join(s["text"] for s in clip_segs),
        })
        if a.dry_run:
            continue
        write_ass(out / ass_name, words, start, end, hook, a.hook_seconds, a.font)
        render(video, out, name, start, end, ass_name, a.layout, a.crf)
        print(f"  wrote {out / name}")

    (out / "reels.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"\nDone: {len(manifest)} Reels in {out}  (details in reels.json)")


if __name__ == "__main__":
    main()
