"""The nightly entrypoint launchd fires, with the watchdog wrapped around it (Phase 10).

This is what runs at 3am with nobody watching, so its contract is narrower than the
interactive entrypoints':

1. **Check power first.** A run on battery is refused before a container is booted, and
   the refusal is written into the briefing (see `power.py`).
2. **Hold the machine awake for the run window only** (`caffeinate.py`), released in a
   `finally` so an exception cannot leak a sleep assertion.
3. **Every stage is wrapped.** A stage that raises becomes a `Failure` on the briefing and
   the night continues with the next stage. Nothing about 3am makes an exception more
   informative than a briefing entry, and there is nobody at the terminal to read it.
4. **The briefing is written no matter what** — including when every stage failed, when
   the power guard refused, and when the process is about to exit non-zero. Phase 7's
   contract is that silence never means success, and this is the run where that matters
   most. It is written in a `finally`.

**Exit codes are launchd's input, not a human's**, because `KeepAlive.SuccessfulExit =
false` relaunches on non-zero. So a night with failed *stages* still exits 0: those are
recorded, not lost, and re-running the whole night would double the spend for no new
information. Only an unhandled crash exits non-zero, which is the one case a relaunch
actually helps. Repeats are bounded by the relaunch budget below.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from config import ConfigError, StandingInstructions, load_config, use_config
from models import Briefing
from orchestrator.caffeinate import keep_awake
from orchestrator.power import PowerState, power_refusal, read_power_state

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "out" / "briefing.html"
STATE_DIR = Path.home() / "Library" / "Application Support" / "NightShift"
RELAUNCH_STATE = STATE_DIR / "relaunch.json"


class NightResult(BaseModel):
    """What happened, in the shape the CLI (and Phase 11's UI) can act on."""

    model_config = ConfigDict(extra="forbid")

    briefing_path: str = Field(default="")
    refused: str = Field(default="", description="Power-guard reason, if the run was skipped")
    failures: int = Field(default=0, ge=0)
    stages: list[str] = Field(default_factory=list, description="Stages that completed")
    seconds: float = Field(default=0.0, ge=0.0)
    night_id: str = Field(
        default="", description="Run-history id; `transcripts.py list --night <id>`"
    )
    cost_usd: float = Field(default=0.0, ge=0.0, description="What tonight's agents cost")

    @property
    def ran(self) -> bool:
        return not self.refused


@contextmanager
def stage(briefing: Briefing, name: str, message: str, result: NightResult | None = None) -> Iterator[None]:
    """Run one stage; turn any exception into a briefing `Failure` and carry on.

    The traceback goes to the log (launchd captures it in `StandardErrorPath`) and the
    repr goes into the briefing — the morning reader gets a sentence, the debugger gets
    the stack, and neither depends on someone having been awake.
    """
    print(f"\n=== {name}")
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - a 3am stage failure belongs in the briefing
        briefing.add_failure(name, message, repr(exc))
        print(f"FAILED {name}: {exc!r}")
        traceback.print_exc()
    else:
        if result is not None:
            result.stages.append(name)


@contextmanager
def _night_history(store=None) -> Iterator[tuple[object | None, str]]:
    """Open tonight's run-history row and install the transcript recorder (Phase 12).

    Wrapped defensively for the same reason the briefing write is: observability that can
    take the night down with it is worse than no observability. An unwritable database
    costs you the history and prints why; the night still runs, still writes a briefing,
    still queues its approvals.
    """
    try:
        from transcripts import TranscriptStore, recording_night

        manager = recording_night(store if store is not None else TranscriptStore())
        opened = manager.__enter__()
    except Exception as exc:  # noqa: BLE001 - history is best-effort by contract
        print(f"Run history unavailable ({exc!r}); tonight will not be recorded.")
        yield None, ""
        return
    try:
        yield opened
    finally:
        manager.__exit__(None, None, None)


def _finish_night(store, night_id: str, result: NightResult, *, crashed: bool) -> None:
    """Close the run-history row with tonight's outcome. Never raises."""
    if store is None or not night_id:
        return
    from transcripts import NightOutcome

    if crashed:
        outcome = NightOutcome.CRASHED
    elif result.refused:
        outcome = NightOutcome.REFUSED
    elif result.failures:
        outcome = NightOutcome.FAILED
    else:
        outcome = NightOutcome.COMPLETED
    try:
        result.cost_usd = store.night_cost(night_id)
        store.finish_night(
            night_id,
            outcome=outcome,
            failures=result.failures,
            stages=result.stages,
            briefing_path=result.briefing_path,
            seconds=result.seconds,
            refused=result.refused,
        )
    except Exception as exc:  # noqa: BLE001 - see `_night_history`
        print(f"Could not record tonight's run history: {exc!r}")


def _prune_history(store, config: StandingInstructions) -> None:
    """Apply `[retention]` so a transcript per agent per night is not forever."""
    if store is None:
        return
    try:
        runs, nights = store.prune(older_than_days=config.retention.transcript_days)
        if runs or nights:
            print(f"Pruned {runs} old agent run(s) and {nights} old night(s) from the history.")
    except Exception as exc:  # noqa: BLE001 - see `_night_history`
        print(f"Could not prune the transcript store: {exc!r}")


def _prune_snapshots(config: StandingInstructions) -> None:
    """Apply `[retention]` to the Phase 13 snapshot store, one per project per night.

    Separate from the transcript prune and separately defensive: these two stores fail for
    different reasons (a snapshot prune also touches each project's repo), and losing the
    ability to prune must never be the thing that fails a night.
    """
    try:
        from snapshots import prune_snapshots

        deleted = prune_snapshots(config)
        if deleted:
            print(f"Pruned {len(deleted)} aged-out project snapshot(s).")
    except Exception as exc:  # noqa: BLE001 - see `_night_history`
        print(f"Could not prune the snapshot store: {exc!r}")


def _notify(briefing: Briefing, result: NightResult, config: StandingInstructions) -> None:
    """Post the wake-up nudge (Phase 15). Best-effort in exactly the way the prune is.

    Fired from the outermost `finally`, so it happens for a clean night, a night with failed
    stages, a refused one and a crashed one alike — a crash is the night you most want to be
    told about. `notify_night` swallows its own errors; the import is wrapped too, because a
    module that fails to import must not be able to take down a night that already worked.
    """
    try:
        from orchestrator.notify import notify_night

        notify_night(
            briefing,
            briefing_path=result.briefing_path,
            refused=result.refused,
            enabled=config.notifications.enabled,
        )
    except Exception as exc:  # noqa: BLE001 - a banner is never worth failing a night for
        print(f"Could not notify: {exc!r}")


def _write_briefing(briefing: Briefing, out: Path) -> str:
    """Render and write the artifact. Never raises — a failed write still gets reported."""
    try:
        from briefing import render_briefing_html

        html = render_briefing_html(briefing)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"\nWrote briefing to {out} ({len(html)} bytes).")
        return str(out)
    except Exception as exc:  # noqa: BLE001 - last line of defence; log and move on
        print(f"Could not write the briefing to {out}: {exc!r}")
        traceback.print_exc()
        return ""


