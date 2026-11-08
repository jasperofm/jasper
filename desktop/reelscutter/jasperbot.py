"""Jasper Bot: an assistant inside Reels Cutter that can run the app for you.

It talks to Claude (with your Anthropic API key from Settings) and uses tools that call the same functions as the
buttons in the app: status, videos, clips, making clips, review, autopilot folders, posting calendar, queue control.
Anything that publishes or changes settings asks you first through `confirm`.

Without a key, the quick actions still work (they call the tools directly).
"""
import json
import time
from pathlib import Path

from . import autopilot, db, posting, production, selection

MODEL = "claude-opus-5-5"
SYSTEM = """You are Jasper Bot, the assistant built into Reels Cutter, a Mac app that turns the user's own long videos
into short vertical clips (Reels, TikTok, Shorts) and posts them through Ayrshare.
Use the tools to look things up and to do what the user asks; don't guess numbers you can look up.
Keep replies short and friendly, in plain words, no jargon. Say what you did and what happens next.
The app does not make re-uploads look new to platforms and you must not offer or attempt that (no spoofing,
fingerprint/metadata tricks, or variations meant to dodge duplicate detection). Clips are cut from the user's own
videos and exported without the source file's metadata as a privacy default.
Posting and settings changes need the user's OK; the tools ask them, so just call the tool."""

