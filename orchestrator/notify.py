"""The one-line nudge that gets a human from "asleep" to "reading the briefing" (Phase 15).

**When it fires: at run completion, not on wake.** The obvious reading of "notify on wake"
is to watch for a wake/unlock event and post then, but the thing that would do the watching
is the menu bar app, and that is a process the user can quit, that is not running during a
launchd-only install, and that would have to duplicate the run's state to know what to say.
macOS already solves this: a notification posted at 03:40 to a sleeping Mac is *delivered*
to Notification Center and is sitting there, unread, when the lid opens. The log confirms
the record is stored with `visibility: [history, alert, lockscreen]`. So the night posts
once, when it knows the answer, and the OS handles the waiting.

The honest limit of that choice: if the user is awake and at the machine at 03:40, the
banner appears then rather than in the morning. For the run this is designed around — a
scheduled 3am night on a sleeping laptop — that case does not arise.

**How it is delivered, in preference order, degrading rather than failing:**

1. **`terminal-notifier`** if it is on `PATH`. It is the only option here that can carry a
   click action (`-open file://…/briefing.html`), which is the whole "expanding to the
   briefing" half of the phase. Third-party and not assumed — `brew install terminal-notifier`.
2. **`osascript -e 'display notification …'`**, which ships with macOS and always works,
   but has **no click action**: the notification is attributed to Script Editor, and
   clicking it opens Script Editor rather than the briefing. Verified on macOS 26.
3. **Nothing.** No notifier, a non-macOS box, a `subprocess` failure — all print a line and
   return. A missing notifier must never cost a night: the briefing is already on disk and
   the menu bar app's "Open last briefing" is the fallback route to it.

**The headline is built from counts, never from email text.** A notification is a rendering
surface like the briefing is, and the same rule applies (security rule 2): everything the
banner says is host-authored words and integers derived from the `Briefing` model. No
subject line, no sender, no agent-written sentence ever reaches it. That is not only an
injection defence — a banner is 60 characters and truncated by the OS, so an attacker's
text would crowd out the only useful thing there is room for.

The one string that is *not* a count is the power guard's refusal, which is written by
`power.py` on the host from a `pmset` probe. It is still pushed through `_one_line`, which
strips control characters and clips, because "trusted today" is not an argument for an
unbounded string in a system dialog.

**Nothing is interpolated into a script.** The AppleScript form takes its text as `run`
arguments (`on run argv … item 1 of argv`), so a quote or a newline in the message is data,
not AppleScript. `terminal-notifier` takes an argv list. Neither path ever sees a shell.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from models import Briefing

# A banner shows roughly this much before the OS truncates it; clipping ourselves means we
# choose what falls off the end rather than letting the counts get cut mid-word.
MAX_MESSAGE = 180
MAX_TITLE = 80

# A notifier that hangs would hang the last line of the night. These calls are a fire-and-
# forget IPC to a running daemon; anything past a couple of seconds is a broken system.
NOTIFY_TIMEOUT = 10.0

TITLE = "Night Shift"

# Collapses to one banner per night instead of stacking (terminal-notifier only).
GROUP_ID = "dev.nightshift.briefing"


class Notification(BaseModel):
    """What to post, decided host-side before any backend is chosen.

    A model rather than three loose strings so the backend selection, the tests and the
    caller all agree on the same bounded shape — and so `open_path` cannot quietly become
    a URL an agent picked. It is always a local file this run wrote.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default=TITLE, max_length=MAX_TITLE)
    subtitle: str = Field(default="", max_length=MAX_TITLE)
    message: str = Field(default="", max_length=MAX_MESSAGE)
    open_path: str = Field(
        default="", max_length=1000, description="Local briefing to open on click, if supported"
    )


# --------------------------------------------------------------------------------------
# Headline
# --------------------------------------------------------------------------------------


def _one_line(text: str, limit: int) -> str:
    """Flatten to a single printable line and clip. Every string out of here goes through it.

    Control characters become spaces rather than being deleted: a newline in a banner is at
    best invisible and at worst a way to fake a second field, but deleting one would splice
    two words together and misreport what the text said. The whitespace is collapsed after,
    so the substitution never shows.
    """
    flat = "".join(ch if ch.isprintable() else " " for ch in str(text))
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _plural(count: int, singular: str, plural: str = "") -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def headline(briefing: Briefing, *, refused: str = "") -> str:
    """The one line the banner shows. Counts and host words only — see the module docstring.

    Ordered by how likely it is to change what the reader does first: replies waiting beat
    a meeting count, and a failure is appended last because it is the part that survives
    truncation best when it is the only thing there.
    """
    if refused:
        return _one_line(f"Tonight's run was skipped: {refused}", MAX_MESSAGE)

    parts: list[str] = []
    if briefing.email is not None:
        digest = briefing.email
        if digest.needs_reply_count:
            parts.append(
                f"{_plural(digest.count, 'email')}, "
                f"{digest.needs_reply_count} need{'s' if digest.needs_reply_count == 1 else ''} "
                "a reply"
            )
        else:
            parts.append(f"{_plural(digest.count, 'email')}, none need a reply")
    if briefing.calendar is not None and briefing.calendar.events:
        parts.append(_plural(len(briefing.calendar.events), "event"))
    if briefing.tasks is not None and briefing.tasks.items:
        parts.append(_plural(len(briefing.tasks.items), "task"))
    if briefing.projects is not None and briefing.projects.projects:
        parts.append(_plural(len(briefing.projects.projects), "project"))
    if briefing.failures:
        parts.append(_plural(len(briefing.failures), "failure"))

    if not parts:
        # A night that found nothing is still a night that ran, and saying so is the point:
        # silence is the one thing the briefing contract does not allow to mean "fine".
        return "Nothing to report — the briefing is ready."
    return _one_line(" · ".join(parts), MAX_MESSAGE)


