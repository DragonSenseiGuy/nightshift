#!/bin/bash
# The release build: `dist/NightShift-<version>.dmg`, the thing a stranger downloads.
#
#   ./packaging/build_app.sh            # daemon + app + dmg
#   ./packaging/build_app.sh --no-dmg   # stop at NightShift.app
#
# Three steps, in this order, because each one depends on the last:
#
#   1. `build_daemon.sh` freezes the UI daemon into `dist/nightshiftd` (arm64; see that
#      script for why the architecture is decided there).
#   2. `app/NightShiftUI/build.sh` builds the SwiftUI binary, assembles NightShift.app
#      around it, copies the daemon into Contents/Resources and ad-hoc signs the result.
#   3. `hdiutil` wraps the bundle in a .dmg with an /Applications symlink next to it, which
#      is the format a Mac user knows what to do with without being told.
#
# The output is **unsigned by Apple** — there is no paid Developer ID behind this — so the
# first launch needs a right-click → Open. That is documented in the README and in the
# .dmg's own README file, because "the app is damaged" is what macOS says instead.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
APP=".build/NightShift.app"
DMG="dist/NightShift-${VERSION}.dmg"
STAGE="build/dmg"

./packaging/build_daemon.sh

app/NightShiftUI/build.sh

BUNDLE="$ROOT/app/NightShiftUI/$APP"
[[ -d "$BUNDLE" ]] || { echo "no bundle at $BUNDLE" >&2; exit 1; }

if [[ "${1:-}" == "--no-dmg" ]]; then
    echo "Built $BUNDLE"
    exit 0
fi

rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE" dist
cp -R "$BUNDLE" "$STAGE/NightShift.app"
ln -s /Applications "$STAGE/Applications"

# The one instruction that cannot live in the README, because the person hitting the
# problem is looking at a mounted disk image and nothing else.
cat > "$STAGE/READ ME FIRST.txt" <<'TXT'
NightShift
==========

1. Drag NightShift.app to the Applications folder next to it.
2. The first time you open it: RIGHT-CLICK the app and choose "Open", then confirm.
   (Double-clicking an app that Apple has not notarised shows a scary error instead of
   an Open button. NightShift has no paid Apple Developer certificate, so this is the
   normal path, once. Every launch after that is a normal double-click.)
3. A moon appears in the menu bar. On a machine with no NightShift daemon running, the
   app starts in DEMO MODE: a canned night from the project's test fixtures, with every
   side effect disarmed. Nothing is your real mail, and approving sends nothing.

To run it for real — your inbox, your calendar, your projects — you need the source
checkout and about twenty minutes of setup. See the README:
https://github.com/DragonSenseiGuy/nightshift

Requires macOS 14 or later on Apple Silicon.
TXT

hdiutil create -quiet -volname "NightShift" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo "Built $DMG ($(du -h "$DMG" | cut -f1))"
