# Reels cutter

Turns one long video you own into several **different** vertical (1080x1920) Reels:

- **Different moments.** It transcribes the video with Whisper, scores every stretch of speech (little dead air, a strong opening line, ends on a full sentence) and picks clips that don't overlap.
- **Different lengths.** Each Reel aims at its own length, spread between `--min` and `--max` (defaults are 15 to 90 seconds). With 5 Reels that's roughly 15, 34, 53, 71 and 90 seconds.
- **Its own hook.** The clip's opening line is shown as a bold title for the first 3 seconds. You can write your own hooks instead with `--hooks`.
- **Burned-in captions.** Word-by-word captions, 1 to 3 words at a time, with the spoken word highlighted in yellow.

## Setup (once)

1. ffmpeg comes with the Python packages below. If you already have ffmpeg installed (e.g. `brew install ffmpeg`), that one is used.
2. Install Python 3.9+ and then: `pip install faster-whisper`

## Mac app (easiest)

After the setup above, run this once inside the `jasper` folder:

```bash
bash mac/make_mac_app.sh
```

That puts **Reels Cutter** in your Applications folder. Open it, or drag a video onto its icon, then choose how many Reels you want and the layout. A Terminal window shows the progress, and the finished Reels' folder opens on its own when it's done.

## Run it from Terminal

```bash
python reels_cutter.py my_video.mp4                         # 5 Reels, 15-90s, saved in my_video_reels/
python reels_cutter.py my_video.mp4 --dry-run               # only show which moments it would pick
python reels_cutter.py my_video.mp4 --count 6 --min 20 --max 60
python reels_cutter.py my_video.mp4 --layout crop           # fill the frame (good if you're centered)
python reels_cutter.py my_video.mp4 --hooks hooks.txt       # your own hook per Reel, one per line
python reels_cutter.py my_video.mp4 --language nl --model medium
```

| Option | Default | What it does |
|---|---|---|
| `--count` | 5 | How many Reels |
| `--min` / `--max` | 15 / 90 | Shortest and longest Reel, in seconds |
| `--layout` | `blur` | `blur` shows the whole frame on a blurred background; `crop` center-crops to fill the screen |
| `--model` | `small` | Whisper model: `tiny`, `base`, `small`, `medium`, `large-v3` (bigger is more accurate, slower) |
| `--language` | auto | Force a language, e.g. `en`, `nl` |
| `--hooks` | none | Text file with your own hook lines, one per Reel in order |
| `--hook-seconds` | 3 | How long the hook title stays up |
| `--font` | Arial | Any font installed on your computer |
| `--out` | `<video>_reels` | Output folder |

## What you get

In the output folder: `reel_01_34s.mp4` and so on, plus `reels.json` listing each Reel's time range in the original video, its hook and its full transcript (handy for writing the post caption). The transcript is cached in `transcript.json`, so a re-run with different options skips transcription.

Tip: run `--dry-run` first, then put better hooks in a `hooks.txt` if the auto ones (the clip's first sentence) aren't punchy enough.
