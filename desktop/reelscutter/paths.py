"""Where Reels Cutter keeps its data, and how it finds FFmpeg."""
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Reels Cutter"
VERSION = "1.0.0"


def data_dir() -> Path:
    """Per-user app data folder (database, thumbnails, logs, temp files)."""
    override = os.environ.get("REELSCUTTER_HOME")
    if override:
        d = Path(override)
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform.startswith("win"):
        d = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        d = Path.home() / ".local" / "share" / "reels-cutter"
    d.mkdir(parents=True, exist_ok=True)
    for sub in ("thumbs", "logs", "tmp", "subs"):
        (d / sub).mkdir(exist_ok=True)
    return d


def default_output_dir() -> Path:
    d = Path.home() / "Movies" / APP_NAME
    return d


_ffmpeg = None


def ffmpeg() -> str:
    """FFmpeg binary: bundled copy first (imageio-ffmpeg), then the system one."""
    global _ffmpeg
    if _ffmpeg:
        return _ffmpeg
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            _ffmpeg = exe
            return exe
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("FFmpeg was not found. Reinstall Reels Cutter.")
    _ffmpeg = exe
    return exe


def resource(name):
    """A file shipped with the app (works from source and inside the packaged .app)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for cand in (base / "assets" / name, Path(__file__).resolve().parent.parent / "assets" / name):
        if cand.exists():
            return cand
    return None
