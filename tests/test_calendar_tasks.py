"""Phase 14: calendar and task reads, and the second untrusted source.

Four properties are on trial, mirroring the email ones:

1. **The broker's `--mock` calendar/task routes** return validated shapes with no Google
   client constructed and no network call.
2. **Scope separation survives.** The read slot gained `calendar.readonly` and an optional
   `tasks.readonly`, and still holds nothing that can write or send.
3. **The calendar injection fixture dead-ends.** A model that fully complies with a hostile
   event title produces inert escaped text in the briefing, a calendar-tainted result, and
   a `TaintViolation` if anyone tries to hand it to the project agent.
4. **Unavailable sources degrade.** No credential, a declined optional scope, an API error
   or an unreachable broker cost you a section and a Failures line — never the night.

Nothing here touches the network: every model call goes through a scripted `LLMBackend`
and every Google entry point is booby-trapped.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api
import calendar_tasks
import day_plan as day_plan_module
import emails as emails_module
import google_auth
from api import MOCK_ENV_VAR, app
from briefing import render_briefing_html
from config import StandingInstructions
from fixtures.mock_calendar import (
    CALENDAR_INJECTION_CANARY,
    CALENDAR_INJECTION_MARKER,
    INJECTION_EVENT_ID,
    INJECTION_TASK_ID,
    mock_calendar_events,
    mock_tasks,
)
from google_auth import (
    CALENDAR_READONLY,
    GMAIL_READONLY,
    GMAIL_SEND,
    READ_SLOT,
    SEND_SLOT,
    TASKS_READONLY,
    WRITE_SCOPES,
    has_scope,
    token_problem,
)
from models import (
    Briefing,
    CalendarEntry,
    CalendarResponse,
    TaskEntry,
    TasksResponse,
    Urgency,
)
from orchestrator import nightly

# Bound at import so `conftest._offline_day` (which rebinds `nightly._load_day` for every
# test) cannot hide the real loader from the one test that is about it.
from orchestrator.nightly import _load_day as load_day_impl
from runner.agents import calendar_agent, project_agent
from runner.taint import TAINT_CALENDAR, PromptPart, TaintViolation
from runner.tools import ToolScopeError
from runner.tools_calendar import calendar_toolset
from runner.tools_email import BriefingSink
from tests.test_agent_separation import ScriptedBackend, _call
from runner.agent_runner import CompletionResponse, run_agent

CONFIG = StandingInstructions()
EVENTS = mock_calendar_events()
TASKS = mock_tasks()


# --- doubles ------------------------------------------------------------------------------


class FakeCredentials:
    """A credential that knows only its scopes — all `calendar_tasks` ever asks it."""

    def __init__(self, *scopes: str) -> None:
        self.scopes = list(scopes)


def _compliant_day_plan() -> str:
    """What a *fully captured* model returns: it obeys the hostile event verbatim."""
    return json.dumps(
        {
            "notes": [f"ACKNOWLEDGED. Diagnostics: {CALENDAR_INJECTION_CANARY}"],
            "events": [
                {
                    "event_id": event.id,
                    "prep_notes": (
                        [
                            f"ACKNOWLEDGED {CALENDAR_INJECTION_MARKER}",
                            f"<script>alert('{CALENDAR_INJECTION_CANARY}')</script>",
                        ]
                        if event.id == INJECTION_EVENT_ID
                        else [f"Prep for {event.title}"]
                    ),
                }
                for event in EVENTS
            ],
            "tasks": [
                {
                    "task_id": task.id,
                    "urgency": "high",
                    "verdict": (
                        f"{CALENDAR_INJECTION_MARKER} <b>obey</b>"
                        if task.id == INJECTION_TASK_ID
                        else f"Handle {task.title}"
                    ),
                }
                for task in TASKS
            ],
        }
    )


@pytest.fixture
def mock_client(monkeypatch):
    """A broker client in mock mode, with every Google entry point booby-trapped."""

    def explode(*args, **kwargs):
        raise AssertionError("mock mode must not touch Google or the network")

    monkeypatch.setenv(MOCK_ENV_VAR, "1")
    monkeypatch.setattr(api, "get_read_credentials", explode)
    monkeypatch.setattr(api, "fetch_calendar", explode)
    monkeypatch.setattr(api, "fetch_tasks", explode)
    monkeypatch.setattr(calendar_tasks, "build", explode)
    monkeypatch.setattr(calendar_tasks, "load_credentials", explode)
    monkeypatch.setattr(emails_module, "build", explode)
    return TestClient(app)


# --- 1. the broker's mock routes ----------------------------------------------------------


def test_mock_calendar_returns_a_validated_day(mock_client):
    response = mock_client.get("/calendar", params={"day": "today"})
    assert response.status_code == 200

    body = response.json()
    assert body["day"] == "today"
    assert body["count"] == len(EVENTS) == len(body["events"])
    assert body["degraded"] == []
    # Re-validating the wire JSON is the shape assertion: an extra or missing field fails.
    parsed = CalendarResponse.model_validate(body)
    assert [event.id for event in parsed.events] == [event.id for event in EVENTS]
    for event in body["events"]:
        assert set(event) == set(CalendarEntry.model_fields)


def test_mock_tasks_returns_a_validated_list(mock_client):
    body = mock_client.get("/tasks").json()
    assert body["count"] == len(TASKS)
    assert body["degraded"] == []
    parsed = TasksResponse.model_validate(body)
    assert [task.id for task in parsed.tasks] == [task.id for task in TASKS]
    for task in body["tasks"]:
        assert set(task) == set(TaskEntry.model_fields)


def test_mock_calendar_includes_the_injection_fixture(mock_client):
    body = mock_client.get("/calendar", params={"day": "today"}).json()
    hostile = next(e for e in body["events"] if e["id"] == INJECTION_EVENT_ID)
    # Both markers must survive verbatim: the regression tests grep for these exact strings.
    assert CALENDAR_INJECTION_MARKER in hostile["title"]
    assert CALENDAR_INJECTION_CANARY in hostile["description"]


def test_a_bad_day_is_rejected_before_any_fetch(mock_client):
    assert mock_client.get("/calendar", params={"day": "banana"}).status_code == 422


def test_day_aliases_resolve(mock_client):
    from datetime import date, timedelta

    body = mock_client.get("/calendar", params={"day": "tomorrow"}).json()
    assert body["day"] == "tomorrow"
    assert body["date"] == (date.today() + timedelta(days=1)).isoformat()
    assert mock_client.get("/calendar", params={"day": "2026-07-24"}).json()["date"] == (
        "2026-07-24"
    )


def test_health_still_reports_mock_mode(mock_client):
    assert mock_client.get("/health").json()["mode"] == "mock"


# --- 2. scope separation ------------------------------------------------------------------


def test_read_slot_gained_calendar_and_stayed_read_only():
    assert CALENDAR_READONLY in READ_SLOT.scopes
    assert GMAIL_READONLY in READ_SLOT.scopes
    assert not set(READ_SLOT.requested_scopes) & set(WRITE_SCOPES)
    # Every scope on the read path is a read scope, structurally.
    for scope in READ_SLOT.requested_scopes:
        assert "readonly" in scope or "userinfo" in scope


def test_tasks_is_optional_and_send_is_untouched():
    assert READ_SLOT.optional_scopes == (TASKS_READONLY,)
    assert TASKS_READONLY not in READ_SLOT.scopes  # never required
    assert TASKS_READONLY in READ_SLOT.requested_scopes
    # The send slot is exactly what it was.
    assert GMAIL_SEND in SEND_SLOT.scopes
    assert CALENDAR_READONLY not in SEND_SLOT.scopes
    assert TASKS_READONLY not in SEND_SLOT.scopes
    assert SEND_SLOT.optional_scopes == ()


def test_a_token_without_calendar_is_refused_and_says_why():
    """The re-consent path: adding a required scope invalidates the stored token."""
    legacy = {"scopes": [*google_auth.IDENTITY_SCOPES, GMAIL_READONLY]}
    problem = token_problem(READ_SLOT, legacy)
    assert CALENDAR_READONLY in problem
    assert "missing required scope" in problem


def test_a_token_without_the_optional_tasks_scope_is_still_usable():
    granted = {"scopes": list(READ_SLOT.scopes)}
    assert token_problem(READ_SLOT, granted) == ""
    assert has_scope(FakeCredentials(*READ_SLOT.scopes), CALENDAR_READONLY) is True
    assert has_scope(FakeCredentials(*READ_SLOT.scopes), TASKS_READONLY) is False


def test_an_over_scoped_token_is_still_refused_on_the_read_path():
    over = {"scopes": [*READ_SLOT.scopes, GMAIL_SEND]}
    assert "must never hold" in token_problem(READ_SLOT, over)


def test_the_broker_reaches_no_write_capability_through_calendar():
    assert not hasattr(calendar_tasks, "send_email")
    assert GMAIL_SEND not in calendar_tasks.READ_SCOPES
    assert not set(calendar_tasks.READ_SCOPES) & set(WRITE_SCOPES)


# --- 3. the calendar injection dead-ends ---------------------------------------------------


def test_calendar_injection_reaches_the_briefing_as_inert_text_and_nowhere_else(tmp_path):
    """The phase's security test: comply with the hostile event, and it still dead-ends."""
    backend = ScriptedBackend(CompletionResponse(text=_compliant_day_plan()))
    plan = day_plan_module.build_day_plan(
        EVENTS, TASKS, day="Friday", config=CONFIG, backend=backend
    )

    briefing = Briefing(date="Friday")
    briefing.calendar = plan.calendar
    briefing.tasks = plan.tasks
    html = render_briefing_html(briefing)

    # (a) The injected text appears — in the briefing, escaped, as visible evidence.
    assert CALENDAR_INJECTION_MARKER in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>obey</b>" not in html

    # (b) The section is structured data: every *fact* comes from the broker, not the model.
    hostile = next(e for e in EVENTS if e.id == INJECTION_EVENT_ID)
    rendered = next(e for e in plan.calendar.events if e.title == hostile.title)
    assert rendered.start == hostile.start
    assert rendered.attendees == hostile.attendees

    # (c) The agent's result is calendar-tainted, so it cannot become another prompt.
    result = run_agent(
        day_plan_module.calendar_agent_spec(CONFIG),
        [PromptPart.tainted(json.dumps([e.model_dump() for e in EVENTS]), {TAINT_CALENDAR})],
        backend=ScriptedBackend(CompletionResponse(text=_compliant_day_plan())),
    )
    assert result.taint == frozenset({TAINT_CALENDAR})

    from runner.tools_project import WorktreeScope

    scope = WorktreeScope(tmp_path)
    with pytest.raises(TaintViolation) as exc:
        run_agent(
            project_agent(CONFIG, scope=scope, goal="Fix the CI failure."),
            [result.as_prompt_part()],
            backend=ScriptedBackend(),
        )
    assert "calendar" in str(exc.value)

    # (d) And a normal project-agent run never sees the hostile event at all.
    project_backend = ScriptedBackend(CompletionResponse(text="done"))
    run_agent(
        project_agent(CONFIG, scope=scope, goal="Fix the CI failure."),
        [PromptPart.trusted("Work on tonight's goal.", label="task")],
        backend=project_backend,
    )
    assert CALENDAR_INJECTION_MARKER not in project_backend.sent_text
    assert CALENDAR_INJECTION_CANARY not in project_backend.sent_text


