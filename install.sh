#!/bin/bash
# One-step installer for Reels Cutter on a Mac. Safe to run again to update.
set -e
DIR="$HOME/jasper"
echo "==> Getting the latest Reels Cutter into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone https://github.com/jasperofm/jasper.git "$DIR"
fi
cd "$DIR"
echo "==> Installing Python packages (Whisper + ffmpeg). This can take a few minutes."
python3 -m pip install --user --upgrade -r requirements.txt
echo "==> Building the Reels Cutter app"
bash mac/make_mac_app.sh
echo
echo "All set! Open 'Reels Cutter' from Launchpad or Spotlight, or drag a video onto its icon."
