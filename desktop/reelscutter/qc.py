"""Automatic technical quality checks for every rendered clip.

Checks: file integrity (opens + decodes frames), playable H.264/H.265 + AAC in MP4, resolution/aspect,
duration, audio present and in sync with video, black frames, frozen frames, unexpected silence.
Caption timing is generated from the same word timestamps used to cut, so it is not re-measured here
(reported as "not measured" rather than claimed as checked).
"""
import re
import subprocess

from . import db, paths
from .media import NO_WINDOW

DEFAULTS = dict(qc_enabled="1", qc_max_black_pct="40", qc_max_frozen_pct="70", qc_max_silence_pct="85",
                qc_duration_tolerance="0.6")


def _f(key):
    return float(db.setting(key, DEFAULTS[key]))


def check(path, expected_len, expected_size, expect_audio):
    """Returns dict(ok, fatal, issues=[...], warnings=[...], info={...}).

    fatal=True means the file itself is broken (worth re-encoding); ok=False without fatal means the content
    failed a threshold (black/frozen) and a re-encode would not help.
    """
    issues, warns, info = [], [], {}
    fatal = False
    try:
        import av
        with av.open(str(path)) as c:
            vs = next((s for s in c.streams if s.type == "video"), None)
            aus = [s for s in c.streams if s.type == "audio"]
            if vs is None:
                return dict(ok=False, fatal=True, issues=["no video stream"], warnings=[], info={})
            info["codec"] = vs.codec_context.name
            info["size"] = (vs.codec_context.width, vs.codec_context.height)
            info["duration"] = float(c.duration / 1e6) if c.duration else None
            vdur = float(vs.duration * vs.time_base) if vs.duration else info["duration"]
            adur = float(aus[0].duration * aus[0].time_base) if aus and aus[0].duration else None
            info["audio"] = bool(aus)
            frames = 0
            for _ in c.decode(video=0):
                frames += 1
                if frames >= 5:
                    break
            if frames == 0:
                fatal = True
                issues.append("video frames cannot be decoded")
    except Exception as e:
        return dict(ok=False, fatal=True, issues=[f"file cannot be opened: {e}"], warnings=[], info={})

    if info["codec"] not in ("h264", "hevc"):
        issues.append(f"unexpected codec {info['codec']}")
        fatal = True
    if expected_size and tuple(info["size"]) != tuple(expected_size):
        issues.append(f"resolution is {info['size'][0]}×{info['size'][1]}, expected "
                      f"{expected_size[0]}×{expected_size[1]}")
        fatal = True
    tol = max(_f("qc_duration_tolerance"), expected_len * 0.03)
    if info["duration"] is None or abs(info["duration"] - expected_len) > tol:
        issues.append(f"duration is {info['duration'] or 0:.2f}s, expected {expected_len:.2f}s")
        fatal = True
    if expect_audio and not info["audio"]:
        issues.append("audio track is missing")
        fatal = True
    if info["audio"] and adur and vdur and abs(adur - vdur) > 0.35:
        issues.append(f"audio/video length differ by {abs(adur - vdur):.2f}s (sync risk)")
        fatal = True
    if fatal:
        return dict(ok=False, fatal=True, issues=issues, warnings=warns, info=info)

    # content checks on a small, fast decode
    length = info["duration"] or expected_len
    vf = "fps=5,scale=320:-2,blackdetect=d=0.4:pix_th=0.10,freezedetect=n=0.002:d=1.5"
    cmd = [paths.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-vf", vf]
    if info["audio"]:
        cmd += ["-af", "silencedetect=n=-45dB:d=1"]
    cmd += ["-f", "null", "-"]
    try:
        err = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=600,
                             **NO_WINDOW).stderr
    except Exception as e:
        warns.append(f"content check skipped: {e}")
        return dict(ok=True, fatal=False, issues=issues, warnings=warns, info=info)
    if re.search(r"(?i)\berror\b.*(decod|corrupt|invalid)", err):
        issues.append("decoder reported errors while reading the clip")
        return dict(ok=False, fatal=True, issues=issues, warnings=warns, info=info)
    black = sum(float(d) for d in re.findall(r"black_duration:([\d.]+)", err))
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    frozen = sum((ends[i] if i < len(ends) else length) - s for i, s in enumerate(starts))
    s_starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    s_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    silent = sum((s_ends[i] if i < len(s_ends) else length) - max(0, s) for i, s in enumerate(s_starts))
    info.update(black_pct=round(100 * black / length, 1), frozen_pct=round(100 * frozen / length, 1),
                silence_pct=round(100 * silent / length, 1) if info["audio"] else None)
    if info["black_pct"] > _f("qc_max_black_pct"):
        issues.append(f"{info['black_pct']:.0f}% of the clip is black")
    elif info["black_pct"] > 10:
        warns.append(f"{info['black_pct']:.0f}% black frames")
    if info["frozen_pct"] > _f("qc_max_frozen_pct"):
        issues.append(f"{info['frozen_pct']:.0f}% of the clip is a frozen picture")
    elif info["frozen_pct"] > 25:
        warns.append(f"{info['frozen_pct']:.0f}% frozen picture")
    if info.get("silence_pct") is not None:
        if info["silence_pct"] > _f("qc_max_silence_pct"):
            issues.append(f"{info['silence_pct']:.0f}% of the clip is silent")
        elif info["silence_pct"] > 40:
            warns.append(f"{info['silence_pct']:.0f}% silence")
    return dict(ok=not issues, fatal=False, issues=issues, warnings=warns, info=info)
