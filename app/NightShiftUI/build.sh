#!/bin/bash
# Build NightShift.app — the SwiftUI client (Phase 17).
#
# SwiftPM produces a bare executable, and a bare executable cannot be a menu bar agent: it
# has no bundle identifier (so `UNUserNotificationCenter.current()` traps), no LSUIElement
# (so it takes a Dock icon and a menu bar of its own), and no place for an Info.plist. So
# this script builds the binary and assembles the bundle around it, then ad-hoc signs it —
# unsigned bundles are refused a notification authorisation on current macOS.
#
#   ./build.sh            # release build into .build/NightShift.app
#   ./build.sh --run      # ... and launch it
#   ./build.sh --install  # ... and copy it to /Applications
#
# If `dist/nightshiftd` exists (build it with `packaging/build_daemon.sh`) it is copied into
# Contents/Resources and the app becomes self-contained: `DaemonSupervisor.swift` starts it
# in demo mode when no real daemon is running, which is what makes a downloaded copy of
# this app show something on a machine with no Python and no checkout. Without it the app
# still builds and still drives a developer's `uv run python -m app serve` — that is the
# difference between the developer build and the release build, and it is only this file.
set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"

APP_NAME="NightShift"
BUNDLE_ID="dev.adityan.nightshift"
CONFIG="release"
DEST=".build/${APP_NAME}.app"
DAEMON="$REPO_ROOT/dist/nightshiftd"

swift build -c "$CONFIG"
BIN="$(swift build -c "$CONFIG" --show-bin-path)/NightShiftUI"

rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
cp "$BIN" "$DEST/Contents/MacOS/$APP_NAME"

if [[ -x "$DAEMON" ]]; then
    cp "$DAEMON" "$DEST/Contents/Resources/nightshiftd"
    echo "Embedded $(file -b "$DAEMON")"
else
    echo "No dist/nightshiftd — building a developer app (needs \`python -m app serve\`)."
fi

cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key><string>Night Shift</string>
    <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
    <key>CFBundleExecutable</key><string>${APP_NAME}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <!-- Menu bar agent: no Dock icon, no app menu. Without this the client shows up as a
         second, empty window-less app every time it launches. -->
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc signature. Not a distribution signature — it is what makes the bundle a stable
# identity for TCC, so a granted notification permission survives a rebuild. The embedded
# daemon is signed first: signing the bundle seals its contents, so the order is not
# cosmetic — re-signing the helper afterwards would invalidate the seal it sits inside.
if [[ -f "$DEST/Contents/Resources/nightshiftd" ]]; then
    codesign --force --sign - "$DEST/Contents/Resources/nightshiftd" >/dev/null
fi
codesign --force --sign - --identifier "$BUNDLE_ID" "$DEST" >/dev/null

echo "Built $DEST"

case "${1:-}" in
    --run)
        open "$DEST"
        ;;
    --install)
        rm -rf "/Applications/${APP_NAME}.app"
        cp -R "$DEST" "/Applications/${APP_NAME}.app"
        echo "Installed /Applications/${APP_NAME}.app"
        ;;
esac
