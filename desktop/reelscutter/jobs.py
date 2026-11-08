"""Background work: importing, planning clips, and the export queue.

Everything is persisted in SQLite, so the queue survives quitting or a crash:
on the next launch, interrupted jobs go back to "queued" and half-written files are removed.
"""
import json
import os
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import db, export, hardware, media, paths, qc, scoring, selection, subtitles


def drive_root(path):
    """'/Volumes/MySSD' for paths on an external drive (macOS), else None."""
    parts = Path(str(path)).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(Path(*parts[:3]))
    return None


def clip_words(v, start, end):
    """Words for a clip: reuse the cached full-video transcript when there is one, else transcribe the range."""
    if v["transcript"]:
        allw = json.loads(v["transcript"])
        return [tuple(w) for w in allw if w[1] > start and w[0] < end]
    return subtitles.transcribe_file(v["path"], start, end)


def in_schedule(now=None):
    """True inside the user's processing window (start hour to end hour, may wrap past midnight)."""
    h = time.localtime(now).tm_hour
    a, b = int(db.setting("schedule_start", "22")), int(db.setting("schedule_end", "7"))
    return a <= h < b if a < b else (h >= a or h < b)


class Events:
    """Tiny pub/sub; the UI subscribes and re-emits on its own thread."""

    def __init__(self):
        self._subs = []

    def subscribe(self, fn):
        self._subs.append(fn)

    def emit(self, kind, **data):
        for fn in list(self._subs):
            try:
                fn(kind, data)
            except Exception:
                pass


events = Events()


# ---------------------------------------------------------------- import
class Importer(threading.Thread):
    """Adds files/folders to the library: fingerprint (duplicates), probe, thumbnail. One file at a time."""

    def __init__(self, sources, project="Default"):
        super().__init__(daemon=True)
        self.sources = [Path(s) for s in sources]
        self.project_id = db.project_id(project)
        self.stop = False
        self.stats = dict(found=0, added=0, duplicates=0, skipped=0, failed=0)
        self.added_ids = []

    def _files(self):
        for s in self.sources:
            if s.is_dir():
                for root, dirs, files in os.walk(s):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for f in sorted(files):
                        if media.is_video(f):
                            yield Path(root) / f
            elif s.is_file() and media.is_video(s):
                yield s

    def run(self):
        self.t0 = time.time()
        files = list(self._files())
        self.stats["found"] = len(files)
        events.emit("import_started", total=len(files))
        for i, f in enumerate(files, 1):
            if self.stop:
                break
            try:
                self._one(f)
            except Exception as e:
                self.stats["failed"] += 1
                db.log("error", f"Import failed for {f}: {e}")
            events.emit("import_progress", done=i, total=len(files), **self.stats)
        try:
            db.run("INSERT INTO imports(ts,sources,found,added,duplicates,skipped,failed,seconds) VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), json.dumps([str(s) for s in self.sources]), self.stats["found"], self.stats["added"],
                    self.stats["duplicates"], self.stats["skipped"], self.stats["failed"], time.time() - self.t0))
        except Exception:
            pass
        events.emit("import_finished", **self.stats)

    def _one(self, f):
        path = str(f.resolve())
        if db.q1("SELECT id FROM videos WHERE path=?", (path,)):
            self.stats["skipped"] += 1
            return
        fp = media.fingerprint(f)
        dup = db.q1("SELECT name FROM videos WHERE fingerprint=?", (fp,))
        if dup:
            self.stats["duplicates"] += 1
            db.log("info", f"Skipped {f.name}: same file as {dup['name']} already in library")
            return
        info = media.probe(f)
        if not info["duration"] or info["duration"] < 1:
            raise ValueError("video is empty or unreadable")
        st = f.stat()
        vid = db.run(
            "INSERT INTO videos(path,name,size,mtime,fingerprint,duration,width,height,fps,vcodec,acodec,fmt,"
            "status,project_id,added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
            (path, f.name, st.st_size, st.st_mtime, fp, info["duration"], info["width"], info["height"],
             info["fps"], info["vcodec"], info["acodec"], info["fmt"], self.project_id, time.time()))
        try:
            thumb = media.make_thumbnail(f, info["duration"], vid)
            db.run("UPDATE videos SET thumb=? WHERE id=?", (thumb, vid))
        except Exception as e:
            db.log("warning", f"No thumbnail for {f.name}: {e}")
        self.stats["added"] += 1
        self.added_ids.append(vid)


