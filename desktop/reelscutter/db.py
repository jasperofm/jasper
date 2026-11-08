"""SQLite storage: library, projects, saved edits, presets, jobs and logs.

Every thread gets its own connection; WAL mode lets the UI read while workers write.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS videos(
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL, size INTEGER, mtime REAL,
    fingerprint TEXT, duration REAL, width INTEGER, height INTEGER, fps REAL, vcodec TEXT, acodec TEXT,
    fmt TEXT, thumb TEXT, status TEXT NOT NULL DEFAULT 'new', error TEXT, project_id INTEGER,
    analysis TEXT, added_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS videos_fp ON videos(fingerprint);
CREATE TABLE IF NOT EXISTS segments(
    id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    label TEXT, kind TEXT NOT NULL DEFAULT 'keep', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS presets(
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, data TEXT NOT NULL, builtin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS batches(
    id INTEGER PRIMARY KEY, name TEXT, project_id INTEGER, settings TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY, batch_id INTEGER, video_id INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    selection TEXT, preset TEXT NOT NULL, out_path TEXT, status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL, out_size INTEGER);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS batch_videos(
    batch_id INTEGER NOT NULL, video_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT,
    PRIMARY KEY(batch_id, video_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS log(
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, job_id INTEGER, message TEXT NOT NULL);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized = set()


# Each entry upgrades the database by one version (PRAGMA user_version). Never edit old entries; append new ones.
MIGRATIONS = [
    """
    ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN review TEXT NOT NULL DEFAULT 'awaiting';
    ALTER TABLE jobs ADD COLUMN title TEXT;
    ALTER TABLE jobs ADD COLUMN tags TEXT NOT NULL DEFAULT '';
    ALTER TABLE jobs ADD COLUMN score REAL;
    ALTER TABLE jobs ADD COLUMN score_info TEXT;
    ALTER TABLE jobs ADD COLUMN thumb TEXT;
    ALTER TABLE jobs ADD COLUMN qc TEXT;
    ALTER TABLE videos ADD COLUMN tags TEXT NOT NULL DEFAULT '';
    ALTER TABLE videos ADD COLUMN audio_tracks INTEGER;
    ALTER TABLE videos ADD COLUMN analysis_level TEXT;
    ALTER TABLE videos ADD COLUMN transcript TEXT;
    CREATE TABLE IF NOT EXISTS imports(
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, sources TEXT, found INTEGER, added INTEGER, duplicates INTEGER,
        skipped INTEGER, failed INTEGER, seconds REAL);
    CREATE INDEX IF NOT EXISTS videos_name ON videos(name);
    CREATE INDEX IF NOT EXISTS videos_status ON videos(status);
    CREATE INDEX IF NOT EXISTS videos_added ON videos(added_at);
    CREATE INDEX IF NOT EXISTS videos_project ON videos(project_id);
    CREATE INDEX IF NOT EXISTS jobs_video ON jobs(video_id);
    CREATE INDEX IF NOT EXISTS jobs_review ON jobs(review);
    CREATE INDEX IF NOT EXISTS segments_video ON segments(video_id);
    """,
    """
    ALTER TABLE jobs ADD COLUMN repairs TEXT;
    CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, job_id INTEGER, decision TEXT NOT NULL, features TEXT);
    CREATE TABLE IF NOT EXISTS watch_folders(
        id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, project TEXT, profile TEXT, enabled INTEGER NOT NULL
        DEFAULT 1, last_scan REAL);
    CREATE TABLE IF NOT EXISTS profiles(
        id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
    """,
]


def _migrate(c):
    v = c.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[v:], v + 1):
        c.execute("BEGIN")
        try:
            for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
                c.execute(stmt)
            c.execute(f"PRAGMA user_version={i}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def db_path() -> Path:
    return paths.data_dir() / "library.sqlite3"


def conn() -> sqlite3.Connection:
    path = str(db_path())
    c = getattr(_local, "conn", None)
    if c is not None and getattr(_local, "path", None) == path:
        return c
    c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    with _init_lock:
        if path not in _initialized:
            c.executescript(SCHEMA)
            _migrate(c)
            _seed(c)
            _initialized.add(path)
    _local.conn, _local.path = c, path
    return c


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn().execute(sql, args).fetchone()


def run(sql, args=()):
    cur = conn().execute(sql, args)
    return cur.lastrowid


def many(sql, rows):
    c = conn()
    c.execute("BEGIN")
    try:
        c.executemany(sql, rows)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "output_dir": str(paths.default_output_dir()),
    "folder_template": "{project}/{video}",
    "name_template": "{video}_clip{n:03d}_{start}",
    "concurrency": "1",
    "threads_per_job": "2",
    "hw_accel": "off",
    "low_memory": "1",
    "profile": "safe",
    "analysis_workers": "1",
    "min_free_gb": "5",
    "internal_reserve_gb": "20",
    "temp_dir": "",
    "thumb_cache_gb": "1",
    "temp_cache_gb": "10",
    "pause_on_battery": "1",
    "schedule_enabled": "0",
    "schedule_start": "22",
    "schedule_end": "7",
    "max_repairs": "2",
    "auto_retry_limit": "1",
    "review_required": "0",
    "learning_enabled": "1",
    "whisper_model": "base",
    "model_idle_minutes": "5",
    "cloud_ai_enabled": "0",
    "anthropic_api_key": "",
    "onboarded": "0",
    "default_preset": "Reels 9:16 1080p",
    "auto_retry": "1",
}


def setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    if r is None:
        return DEFAULT_SETTINGS.get(key, default)
    return r["value"]


def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))


# ---------------------------------------------------------------- presets
BUILTIN_PRESETS = {
    "Reels 9:16 1080p": dict(aspect="9:16", resolution="1080p", fps="30", codec="h264", quality_mode="crf",
                             crf=18, bitrate_mbps=12, audio_kbps=192, fit="blur", fade_in=0.2, fade_out=0.3,
                             normalize_audio=True, subtitles=False, caption_style="classic"),
    "Shorts 9:16 1080p 60fps": dict(aspect="9:16", resolution="1080p", fps="60", codec="h264", quality_mode="crf",
                                    crf=18, bitrate_mbps=16, audio_kbps=192, fit="crop", fade_in=0.0, fade_out=0.3,
                                    normalize_audio=True, subtitles=False, caption_style="classic"),
    "Square 1:1 1080p": dict(aspect="1:1", resolution="1080p", fps="source", codec="h264", quality_mode="crf",
                             crf=19, bitrate_mbps=10, audio_kbps=192, fit="crop", fade_in=0.2, fade_out=0.3,
                             normalize_audio=True, subtitles=False, caption_style="classic"),
    "Feed 4:5 1080p": dict(aspect="4:5", resolution="1080p", fps="source", codec="h264", quality_mode="crf",
                           crf=19, bitrate_mbps=10, audio_kbps=192, fit="crop", fade_in=0.2, fade_out=0.3,
                           normalize_audio=True, subtitles=False, caption_style="classic"),
    "YouTube 16:9 4K HEVC": dict(aspect="16:9", resolution="2160p", fps="source", codec="h265",
                                 quality_mode="crf", crf=20, bitrate_mbps=40, audio_kbps=256, fit="fit",
                                 fade_in=0.0, fade_out=0.0, normalize_audio=True, subtitles=False,
                                 caption_style="classic"),
    "Original (fast copy quality)": dict(aspect="original", resolution="source", fps="source", codec="h264",
                                         quality_mode="crf", crf=16, bitrate_mbps=20, audio_kbps=256, fit="fit",
                                         fade_in=0.0, fade_out=0.0, normalize_audio=False, subtitles=False,
                                         caption_style="classic"),
}


def _seed(c):
    for name, data in BUILTIN_PRESETS.items():
        c.execute("INSERT OR IGNORE INTO presets(name,data,builtin) VALUES(?,?,1)", (name, json.dumps(data)))
    c.execute("INSERT OR IGNORE INTO projects(name,created_at) VALUES('Default',?)", (time.time(),))


def presets():
    return {r["name"]: json.loads(r["data"]) for r in q("SELECT name,data FROM presets ORDER BY builtin DESC, name")}


def save_preset(name, data):
    run("INSERT INTO presets(name,data,builtin) VALUES(?,?,0) ON CONFLICT(name) DO UPDATE SET data=excluded.data",
        (name, json.dumps(data)))


def delete_preset(name):
    run("DELETE FROM presets WHERE name=? AND builtin=0", (name,))


# ---------------------------------------------------------------- projects
def projects():
    return q("SELECT * FROM projects ORDER BY created_at DESC")


def project_id(name):
    name = (name or "Default").strip() or "Default"
    r = q1("SELECT id FROM projects WHERE name=?", (name,))
    if r:
        return r["id"]
    return run("INSERT INTO projects(name,created_at) VALUES(?,?)", (name, time.time()))


def project_name(pid):
    r = q1("SELECT name FROM projects WHERE id=?", (pid,))
    return r["name"] if r else "Default"


# ---------------------------------------------------------------- logging
def log(level, message, job_id=None):
    try:
        run("INSERT INTO log(ts,level,job_id,message) VALUES(?,?,?,?)", (time.time(), level, job_id, message[:4000]))
    except Exception:
        pass
