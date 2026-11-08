"""Auto-posting through Ayrshare: a posting calendar, best-time slots, AI-written post text, and stats.

Ayrshare is already approved by Instagram, TikTok, YouTube, Threads, X and others, so Reels Cutter only needs the
user's Ayrshare API key. Each clip is uploaded once, then handed to Ayrshare with a scheduled time, so posts go out
even when this Mac is off. Views and likes are pulled back later and used to pick better posting hours.
"""
import json
import mimetypes
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

API = "https://api.ayrshare.com/api"
PLATFORMS = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube Shorts", "threads": "Threads",
             "twitter": "X", "facebook": "Facebook", "linkedin": "LinkedIn", "reddit": "Reddit"}
# Where you log in to each platform and link it (Ayrshare dashboard → Social Accounts). Logins happen on the
# platforms' own pages; Reels Cutter never sees or stores your social media passwords.
CONNECT_URL = "https://app.ayrshare.com/"
# Starting guesses for when short videos do well (local time). Replaced by your own results as stats come in.
DEFAULT_HOURS = [7, 9, 11, 12, 13, 15, 17, 18, 19, 20, 21, 22]
VIEW_KEYS = ("playsCount", "viewsCount", "videoViews", "views", "mediaView", "impressionCount", "impression")
LIKE_KEYS = ("likeCount", "likes", "totalReactions", "ups")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
    title TEXT, platforms TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
    ayr_id TEXT, media_url TEXT, error TEXT, views INTEGER, likes INTEGER, stats TEXT, stats_at REAL,
    created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS posts_at ON posts(at);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
"""


def ensure_schema():
    c = db.conn()
    for stmt in [x.strip() for x in SCHEMA.split(";") if x.strip()]:
        c.execute(stmt)


class AyrshareError(Exception):
    pass


def _key():
    k = (db.setting("ayrshare_api_key") or "").strip()
    if not k:
        raise AyrshareError("Add your Ayrshare API key in Posting → Settings first.")
    return k


def _req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {_key()}", "Content-Type": "application/json", "User-Agent": "ReelsCutter"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode())
            msg = msg.get("message") or msg.get("errors") or msg
        except Exception:
            msg = e.reason
        raise AyrshareError(f"Ayrshare said: {msg} (HTTP {e.code})") from None
    except urllib.error.URLError as e:
        raise AyrshareError(f"Couldn't reach Ayrshare: {e.reason}") from None


def check_connection():
    """Returns (ok, message, linked platforms)."""
    try:
        u = _req("GET", "/user")
    except AyrshareError as e:
        return False, str(e), []
    linked = u.get("activeSocialAccounts") or []
    return True, ("Connected. Linked accounts: " + (", ".join(PLATFORMS.get(p, p) for p in linked) or "none yet")), linked


# ---------------------------------------------------------------- upload + schedule
def upload(path):
    """Uploads a clip to Ayrshare's storage and returns its public media URL."""
    p = Path(path)
    if not p.exists():
        raise AyrshareError(f"The clip file is missing: {p}")
    ext = p.suffix.lower().lstrip(".") or "mp4"
    info = _req("GET", "/media/uploadUrl?" + urllib.parse.urlencode({"fileName": p.name, "contentType": ext}))
    put = urllib.request.Request(info["uploadUrl"], data=p.read_bytes(), method="PUT",
                                 headers={"Content-Type": info.get("contentType") or mimetypes.guess_type(p.name)[0]
                                          or "video/mp4"})
    try:
        with urllib.request.urlopen(put, timeout=600) as resp:
            if resp.status >= 300:
                raise AyrshareError(f"Upload failed (HTTP {resp.status})")
    except urllib.error.URLError as e:
        raise AyrshareError(f"Upload failed: {getattr(e, 'reason', e)}") from None
    return info["accessUrl"]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send(post_id):
    """Uploads (once) and schedules one planned post with Ayrshare.

    Reddit doesn't accept video uploads through any posting API, so Reddit gets a separate link post (title plus a
    link to the uploaded clip) in the subreddit chosen in settings.
    """
    r = db.q1("SELECT * FROM posts WHERE id=?", (post_id,))
    if not r or r["status"] not in ("planned", "failed"):
        return
    try:
        db.run("UPDATE posts SET status='sending', error=NULL WHERE id=?", (post_id,))
        url = r["media_url"] or upload(r["path"])
        db.run("UPDATE posts SET media_url=? WHERE id=?", (url, post_id))
        plats = json.loads(r["platforms"])
        main = [p for p in plats if p != "reddit"]
        title = (r["title"] or r["text"] or "New video")
        when = {"scheduleDate": _iso(r["at"])} if r["at"] > time.time() + 120 else {}
        ayr_id = r["ayr_id"]
        if main and not ayr_id:
            body = {"post": r["text"] or "", "platforms": main, "mediaUrls": [url], "isVideo": True,
                    "idempotencyKey": f"reelscutter-{post_id}", **when}
            if "youtube" in main:
                body["youTubeOptions"] = {"title": title[:100],
                                          "visibility": db.setting("youtube_visibility", "public"), "shorts": True}
            res = _req("POST", "/post", body, timeout=120)
            if res.get("status") == "error":
                raise AyrshareError(json.dumps(res.get("errors") or res)[:400])
            ayr_id = res.get("id")
            db.run("UPDATE posts SET ayr_id=? WHERE id=?", (ayr_id, post_id))
        if "reddit" in plats and not r["reddit_id"]:
            sub = (db.setting("reddit_subreddit") or "").strip().removeprefix("r/").removeprefix("/r/").strip("/")
            if not sub:
                raise AyrshareError("Reddit needs a subreddit: set it in Auto-Posting settings")
            body = {"post": r["text"] or title, "platforms": ["reddit"],
                    "redditOptions": {"title": title[:300], "subreddit": sub, "link": url},
                    "idempotencyKey": f"reelscutter-{post_id}-reddit", **when}
            res = _req("POST", "/post", body, timeout=120)
            if res.get("status") == "error":
                raise AyrshareError("Reddit: " + json.dumps(res.get("errors") or res)[:400])
            db.run("UPDATE posts SET reddit_id=? WHERE id=?", (res.get("id"), post_id))
            ayr_id = ayr_id or res.get("id")
        db.run("UPDATE posts SET status='scheduled', ayr_id=? WHERE id=?", (ayr_id, post_id))
        db.log("info", f"Post {post_id} scheduled for {datetime.fromtimestamp(r['at']):%a %d %b %H:%M} on "
                       f"{', '.join(PLATFORMS.get(p, p) for p in plats)}", r["job_id"])
    except Exception as e:
        db.run("UPDATE posts SET status='failed', error=? WHERE id=?", (str(e)[:500], post_id))
        db.log("error", f"Posting failed for post {post_id}: {e}", r["job_id"])


