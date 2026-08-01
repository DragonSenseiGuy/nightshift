"""Entry point for the bundled `nightshiftd` binary (the PyInstaller target).

The shipped `NightShift.app` cannot assume the machine it lands on has Python, `uv`, this
checkout, or anything else: reviewers and first-time users get a .app, not a repo. So the
UI daemon — `python -m app serve` — is frozen into a single-file executable and dropped
inside the bundle, and the SwiftUI client starts *that* when no daemon is already running.

It is deliberately the same entry point as the source install rather than a demo-only
special case: the binary takes the same argv (`serve`, `demo`, `--port`, `--demo-dir`,
`--token-file`), so what a reviewer runs is the program, only pre-packaged. `Contents/
MacOS/NightShift` (Swift) decides *which* mode to ask for; see `packaging/build_app.sh`.

Nothing about being frozen changes the trust model: this is still the loopback UI surface,
still token-gated, and still incapable of reaching the sandbox.
"""

from __future__ import annotations

import multiprocessing
import sys

from app.__main__ import main

# `python -m app` with no subcommand means the rumps menu bar, which is not in this binary:
# rumps is a host-only dependency group and the frozen daemon deliberately contains no
# AppKit. The bundle's UI is the SwiftUI client, so a bare `nightshiftd` means `serve`.
DEFAULT_SUBCOMMAND = "serve"
SUBCOMMANDS = {"serve", "demo"}


def argv_for(argv: list[str]) -> list[str]:
    """Insert the default subcommand when the caller only passed flags."""
    if argv and argv[0] in SUBCOMMANDS:
        return argv
    if argv and argv[0] == "menubar":
        raise SystemExit(
            "nightshiftd does not contain the rumps menu bar. Run the SwiftUI app, or "
            "use `uv run python -m app` from a source checkout."
        )
    return [DEFAULT_SUBCOMMAND, *argv]


if __name__ == "__main__":
    # Frozen executables re-exec themselves to spawn child processes; without this a stray
    # `multiprocessing` import anywhere in the dependency tree relaunches the whole daemon.
    multiprocessing.freeze_support()
    sys.exit(main(argv_for(sys.argv[1:])))
