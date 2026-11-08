# Reels Cutter for Mac (desktop app)

A native macOS app that turns long videos you own into finished short clips, automatically and locally.
Import videos, set your production preferences once, press **Start generating**, and Reels Cutter runs the
whole pipeline: analysis → moment selection → framing → captions → export → quality check → automatic repair.

## Install (no Terminal needed)

1. Open the latest release: **https://github.com/jasperofm/jasper/releases/tag/desktop-latest**
2. Download the `.dmg` for your Mac:
   - `…-x86_64.dmg` for **Intel** Macs
   - `…-arm64.dmg` for **Apple Silicon** (M1/M2/M3/M4)
3. Open the .dmg and drag **Reels Cutter** onto **Applications**.
4. The first time, macOS blocks apps from unidentified developers (this app isn't notarized by Apple):
   - **macOS 14 or older:** right-click Reels Cutter in Applications → **Open** → **Open**.
   - **macOS 15 or newer:** double-click it once (it's blocked), then go to **System Settings → Privacy & Security**,
     scroll down and click **Open Anyway**.

Requires macOS 12 or newer. The welcome guide detects your Mac's processor, memory, graphics and hardware
encoders on first launch and starts in **Safe mode**.

## What's in the app

| Page | What it does |
|---|---|
| Dashboard | Live numbers from the database: videos, analyzed, clips, approved, running, pending, failed, free space, recent projects and activity, measured performance |
| Video Library | Import files/folders (or drag & drop), recursive scanning, duplicate detection by fingerprint, thumbnails, metadata, search, filters (length, resolution, format, status, project). Tested with 10,000 records |
| AI Analysis | Fast / Standard / Advanced / Custom: scenes, black frames, silence, loudness, transcript (local Whisper), face positions, picture quality. Results cached. Time estimates from your Mac's benchmark. Speech models are only downloaded when you click Install. Transcript search |
| Clip Generator | 8 strategies (Smart scored, Speech-based, Hybrid, Highlights, Scene, Random, Evenly spread, Manual), clips per video, 15 s – 5 min presets + custom, min/max, spacing, skip intro/outro, pre/post-roll, minimum score, keywords and exclusions, smart reframing, captions, project. Save as production profiles |
| Timeline Editor | Player (play/pause, frame step, speed, volume, fullscreen, go-to time), zoomable timeline with loudness and scene markers, draggable IN/OUT, saved segments, preview, export selections |
| Clip Review Studio | Gallery with Ready / Awaiting Review / Approved / Needs Editing / Rejected / Failed QC. Approve, reject, rename, tag, compare with the original, re-edit, regenerate, copy to a folder, CSV/HTML reports. Review is optional |
| Export Manager | Queue with progress, ETA, pause/resume, cancel, retry, parallel exports; export presets (9:16, 16:9, 1:1, 4:5, original; 720p–4K/source; 24–60 fps/source; H.264/H.265; quality or bitrate; audio bitrate; framing; fades; loudness) |
| Projects | Group videos and clips, counts and health per project |
| Storage Manager | Drives (internal/external), export and temp locations, cache limits, cleanup, missing-file detection and relinking |
| System Monitor | Hardware, live CPU/memory, Safe/Balanced/Performance profiles, real benchmark and capability report, log |
| Settings | Naming templates, automatic retries and repairs, quality-check thresholds, schedule, battery, learning, optional cloud AI |

### Reliability
- Every job is stored in SQLite; quitting or a crash never loses finished clips. Interrupted exports restart on the next launch and half-written files are removed.
- Exports write to a temporary `.part` file and are only renamed after they pass the quality check (opens and decodes, codec, resolution, duration, audio present and in sync, black/frozen/silent percentages).
- Failed exports are retried with safer settings (software encoder, then simple H.264). Clips that still fail content checks go to `_Quarantine`.
- New exports pause automatically when disk space drops below your reserves (default 20 GB internal), when memory is critically low, on battery (optional), outside your schedule (optional), or when an external drive is unplugged; they resume by themselves.
- The database is checked at every launch and backed up daily (7 kept); a damaged database is restored from the newest good backup.
- Original videos are never modified or deleted.

### Honest limits (not built yet)
Speaker diarization, person (body) detection, a multi-track timeline with keyframes/transitions, a word-level
caption text editor, watch folders and a project health page are not in this version. Face tracking uses
OpenCV's classic face detector, which works best on clear, front-facing faces.
Scores rank moments in your own footage; they don't predict views. Choosing different sections or
re-encoding does not make a clip "new" to any platform's duplicate detection, and the app doesn't try to.

## Run from source / build
```
cd desktop
pip install -r requirements-app.txt faster-whisper
python run_app.py                 # start the app
python -m reelscutter --selftest  # headless check: export + quality check + every page opens
bash packaging/build_mac.sh       # on a Mac: builds dist/Reels Cutter.app and a .dmg
```
Builds for Intel and Apple Silicon are made automatically by GitHub Actions (`.github/workflows/mac-app.yml`).