def build(briefing: Briefing, *, briefing_path: Path | str = "", refused: str = "") -> Notification:
    """Turn tonight's briefing into the notification to post.

    `briefing.date` is the host's own `strftime` output, not anything an agent or an email
    supplied, so it is safe as the subtitle — but it is clipped like everything else.
    """
    return Notification(
        title=TITLE,
        subtitle=_one_line(briefing.date, MAX_TITLE),
        message=headline(briefing, refused=refused),
        open_path=str(briefing_path or ""),
    )


# --------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------


def terminal_notifier_command(notification: Notification, *, executable: str) -> list[str]:
    """argv for `terminal-notifier` — the backend that can open the briefing on click."""
    command = [
        executable,
        "-title",
        notification.title,
        "-message",
        notification.message or " ",
        "-group",
        GROUP_ID,
    ]
    if notification.subtitle:
        command += ["-subtitle", notification.subtitle]
    if notification.open_path:
        # A `file://` URL, resolved host-side. `-open` is what makes the banner clickable;
        # `-execute` would run a shell command and is deliberately not used.
        command += ["-open", Path(notification.open_path).resolve().as_uri()]
    return command


def osascript_command(notification: Notification) -> list[str]:
    """argv for the built-in fallback. No click action exists for `display notification`.

    The text is passed as `run` arguments rather than spliced into the `-e` source, so the
    message can contain anything at all and still be a string literal to AppleScript.
    """
    script = "display notification (item 1 of argv) with title (item 2 of argv)"
    if notification.subtitle:
        script += " subtitle (item 3 of argv)"
    argv = [
        "osascript",
        "-e",
        "on run argv",
        "-e",
        script,
        "-e",
        "end run",
        "--",
        notification.message or " ",
        notification.title,
    ]
    if notification.subtitle:
        argv.append(notification.subtitle)
    return argv


def choose_command(
    notification: Notification, *, which=shutil.which
) -> tuple[list[str], str, bool]:
    """Pick a backend: `(argv, name, click_opens_briefing)`. Empty argv means none available."""
    executable = which("terminal-notifier")
    if executable:
        return (
            terminal_notifier_command(notification, executable=executable),
            "terminal-notifier",
            bool(notification.open_path),
        )
    if which("osascript"):
        return osascript_command(notification), "osascript", False
    return [], "", False


def send(notification: Notification, *, runner=subprocess.run, which=shutil.which) -> str:
    """Post the notification. Returns the backend used, or "" if none was. Never raises.

    `runner` and `which` are the seams the tests inject through, for the same reason
    `keep_awake` takes a `spawn`: the suite must be able to assert the exact argv without
    putting a banner on a developer's screen on every `pytest`.
    """
    command, backend, clickable = choose_command(notification, which=which)
    if not command:
        print(
            "No notifier available (no terminal-notifier, no osascript); "
            "skipping tonight's notification. The briefing is still on disk."
        )
        return ""
    try:
        completed = runner(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=NOTIFY_TIMEOUT,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - a banner is never worth failing a night for
        print(f"Could not post the notification via {backend} ({exc!r}).")
        return ""
    if getattr(completed, "returncode", 0) != 0:
        # Note that osascript exits 0 even when Notification Center suppresses the banner,
        # so a 0 here means "the request was accepted", not "the user saw it". Claiming
        # more than that would be a lie the user only discovers by missing a briefing.
        print(f"{backend} exited {completed.returncode}; the notification may not have shown.")
        return ""
    print(
        f"Notified via {backend}: {notification.message}"
        + ("" if clickable else " (click opens the notifier, not the briefing)")
    )
    return backend


def notify_night(
    briefing: Briefing,
    *,
    briefing_path: Path | str = "",
    refused: str = "",
    enabled: bool = True,
    runner=subprocess.run,
    which=shutil.which,
) -> str:
    """Post the completion notification for one night. Never raises, whatever happened.

    Called from `run_night`'s `finally` for *every* outcome including a crashed one: a night
    that broke is precisely the night worth telling someone about, and a briefing full of
    failures that nobody is nudged to open is the same as no briefing.
    """
    if not enabled:
        return ""
    try:
        return send(build(briefing, briefing_path=briefing_path, refused=refused),
                    runner=runner, which=which)
    except Exception as exc:  # noqa: BLE001 - belt and braces around `send`'s own guards
        print(f"Could not build tonight's notification ({exc!r}).")
        return ""


def main(argv: list[str] | None = None) -> int:
    """`python -m orchestrator notify` — post a sample banner to check the setup.

    Exists because notification *permission* is the failure nobody can debug from a log:
    the API says 0 and the banner never appears. Running this and seeing nothing is the
    signal to go to System Settings → Notifications and allow Script Editor (or whichever
    app terminal-notifier registers as).
    """
    import argparse
    from datetime import datetime

    from orchestrator.nightly import DEFAULT_OUT

    parser = argparse.ArgumentParser(
        prog="orchestrator notify", description="Post a test notification."
    )
    parser.add_argument("--briefing", type=Path, default=DEFAULT_OUT, help="Briefing to link to.")
    args = parser.parse_args(argv)

    sample = Briefing(date=datetime.now().strftime("%A, %d %B %Y"))
    sample.add_failure("notify_test", "This is a test notification")
    path = args.briefing if Path(args.briefing).is_file() else ""
    backend = notify_night(sample, briefing_path=path, refused="")
    if not backend:
        print("Nothing was posted. See the message above.")
        return 1
    print(
        "If no banner appeared, notifications are blocked for the delivering app: "
        "System Settings → Notifications."
    )
    return 0
