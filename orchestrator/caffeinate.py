"""Hold the Mac awake for exactly as long as the run takes (Phase 10).

`caffeinate -dims` asserts display, idle, disk and system-sleep prevention for as long as
it lives. That is precisely the wrong lifetime to get wrong in either direction: release
it early and the machine sleeps mid-container; leak it and the user's laptop never sleeps
again — silently, with no UI anywhere saying why. So this module is one context manager
with the same try/finally discipline `sandbox/orchestrator.py` uses for containers and
`sandbox/colima.py` uses for its SSH tunnel.

Two independent releases, because "the process that cleans up" is exactly what a crash
takes out:

1. the `finally` here terminates the child on the way out, whatever happened inside; and
2. the child is spawned with ``-w <our pid>``, so caffeinate exits on its own when this
   process does — including `kill -9`, a panic, or launchd tearing the job down, none of
   which run a `finally` block.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

# -d display, -i idle, -m disk, -s system. Everything the machine could do to fall asleep.
CAFFEINATE_FLAGS = ("-d", "-i", "-m", "-s")
STOP_TIMEOUT = 10.0


def caffeinate_command(pid: int | None = None) -> list[str]:
    """The exact argv used, exposed so tests can assert the flags rather than guess."""
    command = ["caffeinate", *CAFFEINATE_FLAGS]
    if pid is not None:
        command += ["-w", str(pid)]
    return command


def _spawn_caffeinate(command: list[str]):
    if shutil.which(command[0]) is None:  # non-macOS, or a stripped-down system
        raise FileNotFoundError(command[0])
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def keep_awake(*, enabled: bool = True, spawn=_spawn_caffeinate) -> Iterator[object | None]:
    """Keep the machine awake inside the block; always release it on the way out.

    `spawn` is the seam: tests inject a fake process so the suite never actually pins a
    developer's Mac awake. `enabled=False` yields None and touches nothing, which is what
    `--no-caffeinate` and a `[schedule] caffeinate = false` config resolve to.

    A machine without `caffeinate` is a warning, not a failure — the night still runs, it
    just might get slept. Losing the whole briefing over a missing convenience binary
    would be the worse trade.
    """
    process = None
    if enabled:
        try:
            process = spawn(caffeinate_command(os.getpid()))
            print("caffeinate: holding the machine awake for this run.")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"caffeinate unavailable ({exc!r}); the run may be interrupted by sleep.")
            process = None
    try:
        yield process
    finally:
        if process is not None:
            release(process)


def release(process) -> None:
    """Terminate a caffeinate child, escalating to kill. Never raises."""
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT)
        print("caffeinate: released; the machine may sleep again.")
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the run's own error
        print(f"caffeinate: could not release the sleep assertion cleanly: {exc!r}")
