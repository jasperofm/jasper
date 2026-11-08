"""Auto-posting test against a local fake of the Ayrshare API (no real account or network needed).

Checks: connection test, calendar slots (count per day, spacing, posting window), upload + schedule calls with the
right fields, stats parsing, best-hour learning, and cancel.
"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["REELSCUTTER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reelscutter import db, posting  # noqa: E402

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
        calls.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.startswith("/api/user"):
            return self._send({"activeSocialAccounts": ["instagram", "tiktok", "youtube"]})
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
        if self.path == "/api/post":
            if body.get("post") == "FAIL":
                return self._send({"status": "error", "errors": [{"message": "bad"}]}, 400)
            return self._send({"status": "success", "id": f"ayr{len(calls)}"})
        if self.path == "/api/analytics/post":
            return self._send({"instagram": {"analytics": {"playsCount": 1200, "likeCount": 80}},
                               "youtube": {"analytics": {"views": 300, "likes": 12}}})
        self._send({}, 404)

    def do_DELETE(self):
        calls.append(("DELETE", self.path, json.loads(self._body() or b"{}")))
        self._send({"status": "success"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
posting.API = f"http://127.0.0.1:{srv.server_address[1]}/api"

db.conn()
ok, msg, linked = posting.check_connection()
assert not ok and "API key" in msg, msg
db.set_setting("ayrshare_api_key", "test-key")
ok, msg, linked = posting.check_connection()
assert ok and linked == ["instagram", "tiktok", "youtube"], msg
assert calls[-1][2] == "Bearer test-key"

# fake finished clips
work = Path(tempfile.mkdtemp())
vid = db.run("INSERT INTO videos(path,name,duration,added_at) VALUES(?,?,?,?)", ("/x.mp4", "talk.mp4", 600, time.time()))
vid = db.q1("SELECT id FROM videos")["id"]
for i in range(9):
    f = work / f"clip{i}.mp4"
    f.write_bytes(b"\0" * 1000)
    db.run("INSERT INTO jobs(video_id,start,end,preset,out_path,status,created_at,review,title,tags) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)", (vid, i * 30, i * 30 + 30, "p", str(f), "done", time.time(), "approved",
                                         f"Clip {i}", "money tips,growth"))
db.set_setting("posts_per_day", "4")
db.set_setting("post_window_start", "9")
db.set_setting("post_window_end", "21")
ids = [r["id"] for r in db.q("SELECT id FROM jobs")]
n = posting.add_to_calendar(ids, ["instagram", "youtube"])
assert n == 9, n
assert posting.add_to_calendar(ids, ["instagram"]) == 0          # no double-planning
rows = db.q("SELECT * FROM posts ORDER BY at")
ts = [r["at"] for r in rows]
assert all(b - a >= 45 * 60 for a, b in zip(ts, ts[1:])), "posts too close together"
per_day = {}
for t in ts:
    d = datetime.fromtimestamp(t)
    assert 9 <= d.hour <= 21, d
    per_day[d.date()] = per_day.get(d.date(), 0) + 1
assert max(per_day.values()) <= 4, per_day
assert "#MoneyTips" in rows[0]["text"], rows[0]["text"]
print("calendar:", [datetime.fromtimestamp(t).strftime("%a %H:%M") for t in ts])

# send: upload + schedule
db.run("UPDATE posts SET text='FAIL' WHERE id=?", (rows[-1]["id"],))
sent = posting.send_all()
assert sent == 9
st = {r["status"] for r in db.q("SELECT status FROM posts")}
assert st == {"scheduled", "failed"}, st
post_calls = [c for c in calls if c[0] == "POST" and c[1] == "/api/post"]
b0 = post_calls[0][2]
assert b0["mediaUrls"] == ["https://cdn.example/x.mp4"] and b0["isVideo"] is True
assert b0["scheduleDate"].endswith("Z") and b0["youTubeOptions"]["shorts"] is True and b0["youTubeOptions"]["title"]
assert sum(1 for c in calls if c[0] == "PUT") == 9
failed = db.q1("SELECT * FROM posts WHERE status='failed'")
assert "bad" in (failed["error"] or ""), failed["error"]

# stats + learning: pretend posts already went out at 10:00 and 18:00 on past days
past = time.time() - 3 * 86400
for i, r in enumerate(db.q("SELECT id FROM posts WHERE status='scheduled'")):
    h = 10 if i % 2 else 18
    t = datetime.fromtimestamp(past - i * 86400).replace(hour=h, minute=5).timestamp()
    db.run("UPDATE posts SET at=? WHERE id=?", (t, r["id"]))
assert posting.refresh_stats(max_age_h=0) == 8
r = db.q1("SELECT views, likes FROM posts WHERE status='scheduled'")
assert r["views"] == 1500 and r["likes"] == 92, dict(r)
hs = posting.hour_scores()
assert set(hs) == {10, 18}, hs
best = posting.best_hours(4)
assert 10 in best and 18 in best and len(best) == 4, best
print("best hours:", best)

# cancel a future scheduled post
db.run("UPDATE posts SET at=?, status='scheduled', ayr_id='ayrX' WHERE id=?", (time.time() + 86400, failed["id"]))
posting.cancel(failed["id"])
assert calls[-1][0] == "DELETE" and calls[-1][2] == {"id": "ayrX"}
assert not db.q1("SELECT 1 FROM posts WHERE id=?", (failed["id"],))
print("posting test passed")
