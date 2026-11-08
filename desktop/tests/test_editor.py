"""Timeline Editor end to end in the real window: the newest video opens by itself, its first frame shows before
pressing Play, IN/OUT can be marked, a segment saved, exported, and the clip file plays.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["REELSCUTTER_HOME"] = tempfile.mkdtemp()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["REELSCUTTER_NO_ONBOARDING"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from reelscutter import db, paths  # noqa: E402
from reelscutter.jobs import Importer, queue  # noqa: E402

work = Path(tempfile.mkdtemp())
src = work / "talk.mp4"
subprocess.run([paths.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=20", "-f", "lavfi",
                "-i", "sine=f=220:d=20", "-af", "volume=0.1", "-t", "20", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a",
                "aac", str(src)], check=True)
db.set_setting("output_dir", str(work / "out"))
db.set_setting("internal_reserve_gb", "0.5")
db.set_setting("min_free_gb", "0.5")
db.set_setting("pause_on_battery", "0")
Importer([src]).run()

app = QApplication([])
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)     # no blocking popups in the test
from reelscutter.ui.main_window import MainWindow  # noqa: E402

w = MainWindow()
w.show()
w.go("editor")
ed = w.p["editor"]
assert ed.vid, "editor didn't open the newest video"
assert ed.pick.count() == 2 and ed.pick.currentData() == ed.vid
assert ed.btn_in.isEnabled() and ed.btn_export.isEnabled() and ed.btn_add.isEnabled()


def wait(cond, secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


assert wait(lambda: ed.player.frames_shown > 0), "no picture before pressing Play"
assert wait(lambda: ed.player.player.playbackState() != QMediaPlayer.PlayingState), "still playing after first frame"
assert not ed.player.audio.isMuted(), "sound left muted"
print("first frame shown before Play; frames:", ed.player.frames_shown)

# a video added after the app started shows up in the editor list
src2 = work / "second.mp4"
subprocess.run(["cp", str(src), str(src2)], check=True)
db.run("INSERT INTO videos(path,name,duration,width,height,fps,added_at) VALUES(?,?,?,?,?,?,?)",
       (str(src2), "second.mp4", 20, 1280, 720, 30, time.time() + 1))
w.go("library")
w.go("editor")
assert ed.pick.findText("second.mp4") >= 0, "new video missing from the editor list"

# play, scrub, mark, save, export
ed.player.toggle()
assert wait(lambda: ed.player.pos() > 0.3), "play didn't advance"
ed.player.pause()
ed.player.seek(3.0)
wait(lambda: abs(ed.player.pos() - 3.0) < 0.2, 5)
ed.set_in()
ed.player.seek(11.0)
wait(lambda: abs(ed.player.pos() - 11.0) < 0.2, 5)
ed.set_out()
assert ed.timeline.in_pt is not None and ed.timeline.out_pt is not None, (ed.timeline.in_pt, ed.timeline.out_pt)
print("IN/OUT:", round(ed.timeline.in_pt, 2), round(ed.timeline.out_pt, 2))
ed.add_segment()
segs = db.q("SELECT * FROM segments WHERE video_id=?", (ed.vid,))
assert len(segs) == 1 and segs[0]["end"] - segs[0]["start"] > 5, [dict(s) for s in segs]
ed.export_segments()
queue.start()
assert wait(lambda: db.q1("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running')")["c"] == 0
            and db.q1("SELECT COUNT(*) c FROM jobs")["c"] > 0, 300)
j = db.q1("SELECT * FROM jobs")
assert j["status"] == "done", dict(j)
out = subprocess.run([paths.ffmpeg(), "-v", "error", "-i", j["out_path"], "-f", "null", "-"], capture_output=True,
                     text=True)
assert out.returncode == 0 and Path(j["out_path"]).stat().st_size > 10000, out.stderr
print("exported and decodes:", j["out_path"])
queue.shutdown()
print("editor test passed")
os._exit(0)
