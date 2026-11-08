"""End-to-end pipeline test (CI): import → analysis → smart selection → captions → export → quality check.

Needs espeak-ng (for a talking test video). Downloads the tiny speech model when --speech is given.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp()
os.environ["REELSCUTTER_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reelscutter import analysis, db, paths, production, selection, subtitles  # noqa: E402
from reelscutter.jobs import Importer, queue  # noqa: E402

work = Path(tempfile.mkdtemp())
text = ("Welcome to the show. Today we talk about saving money. The first secret is simple. Spend less than you "
        "earn. The second secret is patience. Small steps add up over time. Thanks for watching and see you soon.")
wav = work / "speech.wav"
subprocess.run(["espeak-ng", "-s", "140", "-w", str(wav), text], check=True)
src = work / "talk.mp4"
subprocess.run([paths.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40", "-i", str(wav),
                "-filter_complex", "[1:a]apad=whole_dur=40[a]", "-map", "0:v", "-map", "[a]", "-t", "40",
                "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
db.set_setting("output_dir", str(work / "out"))
db.set_setting("internal_reserve_gb", "0.5")
db.set_setting("min_free_gb", "0.5")
imp = Importer([work])
imp.run()
assert imp.stats["added"] == 1, imp.stats
vid = imp.added_ids[0]

mods = ["scenes_audio", "faces", "quality"]
if "--speech" in sys.argv:
    subtitles.download_model("tiny")
    db.set_setting("whisper_model", "tiny")
    mods.append("transcript")
analysis.analyze_video(vid, mods)
v = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
print("analysis level:", v["analysis_level"], "transcript words:", len(json.loads(v["transcript"] or "[]")))
if "--speech" in sys.argv:
    words = json.loads(v["transcript"])
    assert len(words) > 20, words
    hits = analysis.search_transcripts("secret")
    print("search 'secret':", [(h[1], round(h[2], 1)) for h in hits])
    assert hits

g = dict(selection=selection.SelectionSettings(mode="speech" if "--speech" in sys.argv else "smart",
                                               clips_per_video=2, durations=[12], min_len=6, max_len=20).to_dict(),
         preset="Reels 9:16 1080p", captions="both" if "--speech" in sys.argv else "none", project="CI")
production.start(g, [vid], "CI")
queue.start()
t0 = time.time()
while time.time() - t0 < 600:
    time.sleep(2)
    busy = db.q1("SELECT COUNT(*) c FROM batch_videos WHERE state IN ('pending','planning')")["c"] + \
        db.q1("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','running')")["c"]
    if not busy:
        break
rows = db.q("SELECT * FROM jobs")
for r in rows:
    print(r["id"], r["status"], r["review"], r["selection"], round(r["start"], 1), round(r["end"], 1),
          r["error"], r["repairs"])
assert rows and all(r["status"] == "done" for r in rows), "not all clips exported"
if "--speech" in sys.argv:
    srts = glob.glob(str(work / "out" / "**" / "*.srt"), recursive=True)
    print("srt files:", len(srts))
    print(Path(srts[0]).read_text()[:300])
    assert srts
print("PIPELINE OK")
queue.shutdown()