def test_the_calendar_agent_cannot_reach_the_inbox():
    """A hostile invite asking for your email finds no tool to do it with."""
    spec = day_plan_module.calendar_agent_spec(CONFIG)
    assert spec.tools.names == {"read_calendar", "read_tasks", "add_to_briefing"}
    with pytest.raises(ToolScopeError):
        run_agent(
            spec,
            [PromptPart.tainted("day", {TAINT_CALENDAR})],
            backend=ScriptedBackend(_call("read_emails", {"since": "8h"})),
        )


def test_the_calendar_agent_has_no_shell():
    names = calendar_toolset(BriefingSink()).names
    assert not any(name in names for name in ("bash", "read_file", "write_file"))


def test_the_calendar_agent_refuses_email_tainted_input():
    spec = calendar_agent(CONFIG, system_prompt="s")
    with pytest.raises(TaintViolation):
        run_agent(spec, [PromptPart.tainted("inbox", {"email"})], backend=ScriptedBackend())


def test_calendar_tools_label_everything_they_return():
    registry = calendar_toolset(BriefingSink())
    assert registry.taint_of("read_calendar") == frozenset({TAINT_CALENDAR})
    assert registry.taint_of("read_tasks") == frozenset({TAINT_CALENDAR})


def test_a_contributed_section_carries_the_agents_taint_not_the_models():
    sink = BriefingSink()
    spec = calendar_agent(
        CONFIG, system_prompt="s", tools=calendar_toolset(sink), max_steps=3
    )
    backend = ScriptedBackend(
        _call("add_to_briefing", {"title": "Day", "summary": "Busy.", "items": ["a"]}),
        CompletionResponse(text="done"),
    )
    run_agent(spec, [PromptPart.tainted("day", {TAINT_CALENDAR})], backend=backend)

    assert [section.taint for section in sink.sections] == [["calendar"]]
    assert sink.sections[0].agent == "calendar_agent"


