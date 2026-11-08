"""Autopilot: videos dropped into an autopilot watch folder go all the way to posted clips without any clicks.

Watch folder → import → analysis → best parts picked → clips edited (framing, captions, sound) → quality check →
approved automatically → put into the posting calendar at the best hours → sent to Ayrshare.

This module also describes that pipeline for the Autopilot page: which step every video is at, which parts of the
video were used, and exactly what was changed in each clip.
"""
import json
import threading
import time
from pathlib import Path

from . import db, posting

_lock = threading.Lock()


# ---------------------------------------------------------------- the automatic step after export
def tick():
    """Approves finished autopilot clips, plans them into the calendar and sends them. Safe to call any time."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        posting.ensure_schema()
        rows = db.q("SELECT j.id FROM jobs j JOIN batches b ON b.id=j.batch_id "
                    "WHERE b.autopilot=1 AND j.status='done' AND j.review IN ('ready','awaiting','approved') "
                    "AND NOT EXISTS(SELECT 1 FROM posts p WHERE p.job_id=j.id) "
                    "ORDER BY COALESCE(j.score,0) DESC, j.id")
        ids = [r["id"] for r in rows]
        added = 0
        if ids:
            db.many("UPDATE jobs SET review='approved' WHERE id=?", [(i,) for i in ids])
            plats = json.loads(db.setting("post_platforms", '["instagram","tiktok","youtube"]'))
            added = posting.add_to_calendar(ids, plats)
            if added:
                db.log("info", f"Autopilot: {added} new clip(s) approved and put in the posting calendar "
                               "(best clips first)")
        if db.setting("autopilot_send", "1") == "1" and (db.setting("ayrshare_api_key") or "").strip():
            for r in db.q("SELECT p.id FROM posts p JOIN jobs j ON j.id=p.job_id JOIN batches b ON b.id=j.batch_id "
                          "WHERE b.autopilot=1 AND p.status='planned' ORDER BY p.at"):
                posting.send(r["id"])
        return added
    finally:
        _lock.release()


def tick_async():
    threading.Thread(target=tick, daemon=True, name="autopilot").start()


def _on_event(kind, data):
    if kind == "job_done":
        j = db.q1("SELECT b.autopilot FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE j.id=?", (data.get("id"),))
        if j and j["autopilot"]:
            tick_async()


def install():
    from .jobs import events
    events.subscribe(_on_event)


def enabled():
    return bool(db.q1("SELECT 1 FROM watch_folders WHERE enabled=1 AND auto_post=1"))


# ---------------------------------------------------------------- what the page shows
STEPS = ["Found", "Analyzing", "Picking best parts", "Editing clips", "Quality check", "Scheduled", "Posted"]


def _fmt(t):
    t = max(0, int(round(t or 0)))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def videos(days=14, limit=60):
    """Videos processed recently, newest first, each with its pipeline steps."""
    rows = db.q("SELECT v.*, MAX(b.autopilot) autopilot, MAX(b.id) batch FROM videos v "
                "JOIN batch_videos bv ON bv.video_id=v.id JOIN batches b ON b.id=bv.batch_id "
                "WHERE b.created_at > ? GROUP BY v.id ORDER BY MAX(b.created_at) DESC, v.id DESC LIMIT ?",
                (time.time() - days * 86400, limit))
    return [dict(id=r["id"], name=r["name"], duration=r["duration"] or 0, autopilot=bool(r["autopilot"]),
                 steps=steps(r)) for r in rows]


def steps(v):
    """[(step, state, detail)] where state is done / active / waiting / failed / skipped."""
    vid = v["id"]
    bvs = db.q("SELECT bv.state, bv.error, b.autopilot FROM batch_videos bv JOIN batches b ON b.id=bv.batch_id "
               "WHERE bv.video_id=? ORDER BY b.id", (vid,))
    auto = any(b["autopilot"] for b in bvs)
    plan_states = {b["state"] for b in bvs}
    jobs = db.q("SELECT id,status,progress,review,error FROM jobs WHERE video_id=? AND status!='canceled'", (vid,))
    posts = db.q("SELECT p.status, p.at FROM posts p JOIN jobs j ON j.id=p.job_id WHERE j.video_id=?", (vid,))
    out = [("Found", "done", "Imported " + time.strftime("%a %H:%M", time.localtime(v["added_at"])))]

    analyzing = v["status"] == "analyzing"
    if analyzing:
        out.append(("Analyzing", "active", "Finding scenes, faces, loud moments and speech"))
    elif v["analysis"] or jobs:
        out.append(("Analyzing", "done", "Scenes, sound and faces checked"))
    elif "failed" in plan_states:
        out.append(("Analyzing", "failed", next((b["error"] for b in bvs if b["error"]), "")))
    else:
        out.append(("Analyzing", "waiting", "In line"))

    if "failed" in plan_states and not jobs:
        out.append(("Picking best parts", "failed", next((b["error"] for b in bvs if b["error"]), "")[:200]))
    elif plan_states & {"pending", "planning"} and not jobs:
        out.append(("Picking best parts", "active" if "planning" in plan_states and not analyzing else "waiting",
                    "Scoring every moment"))
    elif jobs:
        out.append(("Picking best parts", "done", f"{len(jobs)} part(s) picked"))
    else:
        out.append(("Picking best parts", "done" if bvs else "waiting", v["error"] or "Nothing usable found"))

    done = sum(j["status"] == "done" for j in jobs)
    failed = sum(j["status"] == "failed" for j in jobs)
    running = [j for j in jobs if j["status"] == "running"]
    if not jobs:
        out.append(("Editing clips", "waiting", ""))
    elif running or any(j["status"] == "queued" for j in jobs):
        pct = (done + sum(j["progress"] for j in running)) / len(jobs)
        out.append(("Editing clips", "active", f"{done} of {len(jobs)} done · {pct:.0%}"))
    else:
        out.append(("Editing clips", "failed" if failed and not done else "done",
                    f"{done} of {len(jobs)} made" + (f", {failed} failed" if failed else "")))

    qc_bad = sum(j["review"] == "failed_qc" for j in jobs)
    if done or qc_bad:
        out.append(("Quality check", "done" if done else "failed",
                    f"{done} passed" + (f", {qc_bad} held back" if qc_bad else "")))
    else:
        out.append(("Quality check", "waiting", ""))

    if not auto and not posts:
        out.append(("Scheduled", "skipped", "Not an autopilot folder: post from Auto-Posting"))
        out.append(("Posted", "skipped", ""))
        return out
    sched = [p for p in posts if p["status"] in ("planned", "sending", "scheduled")]
    pfail = sum(p["status"] == "failed" for p in posts)
    nxt = min((p["at"] for p in sched if p["at"] > time.time()), default=None)
    if sched:
        sent = sum(p["status"] == "scheduled" for p in sched)
        det = f"{len(sched)} in calendar, {sent} handed to Ayrshare"
        if nxt:
            det += " · next " + time.strftime("%a %H:%M", time.localtime(nxt))
        out.append(("Scheduled", "done" if sent == len(sched) else "active", det + (f", {pfail} failed" if pfail
                                                                                     else "")))
    elif pfail:
        out.append(("Scheduled", "failed", f"{pfail} failed to send (see Auto-Posting)"))
    else:
        out.append(("Scheduled", "waiting", ""))
    live = [p for p in sched if p["status"] == "scheduled" and p["at"] <= time.time()]
    out.append(("Posted", "done" if sched and len(live) == len(sched) else "active" if live else "waiting",
                f"{len(live)} of {len(sched)} out" if sched else ""))
    return out


def clips(video_id):
    return db.q("SELECT * FROM jobs WHERE video_id=? AND status!='canceled' ORDER BY start", (video_id,))


def changes(job):
    """What was done to make this clip, as [(what, detail)] in plain words."""
    v = db.q1("SELECT * FROM videos WHERE id=?", (job["video_id"],)) or {}
    pr = json.loads(job["preset"] or "{}")
    a, b = job["start"], job["end"]
    out = [("Cut from", f"{_fmt(a)} to {_fmt(b)} of {v['name'] if v else 'the video'} "
                        f"({b - a:.0f} s out of {_fmt(v['duration'] if v else 0)})")]
    why = job["selection"] or ""
    if why.split(" ")[0] in ("score", "smart", "speech", "hybrid") and job["score"] is not None:
        why = ""                        # the score below already says it
    info = json.loads(job["score_info"]) if job["score_info"] else {}
    picked = {"highlight": "one of the most energetic moments", "scene change": "starts on a clean scene change",
              "manual": "you marked this part", "time": "spread evenly through the video",
              "fill": "filler to reach the clip count", "random": "random pick"}.get(why, why)
    if job["score"] is not None:
        picked += (" · " if picked else "") + f"score {job['score']:.0f}/100"
    if info.get("explanation"):
        picked += f" · {info['explanation']}"
    out.append(("Why this part", picked or "picked by the clip settings"))

    # Finished clips: what the export really applied (recorded by the exporter, after any automatic repair).
    # Clips not finished yet: what is planned, marked as such.
    ap = json.loads(job["applied"]) if "applied" in job.keys() and job["applied"] else None
    tag = "" if ap else " (planned)"
    if ap is None:
        faces = json.loads(v["analysis"]).get("faces") if v and v["analysis"] else None
        ap = {"aspect": pr.get("aspect", "9:16"), "fit": pr.get("fit", "blur"), "fps": pr.get("fps", "source"),
              "tracked": bool(pr.get("reframe_mode") == "auto" and faces), "size": None,
              "captions_burned": bool(pr.get("subtitles")), "caption_style": pr.get("caption_style"),
              "caption_files": bool(pr.get("sidecar_captions")), "caption_words": None,
              "loudness": bool(pr.get("normalize_audio", True)), "fade_in": float(pr.get("fade_in") or 0),
              "fade_out": float(pr.get("fade_out") or 0), "codec": pr.get("codec", "h264"),
              "quality_mode": pr.get("quality_mode"), "crf": pr.get("crf"), "bitrate_mbps": pr.get("bitrate_mbps"),
              "attempt": None}
    aspect, fit = ap.get("aspect") or "9:16", ap.get("fit") or "blur"
    if aspect == "original":
        frame = "kept the original shape"
    elif fit == "crop":
        frame = f"cropped to {aspect}, " + ("following the speaker's face" if ap.get("tracked") else "centered")
    elif fit == "fit":
        frame = f"fitted inside {aspect} with bars"
    else:
        frame = f"{aspect} with a blurred copy of the video behind it"
    src = f"{v['width']}×{v['height']}" if v and v["width"] else "source"
    dst = "×".join(map(str, ap["size"])) if ap.get("size") else pr.get("resolution", "1080p")
    out.append(("Framing" + tag, f"{frame} · {src} → {dst}"
                + (f" · {ap['fps']} fps" if ap.get("fps") not in (None, "source") else "")))
    if ap.get("captions_burned"):
        cap = f"burned in, word by word ({ap.get('caption_style') or 'classic'} style"
        cap += f", {ap['caption_words']} words)" if ap.get("caption_words") else ")"
        if ap.get("caption_files"):
            cap += " + caption files"
    elif ap.get("caption_files"):
        cap = "caption files (.srt/.vtt) next to the clip"
    elif pr.get("subtitles") and not tag:
        cap = "none: captions were planned but dropped (see Repairs)"
    else:
        cap = "none"
    out.append(("Captions" + tag, cap))
    out.append(("Sound" + tag, "loudness levelled to −14 LUFS (the level Reels/TikTok/Shorts play at)"
                if ap.get("loudness") else "untouched"))
    if ap.get("fade_in") or ap.get("fade_out"):
        out.append(("Fades" + tag, f"{ap.get('fade_in', 0):.1f} s in, {ap.get('fade_out', 0):.1f} s out"))
    enc = f"{(ap.get('codec') or 'h264').upper()} · " + (f"{ap.get('bitrate_mbps')} Mbps" if ap.get(
        "quality_mode") == "bitrate" else f"quality {ap.get('crf') or 18}")
    if ap.get("attempt") and ap["attempt"] != "as configured":
        enc += f" · made with fallback: {ap['attempt']}"
    out.append(("Encoding" + tag, enc))
    if job["repairs"]:
        out.append(("Repairs", " | ".join(json.loads(job["repairs"]))[:400]))
    if job["qc"]:
        q = json.loads(job["qc"])
        out.append(("Quality check", "passed" if q.get("ok") else "held back: " + "; ".join(q.get("issues") or [])))
    elif job["status"] in ("queued", "running"):
        out.append(("Status", "editing now" if job["status"] == "running" else "waiting in line"))
    elif job["error"]:
        out.append(("Status", job["error"][:300]))
    p = db.q1("SELECT * FROM posts WHERE job_id=?", (job["id"],))
    if p:
        plats = ", ".join(posting.PLATFORMS.get(x, x) for x in json.loads(p["platforms"]))
        state = {"planned": "planned", "sending": "sending", "scheduled": "scheduled", "failed": "failed"}.get(
            p["status"], p["status"])
        when = time.strftime("%a %d %b %H:%M", time.localtime(p["at"]))
        out.append(("Post", f"{state} for {when} on {plats}" + (f" · {p['error']}" if p["error"] else "")))
        if p["text"]:
            out.append(("Post text", p["text"][:300]))
    if job["out_path"] and Path(job["out_path"]).exists():
        out.append(("File", job["out_path"]))
    return out


def activity(limit=40):
    return db.q("SELECT ts, level, message FROM log ORDER BY id DESC LIMIT ?", (limit,))
