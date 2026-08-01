"""Everything the menu bar does, with no menu bar in sight (Phase 11).

The UI is expected to be replaced — Phase 17 swaps rumps for SwiftUI — so the interesting
parts live here and the rumps file is left with nothing but rendering. That split is not
tidiness: it is the only way the v2 client can be a *client* rather than a rewrite. Three
rules keep it honest.

- **No AppKit, no rumps, no threads-with-opinions in this module.** It is importable and
  testable from a headless pytest run, which is exactly how it is tested.
- **A run is a subprocess, never an in-process call.** `run_night` takes tens of minutes
  and holds a container open; calling it from the UI process would freeze the menu (the
  classic rumps bug) and tie the night's fate to a UI crash. So "Run now" spawns
  `python -m orchestrator run --now` and the service only ever *polls* it.
- **Approving goes through `ApprovalQueue.approve` and nowhere else.** The service adds no
  effect of its own; it adds a `preview()` so the human sees the exact side effect before
  the click that fires it (security rule 3 — the UI is where the human actually acts).

Every external edge — the queue, the clock, the power probe, the spawner, the browser — is
an injectable seam, so the tests never open a browser, read a real battery, or start a run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from approvals import ApprovalQueue
from config import ConfigError, StandingInstructions, load_config
from models import Action, ActionType, DraftReplyPayload, MergeBranchPayload, SendEmailPayload
from orchestrator.launchd import LOG_DIR
from orchestrator.nightly import DEFAULT_OUT, RELAUNCH_STATE
from orchestrator.power import PowerState, power_refusal, read_power_state

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_LOG = LOG_DIR / "run-now.log"

# How long before the scheduled start counts as "bedtime" for the power warning. Two hours
# is the point where telling someone to plug in is still useful advice rather than a
# notification they will sleep through.
BEDTIME_LEAD_MINUTES = 120


class ServiceError(RuntimeError):
    """Something the UI should show the user rather than crash on."""


class RunAlreadyActive(ServiceError):
    """`run_now` while a run this app started is still going."""


# --------------------------------------------------------------------------------------
# State model — what the icon is allowed to say
# --------------------------------------------------------------------------------------


class Status(StrEnum):
    """The four things the icon can mean, in ascending order of "look at me".

    Ordering matters: `worst_of` picks the state that wins when several are true at once,
    and a run in progress deliberately outranks a stale failure — "it is working on it" is
    the more actionable fact while it is true.
    """

    IDLE = "idle"
    ATTENTION = "attention"
    FAILED = "failed"
    RUNNING = "running"


# Glyphs, not image assets: a template .png would need a designer and a bundle, and the
# menu bar renders these at the right weight for both light and dark menu bars for free.
STATUS_ICONS: dict[Status, str] = {
    Status.IDLE: "🌙",
    Status.ATTENTION: "🌙!",
    Status.FAILED: "🌙⚠",
    Status.RUNNING: "🌙…",
}


class RunPhase(StrEnum):
    NEVER = "never"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunSnapshot(BaseModel):
    """The last run this app knows about. Not a history — Phase 12 owns that."""

    model_config = ConfigDict(extra="forbid")

    phase: RunPhase = Field(default=RunPhase.NEVER)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    exit_code: int | None = Field(default=None)
    log_path: str = Field(default="", max_length=1000)

    @property
    def active(self) -> bool:
        return self.phase is RunPhase.RUNNING


class ActionPreview(BaseModel):
    """One pending action, rendered for a human who is about to decide it.

    `effect` is the sentence that must appear before the click that fires it. A queue whose
    UI says only "Approve?" is a queue that sends mail on a misclick.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=64)
    type: ActionType
    title: str = Field(max_length=200, description="One line, for the menu item")
    effect: str = Field(max_length=400, description="What approving will actually do")
    detail: str = Field(default="", max_length=8000, description="Body/diff context")
    tainted: bool = Field(default=False, description="Derived from untrusted input (email)")
    origin: str = Field(default="", max_length=120)

    def confirmation(self) -> str:
        """The full text shown in the confirm dialog. Effect first, always."""
        parts = [self.effect]
        if self.tainted:
            parts.append(
                "This was written by an agent after reading untrusted email. "
                "Read it before approving."
            )
        if self.detail:
            parts.append("")
            parts.append(self.detail)
        return "\n".join(parts)


