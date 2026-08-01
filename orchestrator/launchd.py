"""The launchd job that fires the night, generated rather than checked in (Phase 10).

A committed plist would be a lie on every machine but the author's: it has to name an
absolute repo path, an absolute `uv` binary (launchd jobs get a near-empty PATH, and
nobody's `uv` is in it) and a home directory. So the plist is *built* from this machine's
facts and written to `~/Library/LaunchAgents/`, and `--install` bootstraps it.

Three choices worth defending, because getting them wrong costs the user real money or a
real battery:

- **`KeepAlive = {SuccessfulExit: false}`, not `true`.** Plain `KeepAlive: true` means
  "this job should always be running", so launchd restarts it the moment the night
  *finishes successfully* — an infinite loop of nightly runs, each one a container boot
  and a pile of tokens. `SuccessfulExit: false` restarts only a job that exited non-zero,
  which is the actual requirement: relaunch after a crash. The remaining hazard is a
  *repeating* crash; `ThrottleInterval` spaces the retries and the entrypoint's own
  relaunch budget (`[schedule] max_relaunches`) stops them entirely.
- **`RunAtLoad = false`, plus a window check in the program.** `RunAtLoad = false` is not
  enough on its own: launchd starts *any* `KeepAlive` job the moment it is bootstrapped,
  and it fires a missed calendar interval when the Mac next wakes. Both were observed.
  The plist cannot express "only near 3am", so `orchestrator/nightly.py:within_window`
  does — the job may be started at any hour, and it exits 0 immediately unless it is in
  the nightly window or was passed `--now`.
- **`gui/<uid>` domain, not `system`.** The run needs the user's Keychain (the Gmail
  tokens live there) and their colima VM. A LaunchDaemon in the system domain has
  neither, and would be running as root besides.

The job is a *user* agent, so it fires only while the user is logged in — which is also
the only time the Keychain is unlocked. A logged-out Mac will not run the night; that is
a property of the security model (security rule 1), not a bug to work around here.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "dev.nightshift.nightly"
REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "NightShift"

# Retry spacing for a crashed run. launchd's floor is 10s; a nightly job wants far more,
# so a crash-loop cannot burn the API budget while the user sleeps.
THROTTLE_SECONDS = 900

# launchd starts jobs with a minimal PATH. colima, docker, git and uv all live outside it.
DEFAULT_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


class LaunchdError(RuntimeError):
    """Raised when a `launchctl` call fails or the environment cannot be resolved."""


def plist_path(label: str = LABEL, directory: Path = LAUNCH_AGENTS) -> Path:
    return directory / f"{label}.plist"


def find_uv() -> str:
    """Absolute path to `uv`. Required: launchd will not find it on PATH."""
    found = shutil.which("uv")
    if found:
        return str(Path(found).resolve())
    candidate = Path.home() / ".local" / "bin" / "uv"
    if candidate.exists():
        return str(candidate)
    raise LaunchdError(
        "Could not find the `uv` binary. Install it (https://docs.astral.sh/uv/) or pass "
        "--uv /absolute/path/to/uv."
    )


def _path_env(uv_binary: str) -> str:
    """PATH for the job: uv's own directory first, then the usual tool locations."""
    directories = [str(Path(uv_binary).parent), *DEFAULT_PATH_DIRS]
    seen: list[str] = []
    for directory in directories:
        if directory not in seen:
            seen.append(directory)
    return ":".join(seen)


def build_plist(
    *,
    label: str = LABEL,
    repo_root: Path = REPO_ROOT,
    uv_binary: str | None = None,
    hour: int = 3,
    minute: int = 0,
    log_dir: Path = LOG_DIR,
    home: Path | None = None,
    arguments: list[str] | None = None,
    throttle: int = THROTTLE_SECONDS,
) -> dict:
    """The plist as a dict. Pure — `install` renders this, and the tests read it back."""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise LaunchdError(f"Invalid schedule time {hour:02d}:{minute:02d}.")

    uv_binary = uv_binary or find_uv()
    home = home or Path.home()
    arguments = list(arguments or ["run"])

    return {
        "Label": label,
        # `uv run` resolves the locked environment itself, so the job needs no venv path
        # baked in and survives a `uv sync` that rebuilds it.
        "ProgramArguments": [
            uv_binary,
            "run",
            "python",
            "-m",
            "orchestrator",
            *arguments,
        ],
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "PATH": _path_env(uv_binary),
            "HOME": str(home),
        },
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        # Never on load or login: installing the job must not start a night (see docstring).
        "RunAtLoad": False,
        # Relaunch a *crashed* run only. `True` here would loop completed runs forever.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": int(throttle),
        "StandardOutPath": str(log_dir / "nightly.out.log"),
        "StandardErrorPath": str(log_dir / "nightly.err.log"),
        # Nice-to-have: keeps the job from being classed as interactive work.
        "LowPriorityIO": True,
    }


def plist_bytes(**kwargs) -> bytes:
    return plistlib.dumps(build_plist(**kwargs))


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("launchctl") is None:
        raise LaunchdError("launchctl not found — this is a macOS-only feature.")
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def domain() -> str:
    return f"gui/{os.getuid()}"


