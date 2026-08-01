"""`uv run python -m app` — the UI processes.

Two subcommands, because Phase 17 gave the UI a second front end:

- `menubar` (the default, kept implicit so `python -m app` still means what it did in
  Phase 11) runs the rumps menu bar in this process;
- `serve` runs the loopback API the SwiftUI client renders, and imports no AppKit at all.

rumps is imported inside the command that needs it so `python -m app --help`, `serve` and
the import of `app.service` all stay usable on a machine where AppKit is unavailable.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        from app.api import main as serve_main

        return serve_main(argv[1:])
    if argv and argv[0] == "demo":
        # `serve --demo`, spelled the way someone trying the project out would guess.
        from app.api import main as serve_main

        return serve_main(["--demo", *argv[1:]])
    if argv and argv[0] == "menubar":
        argv = argv[1:]

    from app.menubar import main as menubar_main

    return menubar_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
