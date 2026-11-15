#!/bin/sh
# Builds ~/Applications/Amazing Grok.app around this checkout.
# Needs the Xcode command line tools (swiftc), uv and sox: brew install uv sox
set -e
here="$(cd "$(dirname "$0")" && pwd)"
app="$HOME/Applications/Amazing Grok.app"
mkdir -p "$app/Contents/MacOS"
cp "$here/Info.plist" "$app/Contents/Info.plist"
plutil -replace AGMenubarScript -string "$here/menubar.py" "$app/Contents/Info.plist"
swiftc -O "$here/launcher.swift" -o "$app/Contents/MacOS/AmazingGrok"
# Ad-hoc signed with a fixed identifier, so permissions survive rebuilds.
codesign --force --sign - --identifier io.github.teddytennant.amazing-grok "$app"
echo "built $app"
echo "start it:  open \"$app\""