def run_night(
    config: StandingInstructions,
    *,
    mock: bool = False,
    send: bool | None = None,
    projects: bool | None = None,
    since: str | None = None,
    require_ac: bool | None = None,
    caffeinate: bool | None = None,
    out: Path = DEFAULT_OUT,
    config_path: Path | str | None = None,
    power_state: PowerState | None = None,
    queue_drafts: bool | None = None,
    store=None,
) -> NightResult:
    """Run one whole night and return what happened. Only raises if `out` is unwritable.

    Every keyword defaults to `None` meaning "take it from `[schedule]`", so the CLI flags
    and the config file describe the same run and neither has to know about the other.
    `power_state` and `store` are injected by the tests; production probes the machine and
    opens the default transcript store.
    """
    schedule = config.schedule
    send = schedule.send if send is None else send
    projects = schedule.projects if projects is None else projects
    since = since or schedule.since
    require_ac = schedule.require_ac if require_ac is None else require_ac
    caffeinate = schedule.caffeinate if caffeinate is None else caffeinate
    if queue_drafts is None:
        # Default on for a real night, off under --mock: the approval queue is durable
        # host state, and a rehearsal must not leave fabricated drafts in it for the
        # morning reviewer to sift out. `--queue-drafts` still forces it on.
        queue_drafts = not mock
    if mock:
        # Mock mode must not reach Google at all, and sending is a Google call.
        send = False

    started = time.monotonic()
    briefing = Briefing(date=datetime.now().strftime("%A, %d %B %Y"))
    result = NightResult()

    # The history row is opened before any agent runs and closed in a `finally`, so a night
    # that crashes is a row that says `crashed` rather than a row that never appeared.
    # Installing the recorder here is also what gives every agent run tonight the same
    # night id, on the host and (via the environment) inside the sandbox.
    with _night_history(store) as (history, night_id):
        result.night_id = night_id
        crashed = False
        try:
            try:
                state = power_state or read_power_state()
                print(f"Power: {state.describe()}")
                for note in state.notes:
                    print(f"  note: {note}")
                    briefing.add_failure("power_guard", note)

                refusal = power_refusal(state, require_ac=require_ac)
                if refusal:
                    # Not an error the run can recover from, and not a crash either: the
                    # machine said no. Record it where the user will see it and stop before
                    # spending money.
                    print(f"Power guard: refusing to run — {refusal}")
                    briefing.add_failure("power_guard", "Nightly run skipped", refusal)
                    result.refused = refusal
                    return result

                with keep_awake(enabled=caffeinate):
                    with stage(briefing, "email_agent", "Summarising email failed", result):
                        emails = _load_emails(since, mock=mock)
                        if emails:
                            from summarise import build_digest

                            digest = build_digest(emails, since=since)
                            briefing.email = digest
                            print(
                                f"Digest: {digest.count} item(s), "
                                f"{digest.needs_reply_count} needing a reply"
                            )
                        else:
                            print("No email in the window; nothing to summarise.")

                    with stage(
                        briefing, "calendar_agent", "Calendar and task triage failed", result
                    ):
                        from day_plan import build_day_plan

                        events, tasks, unavailable = _load_day(mock=mock)
                        plan = build_day_plan(
                            events,
                            tasks,
                            day=briefing.date,
                            config=config,
                            degraded=unavailable,
                        )
                        briefing.calendar = plan.calendar
                        briefing.tasks = plan.tasks
                        # Degradations are *reported*, never swallowed: a declined Tasks
                        # scope or an unreadable calendar has to be visible at 8am, or an
                        # empty section reads as "a free day" rather than "we could not look".
                        for note in plan.degraded:
                            briefing.add_failure("calendar_agent", note)
                        print(
                            f"Day plan: {len(plan.calendar.events)} event(s), "
                            f"{len(plan.tasks.items)} task(s)"
                            + (f", {len(plan.degraded)} issue(s)" if plan.degraded else "")
                        )

                    if queue_drafts and briefing.email is not None:
                        with stage(
                            briefing, "approval_queue", "Queueing draft replies failed", result
                        ):
                            from approvals import ApprovalQueue, enqueue_digest_drafts

                            queue = ApprovalQueue()
                            queued = enqueue_digest_drafts(queue, briefing.email)
                            print(f"Queued {len(queued)} draft repl(ies) at {queue.path}.")

                    if projects:
                        with stage(briefing, "project_agent", "Project work failed", result):
                            from approvals import ApprovalQueue
                            from nightly_project import nightly_projects

                            # `nightly_projects` already turns a per-project failure into a
                            # briefing entry; this wrapper only catches the ones before that
                            # (config, queue, colima).
                            nightly_projects(
                                config,
                                briefing,
                                config_path=config_path,
                                queue=ApprovalQueue(),
                                store=history,
                                night_id=night_id,
                            )
            finally:
                result.seconds = round(time.monotonic() - started, 1)
                result.failures = len(briefing.failures)
                result.briefing_path = _write_briefing(briefing, out)

            if send:
                with stage(briefing, "send", "Sending the briefing failed", result):
                    from send_emails import get_send_credentials, send_to_self

                    html = Path(result.briefing_path).read_text(encoding="utf-8")
                    send_to_self(
                        get_send_credentials(), subject="Your morning digest", html_body=html
                    )
                    print("Sent the morning digest.")
                if briefing.failures != result.failures:
                    # A send failure arrived after the artifact was written; rewrite it so
                    # the morning shows "the email never went out" rather than a
                    # clean-looking file.
                    result.failures = len(briefing.failures)
                    _write_briefing(briefing, out)

            return result
        except BaseException:
            crashed = True
            raise
        finally:
            _finish_night(history, night_id, result, crashed=crashed)
            # Before the prunes: those touch two databases and every project repo, and the
            # nudge should not wait on housekeeping to reach a screen.
            _notify(briefing, result, config)
            _prune_history(history, config)
            _prune_snapshots(config)


