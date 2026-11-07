#!/usr/bin/env bash
# Builds a sample video with spoken audio (espeak-ng) and runs the cutter end to end.
set -euo pipefail
cd "$(dirname "$0")/.."
TXT="Here's the biggest mistake people make with their morning routine. Most of us grab the phone right away. That kills your focus for hours. So what should you do instead? Try ten minutes of daylight first. It sounds simple, but it works. Why does nobody talk about this? Because it is not exciting. In our test, three out of four people slept better."
espeak-ng -s 150 -w speech1.wav "$TXT"
ffmpeg -y -loglevel error -i speech1.wav -i speech1.wav -i speech1.wav -filter_complex concat=n=3:v=0:a=1 speech.wav
D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 speech.wav)
ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=$D" -i speech.wav \
  -shortest -c:v libx264 -preset ultrafast -c:a aac sample.mp4
python reels_cutter.py sample.mp4 --count 2 --min 10 --max 40 --model tiny --language en --library sample_lib --look punchy
ls sample_lib/sample/*.mp4
for f in sample_lib/sample/*.mp4; do
  test "$(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0 "$f")" = "1080,1920"
done
test -f sample_lib/gallery.html
echo "smoke test passed"
