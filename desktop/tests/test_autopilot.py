"""Autopilot end to end: a video dropped into an autopilot folder becomes clips that are approved, put in the
calendar and sent to a local fake of the Ayrshare API, with no clicks. Also checks the pipeline steps the Autopilot
page shows, the "what changed" list for each clip, and Reddit link posts.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["REELSCUTTER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reelscutter import autopilot, db, paths, posting, production, selection  # noqa: E402
from reelscutter.jobs import queue  # noqa: E402

calls = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path.startswith("/api/media/uploadUrl"):
            port = self.server.server_address[1]
            return self._send({"uploadUrl": f"http://127.0.0.1:{port}/put/x.mp4", "contentType": "video/mp4",
                               "accessUrl": "https://cdn.example/x.mp4"})
        self._send({}, 404)

    def do_PUT(self):
        calls.append(("PUT", self.path, len(self._body())))
        self._send({})

    def do_POST(self):
        body = json.loads(self._body() or b"{}")
        calls.append(("POST", self.path, body))
        return self._send({"status": "success", "id": f"ayr{len(calls)}"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
posting.API = f"http://127.0.0.1:{srv.server_address[1]}/api"

work = Path(tempfile.mkdtemp())
inbox = work / "inbox"
inbox.mkdir()
db.set_setting("output_dir", str(work / "out"))
db.set_setting("internal_reserve_gb", "0.5")
db.set_setting("min_free_gb", "0.5")
db.set_setting("pause_on_battery", "0")      # so the test also runs on a laptop on battery
db.set_setting("ayrshare_api_key", "test-key")
db.set_setting("post_platforms", json.dumps(["instagram", "youtube", "reddit"]))
db.set_setting("reddit_subreddit", "r/testsub")
g = dict(selection=selection.SelectionSettings(mode="smart", clips_per_video=2, durations=[8], min_len=5,
                                               max_len=12).to_dict(),
         preset="Reels 9:16 1080p", captions="none", project="Auto")
db.set_setting("last_generator", json.dumps(g))
db.run("INSERT INTO watch_folders(path,project,profile,enabled,auto_post) VALUES(?,?,?,1,1)",
       (str(inbox), "Auto", ""))
assert autopilot.enabled()
autopilot.install()

# a 40 s test video with sound "dropped" into the folder
subprocess.run([paths.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40", "-f", "lavfi",
                "-i", "sine=f=440:d=40", "-t", "40", "-c:v", "libx264", "-c:a", "aac",
                str(inbox / "talk.mp4")], check=True)
production.watcher.scan_all()          # first look: notes the size
production.watcher.scan_all()          # size unchanged: imports and queues it
assert db.q1("SELECT autopilot FROM batches")["autopilot"] == 1
v = db.q1("SELECT * FROM videos")
print("steps right after drop:", [(n, s) for n, s, _ in autopilot.steps(v)])

queue.start()
t0 = time.time()
seen = set()
while time.time() - t0 < 600:
    time.sleep(1)
    v = db.q1("SELECT * FROM videos")
    seen |= {(n, s) for n, s, _ in autopilot.steps(v)}
    if db.q1("SELECT COUNT(*) c FROM posts WHERE status='scheduled'")["c"] >= 2:
        break
queue.shutdown()

jobs = db.q("SELECT * FROM jobs ORDER BY start")
for j in jobs:
    print("clip", j["id"], j["status"], j["review"], round(j["start"], 1), round(j["end"], 1))
assert len(jobs) == 2 and all(j["status"] == "done" and j["review"] == "approved" for j in jobs)
posts = db.q("SELECT * FROM posts ORDER BY at")
assert len(posts) == 2 and all(p["status"] == "scheduled" for p in posts), [dict(p) for p in posts]
assert all(p["reddit_id"] and p["ayr_id"] for p in posts)
assert ("Editing clips", "active") in seen or ("Editing clips", "done") in seen, seen

post_bodies = [c[2] for c in calls if c[1] == "/api/post"]
video = [b for b in post_bodies if "reddit" not in b["platforms"]]
reddit = [b for b in post_bodies if b["platforms"] == ["reddit"]]
assert len(video) == 2 and all(b["platforms"] == ["instagram", "youtube"] and b["isVideo"] for b in video)
assert len(reddit) == 2 and all(b["redditOptions"]["subreddit"] == "testsub" and
                                b["redditOptions"]["link"] == "https://cdn.example/x.mp4" for b in reddit)
assert sum(1 for c in calls if c[0] == "PUT") == 2           # each clip uploaded once

steps = autopilot.steps(db.q1("SELECT * FROM videos"))
print("final steps:", steps)
d = dict((n, s) for n, s, _ in steps)
assert d["Found"] == d["Analyzing"] == d["Picking best parts"] == d["Editing clips"] == d["Quality check"] == "done"
assert d["Scheduled"] == "done", steps

ch = autopilot.changes(jobs[0])
print("what changed:")
for what, det in ch:
    print(f"  {what}: {det}")
names = [w for w, _ in ch]
for need in ("Cut from", "Why this part", "Framing", "Captions", "Sound", "Quality check", "Post", "File"):
    assert need in names, (need, names)
assert "blurred" in dict(ch)["Framing"] and "1080×1920" in dict(ch)["Framing"] and "−14 LUFS" in dict(ch)["Sound"]
assert json.loads(jobs[0]["applied"])["captions_burned"] is False

# a clip whose captions were planned but dropped by an automatic repair must say so, not claim captions
j = dict(jobs[0])
j["preset"] = json.dumps(dict(json.loads(j["preset"]), subtitles=True))
fake = type("Row", (), {"__getitem__": lambda s_, k: j[k], "keys": lambda s_: j.keys()})()
assert "dropped" in dict(autopilot.changes(fake))["Captions"], autopilot.changes(fake)

vids = autopilot.videos()
assert vids and vids[0]["autopilot"] and len(vids[0]["steps"]) == len(autopilot.STEPS)

# nothing is posted twice on later ticks
before = len(calls)
autopilot.tick()
assert len(calls) == before

# cancelling a scheduled post removes both the video post and the Reddit post
calls.clear()
p = db.q1("SELECT * FROM posts ORDER BY at DESC")
db.run("UPDATE posts SET at=? WHERE id=?", (time.time() + 86400, p["id"]))


class Del(Fake):
    def do_DELETE(self):
        calls.append(("DELETE", self.path, json.loads(self._body() or b"{}")))
        self._send({"status": "success"})


srv.RequestHandlerClass = Del
posting.cancel(p["id"])
assert sorted(c[2]["id"] for c in calls if c[0] == "DELETE") == sorted([p["ayr_id"], p["reddit_id"]]), calls
print("autopilot test passed")