def _load_emails(since: str, *, mock: bool):
    """Canned inbox in mock mode; otherwise the broker, falling back to direct Gmail."""
    if mock:
        from fixtures.mock_emails import mock_emails

        emails = mock_emails()
        print(f"Loaded {len(emails)} canned emails (mock mode — no Gmail).")
        return emails

    from main import load_emails

    return load_emails(since)


def _load_day(*, day: str = "today", mock: bool):
    """Today's events and open tasks, plus any reason a source could not be read.

    Returns `(events, tasks, degraded)` and never raises: an unreachable broker is one of
    the more likely 3am failures, and it must cost the two calendar sections rather than
    the night. Unlike `_load_emails` there is no direct-to-Google fallback — the calendar
    read lives behind the broker by design, and a second code path holding the credential
    is exactly the thing the broker exists to prevent.
    """
    if mock:
        from fixtures.mock_calendar import mock_calendar_events, mock_tasks

        events, tasks = mock_calendar_events(day), mock_tasks()
        print(f"Loaded {len(events)} canned event(s) and {len(tasks)} task(s) (mock mode).")
        return events, tasks, []

    from broker_client import BrokerClient

    try:
        with BrokerClient.from_env() as client:
            calendar = client.fetch_calendar(day)
            tasks_response = client.fetch_tasks()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(f"Broker unavailable for calendar/tasks ({exc!r}).")
        return [], [], [f"Calendar and tasks unavailable: the broker could not be reached ({exc!r})."]

    print(
        f"Loaded {calendar.count} event(s) and {tasks_response.count} task(s) from the broker."
    )
    return (
        calendar.events,
        tasks_response.tasks,
        [*calendar.degraded, *tasks_response.degraded],
    )


