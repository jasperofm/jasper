"""Start the Reels Cutter desktop app:  python -m reelscutter"""
import faulthandler
import os
import sys


def selftest():
    """Headless check used by the build: FFmpeg works, a real clip exports and passes quality checks."""
    import json
    import subprocess
    import tempfile
    from pathlib import Path
    os.environ.setdefault("REELSCUTTER_HOME", tempfile.mkdtemp())
    from . import db, export, hardware, media, paths, qc
    print("ffmpeg:", paths.ffmpeg())
    print("hardware:", json.dumps({k: v for k, v in hardware.detect().items() if k != "encoders"}))
    print("hw encoders:", hardware.test_hw_encoders())
    d = Path(tempfile.mkdtemp())
    src = d / "src.mp4"
    subprocess.run([paths.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi",
                    "-i", "sine=f=330:d=12", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)], check=True)
    info = media.probe(src)
    an = media.analyze(src, True)
    preset = db.presets()["Reels 9:16 1080p"]
    out = d / "clip.mp4"
    cmd, size = export.build_command(src, 2, 10, preset, out, info)
    export.Encode(cmd, 8).run()
    r = qc.check(out, 8, size, True)
    print("analysis keys:", sorted(an), "qc:", r["ok"], r["issues"], r["info"].get("size"))
    try:
        from PySide6.QtWidgets import QApplication
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["REELSCUTTER_NO_ONBOARDING"] = "1"
        app = QApplication([])
        from .ui.main_window import MainWindow
        w = MainWindow()
        for key in ("dashboard", "library", "analysis", "generator", "editor", "review", "export", "projects",
                    "storage", "system", "settings"):
            w.go(key)
            app.processEvents()
        print("ui: all pages opened")
    except Exception as e:
        print("ui failed:", e)
        return 1
    from . import subtitles
    print("speech recognition component:", subtitles.available())
    return 0 if r["ok"] else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    from . import paths
    log_dir = paths.data_dir() / "logs"
    try:
        faulthandler.enable(open(log_dir / "crash.log", "a"))
    except Exception:
        pass

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPixmapCache
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.setApplicationName(paths.APP_NAME)
    QApplication.setOrganizationName(paths.APP_NAME)
    QApplication.setApplicationVersion(paths.VERSION)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    from .ui import theme
    app.setStyleSheet(theme.QSS)
    icon = paths.resource("icon.png")
    if icon and icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    QPixmapCache.setCacheLimit(64 * 1024)     # KB: bounded thumbnail memory

    from . import backup, db, hardware
    from .jobs import queue
    try:
        db.conn()
        ok, msg = backup.startup_check()
    except Exception as e:
        QMessageBox.critical(None, "Reels Cutter can't start", f"The library database couldn't be opened:\n{e}")
        return 1

    try:
        from .ui.storage_page import trim_cache
        keep = {r["thumb"] for r in db.q("SELECT thumb FROM videos WHERE thumb IS NOT NULL")}
        trim_cache(paths.data_dir() / "thumbs", float(db.setting("thumb_cache_gb")) * 1e9, keep)
    except Exception as e:
        db.log("warning", f"Thumbnail cache cleanup failed: {e}")

    from .ui.main_window import MainWindow
    w = MainWindow()
    w.show()
    if not ok:
        QMessageBox.warning(w, "Library check", msg)
    if db.setting("onboarded", "0") != "1" and not os.environ.get("REELSCUTTER_NO_ONBOARDING"):
        w.onboarding()
    queue.start()
    files = [a for a in sys.argv[1:] if os.path.exists(a)]
    if files:
        w.start_import(files)
    rc = app.exec()
    queue.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