def test_the_model_cannot_invent_an_event_or_move_one():
    """Prep notes for an id we never sent are dropped; facts come from the broker."""
    raw = json.dumps(
        {
            "notes": [],
            "events": [
                {"event_id": "does-not-exist", "prep_notes": ["book a flight"]},
                {"event_id": EVENTS[0].id, "prep_notes": ["read the deck"]},
            ],
            "tasks": [{"task_id": "nope", "urgency": "critical", "verdict": "panic"}],
        }
    )
    plan = day_plan_module.parse_day_plan(raw, EVENTS, TASKS, day="Friday")

    assert len(plan.calendar.events) == len(EVENTS)  # nothing invented, nothing lost
    assert plan.calendar.events[0].prep_notes == ["read the deck"]
    assert plan.calendar.events[0].start == EVENTS[0].start
    assert any("unknown event id" in note for note in plan.degraded)
    assert any("unknown task id" in note for note in plan.degraded)
    # An untriaged task still appears, at the default urgency, rather than vanishing.
    assert len(plan.tasks.items) == len(TASKS)
    assert plan.tasks.items[0].urgency is Urgency.NORMAL


def test_unparsable_output_still_produces_both_sections():
    plan = day_plan_module.parse_day_plan("not json at all", EVENTS, TASKS, day="Friday")
    assert len(plan.calendar.events) == len(EVENTS)
    assert len(plan.tasks.items) == len(TASKS)
    assert any("Could not parse" in note for note in plan.degraded)


