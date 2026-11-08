"""Project health checks with one-click fixes."""
import os
from pathlib import Path

from . import backup, db, hardware, paths


def checks():
    """List of dicts: level ('ok'|'warn'|'error'), title, detail, fix (key or None), fix_label."""
    out = []
    from .jobs import drive_root, queue
    missing = db.q("SELECT path FROM videos WHERE status='missing'")
    roots = {drive_root(r["path"]) for r in missing} - {None}
    unplugged = [r for r in roots if not Path(r).exists()]
    if unplugged:
        out.append(dict(level="warn", title=f"{len(unplugged)} external drive(s) disconnected",
                        detail=", ".join(Path(r).name for r in unplugged) + ". Plug them in, then press Check again.",
                        fix="recheck", fix_label="Check again"))
    if missing:
        out.append(dict(level="error", title=f"{len(missing)} source video(s) missing",
                        detail="Moved, renamed or on a disconnected drive. Relink them in Storage Manager.",
                        fix="storage", fix_label="Open Storage Manager"))
    gone = [r["id"] for r in db.q("SELECT id,out_path FROM jobs WHERE status='done' AND out_path IS NOT NULL")
            if not Path(r["out_path"]).exists()]
    if gone:
        out.append(dict(level="warn", title=f"{len(gone)} exported clip file(s) no longer exist",
                        detail="They were moved or deleted outside Reels Cutter.", fix="forget_gone",
                        fix_label="Remove from list", data=gone))
    running = {str(r["out_path"]) + ".part.mp4" for r in db.q("SELECT out_path FROM jobs WHERE status='running'")}
    parts = []
    outdir = Path(db.setting("output_dir"))
    if outdir.exists():
        for root, dirs, files in os.walk(outdir):
            parts += [os.path.join(root, f) for f in files if f.endswith(".part.mp4")
                      and os.path.join(root, f) not in running]
    if parts:
        out.append(dict(level="warn", title=f"{len(parts)} incomplete export file(s)",
                        detail="Left over from an interrupted export. Safe to delete.", fix="clean_parts",
                        fix_label="Delete them", data=parts))
    failed = db.q1("SELECT COUNT(*) c FROM jobs WHERE status='failed' AND review!='failed_qc'")["c"]
    if failed:
        out.append(dict(level="error", title=f"{failed} failed export(s)", detail="See the error in Export Manager.",
                        fix="retry", fix_label="Retry all"))
    qcf = db.q1("SELECT COUNT(*) c FROM jobs WHERE review='failed_qc'")["c"]
    if qcf:
        out.append(dict(level="warn", title=f"{qcf} clip(s) failed the quality check",
                        detail="They are in the _Quarantine folder. Review them in Clip Review Studio.",
                        fix="review", fix_label="Open review"))
    plan_failed = db.q1("SELECT COUNT(*) c FROM batch_videos WHERE state='failed'")["c"]
    if plan_failed:
        out.append(dict(level="warn", title=f"{plan_failed} video(s) couldn't be planned",
                        detail="Analysis failed or the file was unreadable. Details are in the log.",
                        fix="replan", fix_label="Try again"))
    if queue.paused:
        out.append(dict(level="warn", title="Processing is paused", detail=queue.blocked_reason or "",
                        fix="resume", fix_label="Resume"))
    elif queue.blocked_reason:
        out.append(dict(level="warn", title="Processing is waiting", detail=queue.blocked_reason, fix=None))
    for key, label_ in (("output_dir", "export drive"), (str(paths.data_dir()), "internal drive")):
        p = db.setting(key) if key == "output_dir" else key
        free = hardware.free_gb(p)
        reserve = float(db.setting("min_free_gb" if key == "output_dir" else "internal_reserve_gb"))
        if free is not None and free < reserve * 1.5:
            out.append(dict(level="error" if free < reserve else "warn",
                            title=f"Low space on the {label_}: {free:.1f} GB free",
                            detail=f"New exports pause below {reserve:.0f} GB.", fix="storage",
                            fix_label="Open Storage Manager"))
    for w in db.q("SELECT path FROM watch_folders WHERE enabled=1"):
        if not Path(w["path"]).exists():
            out.append(dict(level="warn", title="Watch folder unavailable", detail=w["path"], fix=None))
    ok, msg = backup.integrity_ok()
    if not ok:
        out.append(dict(level="error", title="Database problem", detail=msg, fix="backup",
                        fix_label="Back up now"))
    if not out:
        out.append(dict(level="ok", title="Everything looks healthy", detail="", fix=None))
    return out


def fix(item):
    from .jobs import queue
    k = item.get("fix")
    if k == "forget_gone":
        db.many("DELETE FROM jobs WHERE id=?", [(i,) for i in item["data"]])
    elif k == "clean_parts":
        for p in item["data"]:
            Path(p).unlink(missing_ok=True)
    elif k == "retry":
        queue.retry([r["id"] for r in db.q("SELECT id FROM jobs WHERE status='failed' AND review!='failed_qc'")])
    elif k == "replan":
        db.run("UPDATE batch_videos SET state='pending', error=NULL WHERE state='failed'")
        queue.wake.set()
    elif k == "resume":
        queue.resume()
    elif k == "recheck":
        for r in db.q("SELECT id,path FROM videos WHERE status='missing'"):
            if Path(r["path"]).exists():
                db.run("UPDATE videos SET status='new' WHERE id=?", (r["id"],))
        queue.wake.set()
    elif k == "backup":
        backup.backup("manual")
