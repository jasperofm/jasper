"""Turning a (video, start, end, preset) into an FFmpeg command, and running it with progress."""
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import paths
from .media import NO_WINDOW

RESOLUTIONS = {"720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "source": None}
RES_LABELS = {"720p": "720p", "1080p": "1080p", "1440p": "1440p (2K)", "2160p": "4K", "source": "Source"}
FPS_CHOICES = ["source", "24", "25", "30", "50", "60"]
ASPECTS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5), "original": None}
FITS = {"crop": "Crop to fill", "blur": "Fit on blurred background", "fit": "Fit with black bars"}
CAPTION_STYLES = ["classic", "bold", "boxed", "minimal"]

DEFAULT_PRESET = dict(aspect="9:16", resolution="1080p", fps="30", codec="h264", quality_mode="crf", crf=18,
                      bitrate_mbps=12, audio_kbps=192, fit="blur", reframe_x=0.5, fade_in=0.2, fade_out=0.3,
                      normalize_audio=True, subtitles=False, caption_style="classic", hw_accel=None)

_encoders = None


def encoders():
    global _encoders
    if _encoders is None:
        try:
            out = subprocess.run([paths.ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True,
                                 timeout=30, **NO_WINDOW).stdout
            _encoders = set(re.findall(r"^\s*V\S*\s+(\S+)", out, re.M))
        except Exception:
            _encoders = set()
    return _encoders


def hw_available(codec):
    if sys.platform != "darwin":
        return False
    return ("h264_videotoolbox" if codec == "h264" else "hevc_videotoolbox") in encoders()


def even(x):
    x = int(round(x))
    return x - (x % 2)


def target_size(preset, src_w, src_h):
    """Output width/height for this preset and source size."""
    res = RESOLUTIONS.get(preset.get("resolution", "1080p"))
    ar = ASPECTS.get(preset.get("aspect", "9:16"))
    if ar is None:                      # keep the source shape
        if res is None:
            return even(src_w), even(src_h)
        short = min(src_w, src_h)
        k = res / short
        return even(src_w * k), even(src_h * k)
    aw, ah = ar
    if res is None:                     # source resolution: largest frame of this shape that fits the source
        res = min(src_w, src_h) if aw != ah else min(src_w, src_h)
    if aw <= ah:                        # portrait or square: width is the short side
        w = res
        h = res * ah / aw
    else:
        h = res
        w = res * aw / ah
    return even(w), even(h)


def track_points(faces, start, end, sensitivity=0.5, default=0.5):
    """Smoothed horizontal subject positions [(t_rel, x 0..1)] for a clip, from analysis face samples.

    sensitivity 0..1: low = calm camera that only moves for big shifts, high = follows the face closely.
    """
    pts = [(f[0] - start, f[1]) for f in faces or [] if start - 2 <= f[0] <= end + 2]
    if len(pts) < 2:
        return None
    # fill gaps with the previous position, smooth with a moving average, then limit speed
    win = max(1, int(round(4 - 3 * sensitivity)))
    xs = [x for _, x in pts]
    sm = [sum(xs[max(0, i - win): i + win + 1]) / len(xs[max(0, i - win): i + win + 1]) for i in range(len(xs))]
    dead = 0.12 * (1 - sensitivity)          # ignore small movements
    out, cur = [], sm[0]
    for (t, _), x in zip(pts, sm):
        if abs(x - cur) > dead:
            cur = cur + (x - cur) * (0.4 + 0.6 * sensitivity)
        out.append((max(0.0, t), cur))
    # thin out to at most ~40 keyframes
    step = max(1, len(out) // 40)
    out = out[::step]
    return out


def _x_expr(points):
    """FFmpeg expression: piecewise-linear interpolation of x(t) between keyframes."""
    if len(points) == 1:
        return f"{points[0][1]:.3f}"
    expr = f"{points[-1][1]:.3f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(points, points[1:]))):
        if t1 - t0 < 1e-3:
            continue
        seg = f"({x0:.3f}+({x1 - x0:.3f})*(t-{t0:.2f})/{t1 - t0:.2f})"
        expr = f"if(lt(t\\,{t1:.2f})\\,{seg}\\,{expr})"
    return f"if(lt(t\\,{points[0][0]:.2f})\\,{points[0][1]:.3f}\\,{expr})"


def video_filter(preset, src_w, src_h, length, subs_path=None, track=None):
    W, H = target_size(preset, src_w, src_h)
    fit = preset.get("fit", "blur")
    fps = preset.get("fps", "source")
    pre = f"fps={fps}," if fps != "source" else ""
    if (W, H) == (even(src_w), even(src_h)) or ASPECTS.get(preset.get("aspect")) is None:
        core = f"[0:v]{pre}scale={W}:{H}:flags=lanczos,setsar=1"
    elif fit == "crop":
        rx = float(preset.get("reframe_x", 0.5))
        if track:     # follow the subject: centre the crop on x(t), clamped to the frame
            cx = _x_expr(track)
            xexpr = f"max(0\\,min(iw-{W}\\,iw*{cx}-{W}/2))"
        else:
            xexpr = f"(iw-{W})*{rx:.3f}"
        core = (f"[0:v]{pre}scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={W}:{H}:{xexpr}:(ih-{H})/2,setsar=1")
    elif fit == "fit":
        core = (f"[0:v]{pre}scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    else:  # blur
        core = (f"[0:v]{pre}split[a][b];[a]scale={W//4*2}:{H//4*2}:force_original_aspect_ratio=increase,"
                f"crop={W//4*2}:{H//4*2},boxblur=20:2,scale={W}:{H}[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    fi, fo = float(preset.get("fade_in", 0) or 0), float(preset.get("fade_out", 0) or 0)
    if fi > 0:
        core += f",fade=t=in:st=0:d={fi:.2f}"
    if fo > 0 and length > fo:
        core += f",fade=t=out:st={length - fo:.2f}:d={fo:.2f}"
    if subs_path:
        core += f",ass='{_esc(subs_path)}'"
    return core + "[v]", (W, H)


def audio_filter(preset, length):
    parts = []
    if preset.get("normalize_audio", True):
        parts.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    fi, fo = float(preset.get("fade_in", 0) or 0), float(preset.get("fade_out", 0) or 0)
    if fi > 0:
        parts.append(f"afade=t=in:st=0:d={fi:.2f}")
    if fo > 0 and length > fo:
        parts.append(f"afade=t=out:st={length - fo:.2f}:d={fo:.2f}")
    parts.append("aresample=48000")
    return "[0:a]" + ",".join(parts) + "[a]"


def _esc(p):
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_command(src, start, end, preset, out_path, src_info, threads=0, hw="auto", subs_path=None, track=None):
    length = end - start
    vf, (W, H) = video_filter(preset, src_info["width"], src_info["height"], length, subs_path, track)
    has_audio = bool(src_info.get("acodec"))
    graph = vf + (";" + audio_filter(preset, length) if has_audio else "")
    codec = preset.get("codec", "h264")
    use_hw = (hw == "on" or (hw == "auto" and preset.get("quality_mode") == "bitrate")) and hw_available(codec)
    cmd = [paths.ffmpeg(), "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src), "-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", f"{int(preset.get('audio_kbps', 192))}k"]
    if use_hw:
        enc = "h264_videotoolbox" if codec == "h264" else "hevc_videotoolbox"
        cmd += ["-c:v", enc]
        if preset.get("quality_mode") == "bitrate":
            cmd += ["-b:v", f"{float(preset.get('bitrate_mbps', 12))}M"]
        else:
            q = max(1, min(100, int(100 - float(preset.get("crf", 18)) * 2.2)))
            cmd += ["-q:v", str(q)]
    else:
        enc = "libx264" if codec == "h264" else "libx265"
        cmd += ["-c:v", enc, "-preset", "slow" if codec == "h264" else "medium"]
        if preset.get("quality_mode") == "bitrate":
            br = float(preset.get("bitrate_mbps", 12))
            cmd += ["-b:v", f"{br}M", "-maxrate", f"{br * 1.5}M", "-bufsize", f"{br * 2}M"]
        else:
            cmd += ["-crf", str(int(preset.get("crf", 18)))]
        if codec == "h264":
            cmd += ["-profile:v", "high"]
        if threads:
            cmd += ["-threads", str(int(threads))]
    if codec == "h265":
        cmd += ["-tag:v", "hvc1"]   # plays in QuickTime / Photos
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-map_metadata", "-1", str(out_path)]
    return cmd, (W, H)


class Encode:
    """Runs one FFmpeg encode; supports progress, pause/resume and cancel."""

    def __init__(self, cmd, length, on_progress=None):
        self.cmd, self.length, self.on_progress = cmd, max(0.01, length), on_progress
        self.proc = None
        self.canceled = False
        self.paused = False
        self._lock = threading.Lock()

    def run(self):
        with self._lock:
            if self.canceled:
                raise InterruptedError("canceled")
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         errors="replace", **NO_WINDOW)
        err_chunks = []
        t = threading.Thread(target=lambda: err_chunks.append(self.proc.stderr.read()), daemon=True)
        t.start()
        for line in self.proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    if self.on_progress:
                        self.on_progress(min(1.0, us / 1e6 / self.length))
                except ValueError:
                    pass
        self.proc.wait()
        t.join(timeout=5)
        if self.canceled:
            raise InterruptedError("canceled")
        if self.proc.returncode != 0:
            err = "".join(err_chunks).strip()
            raise RuntimeError(err.splitlines()[-1] if err else f"ffmpeg exited with {self.proc.returncode}")

    def pause(self):
        if self.proc and self.proc.poll() is None and hasattr(signal, "SIGSTOP"):
            os.kill(self.proc.pid, signal.SIGSTOP)
            self.paused = True

    def resume(self):
        if self.proc and self.proc.poll() is None and hasattr(signal, "SIGCONT"):
            os.kill(self.proc.pid, signal.SIGCONT)
            self.paused = False

    def cancel(self):
        with self._lock:
            self.canceled = True
            if self.proc and self.proc.poll() is None:
                if self.paused:
                    self.resume()
                self.proc.kill()


def safe_name(s, maxlen=80):
    s = re.sub(r"[^\w\-. ]+", "_", str(s)).strip(" ._")
    return (s or "clip")[:maxlen]


def fmt_ts(t):
    t = int(t)
    return f"{t // 3600:02d}h{t % 3600 // 60:02d}m{t % 60:02d}s" if t >= 3600 else f"{t // 60:02d}m{t % 60:02d}s"


def output_path(out_root, folder_template, name_template, *, project, video_name, n, start, end, preset_name,
                ext=".mp4"):
    """Build a non-overwriting output path from the user's folder and name templates."""
    stem = Path(video_name).stem
    fields = dict(project=safe_name(project), video=safe_name(stem), n=n, start=fmt_ts(start), end=fmt_ts(end),
                  length=int(round(end - start)), preset=safe_name(preset_name))
    try:
        folder = folder_template.format(**fields)
    except (KeyError, ValueError, IndexError):
        folder = "{project}/{video}".format(**fields)
    try:
        name = name_template.format(**fields)
    except (KeyError, ValueError, IndexError):
        name = "{video}_clip{n:03d}_{start}".format(**fields)
    parts = [safe_name(p) for p in Path(folder).parts if p not in ("/", "\\", "..", ".")]
    d = Path(out_root).joinpath(*parts) if parts else Path(out_root)
    base = d / (safe_name(name) + ext)
    p, k = base, 2
    while p.exists():
        p = d / f"{base.stem} ({k}){ext}"
        k += 1
    return p