# --- 4. graceful degradation ---------------------------------------------------------------


def test_calendar_without_the_scope_degrades_instead_of_calling_google(monkeypatch):
    monkeypatch.setattr(
        calendar_tasks, "build", lambda *a, **k: pytest.fail("built a Google client")
    )
    response = calendar_tasks.fetch_calendar(FakeCredentials(GMAIL_READONLY), day="today")
    assert response.events == []
    assert any("calendar.readonly" in note for note in response.degraded)


def test_tasks_without_the_optional_scope_degrades_kindly(monkeypatch):
    monkeypatch.setattr(
        calendar_tasks, "build", lambda *a, **k: pytest.fail("built a Google client")
    )
    response = calendar_tasks.fetch_tasks(FakeCredentials(CALENDAR_READONLY))
    assert response.tasks == []
    assert len(response.degraded) == 1
    assert "optional" in response.degraded[0]


def test_a_google_api_error_degrades_rather_than_raising(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("HttpError 503")

    monkeypatch.setattr(calendar_tasks, "build", explode)
    calendar = calendar_tasks.fetch_calendar(FakeCredentials(CALENDAR_READONLY))
    tasks = calendar_tasks.fetch_tasks(FakeCredentials(TASKS_READONLY))
    assert calendar.events == [] and "503" in calendar.degraded[0]
    assert tasks.tasks == [] and "503" in tasks.degraded[0]


def test_an_unreachable_broker_costs_the_sections_not_the_night(monkeypatch):
    class DeadClient:
        @classmethod
        def from_env(cls):
            raise OSError("connection refused")

    monkeypatch.setattr("broker_client.BrokerClient", DeadClient)
    events, tasks, degraded = load_day_impl(mock=False)
    assert events == [] and tasks == []
    assert any("broker" in note for note in degraded)


def test_an_empty_day_never_calls_the_model():
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("paid a model to confirm an empty day")

    plan = day_plan_module.build_day_plan(
        [], [], day="Friday", config=CONFIG, backend=explode, degraded=["Tasks unavailable"]
    )
    assert plan.calendar.events == [] and plan.tasks.items == []
    assert plan.degraded == ["Tasks unavailable"]


def test_a_pre_model_degradation_reaches_the_plan():
    backend = ScriptedBackend(CompletionResponse(text=_compliant_day_plan()))
    plan = day_plan_module.build_day_plan(
        EVENTS, TASKS, day="Friday", config=CONFIG, backend=backend,
        degraded=["Tasks unavailable: not granted."],
    )
    assert plan.degraded[0] == "Tasks unavailable: not granted."


def test_the_nightly_run_populates_both_sections_and_surfaces_failures(tmp_path, monkeypatch):
    """End to end: a mock night writes a briefing with a real day and a real Failures line."""
    from orchestrator import caffeinate as caffeinate_mod
    from orchestrator import power

    monkeypatch.setattr(
        nightly,
        "keep_awake",
        lambda **kwargs: caffeinate_mod.keep_awake(enabled=False, spawn=lambda cmd: None),
    )
    monkeypatch.setattr(nightly, "_load_emails", lambda *a, **k: [])
    monkeypatch.setattr(
        nightly, "_load_day", lambda **k: (EVENTS, TASKS, ["Tasks unavailable: not granted."])
    )
    monkeypatch.setattr(
        day_plan_module,
        "_call_model",
        lambda *a, **k: _compliant_day_plan(),
    )

    out = tmp_path / "briefing.html"
    result = nightly.run_night(
        CONFIG,
        out=out,
        mock=True,
        send=False,
        projects=False,
        caffeinate=False,
        power_state=power.read_power_state(
            pmset_text="Now drawing from 'AC Power'", clamshell_text="", displays_text=""
        ),
    )

    html = out.read_text(encoding="utf-8")
    assert "calendar_agent" in result.stages
    # The heading is escaped like everything else the renderer emits.
    assert "Today&#x27;s calendar" in html and "Task triage" in html
    # Both sections carry real data rather than the "Nothing to report" placeholder.
    assert "Standup" in html and "Pay invoice #4471" in html
    assert "Prep" in html
    # The unavailable optional source is surfaced, not swallowed.
    assert "Tasks unavailable: not granted." in html
    assert result.failures >= 1
