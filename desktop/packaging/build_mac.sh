#!/bin/bash
# Builds "Reels Cutter.app" and a .dmg on macOS. Used by GitHub Actions; also works on a Mac with Python 3.11+.
set -euo pipefail
cd "$(dirname "$0")/.."
ARCH=$(uname -m)
python3 -m pip install --upgrade pip wheel
python3 -m pip install -r requirements-app.txt pyinstaller
python3 -m pip install faster-whisper || echo "::warning::faster-whisper not available for $ARCH; building without speech recognition"
# icon
mkdir -p build/icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s assets/icon.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) assets/icon.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/icon.iconset -o packaging/ReelsCutter.icns
python3 -m PyInstaller packaging/ReelsCutter.spec --noconfirm
APP="dist/Reels Cutter.app"
codesign --force --deep --sign - "$APP"     # ad-hoc signature (no Apple developer account)
echo "== self-test of the built app =="
QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/Reels Cutter" --selftest
# dmg with an Applications shortcut
rm -rf build/dmg && mkdir -p build/dmg
cp -R "$APP" build/dmg/
ln -s /Applications build/dmg/Applications
OUT="dist/ReelsCutter-1.0.0-macOS-${ARCH}.dmg"
hdiutil create -volname "Reels Cutter" -srcfolder build/dmg -ov -format UDZO "$OUT"
du -sh "$APP" "$OUT"
