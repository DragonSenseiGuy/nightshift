"""Demo mode — NightShift with the whole night already over and every effect disarmed.

A real night needs a Google account, an LLM key, colima and about twenty minutes. That is
the right cost for the person whose inbox it is, and the wrong cost for someone who has
thirty seconds and wants to know what this *is*. So demo mode assembles the morning out of
`fixtures/demo_night.py` — a finished briefing, a queue with three proposals in it, a run
history with transcripts — and serves it through the same `NightShiftService`, the same
`app/api.py`, the same SwiftUI client. Nothing is stubbed at the UI layer: the app a
reviewer clicks through is the app.

Three things make it safe to hand to a stranger:

- **The effects are inert.** The queue is constructed with a replacement effect table
  (`inert_effects`), so approving in demo mode moves the row to `done` and returns a note
  saying what a real approval would have done. `send_emails` is not imported, `gitops` is
  not called, and the demo queue is a separate database file from the real one — there is
  no path by which clicking Approve here sends mail.
- **It cannot start a night.** `DemoService.run_now` refuses with an explanation instead of
  spawning `orchestrator run`, which on a machine with no key and no Docker would fail
  slowly and confusingly rather than quickly and clearly.
- **It writes only inside its own directory.** Briefing, queue and transcripts all live in
  the demo root (default `~/Library/Application Support/NightShift/demo`), so a demo run
  cannot overwrite a real briefing or a real queue, and deleting that one directory undoes
  it entirely.

The canned data is not sanitised: the prompt-injection email and the hostile calendar
invite are both in the briefing, rendered as escaped text under a summary that calls them
what they are. That is the part of the design worth showing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from approvals import ApprovalQueue
from app.service import NightShiftService, ServiceError
from briefing import render_briefing_html
from fixtures.demo_night import (
    DEMO_BRANCH,
    DEMO_DATE,
    DEMO_NIGHT_ID,
    DEMO_PROJECT,
    DEMO_TRANSCRIPT_ID,
    demo_actions,
    demo_briefing,
)
from models import Action, ActionType, TokenUsage
from runner.observe import AgentRunRecord
from runner.tools import ToolCallRecord
from transcripts import NightOutcome, TranscriptStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO_ROOT / "config" / "standing_instructions.toml"


def default_demo_root() -> Path:
    """Where demo state lives — beside the real state, never on top of it."""
    return Path.home() / "Library" / "Application Support" / "NightShift" / "demo"


# --------------------------------------------------------------------------------------
# Disarmed effects
# --------------------------------------------------------------------------------------


def inert_effects() -> dict[ActionType, object]:
    """An effect table that performs nothing and says so.

    Same shape as `approvals.DEFAULT_EFFECTS`, so the queue's approve path is exercised in
    full — claim the row, run the effect, record the result — with the last step replaced.
    Written as three separate closures rather than one generic handler because the returned
    sentence is the only feedback the reviewer gets, and it should name the specific thing
    that did not happen.
    """

    def draft_reply(action: Action) -> str:
        return (
            f"Demo mode: nothing was sent. A real approval would email this reply to "
            f"{action.payload.to} from your Gmail account."
        )

    def send_email(action: Action) -> str:
        return (
            f"Demo mode: nothing was sent. A real approval would email "
            f"{action.payload.to} from your Gmail account."
        )

    def merge_branch(action: Action) -> str:
        payload = action.payload
        return (
            f"Demo mode: nothing was merged. A real approval would merge "
            f"{payload.branch} into {payload.into} in {payload.project} on this machine."
        )

    return {
        ActionType.DRAFT_REPLY: draft_reply,
        ActionType.SEND_EMAIL: send_email,
        ActionType.MERGE_BRANCH: merge_branch,
    }


# --------------------------------------------------------------------------------------
# Canned run history
# --------------------------------------------------------------------------------------


def _demo_runs() -> list[AgentRunRecord]:
    """Three finished agent runs with their tool calls, for the transcript browser.

    The taint labels are real ones (`email`, `calendar`, and none at all for the project
    agent), because the viewer's UNTRUSTED banner is one of the things worth seeing.
    """
    start = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)
    return [
        AgentRunRecord(
            id="demo-run-email",
            night_id=DEMO_NIGHT_ID,
            agent="email_agent",
            model="demo/offline-model",
            source="host",
            started_at=start,
            finished_at=start + timedelta(seconds=41),
            stop_reason="completed",
            steps=2,
            usage=TokenUsage(prompt_tokens=5120, completion_tokens=880),
            cost_usd=0.0142,
            taint=["email"],
            text="Digest written: 5 emails, 2 needing a reply, 1 injection attempt flagged.",
            transcript=[
                ToolCallRecord(
                    step=1,
                    tool="read_emails",
                    arguments={"since": "2h"},
                    ok=True,
                    result="5 emails since 2h (mock-0001 … mock-0005).",
                    taint=["email"],
                ),
                ToolCallRecord(
                    step=2,
                    tool="add_to_briefing",
                    arguments={"title": "Watch out"},
                    ok=True,
                    result="Section accepted.",
                    taint=["email"],
                ),
            ],
            messages=[
                {"role": "system", "content": "You triage email. Return JSON only."},
                {"role": "user", "content": "[UNTRUSTED EMAIL] 5 messages follow …"},
                {"role": "assistant", "content": "{\"items\": [ … ]}"},
            ],
        ),
        AgentRunRecord(
            id="demo-run-calendar",
            night_id=DEMO_NIGHT_ID,
            agent="calendar_agent",
            model="demo/offline-model",
            source="host",
            started_at=start + timedelta(minutes=1),
            finished_at=start + timedelta(minutes=1, seconds=28),
            stop_reason="completed",
            steps=2,
            usage=TokenUsage(prompt_tokens=2310, completion_tokens=460),
            cost_usd=0.0067,
            taint=["calendar"],
            text="Day plan written: 4 events, 3 tasks triaged.",
            transcript=[
                ToolCallRecord(
                    step=1,
                    tool="read_calendar",
                    arguments={"day": "today"},
                    ok=True,
                    result="4 events for Thursday 30 July.",
                    taint=["calendar"],
                ),
                ToolCallRecord(
                    step=2,
                    tool="read_tasks",
                    arguments={},
                    ok=True,
                    result="3 open tasks (1 list unavailable).",
                    taint=["calendar"],
                ),
            ],
            messages=[
                {"role": "system", "content": "You plan the day. Return JSON only."},
                {"role": "user", "content": "[UNTRUSTED CALENDAR] 4 events follow …"},
            ],
        ),
        AgentRunRecord(
            id=DEMO_TRANSCRIPT_ID,
            night_id=DEMO_NIGHT_ID,
            agent="project_agent",
            model="demo/offline-model",
            source="sandbox",
            project=DEMO_PROJECT,
            started_at=start + timedelta(minutes=3),
            finished_at=start + timedelta(minutes=21),
            stop_reason="completed",
            steps=6,
            usage=TokenUsage(prompt_tokens=18400, completion_tokens=3120),
            cost_usd=0.0613,
            taint=[],
            text="Fixed the two failing broker tests on a branch and reported the work.",
            transcript=[
                ToolCallRecord(
                    step=1,
                    tool="bash",
                    arguments={"command": "uv run pytest tests/test_broker.py -q"},
                    ok=False,
                    result="",
                    error="2 errors in 1.8s",
                ),
                ToolCallRecord(
                    step=2,
                    tool="read_file",
                    arguments={"path": "tests/test_broker.py"},
                    ok=True,
                    result="… fixture binds 127.0.0.1:8400 …",
                ),
                ToolCallRecord(
                    step=3,
                    tool="write_file",
                    arguments={"path": "tests/test_broker.py"},
                    ok=True,
                    result="Wrote 92 lines.",
                ),
                ToolCallRecord(
                    step=4,
                    tool="bash",
                    arguments={"command": "uv run pytest tests/test_broker.py -q"},
                    ok=True,
                    result="12 passed in 2.1s",
                ),
                ToolCallRecord(
                    step=5,
                    tool="report_work",
                    arguments={"completed": True},
                    ok=True,
                    result="Work report accepted.",
                ),
            ],
            messages=[
                {"role": "system", "content": "You are working in /workspace on nightshift."},
                {"role": "user", "content": "Goal: fix the failing tests on main."},
            ],
        ),
    ]


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoEnvironment:
    """Everything demo mode built, so a caller can point a service or a test at it."""

    root: Path
    briefing_path: Path
    queue_path: Path
    transcripts_path: Path

    @property
    def queue(self) -> ApprovalQueue:
        """A queue over the demo database, with the real effects replaced."""
        return ApprovalQueue(self.queue_path, effects=inert_effects())


class DemoService(NightShiftService):
    """The real service, minus the one button that would need a real machine behind it."""

    def run_now(self):  # type: ignore[override]
        raise ServiceError(
            "Demo mode does not run a real night: that needs your Google account, an LLM "
            "key and the colima sandbox. The briefing, the approval queue and the "
            "transcripts you can see here are the output of one canned night."
        )


def seed(root: Path | str | None = None, *, reset: bool = True) -> DemoEnvironment:
    """Build (or rebuild) the demo state and return where it landed.

    `reset` defaults to True because demo mode should be the same demo every time — a
    reviewer who approved everything last launch should still find three pending actions
    on the next one. It deletes only the demo root, which this module owns.
    """
    root = Path(root) if root is not None else default_demo_root()
    if reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    briefing_path = root / "briefing.html"
    briefing_path.write_text(render_briefing_html(demo_briefing()), encoding="utf-8")

    queue_path = root / "approvals.db"
    queue = ApprovalQueue(queue_path, effects=inert_effects())
    for action_type, payload, kwargs in demo_actions():
        queue.enqueue(ActionType(action_type), payload, **kwargs)

    transcripts_path = root / "transcripts.db"
    store = TranscriptStore(transcripts_path)
    store.start_night(DEMO_NIGHT_ID)
    for record in _demo_runs():
        store.save(record)
    store.finish_night(
        DEMO_NIGHT_ID,
        outcome=NightOutcome.COMPLETED,
        failures=1,
        stages=["email", "calendar", "tasks", "project", "briefing"],
        briefing_path=str(briefing_path),
        seconds=1_320.0,
        note=f"Demo night for {DEMO_DATE}; branch {DEMO_BRANCH} left unmerged.",
    )

    return DemoEnvironment(
        root=root,
        briefing_path=briefing_path,
        queue_path=queue_path,
        transcripts_path=transcripts_path,
    )


def demo_service(env: DemoEnvironment) -> DemoService:
    """A service wired to the demo environment. Config comes from the repo's own TOML so
    the bedtime/schedule logic behaves exactly as it does in a real install."""
    return DemoService(
        queue=env.queue,
        briefing_path=env.briefing_path,
        config_path=DEMO_CONFIG if DEMO_CONFIG.is_file() else None,
    )
