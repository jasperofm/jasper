"""Hardware detection, live resource monitoring, benchmarks and performance profiles.

Nothing here assumes Apple Silicon: every capability (CPU, RAM, GPU, VideoToolbox encoders) is detected
or measured on the actual machine, and results are stored so the UI can show a capability report.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import db, paths
from .media import NO_WINDOW

PROFILES = ["safe", "balanced", "performance"]
PROFILE_LABELS = {
    "safe": "Safe — 1 export at a time, low memory, gentle on the CPU",
    "balanced": "Balanced — workers sized to your RAM, hardware encoding where verified",
    "performance": "Performance — more parallel work; needs a benchmark first",
}


def _sh(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **NO_WINDOW).stdout.strip()
    except Exception:
        return ""


def _sysctl(key):
    return _sh(["sysctl", "-n", key]) if sys.platform == "darwin" else ""


# ---------------------------------------------------------------- live stats
def memory():
    """(total_bytes, available_bytes, used_percent). Works with or without psutil."""
    try:
        import psutil
        m = psutil.virtual_memory()
        return m.total, m.available, m.percent
    except Exception:
        pass
    if sys.platform == "darwin":
        total = int(_sysctl("hw.memsize") or 0)
        vm = _sh(["vm_stat"])
        page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size" in vm else 4096
        pages = {k: int(v) for k, v in re.findall(r"Pages (\w[\w ]*?):\s+(\d+)", vm)}
        avail = (pages.get("free", 0) + pages.get("inactive", 0) + pages.get("speculative", 0)) * page
    else:
        info = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        except Exception:
            pass
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    pct = 100.0 * (1 - avail / total) if total else 0.0
    return total, avail, pct


def cpu_percent():
    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:
        try:
            return min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
        except Exception:
            return 0.0


def memory_pressure():
    """'normal', 'warn' or 'critical' — used to throttle new work."""
    total, avail, pct = memory()
    if not total:
        return "normal"
    free_frac = avail / total
    if free_frac < 0.06 or avail < 400e6:
        return "critical"
    if free_frac < 0.12 or avail < 900e6:
        return "warn"
    return "normal"


def on_battery():
    """True when a Mac laptop is running on battery. False if unknown or on AC."""
    if sys.platform == "darwin":
        return "Battery Power" in _sh(["pmset", "-g", "batt"])
    try:
        import psutil
        b = psutil.sensors_battery()
        return bool(b and not b.power_plugged)
    except Exception:
        return False


def volumes():
    """Mounted drives with free space; marks external ones on macOS (/Volumes/*)."""
    out = []
    seen = set()
    cands = [Path("/")]
    if sys.platform == "darwin" and Path("/Volumes").exists():
        cands += [p for p in Path("/Volumes").iterdir() if p.is_dir()]
    for p in cands:
        try:
            u = shutil.disk_usage(p)
            dev = os.stat(p).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        out.append(dict(path=str(p), name=p.name or "Macintosh HD", total=u.total, free=u.free,
                        external=str(p).startswith("/Volumes/")))
    return out


def same_drive(a, b):
    try:
        return os.stat(_existing(a)).st_dev == os.stat(_existing(b)).st_dev
    except OSError:
        return False


def _existing(p):
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def free_gb(p):
    try:
        return shutil.disk_usage(_existing(p)).free / 1e9
    except OSError:
        return None


# ---------------------------------------------------------------- detection
def detect():
    """Static facts about this computer."""
    from . import export
    mac = platform.mac_ver()[0]
    info = dict(
        os=f"macOS {mac}" if mac else f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        python_arch=platform.architecture()[0],
        cpu=_sysctl("machdep.cpu.brand_string") or platform.processor() or "Unknown CPU",
        physical_cores=int(_sysctl("hw.physicalcpu") or 0) or (os.cpu_count() or 1),
        logical_cores=int(_sysctl("hw.logicalcpu") or 0) or (os.cpu_count() or 1),
        ram=memory()[0],
        model=_sysctl("hw.model"),
        gpu=[],
        encoders=sorted(e for e in export.encoders() if any(k in e for k in ("264", "265", "hevc", "toolbox"))),
    )
    if sys.platform == "darwin":
        try:
            js = json.loads(_sh(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20) or "{}")
            for g in js.get("SPDisplaysDataType", []):
                info["gpu"].append(g.get("sppci_model") or g.get("_name", "GPU"))
        except Exception:
            pass
    info["apple_silicon"] = info["arch"] == "arm64"
    return info


def test_hw_encoders():
    """Actually encode 1 second with each VideoToolbox encoder. Returns {encoder: True/False}."""
    from . import export
    res = {}
    for enc in ("h264_videotoolbox", "hevc_videotoolbox"):
        if enc not in export.encoders():
            res[enc] = False
            continue
        with tempfile.TemporaryDirectory(dir=temp_dir()) as d:
            out = Path(d) / "t.mp4"
            cmd = [paths.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=1",
                   "-c:v", enc, "-b:v", "4M", "-pix_fmt", "yuv420p", str(out)]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60, **NO_WINDOW)
                res[enc] = r.returncode == 0 and out.exists() and out.stat().st_size > 1000
            except Exception:
                res[enc] = False
    return res


def temp_dir():
    d = Path(db.setting("temp_dir") or (paths.data_dir() / "tmp"))
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        d = paths.data_dir() / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d


# ---------------------------------------------------------------- benchmark
def _time_ffmpeg(args, seconds_of_video):
    t0 = time.time()
    r = subprocess.run([paths.ffmpeg(), "-v", "error", "-y"] + args, capture_output=True, timeout=600, **NO_WINDOW)
    dt = time.time() - t0
    if r.returncode != 0:
        return None
    return round(seconds_of_video / dt, 2)   # seconds of video per wall second (1.0 = real time)


def benchmark(progress=lambda msg: None):
    """Measure this Mac. Every number in the report comes from a real run here; failures are recorded as None."""
    rep = dict(ts=time.time(), hardware=detect(), results={}, notes=[])
    r = rep["results"]
    with tempfile.TemporaryDirectory(dir=temp_dir()) as d:
        d = Path(d)
        src = d / "src.mp4"
        progress("Making a 10-second 1080p test clip…")
        ok = _time_ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=10", "-f", "lavfi", "-i",
                           "sine=f=440:d=10", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                           "-shortest", str(src)], 10)
        if not ok:
            rep["notes"].append("Could not create the test clip; FFmpeg may be broken.")
            return rep
        progress("Software H.264 export (Reels 9:16 1080p, high quality)…")
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        r["software_h264"] = _time_ffmpeg(["-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "slow",
                                           "-crf", "18", str(d / "a.mp4")], 10)
        progress("Software H.265 export…")
        r["software_h265"] = _time_ffmpeg(["-i", str(src), "-t", "5", "-vf", vf, "-c:v", "libx265", "-preset",
                                           "medium", "-crf", "20", str(d / "b.mp4")], 5)
        hw = test_hw_encoders()
        rep["hw_encoders"] = hw
        for enc in ("h264_videotoolbox", "hevc_videotoolbox"):
            if hw.get(enc):
                progress(f"Hardware export ({enc})…")
                r[enc] = _time_ffmpeg(["-i", str(src), "-vf", vf, "-c:v", enc, "-b:v", "12M",
                                       str(d / f"{enc}.mp4")], 10)
            else:
                r[enc] = None
        progress("Scene + audio analysis speed…")
        from . import media
        t0 = time.time()
        try:
            media.analyze(src, has_audio=True)
            r["analysis"] = round(10 / (time.time() - t0), 2)
        except Exception as e:
            r["analysis"] = None
            rep["notes"].append(f"Analysis test failed: {e}")
        progress("Disk write speed (temp folder)…")
        try:
            blob = os.urandom(8 * 1024 * 1024)
            t0 = time.time()
            with open(d / "disk.bin", "wb") as f:
                for _ in range(32):
                    f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            r["disk_write_mb_s"] = round(256 / (time.time() - t0), 1)
        except Exception:
            r["disk_write_mb_s"] = None
        try:
            from . import subtitles
            if subtitles.available() and subtitles.model_installed("tiny"):
                progress("Speech recognition (Whisper tiny)…")
                t0 = time.time()
                subtitles.transcribe_file(src, 0, 10, model="tiny")
                r["whisper_tiny"] = round(10 / (time.time() - t0), 2)
                subtitles.unload()
            else:
                r["whisper_tiny"] = None
                rep["notes"].append("Speech recognition not benchmarked: no local model installed yet.")
        except Exception as e:
            r["whisper_tiny"] = None
            rep["notes"].append(f"Speech recognition test failed: {e}")
    rep["recommended"] = recommend(rep)
    db.set_setting("hardware_report", json.dumps(rep))
    return rep


def last_report():
    try:
        return json.loads(db.setting("hardware_report") or "null")
    except Exception:
        return None


def recommend(rep=None):
    """Suggested settings from detected hardware and (if present) benchmark results."""
    hw = (rep or {}).get("hardware") or detect()
    res = (rep or {}).get("results") or {}
    ram_gb = hw["ram"] / 1e9 if hw.get("ram") else 0
    cores = hw.get("physical_cores") or 2
    hw_ok = (rep or {}).get("hw_encoders", {}).get("h264_videotoolbox", False)
    balanced_workers = max(1, min(cores // 2, int(ram_gb // 6) if ram_gb else 1, 3))
    perf_workers = max(balanced_workers, min(cores - 1, int(ram_gb // 4) if ram_gb else 1, 6))
    can_perf = bool(res) and (res.get("software_h264") or 0) > 0
    return dict(
        safe=dict(concurrency=1, threads_per_job=max(2, cores // 2), hw_accel="off", analysis_workers=1),
        balanced=dict(concurrency=balanced_workers, threads_per_job=max(2, cores // balanced_workers),
                      hw_accel="auto" if hw_ok else "off", analysis_workers=1),
        performance=dict(concurrency=perf_workers, threads_per_job=0, hw_accel="auto" if hw_ok else "off",
                         analysis_workers=1 if ram_gb < 16 else 2),
        performance_allowed=can_perf,
    )


def apply_profile(name):
    """Write the profile's settings. Performance needs a benchmark; falls back to balanced otherwise."""
    rec = recommend(last_report())
    if name == "performance" and not rec["performance_allowed"]:
        name = "balanced"
    p = rec[name]
    db.set_setting("profile", name)
    for k in ("concurrency", "threads_per_job", "hw_accel", "analysis_workers"):
        db.set_setting(k, p[k])
    db.set_setting("low_memory", "1" if name == "safe" else "0")
    return name, p