# --------------------------------------------------------------------------------------
# Relaunch budget
# --------------------------------------------------------------------------------------
#
# `KeepAlive = {SuccessfulExit: false}` is the right relaunch rule for a crash and a very
# expensive one for a *reproducible* crash: launchd would restart the job every
# ThrottleInterval until morning, booting a VM and spending tokens each time. launchd has
# no "give up after N" knob, so the job counts its own attempts per night and, once the
# budget is gone, exits 0 — which reads to launchd as "done" and ends the loop. The
# briefing says why.


def within_window(now: datetime, hour: int, minute: int, window_minutes: int) -> bool:
    """Is `now` inside the nightly window that opens at `hour:minute`?

    This guard exists because launchd starts the job at times nobody scheduled:

    - **`KeepAlive` implies start-at-load.** A job with any `KeepAlive` is launched the
      moment it is bootstrapped, and `RunAtLoad = false` does not prevent it (verified:
      installing the job at 21:00 for 03:00 ran it immediately). Without this guard,
      installing the schedule at bedtime would start a full night on the spot.
    - **Missed calendar intervals fire on wake.** A Mac asleep at 03:00 runs the job when
      it wakes — which is 09:00, mid-workday, with the user watching their laptop suddenly
      boot a VM.

    So the *program* enforces the schedule the plist only requests. The window is generous
    (three hours by default) so a genuinely late start — a slow wake, a crash relaunch 15
    minutes later — still runs; it only rejects "hours away from the night".
    """
    if window_minutes <= 0:
        return True
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elapsed = (now - scheduled).total_seconds() / 60
    if elapsed < 0:  # the window that could still cover us opened yesterday
        elapsed += 24 * 60
    return elapsed <= window_minutes


def _read_state(path: Path) -> dict:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claim_attempt(path: Path, night: str) -> int:
    """Record and return this night's attempt number (1 = the scheduled run)."""
    state = _read_state(path)
    attempt = int(state.get("attempts", 0)) + 1 if state.get("night") == night else 1
    try:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"night": night, "attempts": attempt}), encoding="utf-8"
        )
    except OSError as exc:
        # An unwritable state file must not stop the night; it only weakens the budget.
        print(f"Could not record the relaunch attempt in {path}: {exc!r}")
    return attempt


