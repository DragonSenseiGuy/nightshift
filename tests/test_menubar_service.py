"""The menu bar's brain, tested without a menu bar (Phase 11).

`app.service` exists so that the UI can be replaced (rumps now, SwiftUI in Phase 17) with
no logic to port. This file is the proof: everything the icon says, everything "Run now"
does, and every approval path is exercised here with rumps never imported.

What is deliberately covered:

- **the status model**, because an icon that says "idle" while a run is failing is worse
  than no icon;
- **run triggering as a subprocess**, asserted down to the argv — a "Run now" that called
  `run_night` in-process would freeze the menu for the length of a night;
- **approve/reject**, asserted to reach `ApprovalQueue` and to fire the effect *only* after
  an approve (security rule 3, from the UI's side of the boundary);
- **the preview text**, because the queue's whole safety property is that the human saw the
  side effect before the click that performed it;
- **the bedtime warning**, using the same `power_refusal` the daemon will apply at 3am, so
  the UI cannot promise a run the guard will decline.

The one rumps-touching test is marked `gui` and skipped unless NIGHTSHIFT_GUI_TESTS=1, so
the suite stays green in a headless run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from app.service import (
    STATUS_ICONS,
    NightShiftService,
    RunAlreadyActive,
    RunPhase,
    Status,
    preview,
    run_argv,
)
from approvals import ApprovalQueue
from config import ConfigError, ScheduleConfig, StandingInstructions
from models import Action, ActionStatus, ActionType, DraftReplyPayload, MergeBranchPayload
from orchestrator.power import PowerState

AT_MIDNIGHT = datetime(2026, 7, 24, 0, 30)  # 2.5h before a 03:00 run
AT_TWO_AM = datetime(2026, 7, 24, 2, 0)  # 1h before it — inside the bedtime window
AT_NOON = datetime(2026, 7, 24, 12, 0)

ON_BATTERY = PowerState(on_ac=False, battery_percent=61)
ON_AC = PowerState(on_ac=True, battery_percent=99)


class FakeProcess:
    """A spawned run, driven by the test. `codes` is popped one poll at a time."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.code


@pytest.fixture
def sent():
    """Records every effect that actually fired. Empty is the default assertion."""
    return []


@pytest.fixture
def queue(tmp_path: Path, sent) -> ApprovalQueue:
    def record(action: Action) -> str:
        sent.append(action.id)
        return "recorded"

    return ApprovalQueue(
        tmp_path / "approvals.db",
        effects={t: record for t in ActionType},
    )


def make_service(tmp_path: Path, queue: ApprovalQueue, **kwargs) -> NightShiftService:
    spawned: list = kwargs.pop("spawned", [])
    process = kwargs.pop("process", None)

    def spawn(argv, *, log_path):
        spawned.append((list(argv), log_path))
        return process or FakeProcess(None)

    defaults = dict(
        queue=queue,
        briefing_path=tmp_path / "briefing.html",
        config_loader=lambda _path: StandingInstructions(),
        spawn=spawn,
        power_reader=lambda: ON_AC,
        opener=lambda url: True,
        clock=lambda: AT_NOON,
        run_log=tmp_path / "run.log",
        relaunch_state=tmp_path / "relaunch.json",
    )
    defaults.update(kwargs)
    return NightShiftService(**defaults)


def enqueue_draft(queue: ApprovalQueue, *, to: str = "sam@example.com") -> Action:
    return queue.enqueue(
        ActionType.DRAFT_REPLY,
        DraftReplyPayload(email_id="m1", to=to, subject="Re: launch", body="Sounds good."),
        origin="email_agent",
        taint=["email"],
        summary=f"Reply to {to}",
    )


# --------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------


def test_idle_when_nothing_is_waiting(tmp_path, queue):
    state = make_service(tmp_path, queue).state()
    assert state.status is Status.IDLE
    assert state.pending == 0
    assert state.briefing_available is False
    assert "Idle" in state.summary


def test_pending_approvals_raise_the_icon_to_attention(tmp_path, queue):
    enqueue_draft(queue)
    enqueue_draft(queue, to="kim@example.com")
    state = make_service(tmp_path, queue).state()
    assert state.status is Status.ATTENTION
    assert state.pending == 2
    assert state.icon != STATUS_ICONS[Status.IDLE]  # a distinct glyph, whatever it is
    assert "2 action" in state.summary


def test_running_outranks_pending(tmp_path, queue):
    enqueue_draft(queue)
    service = make_service(tmp_path, queue, process=FakeProcess(None))
    service.run_now()
    state = service.state()
    assert state.status is Status.RUNNING
    assert state.run.phase is RunPhase.RUNNING
    assert state.pending == 1  # still counted, just not the headline


def test_a_failed_run_shows_as_failed_then_the_queue_takes_over(tmp_path, queue):
    process = FakeProcess(None)
    service = make_service(tmp_path, queue, process=process)
    service.run_now()
    process.code = 2  # the run exited non-zero

    state = service.state()
    assert state.status is Status.FAILED
    assert state.run.exit_code == 2
    assert str(service.run_log) in state.summary


