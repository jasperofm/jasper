#!/bin/bash
# Builds "Reels Cutter.app" into your Applications folder. Run once: bash mac/make_mac_app.sh
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/Applications"
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
TMP="$(mktemp -d)"
sed "s|__REPO_DIR__|$REPO|" "$REPO/mac/ReelsCutter.applescript" > "$TMP/app.applescript"
rm -rf "$DEST/Reels Cutter.app"
osacompile -o "$DEST/Reels Cutter.app" "$TMP/app.applescript"
rm -rf "$TMP"
echo "Installed: $DEST/Reels Cutter.app"
echo "Open it from Launchpad or Spotlight, or drag a video onto its icon."
open -R "$DEST/Reels Cutter.app"
