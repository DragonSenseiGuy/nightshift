"""`python -m orchestrator <command>` — the daemon's CLI.

    run        one night, start to finish (this is what launchd invokes)
    schedule   install / uninstall / status / print the launchd job, or run it now
    power      what the power guard currently sees, and what it would decide
    notify     post a test notification, to check delivery and permissions

Dispatch is by hand rather than by argparse subparsers so each command owns its own flag
set and `run`'s flags stay identical whether a human or launchd passes them.
"""

from __future__ import annotations

import sys

USAGE = __doc__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""

    if command == "run":
        from orchestrator.nightly import main as run_main

        return run_main(argv[1:])
    if command == "schedule":
        from orchestrator.launchd import main as schedule_main

        return schedule_main(argv[1:])
    if command == "power":
        from orchestrator.power import main as power_main

        return power_main()
    if command == "notify":
        from orchestrator.notify import main as notify_main

        return notify_main(argv[1:])

    print(USAGE)
    return 0 if command in {"-h", "--help", "help"} else 2


if __name__ == "__main__":
    sys.exit(main())