def test_a_successful_run_returns_to_idle(tmp_path, queue):
    process = FakeProcess(None)
    service = make_service(tmp_path, queue, process=process)
    service.run_now()
    process.code = 0
    state = service.state()
    assert state.run.phase is RunPhase.SUCCEEDED
    assert state.status is Status.IDLE


def test_an_unfinished_daemon_night_is_reported(tmp_path, queue):
    relaunch = tmp_path / "relaunch.json"
    relaunch.write_text(json.dumps({"night": "2026-07-23", "attempts": 2}), encoding="utf-8")
    state = make_service(tmp_path, queue, relaunch_state=relaunch).state()
    assert state.status is Status.FAILED
    assert "2026-07-23" in state.summary


def test_a_broken_config_is_shown_in_the_menu_not_swallowed(tmp_path, queue):
    def explode(_path):
        raise ConfigError("standing_instructions.toml: line 3: expected '='")

    state = make_service(tmp_path, queue, config_loader=explode).state()
    assert state.status is Status.FAILED
    assert "Config error" in state.summary


def test_an_unreadable_queue_degrades_to_a_warning(tmp_path, queue):
    class Broken(ApprovalQueue):
        def pending(self):
            raise RuntimeError("database is locked")

    service = make_service(tmp_path, Broken(tmp_path / "b.db"))
    state = service.state()
    assert state.pending == 0
    assert "database is locked" in state.queue_error


# --------------------------------------------------------------------------------------
# Run now
# --------------------------------------------------------------------------------------


def test_run_argv_overrides_the_window_and_spares_the_relaunch_budget():
    argv = run_argv()
    assert argv[1:5] == ["-m", "orchestrator", "run", "--now"]
    assert "--ignore-relaunch-budget" in argv


def test_run_argv_passes_the_config_through():
    assert run_argv(config="/tmp/c.toml")[-2:] == ["--config", "/tmp/c.toml"]


def test_run_now_spawns_rather_than_running_inline(tmp_path, queue):
    spawned: list = []
    service = make_service(tmp_path, queue, spawned=spawned, process=FakeProcess(None))
    snapshot = service.run_now()

    assert snapshot.phase is RunPhase.RUNNING
    assert len(spawned) == 1
    argv, log_path = spawned[0]
    assert argv == run_argv()
    assert log_path == service.run_log


def test_run_now_refuses_to_start_a_second_run(tmp_path, queue):
    service = make_service(tmp_path, queue, process=FakeProcess(None))
    service.run_now()
    with pytest.raises(RunAlreadyActive):
        service.run_now()


def test_run_now_can_start_again_once_the_first_finished(tmp_path, queue):
    process = FakeProcess(None)
    spawned: list = []
    service = make_service(tmp_path, queue, spawned=spawned, process=process)
    service.run_now()
    process.code = 0
    service.state()
    service.run_now()
    assert len(spawned) == 2


# --------------------------------------------------------------------------------------
# Briefing
# --------------------------------------------------------------------------------------


def test_open_briefing_is_a_no_op_when_there_is_none(tmp_path, queue):
    opened: list = []
    service = make_service(tmp_path, queue, opener=lambda url: opened.append(url) or True)
    assert service.open_briefing() is False
    assert opened == []


def test_open_briefing_opens_a_file_url(tmp_path, queue):
    opened: list = []
    briefing = tmp_path / "briefing.html"
    briefing.write_text("<html></html>", encoding="utf-8")
    service = make_service(
        tmp_path, queue, briefing_path=briefing, opener=lambda url: opened.append(url) or True
    )
    assert service.open_briefing() is True
    assert opened[0].startswith("file://") and opened[0].endswith("briefing.html")
    assert service.state().briefing_available is True


# --------------------------------------------------------------------------------------
# Approvals — the side-effect boundary
# --------------------------------------------------------------------------------------


def test_listing_does_not_fire_anything(tmp_path, queue, sent):
    enqueue_draft(queue)
    service = make_service(tmp_path, queue)
    assert [p.type for p in service.previews()] == [ActionType.DRAFT_REPLY]
    assert sent == []


def test_approve_delegates_to_the_queue_and_fires_the_effect(tmp_path, queue, sent):
    action = enqueue_draft(queue)
    service = make_service(tmp_path, queue)
    assert sent == []

    done = service.approve(action.id)
    assert sent == [action.id]
    assert done.status is ActionStatus.DONE
    assert done.decided_by == "menubar"
    assert queue.pending() == []


def test_reject_never_fires_the_effect(tmp_path, queue, sent):
    action = enqueue_draft(queue)
    service = make_service(tmp_path, queue)
    rejected = service.reject(action.id, reason="not now")
    assert sent == []
    assert rejected.status is ActionStatus.REJECTED
    assert rejected.reason == "not now"
    assert service.state().status is Status.IDLE


def test_a_second_approve_is_refused_not_silently_resent(tmp_path, queue, sent):
    from approvals import ActionNotPending

    action = enqueue_draft(queue)
    service = make_service(tmp_path, queue)
    service.approve(action.id)
    with pytest.raises(ActionNotPending):
        service.approve(action.id)
    assert sent == [action.id]


