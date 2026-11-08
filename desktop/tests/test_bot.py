"""Jasper Bot: the tool loop with a scripted stand-in for Claude, every tool, and the ask-first rule."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

os.environ["REELSCUTTER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reelscutter import db, jasperbot  # noqa: E402

db.conn()
work = Path(tempfile.mkdtemp())
db.run("INSERT INTO videos(path,name,duration,added_at) VALUES(?,?,?,?)", (str(work / "talk.mp4"), "talk.mp4", 600,
                                                                         time.time()))
vid = db.q1("SELECT id FROM videos")["id"]
for i in range(3):
    f = work / f"c{i}.mp4"
    f.write_bytes(b"\0" * 100)
    db.run("INSERT INTO jobs(video_id,start,end,preset,out_path,status,created_at,review,score) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (vid, i * 30, i * 30 + 20, json.dumps({"aspect": "9:16", "fit": "crop"}),
                                         str(f), "done", time.time(), "ready", 50 + i))


class FakeClaude:
    """Replays a script: each step is a list of (tool name, input) calls, or a final text."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.messages = self

    def create(self, **kw):
        self.seen.append(kw)
        assert kw["model"] == "claude-opus-5-5" and kw["thinking"] == {"type": "adaptive"} and kw["tools"]
        step = self.script.pop(0)
        if isinstance(step, str):
            return NS(stop_reason="end_turn", content=[NS(type="text", text=step)])
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t{k}", name=n, input=a)
                                                   for k, (n, a) in enumerate(step)])


asked = []
pages = []
fake = FakeClaude([
    [("get_status", {}), ("list_videos", {})],
    [("list_clips", {"video_id": vid}), ("explain_clip", {"clip_id": 1})],
    [("review_clips", {"clip_ids": [1, 2], "decision": "approve"})],
    [("plan_posts", {"platforms": ["instagram", "tiktok"]})],
    [("send_posts", {}), ("set_posting", {"posts_per_day": 3}), ("open_page", {"page": "posting"})],
    [("nonexistent_tool", {})],
    "Done: 2 clips approved and planned. Posting needs your Ayrshare key first.",
])
bot = jasperbot.Bot(confirm=lambda t: asked.append(t) or False, navigate=pages.append, client=fake)
steps = []
reply = bot.ask("approve my best clips and post them", on_step=steps.append)
print("reply:", reply)
print("steps:", steps)
assert reply.startswith("Done") and steps[:2] == ["get_status", "list_videos"]
assert {r["review"] for r in db.q("SELECT review FROM jobs WHERE id IN (1,2)")} == {"approved"}
assert db.q1("SELECT COUNT(*) c FROM posts")["c"] == 2                 # planned, not sent
assert db.q1("SELECT COUNT(*) c FROM posts WHERE status='planned'")["c"] == 2
assert db.setting("posts_per_day") == "6", "settings changed without a yes"
assert len(asked) == 1 and "posting settings" in asked[0]                # send_posts stopped earlier: no key
assert pages == ["posting"]
# tool results went back with matching ids, errors flagged
results = [m for m in bot.messages if m["role"] == "user" and isinstance(m["content"], list)]
assert all(r["tool_use_id"].startswith("t") for m in results for r in m["content"])
assert any(r.get("is_error") for r in results[-1]["content"])
explain = json.loads(results[1]["content"][1]["content"])
assert any(x.startswith("Cut from") for x in explain), explain

# with a key and a yes, send_posts would publish; with a no it does nothing
db.set_setting("ayrshare_api_key", "k")
bot2 = jasperbot.Bot(confirm=lambda t: False)
assert bot2.run_tool("send_posts", {})["note"] == "The user said no."
assert db.q1("SELECT COUNT(*) c FROM posts WHERE status='planned'")["c"] == 2

# settings change with a yes
bot3 = jasperbot.Bot(confirm=lambda t: True)
bot3.run_tool("set_posting", {"posts_per_day": 3, "platforms": ["instagram", "bogus"]})
assert db.setting("posts_per_day") == "3" and json.loads(db.setting("post_platforms")) == ["instagram"]

# make_clips queues a batch with the overrides
out = bot3.run_tool("make_clips", {"video_ids": [vid], "clips_per_video": 4, "length_seconds": 25})
b = json.loads(db.q1("SELECT settings FROM batches WHERE id=?", (out["batch"],))["settings"])
assert b["selection"]["clips_per_video"] == 4 and b["selection"]["durations"] == [25.0], b

# autopilot folder needs a real folder and a yes
try:
    bot3.run_tool("add_autopilot_folder", {"path": str(work / "nope")})
    raise SystemExit("missing folder accepted")
except ValueError:
    pass
bot3.run_tool("add_autopilot_folder", {"path": str(work)})
assert db.q1("SELECT auto_post FROM watch_folders")["auto_post"] == 1

# no key and no client: friendly message, no crash
db.set_setting("anthropic_api_key", "")
assert "API key" in jasperbot.Bot().ask("hi")

# quick-action summaries (no AI) render
from reelscutter.ui import bot_page  # noqa: E402
for t, a in (("get_status", {}), ("list_clips", {"limit": 5}), ("posting_report", {}),
             ("queue_control", {"action": "retry_failed"})):
    print(bot_page._summary(t, bot3.run_tool(t, dict(a))).split("\n")[0])
print("bot test passed")
