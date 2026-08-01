#!/bin/bash
# Regenerate the README screenshots into docs/.
#
# The briefing is captured headlessly (Chrome renders the same self-contained HTML the app
# opens, so the shot is deterministic and can be regenerated in CI or after a style change).
# The two *app* windows cannot be: a menu bar panel only exists while the menu is open, and
# `screencapture` needs Screen Recording permission that a script cannot grant itself. So
# this script sets the stage — demo daemon up, canned night seeded, windows ready — and
# tells you the two captures to take by hand.
#
#   ./packaging/screenshots.sh            # briefing shots + instructions for the app shots
#   ./packaging/screenshots.sh --briefing # just the headless ones
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="docs"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEMO_ROOT="$HOME/Library/Application Support/NightShift/demo"

mkdir -p "$OUT"

# A fresh canned night, so the shot matches what a first launch shows.
uv run python -c "from app.demo import seed; print(seed().briefing_path)" >/dev/null

[[ -x "$CHROME" ]] || { echo "Google Chrome not found; skipping the briefing shots" >&2; exit 1; }

shoot() {  # shoot <height> <output>
    "$CHROME" --headless --disable-gpu --hide-scrollbars \
        --force-device-scale-factor=2 --window-size="1100,$1" \
        --screenshot="$PWD/$2" "file://$DEMO_ROOT/briefing.html" >/dev/null 2>&1
}

shoot 2400 "$OUT/briefing.png"
shoot 4600 "$OUT/briefing-full.png"
# The second half of the artifact — project work, agent notes, failures — cropped out of the
# full-page shot, because that is the part that shows what the *night* did.
sips -c 2100 2200 --cropOffset 4820 0 "$OUT/briefing-full.png" --out "$OUT/briefing-projects.png" >/dev/null
echo "Wrote $OUT/briefing.png, $OUT/briefing-full.png, $OUT/briefing-projects.png"

[[ "${1:-}" == "--briefing" ]] && exit 0

cat <<'TXT'

Now the two app windows, by hand (⌘⇧4 then Space captures one window):

  1. open app/NightShiftUI/.build/NightShift.app     # or /Applications/NightShift.app
  2. Click the moon in the menu bar → capture the panel  → docs/menu.png
  3. "Review approvals…" → capture the window            → docs/approvals.png
  4. "Run history & transcripts" → open the project run  → docs/transcript.png

The app must be showing the demo-mode banner for these; if it is not, quit any daemon on
port 8402 first (a real daemon takes priority over the demo, by design).
TXT
