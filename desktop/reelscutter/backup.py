"""Database integrity checks and rolling backups (the database holds all projects, decisions and history)."""
import sqlite3
import time
from pathlib import Path

from . import db, paths

KEEP = 7


def backups_dir():
    d = paths.data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def integrity_ok():
    try:
        r = db.q1("PRAGMA quick_check")
        return r[0] == "ok", r[0]
    except Exception as e:
        return False, str(e)


def backup(reason="auto"):
    """Consistent snapshot using SQLite's online backup API (safe while the app is running)."""
    dst = backups_dir() / f"library-{time.strftime('%Y%m%d-%H%M%S')}-{reason}.sqlite3"
    target = sqlite3.connect(str(dst))
    try:
        db.conn().backup(target)
    finally:
        target.close()
    snaps = sorted(backups_dir().glob("library-*.sqlite3"))
    for old in snaps[:-KEEP]:
        old.unlink(missing_ok=True)
    return dst


def daily_backup():
    snaps = sorted(backups_dir().glob("library-*.sqlite3"))
    if not snaps or time.time() - snaps[-1].stat().st_mtime > 20 * 3600:
        return backup("daily")
    return None


def list_backups():
    return sorted(backups_dir().glob("library-*.sqlite3"), reverse=True)


def restore(path):
    """Replace the live database with a snapshot (current one is backed up first). Needs an app restart."""
    backup("before-restore")
    src = sqlite3.connect(str(path))
    try:
        src.backup(db.conn())
    finally:
        src.close()


def startup_check():
    """Run at launch. If the database is damaged, keep a copy and restore the newest good backup."""
    ok, msg = integrity_ok()
    if ok:
        try:
            daily_backup()
        except Exception as e:
            db.log("warning", f"Backup failed: {e}")
        return True, "ok"
    db.log("error", f"Database check failed: {msg}")
    for snap in list_backups():
        try:
            c = sqlite3.connect(str(snap))
            good = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            c.close()
            if good:
                restore(snap)
                return False, f"The library database was damaged and has been restored from {snap.name}."
        except Exception:
            continue
    return False, "The library database reported a problem and no good backup was found."