# --------------------------------------------------------------------------------------
# Previews — what the human reads before clicking
# --------------------------------------------------------------------------------------


def test_a_draft_preview_names_the_recipient_and_says_it_sends(tmp_path, queue):
    action = enqueue_draft(queue, to="sam@example.com")
    view = preview(action)
    assert "sam@example.com" in view.title
    assert "SENDS" in view.effect and "sam@example.com" in view.effect
    assert "Sounds good." in view.detail
    assert view.tainted is True

    confirmation = view.confirmation()
    assert confirmation.startswith(view.effect)
    assert "untrusted email" in confirmation  # the taint is stated, not just flagged
    assert "Sounds good." in confirmation


def test_a_merge_preview_names_both_branches_and_the_project(queue):
    action = queue.enqueue(
        ActionType.MERGE_BRANCH,
        MergeBranchPayload(
            project="nightshift", branch="agent/2026-07-24", into="main", diff_path="out/x.diff"
        ),
    )
    view = preview(action)
    assert "agent/2026-07-24" in view.title and "main" in view.title
    assert "MERGES" in view.effect and "nightshift" in view.effect
    assert "out/x.diff" in view.detail
    assert view.tainted is False


def test_previews_are_plain_text_and_never_rendered_markup(queue):
    action = queue.enqueue(
        ActionType.DRAFT_REPLY,
        DraftReplyPayload(to="a@b.c", subject="hi", body="<script>alert(1)</script>"),
        taint=["email"],
    )
    # The preview carries the characters through verbatim; it is displayed in an NSAlert,
    # never as HTML, so escaping here would only corrupt what the reviewer reads.
    assert "<script>" in preview(action).detail


# --------------------------------------------------------------------------------------
# Bedtime power warning
# --------------------------------------------------------------------------------------


def battery_service(tmp_path, queue, **kwargs):
    return make_service(tmp_path, queue, power_reader=lambda: ON_BATTERY, **kwargs)


def test_no_power_warning_in_the_middle_of_the_day(tmp_path, queue):
    service = battery_service(tmp_path, queue)
    assert service.is_bedtime(AT_NOON) is False
    assert service.bedtime_warning(AT_NOON) == ""


def test_battery_at_bedtime_warns_that_tonight_will_be_skipped(tmp_path, queue):
    service = battery_service(tmp_path, queue)
    assert service.is_bedtime(AT_TWO_AM) is True
    warning = service.bedtime_warning(AT_TWO_AM)
    assert "battery" in warning.lower()
    assert "60 min" in warning


def test_ac_at_bedtime_says_nothing(tmp_path, queue):
    assert make_service(tmp_path, queue).bedtime_warning(AT_TWO_AM) == ""


def test_require_ac_false_suppresses_the_warning(tmp_path, queue):
    relaxed = StandingInstructions(schedule=ScheduleConfig(require_ac=False))
    service = battery_service(tmp_path, queue, config_loader=lambda _p: relaxed)
    assert service.bedtime_warning(AT_TWO_AM) == ""


def test_the_bedtime_window_follows_the_configured_schedule(tmp_path, queue):
    late = StandingInstructions(schedule=ScheduleConfig(hour=23, minute=0))
    service = battery_service(tmp_path, queue, config_loader=lambda _p: late)
    assert service.minutes_to_bedtime(datetime(2026, 7, 24, 22, 0)) == 60
    assert service.is_bedtime(datetime(2026, 7, 24, 22, 0)) is True
    assert service.is_bedtime(AT_MIDNIGHT) is False  # 22.5h until the next 23:00


def test_the_warning_reaches_the_state_the_ui_renders(tmp_path, queue):
    service = battery_service(tmp_path, queue, clock=lambda: AT_TWO_AM)
    assert "battery" in service.state().warning.lower()


# --------------------------------------------------------------------------------------
# The rumps layer — one shape test, skipped unless a GUI session is available
# --------------------------------------------------------------------------------------


def test_alert_codes_fail_closed_towards_cancel():
    pytest.importorskip("rumps")
    from app.menubar import CANCEL, decision_from_alert

    assert decision_from_alert(1) == "approve"
    assert decision_from_alert(-1) == "reject"
    assert decision_from_alert(CANCEL) == "cancel"
    assert decision_from_alert(999) == "cancel"  # an unknown button never approves


@pytest.mark.gui
@pytest.mark.skipif(
    os.getenv("NIGHTSHIFT_GUI_TESTS") != "1",
    reason="builds real NSMenu items; needs a GUI session (NIGHTSHIFT_GUI_TESTS=1)",
)
def test_the_menu_renders_every_pending_action(tmp_path, queue):
    from app.menubar import NightShiftApp

    enqueue_draft(queue)
    enqueue_draft(queue, to="kim@example.com")
    service = make_service(tmp_path, queue)
    app = NightShiftApp.__new__(NightShiftApp)  # no NSApplication, no run loop
    app.service = service
    titles = [getattr(item, "title", "") for item in app.build_menu(service.state())]
    assert any("Run now" in t for t in titles)
    assert any("Open last briefing" in t for t in titles)
    assert sum("Reply to" in t for t in titles) == 2