def send_all(progress=lambda i, n: None):
    ids = [x["id"] for x in db.q("SELECT id FROM posts WHERE status IN ('planned','failed') ORDER BY at")]
    for i, pid in enumerate(ids):
        progress(i, len(ids))
        send(pid)
    progress(len(ids), len(ids))
    return len(ids)


def cancel(post_id):
    """Removes a post from the calendar, and from Ayrshare if it was already scheduled there."""
    r = db.q1("SELECT * FROM posts WHERE id=?", (post_id,))
    if not r:
        return
    if r["status"] == "scheduled" and r["at"] > time.time():
        for aid in {r["ayr_id"], r["reddit_id"]} - {None, ""}:
            try:
                _req("DELETE", "/post", {"id": aid})
            except AyrshareError as e:
                db.log("warning", f"Couldn't delete post {post_id} on Ayrshare: {e}")
    db.run("DELETE FROM posts WHERE id=?", (post_id,))


# ---------------------------------------------------------------- stats + learning
def _pick(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    pm = d.get("publicMetrics")
    return _pick(pm, keys) if isinstance(pm, dict) else None


def refresh_stats(max_age_h=6):
    """Pulls views and likes for posts that have gone out. Returns how many were updated."""
    rows = db.q("SELECT * FROM posts WHERE status='scheduled' AND ayr_id IS NOT NULL AND at < ? "
                "AND (stats_at IS NULL OR stats_at < ?) ORDER BY at DESC LIMIT 50",
                (time.time() - 3600, time.time() - max_age_h * 3600))
    n = 0
    for r in rows:
        try:
            res = _req("POST", "/analytics/post", {"id": r["ayr_id"]})
        except AyrshareError as e:
            db.log("warning", f"Stats unavailable for post {r['id']}: {e}")
            continue
        views = likes = 0
        for plat, v in res.items():
            a = v.get("analytics") if isinstance(v, dict) else None
            if isinstance(a, list):
                a = a[0] if a else None
            views += _pick(a, VIEW_KEYS) or 0
            likes += _pick(a, LIKE_KEYS) or 0
        db.run("UPDATE posts SET views=?, likes=?, stats=?, stats_at=? WHERE id=?",
               (views, likes, json.dumps(res)[:20000], time.time(), r["id"]))
        n += 1
    return n


def hour_scores():
    """Average views per local posting hour from your own posts (only hours with at least 2 posts)."""
    by = {}
    for r in db.q("SELECT at, views FROM posts WHERE views IS NOT NULL"):
        h = datetime.fromtimestamp(r["at"]).hour
        by.setdefault(h, []).append(r["views"])
    return {h: sum(v) / len(v) for h, v in by.items() if len(v) >= 2}


def best_hours(n):
    """The n hours to post at: your best-performing hours, plus a couple of untested ones to keep learning."""
    lo, hi = int(db.setting("post_window_start", "8")), int(db.setting("post_window_end", "22"))
    window = [h for h in range(24) if (lo <= h <= hi if lo <= hi else (h >= lo or h <= hi))]
    learned = hour_scores()
    ranked = sorted([h for h in window if h in learned], key=lambda h: -learned[h])
    defaults = [h for h in DEFAULT_HOURS if h in window and h not in ranked]
    rest = [h for h in window if h not in ranked and h not in defaults]
    explore = max(1, n // 4) if ranked else 0
    picks = ranked[:max(0, n - explore)]
    pool = defaults + rest
    prio = {h: i for i, h in enumerate(pool)}       # defaults first, then any other hour in the window
    if not picks and pool:
        picks.append(min(pool, key=lambda h: (abs(h - 18), prio[h])))   # anchor on the evening peak
        pool.remove(picks[0])
    while len(picks) < n and pool:      # spread the rest through the day: farthest from hours already picked
        h = max(pool, key=lambda x: (min(abs(x - p) for p in picks), -prio[x]))
        picks.append(h)
        pool.remove(h)
    if len(picks) < n:          # more posts than hours in the window: use half hours
        picks += [h + 0.5 for h in picks[: n - len(picks)]]
    return sorted(picks[:n])


# ---------------------------------------------------------------- calendar
def post_text(job):
    """Caption + hashtags for a clip: AI-written when cloud AI is on, otherwise from the clip's title and tags."""
    title = job["title"] or ""
    tags = [t.strip() for t in (job["tags"] or "").split(",") if t.strip()]
    words = ""
    v = db.q1("SELECT transcript FROM videos WHERE id=?", (job["video_id"],))
    if v and v["transcript"]:
        words = " ".join(w[2] for w in json.loads(v["transcript"]) if job["start"] <= w[0] <= job["end"])
    ai = _ai_text(words) if words else None
    if ai:
        return ai.get("caption", title).strip() + "\n\n" + " ".join(ai.get("hashtags", [])[:5]), ai.get("title") or title
    hashtags = " ".join("#" + "".join(ch for ch in t.title() if ch.isalnum()) for t in tags[:5])
    return (title + ("\n\n" + hashtags if hashtags else "")).strip(), title or (words[:80] if words else "")


def _ai_text(words):
    if db.setting("cloud_ai_enabled", "0") != "1" or not db.setting("anthropic_api_key"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=db.setting("anthropic_api_key"))
        msg = client.messages.create(
            model="claude-opus-5-5", max_tokens=800,
            system="You write social media post text for a short vertical video, using only what is said in it. "
                   "Reply with JSON only: {\"title\": max 70 chars, \"caption\": 1-2 short sentences, "
                   "\"hashtags\": 3-5 relevant hashtags}.",
            messages=[{"role": "user", "content": words[:4000]}])
        txt = "".join(getattr(b, "text", "") for b in msg.content)
        return json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    except Exception as e:
        db.log("warning", f"AI post text skipped: {e}")
        return None


def next_slots(count, per_day, start=None):
    """Free time slots from `start` on, per_day a day at the best hours, at least 45 minutes from other posts."""
    taken = [r["at"] for r in db.q("SELECT at FROM posts WHERE at > ?", (time.time(),))]
    hours = best_hours(per_day)
    day = (start or datetime.now()).replace(minute=0, second=0, microsecond=0)
    out = []
    for d in range(0, 366):
        base = (day + timedelta(days=d)).replace(hour=0)
        for h in hours:
            minute = int((h % 1) * 60) + random.randint(0, 9)       # small spread so posts don't all land on :00
            t = (base + timedelta(hours=int(h), minutes=minute)).timestamp()
            if t < time.time() + 600:
                continue
            if any(abs(t - x) < 45 * 60 for x in taken + out):
                continue
            out.append(t)
            if len(out) >= count:
                return out
    return out


def add_to_calendar(job_ids, platforms, per_day=None):
    """Plans the given finished clips into the next free slots. Returns the number added."""
    ensure_schema()
    per_day = per_day or int(db.setting("posts_per_day", "6"))
    jobs = [db.q1("SELECT * FROM jobs WHERE id=? AND status='done'", (j,)) for j in job_ids]
    jobs = [j for j in jobs if j and j["out_path"] and Path(j["out_path"]).exists()
            and not db.q1("SELECT 1 FROM posts WHERE job_id=?", (j["id"],))]
    slots = next_slots(len(jobs), per_day)
    for j, t in zip(jobs, slots):
        text, title = post_text(j)
        db.run("INSERT INTO posts(job_id,path,text,title,platforms,at,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (j["id"], j["out_path"], text, title, json.dumps(platforms), t, "planned", time.time()))
    return min(len(jobs), len(slots))