class AppState(BaseModel):
    """Everything the UI renders, computed in one pass so the menu can never disagree
    with the icon."""

    model_config = ConfigDict(extra="forbid")

    status: Status = Field(default=Status.IDLE)
    icon: str = Field(default=STATUS_ICONS[Status.IDLE], max_length=8)
    summary: str = Field(default="", max_length=300, description="One line for the menu")
    pending: int = Field(default=0, ge=0)
    run: RunSnapshot = Field(default_factory=RunSnapshot)
    briefing_path: str = Field(default="", max_length=1000)
    briefing_available: bool = Field(default=False)
    warning: str = Field(default="", max_length=600, description="Bedtime power warning")
    queue_error: str = Field(default="", max_length=600)


def _spawn_run(argv: Sequence[str], *, log_path: Path) -> subprocess.Popen:
    """Start a nightly run detached from the UI's own lifetime.

    `start_new_session` puts the run in its own process group, so quitting the menu bar
    app (or the UI crashing) does not take a half-finished night with it — the run writes
    its briefing either way. Output goes to a log file because a menu bar has nowhere to
    put a stream, and losing the 3am traceback is how a failure becomes a mystery.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - owned by the child
    handle.write(f"\n\n=== Run now — {datetime.now().isoformat(timespec='seconds')}\n")
    handle.flush()
    return subprocess.Popen(
        list(argv),
        cwd=REPO_ROOT,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def run_argv(*, config: Path | str | None = None) -> list[str]:
    """The exact command "Run now" spawns, exposed so the tests assert it rather than guess.

    `--now` overrides the schedule window (this *is* the manual override the window guard
    documents) and `--ignore-relaunch-budget` keeps a manual run from eating the crash
    budget the daemon needs tonight.
    """
    argv = [sys.executable, "-m", "orchestrator", "run", "--now", "--ignore-relaunch-budget"]
    if config:
        argv += ["--config", str(config)]
    return argv


class NightShiftService:
    """The UI-facing API over the daemon, the queue and the briefing.

    Constructed once and polled; every method is safe to call from a UI timer and none of
    them block for longer than a SQLite read or a `pmset` probe.
    """

    def __init__(
        self,
        *,
        queue: ApprovalQueue | None = None,
        briefing_path: Path | str = DEFAULT_OUT,
        config_path: Path | str | None = None,
        config_loader: Callable[[Path | str | None], StandingInstructions] = load_config,
        spawn: Callable[..., subprocess.Popen] = _spawn_run,
        power_reader: Callable[[], PowerState] = read_power_state,
        opener: Callable[[str], bool] = webbrowser.open,
        clock: Callable[[], datetime] = datetime.now,
        run_log: Path | str = RUN_LOG,
        relaunch_state: Path | str = RELAUNCH_STATE,
        bedtime_lead_minutes: int = BEDTIME_LEAD_MINUTES,
    ) -> None:
        self._queue = queue if queue is not None else ApprovalQueue()
        self.briefing_path = Path(briefing_path)
        self.config_path = config_path
        self._config_loader = config_loader
        self._spawn = spawn
        self._power_reader = power_reader
        self._opener = opener
        self._clock = clock
        self.run_log = Path(run_log)
        self.relaunch_state = Path(relaunch_state)
        self.bedtime_lead_minutes = bedtime_lead_minutes

        self._process: subprocess.Popen | None = None
        self._run = RunSnapshot()
        self.queue_error = ""

    # -- config ----------------------------------------------------------------------

    def config(self) -> StandingInstructions:
        """Reload the config on every read, so editing the TOML shows up without a restart.

        A broken config must not take the menu bar down with it: the UI is the one place
        left to *tell* the user the config is broken, so the error becomes a warning string
        and the defaults keep the rest of the menu alive.
        """
        try:
            return self._config_loader(self.config_path)
        except ConfigError:
            return StandingInstructions()

    def config_error(self) -> str:
        try:
            self._config_loader(self.config_path)
        except ConfigError as exc:
            return str(exc).splitlines()[0][:600]
        return ""

    # -- runs ------------------------------------------------------------------------

    def refresh_run(self) -> RunSnapshot:
        """Poll the spawned run without blocking. The UI calls this on its timer."""
        process = self._process
        if process is None:
            return self._run
        code = process.poll()
        if code is None:
            return self._run
        self._process = None
        self._run = self._run.model_copy(
            update={
                "phase": RunPhase.SUCCEEDED if code == 0 else RunPhase.FAILED,
                "finished_at": self._clock(),
                "exit_code": code,
            }
        )
        return self._run

    def run_now(self) -> RunSnapshot:
        """Trigger a night. Returns immediately; the run outlives this call by design."""
        self.refresh_run()
        if self._run.active:
            raise RunAlreadyActive(
                f"A run is already in progress. Watch it in {self.run_log}."
            )
        argv = run_argv(config=self.config_path)
        try:
            self._process = self._spawn(argv, log_path=self.run_log)
        except OSError as exc:
            self._run = RunSnapshot(
                phase=RunPhase.FAILED,
                started_at=self._clock(),
                finished_at=self._clock(),
                log_path=str(self.run_log),
            )
            raise ServiceError(f"Could not start a run: {exc}") from exc
        self._run = RunSnapshot(
            phase=RunPhase.RUNNING, started_at=self._clock(), log_path=str(self.run_log)
        )
        return self._run

    def daemon_failure(self) -> str:
        """Did the *daemon's* last night end badly, as opposed to a run we started?

        `orchestrator.nightly` writes `relaunch.json` when a night begins and deletes it
        when one finishes cleanly, so a leftover file means the last scheduled night either
        crashed or is still going. That is a weaker signal than an exit code, so it is
        reported in exactly those words rather than asserted as a failure.
        """
        import json

        try:
            data = json.loads(self.relaunch_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict) or not data.get("night"):
            return ""
        attempts = int(data.get("attempts", 0) or 0)
        return (
            f"The nightly run for {data['night']} did not finish cleanly "
            f"({attempts} attempt(s)). See {LOG_DIR / 'nightly.err.log'}."
        )

    # -- briefing ---------------------------------------------------------------------

    def briefing_exists(self) -> bool:
        return self.briefing_path.is_file()

    def open_briefing(self) -> bool:
        """Open the last briefing in the browser. False means there is nothing to open."""
        if not self.briefing_exists():
            return False
        return bool(self._opener(self.briefing_path.resolve().as_uri()))

    # -- approvals --------------------------------------------------------------------

    def pending(self) -> list[Action]:
        """Pending actions, or [] with the reason recorded in `queue_error`.

        A single corrupt row must not blank the whole menu — but nor may it be hidden, so
        it surfaces as a warning line instead of an exception in a UI callback.
        """
        self.queue_error = ""
        try:
            return self._queue.pending()
        except Exception as exc:  # noqa: BLE001 - a queue read must never kill the UI
            self.queue_error = f"Could not read the approval queue: {exc}"
            return []

    def previews(self) -> list[ActionPreview]:
        return [preview(action) for action in self.pending()]

    def approve(self, action_id: str) -> Action:
        """Approve — and therefore *perform* — one action. The human's click lands here."""
        return self._queue.approve(action_id, by="menubar")

    def reject(self, action_id: str, *, reason: str = "") -> Action:
        return self._queue.reject(action_id, by="menubar", reason=reason)

    # -- power / bedtime ---------------------------------------------------------------

    def minutes_to_bedtime(self, now: datetime | None = None) -> int:
        """Minutes until the next scheduled run starts (0..1440)."""
        now = now or self._clock()
        schedule = self.config().schedule
        start = now.replace(
            hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
        )
        if start <= now:
            start += timedelta(days=1)
        return int((start - now).total_seconds() // 60)

    def is_bedtime(self, now: datetime | None = None) -> bool:
        return self.minutes_to_bedtime(now) <= self.bedtime_lead_minutes

    def bedtime_warning(self, now: datetime | None = None) -> str:
        """The AC warning, but only while it is still useful.

        Outside the run-up to the scheduled start this returns "": a machine on battery at
        two in the afternoon is not a problem, and a warning that is always on is a warning
        nobody reads. Inside it, the refusal is the *same* function the daemon will apply
        at 3am (`power_refusal`), so the UI cannot promise a run the guard will decline.
        """
        if not self.is_bedtime(now):
            return ""
        schedule = self.config().schedule
        state = self._power_reader()
        refusal = power_refusal(state, require_ac=schedule.require_ac)
        if refusal:
            minutes = self.minutes_to_bedtime(now)
            return f"Tonight's run starts in {minutes} min and will be skipped: {refusal}"
        return state.notes[0] if state.notes else ""

    # -- the one call the UI actually makes --------------------------------------------

    def state(self, now: datetime | None = None) -> AppState:
        """One consistent snapshot: icon, menu text, counts and warnings together."""
        run = self.refresh_run()
        actions = self.pending()
        queue_error = self.queue_error
        warning = self.bedtime_warning(now)
        config_error = self.config_error()
        daemon_failure = "" if run.active else self.daemon_failure()

        if run.active:
            status = Status.RUNNING
            summary = "Running tonight's shift…"
        elif run.phase is RunPhase.FAILED or daemon_failure:
            status = Status.FAILED
            summary = daemon_failure or (
                f"Last run failed (exit {run.exit_code}). See {self.run_log}."
            )
        elif actions:
            status = Status.ATTENTION
            summary = f"{len(actions)} action(s) waiting for approval"
        else:
            status = Status.IDLE
            summary = "Idle — nothing waiting"

        if config_error and status is not Status.RUNNING:
            # A config the daemon cannot load means tonight will not run at all; that
            # outranks "idle" and must not be discoverable only from a log file.
            status = Status.FAILED
            summary = f"Config error: {config_error}"

        return AppState(
            status=status,
            icon=STATUS_ICONS[status],
            summary=summary[:300],
            pending=len(actions),
            run=run,
            briefing_path=str(self.briefing_path),
            briefing_available=self.briefing_exists(),
            warning=warning[:600],
            queue_error=queue_error[:600],
        )


# --------------------------------------------------------------------------------------
# Rendering an action for a human
# --------------------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def preview(action: Action) -> ActionPreview:
    """Turn a queue row into the sentence a human decides on.

    Written per payload type rather than from a generic dump because the point is to make
    the *irreversible* part obvious: which address the mail leaves for, which branch merges
    into which. Everything here is displayed as plain text — these strings are email-derived
    and are never HTML, never a prompt, never interpreted.
    """
    payload = action.payload
    if isinstance(payload, DraftReplyPayload):
        title = _clip(f"Reply to {payload.to} — {payload.subject or '(no subject)'}", 200)
        effect = f"Approving SENDS this reply to {payload.to} from your Gmail account."
        detail = _clip(payload.body, 4000)
    elif isinstance(payload, SendEmailPayload):
        title = _clip(f"Send to {payload.to} — {payload.subject or '(no subject)'}", 200)
        effect = f"Approving SENDS this email to {payload.to} from your Gmail account."
        detail = "(HTML body — open the briefing to read it in full.)"
    elif isinstance(payload, MergeBranchPayload):
        title = _clip(f"Merge {payload.branch} → {payload.into} ({payload.project})", 200)
        effect = (
            f"Approving MERGES {payload.branch} into {payload.into} "
            f"in project {payload.project} on this machine."
        )
        detail = f"Diff: {payload.diff_path}" if payload.diff_path else ""
    else:  # pragma: no cover - a new payload type without a preview is a bug, loudly
        title = _clip(action.summary or action.type.value, 200)
        effect = f"Approving performs a {action.type.value} action. No preview is available."
        detail = ""

    return ActionPreview(
        id=action.id,
        type=action.type,
        title=title,
        effect=effect,
        detail=detail,
        tainted=bool(action.taint),
        origin=action.origin,
    )