# ---------------------------------------------------------------- queue
NEEDS_ANALYSIS = {"hybrid", "highlight", "scene", "smart", "speech"}


class Queue:
    """Plans clips for batches and runs exports on a pool of worker threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.paused = db.setting("queue_paused", "0") == "1"
        self.running = {}            # job_id -> Encode
        self.threads = []
        self.quit = False
        self.planner = None
        self.speed = []              # recent (clip seconds, wall seconds)
        self.blocked_reason = None

    # ---- lifecycle
    def start(self):
        self.recover()
        n = self.concurrency()
        for i in range(n):
            self._spawn()
        self.planner = threading.Thread(target=self._plan_loop, daemon=True, name="planner")
        self.planner.start()

    def _spawn(self):
        idx = len(self.threads)
        t = threading.Thread(target=self._work_loop, args=(idx,), daemon=True, name=f"worker{idx}")
        self.threads.append(t)
        t.start()

    def concurrency(self):
        try:
            return max(1, min(16, int(db.setting("concurrency", "2"))))
        except ValueError:
            return 2

    def set_concurrency(self, n):
        db.set_setting("concurrency", n)
        while len(self.threads) < n:
            self._spawn()
        self.wake.set()

    def shutdown(self):
        self.quit = True
        self.wake.set()
        with self.lock:
            encs = list(self.running.items())
        for jid, enc in encs:
            enc.cancel()
            db.run("UPDATE jobs SET status='queued', progress=0 WHERE id=? AND status='running'", (jid,))

    @staticmethod
    def recover():
        """After a crash or quit: requeue interrupted jobs and delete partial outputs."""
        for r in db.q("SELECT id,out_path FROM jobs WHERE status='running'"):
            if r["out_path"]:
                Path(r["out_path"] + ".part.mp4").unlink(missing_ok=True)
        db.run("UPDATE jobs SET status='queued', progress=0 WHERE status='running'")
        db.run("UPDATE batch_videos SET state='pending' WHERE state='planning'")
        db.run("UPDATE videos SET status='queued' WHERE status IN ('processing','analyzing')")
        for tmp in {paths.data_dir() / "tmp", hardware.temp_dir()}:   # only runs before any worker starts
            for f in tmp.glob("*"):
                try:
                    shutil.rmtree(f) if f.is_dir() else f.unlink()
                except OSError:
                    pass

    # ---- user actions
    def pause(self):
        self.paused = True
        db.set_setting("queue_paused", "1")
        with self.lock:
            for enc in self.running.values():
                enc.pause()
        events.emit("queue_changed")

    def resume(self):
        self.paused = False
        self.blocked_reason = None
        db.set_setting("queue_paused", "0")
        with self.lock:
            for enc in self.running.values():
                enc.resume()
        self.wake.set()
        events.emit("queue_changed")

    def cancel(self, job_ids):
        for jid in job_ids:
            with self.lock:
                enc = self.running.get(jid)
            db.run("UPDATE jobs SET status='canceled', finished_at=? WHERE id=? AND status IN ('queued','running')",
                   (time.time(), jid))
            if enc:
                enc.cancel()
        events.emit("queue_changed")

    def retry(self, job_ids):
        for jid in job_ids:
            db.run("UPDATE jobs SET status='queued', progress=0, error=NULL, attempts=0 "
                   "WHERE id=? AND status IN ('failed','canceled')", (jid,))
        self.wake.set()
        events.emit("queue_changed")

    def retry_all_failed(self):
        ids = [r["id"] for r in db.q("SELECT id FROM jobs WHERE status='failed' AND review!='failed_qc'")]
        self.retry(ids)
        return len(ids)

    def clear_finished(self):
        db.run("UPDATE jobs SET hidden=1 WHERE status='done'")
        db.run("DELETE FROM jobs WHERE status='canceled'")
        events.emit("queue_changed")

    def create_batch(self, video_ids, sel: selection.SelectionSettings, preset_name, preset, project="Default",
                     name=None):
        pid = db.project_id(project)
        settings = {"selection": sel.to_dict(), "preset_name": preset_name, "preset": preset}
        bid = db.run("INSERT INTO batches(name,project_id,settings,created_at) VALUES(?,?,?,?)",
                     (name or f"Batch {time.strftime('%Y-%m-%d %H:%M')}", pid, json.dumps(settings), time.time()))
        db.many("INSERT OR IGNORE INTO batch_videos(batch_id,video_id) VALUES(?,?)", [(bid, v) for v in video_ids])
        db.many("UPDATE videos SET status='queued' WHERE id=?", [(v,) for v in video_ids])
        self.wake.set()
        events.emit("queue_changed")
        return bid

    def add_manual_jobs(self, video_id, ranges, preset_name, preset, project="Default"):
        """Export exact ranges chosen on the timeline (no planning needed)."""
        sel = selection.SelectionSettings(mode="manual", clips_per_video=len(ranges), avoid_used=False)
        bid = db.run("INSERT INTO batches(name,project_id,settings,created_at) VALUES(?,?,?,?)",
                     ("Timeline export", db.project_id(project),
                      json.dumps({"selection": sel.to_dict(), "preset_name": preset_name, "preset": preset}),
                      time.time()))
        db.run("INSERT INTO batch_videos(batch_id,video_id,state) VALUES(?,?,'planned')", (bid, video_id))
        self._make_jobs(bid, video_id, [(a, b, "manual") for a, b in ranges], preset_name, preset,
                        db.project_name(db.project_id(project)))
        self.wake.set()
        events.emit("queue_changed")
        return bid

    # ---- planning (analysis + selection), one video at a time
    def _plan_loop(self):
        while not self.quit:
            r = db.q1("SELECT bv.batch_id, bv.video_id FROM batch_videos bv WHERE bv.state='pending' "
                      "ORDER BY bv.batch_id, bv.video_id LIMIT 1")
            if r is None or self.paused:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            bid, vid = r["batch_id"], r["video_id"]
            db.run("UPDATE batch_videos SET state='planning' WHERE batch_id=? AND video_id=?", (bid, vid))
            try:
                self._plan(bid, vid)
                db.run("UPDATE batch_videos SET state='planned' WHERE batch_id=? AND video_id=?", (bid, vid))
            except Exception as e:
                db.run("UPDATE batch_videos SET state='failed', error=? WHERE batch_id=? AND video_id=?",
                       (str(e), bid, vid))
                db.run("UPDATE videos SET status='failed', error=? WHERE id=?", (f"Planning failed: {e}", vid))
                db.log("error", f"Planning failed for video {vid}: {e}\n{traceback.format_exc()}")
            self.wake.set()
            events.emit("queue_changed")

    def _plan(self, bid, vid):
        b = db.q1("SELECT * FROM batches WHERE id=?", (bid,))
        v = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
        if not v:
            raise ValueError("video was removed from the library")
        if not Path(v["path"]).exists():
            db.run("UPDATE videos SET status='missing' WHERE id=?", (vid,))
            raise FileNotFoundError(f"source file is missing: {v['path']}")
        st = json.loads(b["settings"])
        sel = selection.SelectionSettings.from_dict(st["selection"])
        analysis = json.loads(v["analysis"]) if v["analysis"] else None
        needs = sel.mode in NEEDS_ANALYSIS or sel.avoid_silence or sel.avoid_black or sel.speech_only \
            or sel.snap_to_pauses
        if needs and analysis is None:
            db.run("UPDATE videos SET status='analyzing' WHERE id=?", (vid,))
            events.emit("queue_changed")
            analysis = media.analyze(v["path"], has_audio=bool(v["acodec"]), cancel=lambda: self.quit)
            db.run("UPDATE videos SET analysis=? WHERE id=?", (media.dumps(analysis), vid))
        pr = st["preset"]
        if pr.get("reframe_mode") == "auto" and pr.get("fit") == "crop" and export.ASPECTS.get(pr.get("aspect")) \
                and (analysis or {}).get("faces") is None:
            from . import analysis as an
            db.run("UPDATE videos SET status='analyzing' WHERE id=?", (vid,))
            events.emit("queue_changed")
            try:
                extra = an._frames_pass(v["path"], v["duration"], True, False, cancel=lambda: self.quit)
                analysis = dict(analysis or {}, **extra)
                db.run("UPDATE videos SET analysis=? WHERE id=?", (media.dumps(analysis), vid))
            except InterruptedError:
                raise
            except Exception as e:
                db.log("warning", f"{v['name']}: face tracking unavailable, using fixed framing ({e})")
        used = [(r["start"], r["end"]) for r in db.q(
            "SELECT start,end FROM jobs WHERE video_id=? AND status IN ('queued','running','done','paused')", (vid,))]
        manual = [(r["start"], r["end"]) for r in db.q(
            "SELECT start,end FROM segments WHERE video_id=? AND kind='keep' ORDER BY start", (vid,))]
        if sel.seed is None:
            sel.seed = int(time.time() * 1000) ^ vid
        transcript = [tuple(w) for w in json.loads(v["transcript"])] if v["transcript"] else None
        clips = selection.select(v["duration"], analysis, sel, used=used, manual=manual, transcript=transcript)
        if not clips:
            db.run("UPDATE videos SET status='done', error=? WHERE id=?",
                   ("No usable new sections left for these settings", vid))
            db.log("warning", f"{v['name']}: no usable sections found for these settings")
            return
        self._make_jobs(bid, vid, clips, st["preset_name"], st["preset"], db.project_name(b["project_id"]))
        db.run("UPDATE videos SET status='queued', error=NULL WHERE id=?", (vid,))

    def _make_jobs(self, bid, vid, clips, preset_name, preset, project):
        v = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
        out_root = db.setting("output_dir")
        n0 = db.q1("SELECT COUNT(*) c FROM jobs WHERE video_id=?", (vid,))["c"]
        rows, taken = [], set()
        for k, (a, b, why) in enumerate(clips, 1):
            n = n0 + k
            while True:
                p = export.output_path(out_root, db.setting("folder_template"), db.setting("name_template"),
                                       project=project, video_name=v["name"], n=n, start=a, end=b,
                                       preset_name=preset_name)
                if str(p) not in taken and not db.q1("SELECT 1 FROM jobs WHERE out_path=?", (str(p),)):
                    break
                n += 1000
            taken.add(str(p))
            sc, info = None, None
            try:
                an = json.loads(v["analysis"]) if v["analysis"] else None
                tr = [tuple(w) for w in json.loads(v["transcript"])] if v["transcript"] else None
                if an:
                    sc, expl, feats = scoring.score(an, a, b, tr)
                    info = json.dumps({"explanation": expl, "features": feats})
            except Exception:
                pass
            rows.append((bid, vid, a, b, why, json.dumps({"name": preset_name, **preset}), str(p), time.time(), sc,
                         info))
        db.many("INSERT INTO jobs(batch_id,video_id,start,end,selection,preset,out_path,status,created_at,score,"
                "score_info) VALUES(?,?,?,?,?,?,?,'queued',?,?,?)", rows)

    # ---- exporting
    def _claim(self):
        c = db.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if r:
                c.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1, progress=0 "
                          "WHERE id=?", (time.time(), r["id"]))
            c.execute("COMMIT")
            return r
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _disk_ok(self, folder):
        free = hardware.free_gb(folder)
        if free is None:
            return True, None
        return free >= float(db.setting("min_free_gb", "5")), free

    def _gate(self):
        """Reasons not to start a new job right now (None = go). Running jobs are never killed by this."""
        out = db.setting("output_dir")
        root = drive_root(out)
        if root and not Path(root).exists():
            return f"Waiting for drive “{Path(root).name}” (export folder) to be reconnected"
        ok, free = self._disk_ok(out)
        if not ok:
            return f"Paused new exports: only {free:.1f} GB free on the export drive " \
                   f"(reserve {db.setting('min_free_gb')} GB)"
        reserve = float(db.setting("internal_reserve_gb", "20"))
        ifree = hardware.free_gb(paths.data_dir())
        if ifree is not None and ifree < reserve:
            return f"Paused new exports: internal drive has {ifree:.1f} GB free (reserve {reserve:.0f} GB)"
        if hardware.memory_pressure() == "critical":
            return "Waiting: the Mac is low on memory"
        if db.setting("pause_on_battery", "1") == "1" and hardware.on_battery():
            return "Waiting: running on battery (plug in, or turn this off in Settings)"
        if db.setting("schedule_enabled", "0") == "1" and not in_schedule():
            return f"Waiting for the processing schedule ({db.setting('schedule_start')}:00–" \
                   f"{db.setting('schedule_end')}:00)"
        return None

    def _work_loop(self, my_index):
        while not self.quit:
            if self.paused or my_index >= self.concurrency():
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            reason = self._gate()
            if reason:
                if reason != self.blocked_reason:
                    db.log("warning", reason)
                    events.emit("queue_changed")
                self.blocked_reason = reason
                self.wake.wait(5.0)
                self.wake.clear()
                continue
            if self.blocked_reason:
                self.blocked_reason = None
                events.emit("queue_changed")
            # under memory pressure only one worker keeps taking jobs
            if my_index > 0 and hardware.memory_pressure() == "warn":
                self.wake.wait(3.0)
                self.wake.clear()
                continue
            job = self._claim()
            if job is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            self._run_job(job)

    def _attempt_plans(self, preset):
        """Export configurations to try in order: as configured, then safer fallbacks."""
        plans = [("as configured", dict(preset), db.setting("hw_accel", "auto"), True)]
        max_repairs = int(db.setting("max_repairs", "2") or 0)
        if max_repairs >= 1:
            plans.append(("software encoder", dict(preset), "off", True))
        if max_repairs >= 2:
            safe = dict(preset, codec="h264", quality_mode="crf", fps=preset.get("fps", "source"))
            if safe.get("fit") == "blur":
                safe["fit"] = "fit"
            plans.append(("safe H.264, simple framing, no captions", safe, "off", False))
        return plans

    def _run_job(self, job):
        jid = job["id"]
        v = db.q1("SELECT * FROM videos WHERE id=?", (job["video_id"],))
        preset = json.loads(job["preset"])
        out = Path(job["out_path"])
        part = Path(str(out) + ".part.mp4")
        t0 = time.time()
        last = [0.0]
        repairs = []

        def progress(p):
            now = time.time()
            if now - last[0] > 0.5 or p >= 1:
                last[0] = now
                db.run("UPDATE jobs SET progress=? WHERE id=?", (round(p, 4), jid))
                events.emit("job_progress", id=jid, progress=p)

        subs = None
        words = None
        try:
            if v is None:
                raise FileNotFoundError("video was removed from the library")
            if not Path(v["path"]).exists():
                root = drive_root(v["path"])
                if root and not Path(root).exists():      # drive unplugged: wait, don't fail
                    db.run("UPDATE jobs SET status='queued', progress=0, attempts=attempts-1 WHERE id=?", (jid,))
                    db.run("UPDATE videos SET status='missing' WHERE id=?", (v["id"],))
                    self.blocked_reason = f"Waiting for drive “{Path(root).name}” (source videos)"
                    time.sleep(3)
                    return
                db.run("UPDATE videos SET status='missing' WHERE id=?", (v["id"],))
                raise FileNotFoundError("source video is missing (moved or deleted). Relink it in Storage.")
            st = os.stat(v["path"])
            if v["size"] and st.st_size != v["size"]:
                db.log("warning", f"{v['name']} changed on disk since import (size differs); using the new file",
                       jid)
            db.run("UPDATE videos SET status='processing' WHERE id=?", (v["id"],))
            out.parent.mkdir(parents=True, exist_ok=True)
            info = dict(width=v["width"], height=v["height"], acodec=v["acodec"])
            threads = int(db.setting("threads_per_job", "0") or 0)
            if db.setting("low_memory", "0") == "1":
                threads = threads or 2
            length = job["end"] - job["start"]
            result = None
            for k, (why, pr, hw, allow_subs) in enumerate(self._attempt_plans(preset)):
                if k and self.quit:
                    raise InterruptedError
                if allow_subs and (pr.get("subtitles") or pr.get("sidecar_captions")) and words is None \
                        and subtitles.available():
                    try:
                        words = clip_words(v, job["start"], job["end"])
                    except Exception as e:
                        repairs.append(f"captions skipped: {e}")
                        words = []
                if allow_subs and pr.get("subtitles") and subs is None and words:
                    size = export.target_size(pr, v["width"], v["height"])
                    subs = subtitles.make_ass(v["path"], job["start"], job["end"],
                                              paths.data_dir() / "subs" / f"job{jid}.ass", size,
                                              pr.get("caption_style", "classic"), words=words,
                                              font=pr.get("caption_font", "Arial"),
                                              position=pr.get("caption_position", "bottom"),
                                              highlight=pr.get("caption_highlight", True))
                track = None
                if pr.get("reframe_mode") == "auto" and pr.get("fit") == "crop" and k < 2 and v["analysis"]:
                    track = export.track_points(json.loads(v["analysis"]).get("faces"), job["start"], job["end"],
                                                float(pr.get("track_sensitivity", 0.5)))
                cmd, size = export.build_command(v["path"], job["start"], job["end"], pr, part, info,
                                                 threads=threads, hw=hw, subs_path=subs if allow_subs else None,
                                                 track=track)
                enc = export.Encode(cmd, length, progress)
                with self.lock:
                    self.running[jid] = enc
                if self.paused:
                    threading.Timer(0.3, enc.pause).start()
                try:
                    enc.run()
                    if not part.exists() or part.stat().st_size < 1024:
                        raise RuntimeError("export produced an empty file")
                except InterruptedError:
                    raise
                except Exception as e:
                    repairs.append(f"attempt {k + 1} ({why}) failed: {str(e)[:300]}")
                    part.unlink(missing_ok=True)
                    continue
                if db.setting("qc_enabled", "1") != "1":
                    result = dict(ok=True, fatal=False, issues=[], warnings=["quality checks are turned off"],
                                  info={})
                    break
                result = qc.check(part, length, size, bool(v["acodec"]))
                if result["ok"] or not result["fatal"]:
                    break
                repairs.append(f"attempt {k + 1} ({why}) failed quality check: {'; '.join(result['issues'])}")
                part.unlink(missing_ok=True)
                result = None
            if result is None:
                raise RuntimeError(repairs[-1] if repairs else "export failed")
            final = out
            if not result["ok"]:                       # content problem: isolate, keep for inspection
                q = Path(db.setting("output_dir")) / "_Quarantine"
                q.mkdir(parents=True, exist_ok=True)
                final = q / out.name
                k = 2
                while final.exists():
                    final = q / f"{out.stem} ({k}){out.suffix}"
                    k += 1
            os.replace(part, final)
            if words and preset.get("sidecar_captions"):
                subtitles.write_srt(words, job["start"], final.with_suffix(".srt"))
                subtitles.write_vtt(words, job["start"], final.with_suffix(".vtt"))
            size_b = final.stat().st_size
            thumb = None
            try:
                thumb = media.extract_frame(final, min(length / 2, 2.0), paths.data_dir() / "thumbs" /
                                            f"clip{jid}.jpg", width=360)
            except Exception:
                pass
            review = ("failed_qc" if not result["ok"] else
                      "awaiting" if db.setting("review_required", "0") == "1" else "ready")
            db.run("UPDATE jobs SET status=?, progress=1, finished_at=?, out_size=?, out_path=?, error=?, qc=?, "
                   "repairs=?, review=?, thumb=? WHERE id=?",
                   ("done" if result["ok"] else "failed", time.time(), size_b, str(final),
                    None if result["ok"] else "Failed quality check: " + "; ".join(result["issues"]),
                    json.dumps(result), json.dumps(repairs) if repairs else None, review, thumb and str(thumb),
                    jid))
            if repairs:
                db.log("info", f"Job {jid} needed repairs: " + " | ".join(repairs), jid)
            self.speed.append((length, time.time() - t0))
            self.speed = self.speed[-30:]
            events.emit("job_done" if result["ok"] else "job_failed", id=jid)
        except InterruptedError:
            part.unlink(missing_ok=True)
            st = db.q1("SELECT status FROM jobs WHERE id=?", (jid,))
            if st and st["status"] == "running":     # app quitting: leave it for next launch
                db.run("UPDATE jobs SET status='queued', progress=0 WHERE id=?", (jid,))
        except Exception as e:
            part.unlink(missing_ok=True)
            msg = str(e) or e.__class__.__name__
            attempts = db.q1("SELECT attempts FROM jobs WHERE id=?", (jid,))["attempts"]
            limit = int(db.setting("auto_retry_limit", "1") or 0) + 1
            if db.setting("auto_retry", "1") == "1" and attempts < limit and not isinstance(e, FileNotFoundError):
                db.run("UPDATE jobs SET status='queued', progress=0, error=?, repairs=? WHERE id=?",
                       (f"Retrying: {msg}", json.dumps(repairs), jid))
            else:
                db.run("UPDATE jobs SET status='failed', finished_at=?, error=?, repairs=? WHERE id=?",
                       (time.time(), msg, json.dumps(repairs) if repairs else None, jid))
            db.log("error", f"Job {jid} failed: {msg}", jid)
            events.emit("job_failed", id=jid, error=msg)
        finally:
            if subs:
                Path(subs).unlink(missing_ok=True)
            with self.lock:
                self.running.pop(jid, None)
            if v:
                self._update_video_status(v["id"])
            events.emit("queue_changed")

    @staticmethod
    def _update_video_status(vid):
        r = db.q1("SELECT SUM(status IN ('queued','running')) open, SUM(status='failed') failed, "
                  "SUM(status='done') done FROM jobs WHERE video_id=?", (vid,))
        pending = db.q1("SELECT COUNT(*) c FROM batch_videos WHERE video_id=? AND state IN ('pending','planning')",
                        (vid,))["c"]
        if r["open"] or pending:
            status = "queued"
        elif r["failed"]:
            status = "failed"
        elif r["done"]:
            status = "done"
        else:
            status = "new"
        db.run("UPDATE videos SET status=? WHERE id=? AND status!='missing'", (status, vid))

    # ---- stats
    def eta_seconds(self):
        rem = db.q1("SELECT SUM((end-start)*(1-progress)) s FROM jobs WHERE status IN ('queued','running')")["s"]
        if not rem:
            return 0
        if self.speed:
            clip = sum(a for a, _ in self.speed)
            wall = sum(b for _, b in self.speed)
            rate = clip / wall if wall > 0 else 1.0
        else:
            rate = 1.0
        return rem / max(0.05, rate * self.concurrency())


queue = Queue()
