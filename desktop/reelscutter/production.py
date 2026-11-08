"""Production profiles (Clip Generator settings) and watch folders that run them automatically."""
import json
import threading
import time
from pathlib import Path

from . import db, selection


def build_preset(g):
    """Export preset for a production profile: the named preset plus framing/caption choices."""
    presets = db.presets()
    p = dict(presets.get(g.get("preset")) or presets[db.setting("default_preset")])
    p["reframe_mode"] = g.get("reframe", "auto")
    p["track_sensitivity"] = g.get("sensitivity", 0.5)
    cap = g.get("captions", "none")
    p["subtitles"] = cap in ("burn", "both")
    p["sidecar_captions"] = cap in ("both", "files")
    p["caption_style"] = g.get("caption_style", "classic")
    p["caption_position"] = g.get("caption_position", "bottom")
    return p


def profile(name):
    r = db.q1("SELECT data FROM profiles WHERE name=?", (name,)) if name else None
    if r:
        return json.loads(r["data"])
    last = db.setting("last_generator")
    return json.loads(last) if last else None


def start(g, video_ids, name="Batch"):
    from .jobs import queue
    s = selection.SelectionSettings.from_dict(g["selection"])
    preset_name = g.get("preset") if g.get("preset") in db.presets() else db.setting("default_preset")
    return queue.create_batch(video_ids, s, preset_name, build_preset(g), project=g.get("project", "Default"),
                              name=name)


class Watcher:
    """Scans enabled watch folders every minute; new videos are imported and processed with the folder's profile.

    A file is only picked up once its size has stopped changing (so copies in progress are skipped).
    """

    def __init__(self):
        self.thread = None
        self.quit = False
        self.sizes = {}
        self.last_msg = ""

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="watcher")
            self.thread.start()

    def _loop(self):
        while not self.quit:
            try:
                self.scan_all()
            except Exception as e:
                db.log("error", f"Watch folder scan failed: {e}")
            for _ in range(60):
                if self.quit:
                    return
                time.sleep(1)

    def scan_all(self):
        for w in db.q("SELECT * FROM watch_folders WHERE enabled=1"):
            self.scan(w)

    def scan(self, w):
        from . import media
        from .jobs import Importer
        root = Path(w["path"])
        if not root.exists():
            self.last_msg = f"Watch folder not available (drive unplugged?): {root}"
            return
        ready = []
        known = {r["path"] for r in db.q("SELECT path FROM videos WHERE path LIKE ?", (str(root.resolve()) + "%",))}
        for f in root.rglob("*"):
            if not f.is_file() or not media.is_video(f) or any(p.startswith(".") for p in f.parts):
                continue
            p = str(f.resolve())
            if p in known:
                continue
            size = f.stat().st_size
            if self.sizes.get(p) == size and size > 0:
                ready.append(f)
            self.sizes[p] = size
        if not ready:
            db.run("UPDATE watch_folders SET last_scan=? WHERE id=?", (time.time(), w["id"]))
            return
        prof = None if w["profile"] == "__none__" else profile(w["profile"])
        imp = Importer(ready, w["project"] or (prof or {}).get("project", "Default"))
        imp.run()                         # synchronous in this background thread
        db.run("UPDATE watch_folders SET last_scan=? WHERE id=?", (time.time(), w["id"]))
        if imp.added_ids and prof:
            g = dict(prof, project=w["project"] or prof.get("project", "Default"))
            start(g, imp.added_ids, name=f"Watch folder: {root.name}")
            db.log("info", f"Watch folder {root.name}: {len(imp.added_ids)} new video(s) queued with profile "
                           f"“{w['profile'] or 'last used settings'}”")
        elif imp.added_ids:
            db.log("info", f"Watch folder {root.name}: {len(imp.added_ids)} new video(s) imported (no profile set, "
                           "so nothing was generated)")


watcher = Watcher()