def install(
    *,
    label: str = LABEL,
    path: Path | None = None,
    force: bool = False,
    bootstrap: bool = True,
    **plist_kwargs,
) -> Path:
    """Write the plist and load it. Refuses to clobber an existing one without `force`.

    Overwriting silently would discard a schedule the user hand-tuned, and they would only
    find out by not being woken with a briefing. The error names the file and the flag.
    """
    target = path or plist_path(label)
    if target.exists() and not force:
        raise LaunchdError(
            f"{target} already exists. Inspect it, then re-run with --force to replace it "
            "(this will overwrite any hand-edits)."
        )

    plist = build_plist(label=label, **plist_kwargs)
    target.parent.mkdir(parents=True, exist_ok=True)
    # launchd does not create the log directory: a missing one makes the job fail to spawn
    # with no log saying so. Create the directory the *plist* names, not the default.
    Path(plist["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(plist))

    if bootstrap:
        # An already-loaded job must be booted out first or `bootstrap` fails with EEXIST.
        _launchctl("bootout", f"{domain()}/{label}")
        result = _launchctl("bootstrap", domain(), str(target))
        if result.returncode != 0:
            raise LaunchdError(
                f"launchctl bootstrap failed ({result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        _launchctl("enable", f"{domain()}/{label}")
    return target


def uninstall(*, label: str = LABEL, path: Path | None = None) -> bool:
    """Boot the job out and delete its plist. Returns whether a plist was removed."""
    target = path or plist_path(label)
    _launchctl("bootout", f"{domain()}/{label}")
    if target.exists():
        target.unlink()
        return True
    return False


def status(*, label: str = LABEL, path: Path | None = None) -> str:
    """Human-readable state of the job: installed? loaded? when does it next fire?"""
    target = path or plist_path(label)
    lines = [f"plist: {target} ({'present' if target.exists() else 'not installed'})"]
    result = _launchctl("print", f"{domain()}/{label}")
    if result.returncode != 0:
        lines.append(f"launchd: not loaded in {domain()}")
        return "\n".join(lines)

    lines.append(f"launchd: loaded in {domain()}")
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "last exit code =", "runs =", "pid =")):
            lines.append(f"  {stripped}")
    return "\n".join(lines)


def kickstart(*, label: str = LABEL) -> str:
    """Ask launchd to run the job now (used by `--run-now`; the same code path as 3am)."""
    result = _launchctl("kickstart", "-p", f"{domain()}/{label}")
    if result.returncode != 0:
        raise LaunchdError(
            f"launchctl kickstart failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip() or f"kickstarted {label}"


def _describe(plist: dict) -> str:
    interval = plist["StartCalendarInterval"]
    return (
        f"{plist['Label']} — nightly at {interval['Hour']:02d}:{interval['Minute']:02d}\n"
        f"  program: {' '.join(plist['ProgramArguments'])}\n"
        f"  cwd:     {plist['WorkingDirectory']}\n"
        f"  logs:    {plist['StandardOutPath']}\n"
        f"  relaunch on crash only (KeepAlive.SuccessfulExit = false), "
        f"throttled {plist['ThrottleInterval']}s"
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m orchestrator schedule ...` — install / uninstall / status / print."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="orchestrator schedule", description="Manage the nightly launchd job."
    )
    parser.add_argument(
        "action", choices=["install", "uninstall", "status", "print", "run-now"]
    )
    parser.add_argument("--at", default=None, help="Time of night as HH:MM (default: config).")
    parser.add_argument("--config", default=None, help="Standing-instructions TOML.")
    parser.add_argument("--uv", default=None, help="Absolute path to the uv binary.")
    parser.add_argument("--label", default=LABEL)
    parser.add_argument("--force", action="store_true", help="Replace an existing plist.")
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Write the plist but do not load it into launchd.",
    )
    args = parser.parse_args(argv)

    if args.action == "status":
        print(status(label=args.label))
        return 0
    if args.action == "uninstall":
        removed = uninstall(label=args.label)
        print(
            f"Booted {args.label} out of {domain()}"
            + (" and removed its plist." if removed else "; no plist was present.")
        )
        return 0
    if args.action == "run-now":
        print(kickstart(label=args.label))
        return 0

    from config import ConfigError, load_config

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error:\n{exc}")
        return 2

    hour, minute = config.schedule.hour, config.schedule.minute
    if args.at:
        try:
            hour_text, _, minute_text = args.at.partition(":")
            hour, minute = int(hour_text), int(minute_text or 0)
        except ValueError:
            print(f"--at expects HH:MM, got {args.at!r}.")
            return 2

    arguments = ["run"] + (["--config", str(Path(args.config).resolve())] if args.config else [])
    try:
        if args.action == "print":
            print(plistlib.dumps(
                build_plist(
                    label=args.label,
                    uv_binary=args.uv,
                    hour=hour,
                    minute=minute,
                    arguments=arguments,
                )
            ).decode())
            return 0

        target = install(
            label=args.label,
            force=args.force,
            bootstrap=not args.no_bootstrap,
            uv_binary=args.uv,
            hour=hour,
            minute=minute,
            arguments=arguments,
        )
    except LaunchdError as exc:
        print(f"launchd: {exc}")
        return 2

    print(f"Installed {target}")
    print(_describe(build_plist(label=args.label, uv_binary=args.uv, hour=hour, minute=minute,
                                arguments=arguments)))
    if args.no_bootstrap:
        print(f"\nNot loaded. Load it with:\n  launchctl bootstrap {domain()} {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