def clear_attempts(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator run", description="Run one night (the launchd entrypoint)."
    )
    parser.add_argument("--config", default=None, help="Standing-instructions TOML.")
    parser.add_argument("--since", default=None, help="Email lookback window, e.g. '8h'.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Briefing path.")
    parser.add_argument("--mock", action="store_true", help="Canned inbox; implies --no-send.")
    parser.add_argument(
        "--send",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Email the briefing (default: [schedule] send).",
    )
    parser.add_argument(
        "--projects",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run the project agent (default: [schedule] projects).",
    )
    parser.add_argument(
        "--require-ac",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Refuse to run on battery (default: [schedule] require_ac).",
    )
    parser.add_argument(
        "--caffeinate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Hold the machine awake for the run (default: [schedule] caffeinate).",
    )
    parser.add_argument(
        "--queue-drafts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Queue the digest's draft replies for morning approval (never sends). "
        "Default: on for a real night, off under --mock.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run regardless of the schedule window (manual 'Run now'; see within_window).",
    )
    parser.add_argument(
        "--ignore-relaunch-budget",
        action="store_true",
        help="Do not count this run against the crash-relaunch budget (manual runs).",
    )
    args = parser.parse_args(argv)

    print(f"\n{'=' * 78}\nNightShift nightly run — {datetime.now().isoformat(timespec='seconds')}")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # A broken config cannot be fixed by relaunching, so exit 0 and let launchd rest.
        print(f"Config error:\n{exc}")
        return 0
    use_config(config)
    print(f"Config: {config.source_path or 'built-in defaults (no config file)'}")

    schedule = config.schedule
    if not args.now and not within_window(
        datetime.now(), schedule.hour, schedule.minute, schedule.window_minutes
    ):
        # Exit 0 and write nothing: this is launchd starting the job at a time nobody asked
        # for, and overwriting last night's briefing with an empty one would destroy the
        # artifact the user has not read yet. Pass --now to run anyway.
        print(
            f"Outside the nightly window ({schedule.hour:02d}:{schedule.minute:02d} "
            f"+{schedule.window_minutes}m); not running. Use --now to override."
        )
        return 0

    night = datetime.now().date().isoformat()
    attempt = 1
    if not args.ignore_relaunch_budget:
        attempt = claim_attempt(RELAUNCH_STATE, night)
        budget = config.schedule.max_relaunches + 1
        if attempt > budget:
            print(
                f"Attempt {attempt} for {night} exceeds the relaunch budget ({budget}); "
                "giving up so launchd stops restarting a crashing run."
            )
            briefing = Briefing(date=datetime.now().strftime("%A, %d %B %Y"))
            briefing.add_failure(
                "watchdog",
                "The nightly run crashed repeatedly and was abandoned",
                f"{attempt - 1} attempt(s) for {night} exited non-zero. "
                "See ~/Library/Logs/NightShift/nightly.err.log.",
            )
            _write_briefing(briefing, args.out)
            return 0
        if attempt > 1:
            print(f"Relaunch after a crash: attempt {attempt} of {budget}.")

    try:
        result = run_night(
            config,
            mock=args.mock,
            send=args.send,
            projects=args.projects,
            since=args.since,
            require_ac=args.require_ac,
            caffeinate=args.caffeinate,
            out=args.out,
            config_path=args.config,
            queue_drafts=args.queue_drafts,
        )
    except Exception:  # noqa: BLE001 - a crash here is what the relaunch rule is for
        traceback.print_exc()
        print("The nightly run crashed; launchd will relaunch it within the budget.")
        return 1

    clear_attempts(RELAUNCH_STATE)
    if result.refused:
        print(f"\nRefused: {result.refused}")
    print(
        f"\nDone in {result.seconds}s — {len(result.stages)} stage(s) completed, "
        f"{result.failures} failure(s). Briefing: {result.briefing_path or '(not written)'}"
    )
    if result.night_id:
        print(
            f"Run history: night {result.night_id}, ${result.cost_usd:.4f} of agent time. "
            f"Transcripts: uv run python transcripts.py list --night {result.night_id}"
        )
    # Deliberately 0 even with failures: they are in the briefing, and a relaunch would
    # spend the budget again for the same answer.
    return 0


if __name__ == "__main__":
    sys.exit(main())