TOOLS = [
    {"name": "get_status", "description": "Overall status: queue, exports running/waiting/failed, autopilot folders, "
                                          "posting setup, next posts, recent errors.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_videos", "description": "Videos in the library, newest first, with their pipeline step.",
     "input_schema": {"type": "object", "properties": {
         "search": {"type": "string", "description": "Optional part of the file name"},
         "limit": {"type": "integer", "description": "Max videos, default 15"}}}},
    {"name": "list_clips", "description": "Clips (finished or in progress) for one video, or the latest clips.",
     "input_schema": {"type": "object", "properties": {
         "video_id": {"type": "integer"}, "limit": {"type": "integer", "description": "Default 20"}}}},
    {"name": "explain_clip", "description": "Exactly what was done to make one clip: cut range, why it was picked, "
                                            "framing, captions, sound, quality check, post.",
     "input_schema": {"type": "object", "properties": {"clip_id": {"type": "integer"}}, "required": ["clip_id"]}},
    {"name": "make_clips", "description": "Queue clip making for library videos using the last Clip Generator "
                                         "settings, optionally overriding count, length and captions.",
     "input_schema": {"type": "object", "properties": {
         "video_ids": {"type": "array", "items": {"type": "integer"}},
         "clips_per_video": {"type": "integer"},
         "length_seconds": {"type": "number"},
         "captions": {"type": "string", "enum": ["none", "burn", "both", "files"]}},
         "required": ["video_ids"]}},
    {"name": "review_clips", "description": "Approve or reject finished clips.",
     "input_schema": {"type": "object", "properties": {
         "clip_ids": {"type": "array", "items": {"type": "integer"}},
         "decision": {"type": "string", "enum": ["approve", "reject"]}}, "required": ["clip_ids", "decision"]}},
    {"name": "plan_posts", "description": "Put approved clips into the posting calendar at the next best times "
                                         "(planned only, nothing is published).",
     "input_schema": {"type": "object", "properties": {
         "clip_ids": {"type": "array", "items": {"type": "integer"},
                      "description": "Clips to plan; omit for all approved clips not planned yet"},
         "platforms": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "send_posts", "description": "Upload planned posts and schedule them on Ayrshare (publishes to the "
                                         "user's accounts). Asks the user first.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "posting_report", "description": "Upcoming and recent posts with views/likes, and the best hours.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_posting", "description": "Change posting settings. Asks the user first.",
     "input_schema": {"type": "object", "properties": {
         "posts_per_day": {"type": "integer"}, "start_hour": {"type": "integer"}, "end_hour": {"type": "integer"},
         "platforms": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "add_autopilot_folder", "description": "Turn on autopilot for a folder: new videos there are cut, "
                                                    "checked and posted automatically. Asks the user first.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "check_folders", "description": "Check watch/autopilot folders for new videos now.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "queue_control", "description": "Pause or resume exporting, or retry failed clips.",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["pause", "resume", "retry_failed"]}}, "required": ["action"]}},
    {"name": "open_page", "description": "Show a page of the app to the user.",
     "input_schema": {"type": "object", "properties": {"page": {"type": "string", "enum": [
         "dashboard", "autopilot", "library", "generator", "editor", "review", "posting", "export", "settings"]}},
         "required": ["page"]}},
]

PLATFORM_KEYS = set(posting.PLATFORMS)


def _when(t):
    return time.strftime("%a %d %b %H:%M", time.localtime(t)) if t else "–"


class Bot:
    """One conversation. `confirm(text) -> bool` asks the user; `navigate(page)` switches the app's page."""

    def __init__(self, confirm=lambda text: False, navigate=lambda page: None, client=None):
        self.confirm, self.navigate = confirm, navigate
        self.messages = []
        self._client = client

    # ------------------------------------------------------------------ chat
    def available(self):
        return self._client is not None or bool((db.setting("anthropic_api_key") or "").strip())

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=db.setting("anthropic_api_key").strip())
        return self._client

    def ask(self, text, on_step=lambda s: None):
        """Runs one user turn through Claude with tools. Returns the reply text."""
        if not self.available():
            return ("I need an Anthropic API key to chat. Add it in Settings → AI, or use the quick buttons "
                    "below, which work without one.")
        self.messages.append({"role": "user", "content": text})
        for _ in range(12):
            resp = self.client().messages.create(model=MODEL, max_tokens=8000, system=SYSTEM, tools=TOOLS,
                                                 thinking={"type": "adaptive"}, messages=self.messages)
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue
            uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text") \
                    or "Done."
            results = []
            for u in uses:
                on_step(u.name)
                try:
                    out = self.run_tool(u.name, dict(u.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": u.id,
                                    "content": json.dumps(out, default=str)[:20000]})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": f"Error: {e}",
                                    "is_error": True})
            self.messages.append({"role": "user", "content": results})
        return "I stopped after several steps. Tell me what to do next."

    def reset(self):
        self.messages = []

    # ------------------------------------------------------------------ tools
    def run_tool(self, name, a):
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        db.log("info", f"Jasper Bot: {name.replace('_', ' ')}")
        return fn(**a)

    def t_get_status(self):
        from .jobs import queue
        c = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) c FROM jobs WHERE hidden=0 GROUP BY status")}
        nxt = db.q1("SELECT at FROM posts WHERE status IN ('planned','scheduled') AND at>? ORDER BY at",
                    (time.time(),))
        return {
            "exporting": c.get("running", 0), "waiting": c.get("queued", 0), "finished": c.get("done", 0),
            "failed": c.get("failed", 0), "paused": queue.paused, "blocked_reason": queue.blocked_reason or None,
            "videos": db.q1("SELECT COUNT(*) c FROM videos")["c"],
            "clips_awaiting_review": db.q1("SELECT COUNT(*) c FROM jobs WHERE status='done' AND review IN "
                                           "('awaiting','ready') AND hidden=0")["c"],
            "autopilot_folders": [r["path"] for r in db.q("SELECT path FROM watch_folders WHERE enabled=1 AND "
                                                          "auto_post=1")],
            "posting_connected": bool((db.setting("ayrshare_api_key") or "").strip()),
            "platforms": json.loads(db.setting("post_platforms", "[]")),
            "posts_planned": db.q1("SELECT COUNT(*) c FROM posts WHERE status='planned'")["c"],
            "posts_failed": db.q1("SELECT COUNT(*) c FROM posts WHERE status='failed'")["c"],
            "next_post": _when(nxt["at"]) if nxt else None,
            "recent_errors": [r["message"][:200] for r in db.q(
                "SELECT message FROM log WHERE level='error' AND ts>? ORDER BY id DESC LIMIT 5",
                (time.time() - 86400,))],
        }

    def t_list_videos(self, search="", limit=15):
        rows = db.q("SELECT * FROM videos WHERE name LIKE ? ORDER BY added_at DESC LIMIT ?",
                    (f"%{search}%", max(1, min(int(limit or 15), 50))))
        out = []
        for v in rows:
            st = autopilot.steps(v)
            cur = next(((n, d) for n, s, d in st if s in ("active", "failed")), None)
            out.append({"id": v["id"], "name": v["name"], "seconds": round(v["duration"] or 0, 1),
                        "status": v["status"], "step": f"{cur[0]}: {cur[1]}" if cur else st[-1][0],
                        "clips": db.q1("SELECT COUNT(*) c FROM jobs WHERE video_id=? AND status!='canceled'",
                                       (v["id"],))["c"], "note": v["error"]})
        return out

    def t_list_clips(self, video_id=None, limit=20):
        sql = "SELECT j.*, v.name vname FROM jobs j JOIN videos v ON v.id=j.video_id WHERE j.status!='canceled' "
        args = []
        if video_id:
            sql += "AND j.video_id=? "
            args.append(int(video_id))
        sql += "ORDER BY j.id DESC LIMIT ?"
        args.append(max(1, min(int(limit or 20), 60)))
        out = []
        for j in db.q(sql, args):
            p = db.q1("SELECT status, at FROM posts WHERE job_id=?", (j["id"],))
            out.append({"clip_id": j["id"], "video": j["vname"], "from": round(j["start"], 1),
                        "to": round(j["end"], 1), "status": j["status"], "review": j["review"],
                        "score": j["score"], "title": j["title"],
                        "post": f"{p['status']} {_when(p['at'])}" if p else None, "error": j["error"]})
        return out

    def t_explain_clip(self, clip_id):
        j = db.q1("SELECT * FROM jobs WHERE id=?", (int(clip_id),))
        if not j:
            raise ValueError("no clip with that id")
        return [f"{w}: {d}" for w, d in autopilot.changes(j)]

    def t_make_clips(self, video_ids, clips_per_video=None, length_seconds=None, captions=None):
        g = production.profile("")
        if not g:
            g = {"selection": selection.SelectionSettings().to_dict(), "preset": db.setting("default_preset"),
                 "captions": "none", "project": "Default"}
        g = json.loads(json.dumps(g))
        sel = g["selection"]
        if clips_per_video:
            sel["clips_per_video"] = max(1, min(int(clips_per_video), 50))
        if length_seconds:
            L = max(5.0, min(float(length_seconds), 180.0))
            sel["durations"] = [L]
            sel["min_len"] = min(sel.get("min_len", 10), L)
            sel["max_len"] = max(sel.get("max_len", 90), L)
        if captions:
            g["captions"] = captions
        ids = [r["id"] for r in db.q(f"SELECT id FROM videos WHERE id IN ({','.join('?' * len(video_ids))})",
                                     [int(i) for i in video_ids])] if video_ids else []
        if not ids:
            raise ValueError("none of those videos are in the library")
        bid = production.start(g, ids, name="Jasper Bot")
        return {"queued_videos": ids, "batch": bid, "clips_per_video": sel["clips_per_video"],
                "lengths": sel.get("durations"), "captions": g.get("captions", "none")}

    def t_review_clips(self, clip_ids, decision):
        val = "approved" if decision == "approve" else "rejected"
        ids = [int(i) for i in clip_ids]
        db.many("UPDATE jobs SET review=? WHERE id=? AND status='done'", [(val, i) for i in ids])
        return {"updated": ids, "review": val}

    def t_plan_posts(self, clip_ids=None, platforms=None):
        plats = [p for p in (platforms or json.loads(db.setting("post_platforms", "[]"))) if p in PLATFORM_KEYS]
        if not plats:
            raise ValueError("no platforms chosen")
        if not clip_ids:
            clip_ids = [r["id"] for r in db.q(
                "SELECT j.id FROM jobs j LEFT JOIN posts p ON p.job_id=j.id WHERE j.status='done' AND "
                "j.review='approved' AND p.id IS NULL ORDER BY j.score DESC, j.id")]
        n = posting.add_to_calendar([int(i) for i in clip_ids], plats)
        rows = db.q("SELECT id, at FROM posts WHERE status='planned' ORDER BY at")
        return {"planned": n, "platforms": plats, "calendar": [_when(r["at"]) for r in rows[:15]]}

    def t_send_posts(self):
        if not (db.setting("ayrshare_api_key") or "").strip():
            return {"sent": 0, "note": "No Ayrshare key yet. Add it in Auto-Posting."}
        n = db.q1("SELECT COUNT(*) c FROM posts WHERE status IN ('planned','failed')")["c"]
        if not n:
            return {"sent": 0, "note": "Nothing waiting to send."}
        if not self.confirm(f"Jasper Bot wants to upload {n} clip(s) and schedule them on your linked accounts. "
                            "Send them?"):
            return {"sent": 0, "note": "The user said no."}
        posting.send_all()
        rows = db.q("SELECT status, COUNT(*) c FROM posts GROUP BY status")
        return {"result": {r["status"]: r["c"] for r in rows}}

    def t_posting_report(self):
        rows = db.q("SELECT p.*, v.name vname FROM posts p LEFT JOIN jobs j ON j.id=p.job_id LEFT JOIN videos v "
                    "ON v.id=j.video_id WHERE p.at > ? ORDER BY p.at LIMIT 40", (time.time() - 14 * 86400,))
        return {"best_hours": posting.best_hours(int(db.setting("posts_per_day", "6"))),
                "posts": [{"when": _when(r["at"]), "video": r["vname"], "status": r["status"],
                           "platforms": json.loads(r["platforms"]), "views": r["views"], "likes": r["likes"],
                           "error": r["error"]} for r in rows]}

    def t_set_posting(self, posts_per_day=None, start_hour=None, end_hour=None, platforms=None):
        ch = {}
        if posts_per_day is not None:
            ch["posts_per_day"] = max(1, min(int(posts_per_day), 24))
        if start_hour is not None:
            ch["post_window_start"] = max(0, min(int(start_hour), 23))
        if end_hour is not None:
            ch["post_window_end"] = max(0, min(int(end_hour), 23))
        if platforms is not None:
            ch["post_platforms"] = json.dumps([p for p in platforms if p in PLATFORM_KEYS])
        if not ch:
            return {"changed": {}}
        if not self.confirm("Change posting settings: " + ", ".join(f"{k} = {v}" for k, v in ch.items()) + "?"):
            return {"changed": {}, "note": "The user said no."}
        for k, v in ch.items():
            db.set_setting(k, v)
        return {"changed": ch}

    def t_add_autopilot_folder(self, path):
        p = Path(path).expanduser()
        if not p.is_dir():
            raise ValueError(f"folder not found: {p}")
        if not self.confirm(f"Turn on autopilot for {p}? New videos there will be cut, checked and posted "
                            "automatically."):
            return {"note": "The user said no."}
        db.run("INSERT INTO watch_folders(path,project,profile,enabled,auto_post) VALUES(?,?,?,1,1) "
               "ON CONFLICT(path) DO UPDATE SET enabled=1, auto_post=1", (str(p), "Default", ""))
        production.watcher.start()
        return {"autopilot_folder": str(p)}

    def t_check_folders(self):
        production.watcher.start()
        production.watcher.scan_all()
        return {"checked": [r["path"] for r in db.q("SELECT path FROM watch_folders WHERE enabled=1")],
                "note": "Files still copying are picked up on the next check."}

    def t_queue_control(self, action):
        from .jobs import queue
        if action == "pause":
            queue.pause()
        elif action == "resume":
            queue.resume()
        else:
            queue.retry_all_failed()
        return {"done": action, "paused": queue.paused}

    def t_open_page(self, page):
        self.navigate(page)
        return {"opened": page}
