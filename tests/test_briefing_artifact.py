"""Phase 7 — the morning briefing artifact.

Three things are worth testing here and they are not the same thing:

1. **Shape** — the briefing model holds every section the spec promised, and its snapshot
   is stable enough that a future refactor can't quietly drop one.
2. **Graceful degradation** — a section with no data says "Nothing to report" rather than
   vanishing, and a *failed* run says so loudly. Silence must never be the failure mode.
3. **Inertness** — everything email-derived arrives in the document escaped. This is the
   summary-as-data rule at its last mile: the artifact is where untrusted text is finally
   allowed to be seen, and it must be seen as text.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api
from briefing import render_briefing_html
from fixtures.mock_emails import INJECTION_CANARY, INJECTION_MARKER, mock_emails
from models import (
    Briefing,
    BriefingSection,
    CalendarEvent,
    CalendarSection,
    DraftReply,
    EmailDigest,
    EmailSummaryItem,
    Failure,
    ProjectSection,
    ProjectWork,
    TaskItem,
    TaskSection,
    Urgency,
)
from runner.tools import ToolError
from runner.tools_email import AddToBriefingArgs, BriefingSink, add_to_briefing_tool

FIXED_TIME = datetime(2026, 7, 24, 3, 30, tzinfo=timezone.utc)


def full_briefing() -> Briefing:
    """A briefing with every section populated — the fixture the renderer is judged on."""
    return Briefing(
        generated_at=FIXED_TIME,
        date="Friday, 24 July 2026",
        email=EmailDigest(
            generated_at=FIXED_TIME,
            since="8h",
            overview="Two things need you before lunch.",
            items=[
                EmailSummaryItem(
                    email_id="e1",
                    sender="billing@vendor.example",
                    subject="Invoice overdue",
                    summary="An invoice is 12 days late.",
                    urgency=Urgency.CRITICAL,
                    needs_reply=True,
                    category="finance",
                    action_items=["Pay or dispute the invoice"],
                    draft_reply=DraftReply(
                        subject="Re: Invoice overdue", body="Paying today.\nThanks."
                    ),
                ),
                EmailSummaryItem(
                    email_id="e2",
                    sender="ci@example.com",
                    subject="Build green",
                    summary="Nothing to do.",
                    urgency=Urgency.LOW,
                ),
            ],
        ),
        calendar=CalendarSection(
            day="Friday, 24 July",
            events=[
                CalendarEvent(
                    start="09:30",
                    end="10:00",
                    title="Standup",
                    location="Zoom",
                    attendees=["a@example.com", "b@example.com"],
                    prep_notes=["Skim yesterday's diff"],
                )
            ],
            notes=["Light day."],
        ),
        tasks=TaskSection(
            items=[
                TaskItem(
                    title="Renew domain",
                    due="today",
                    urgency=Urgency.HIGH,
                    source="Reminders",
                    verdict="Do it before the standup.",
                ),
                TaskItem(title="Read spec", urgency=Urgency.LOW),
            ]
        ),
        projects=ProjectSection(
            projects=[
                ProjectWork(
                    project="nightshift",
                    summary="Wired the briefing artifact.",
                    branch="agent/2026-07-24",
                    diff_path="out/diffs/nightshift-2026-07-24.diff",
                    transcript_id="run-2026-07-24-project",
                    commits=["a1b2c3d wire briefing"],
                    highlights=["All sections render"],
                )
            ]
        ),
        contributed=[
            BriefingSection(
                agent="email_agent",
                title="Inbox notes",
                summary="Two senders are waiting.",
                items=["Vendor wants payment"],
                taint=["email"],
            )
        ],
    )


# ---------------------------------------------------------------------------------------
# Model shape
# ---------------------------------------------------------------------------------------


def test_briefing_model_snapshot_holds_every_promised_section():
    """The spec promises six things. Assert on the *shape*, so dropping one is a failure."""
    dumped = full_briefing().model_dump(mode="json")

    assert set(dumped) == {
        "generated_at",
        "date",
        "email",
        "calendar",
        "tasks",
        "projects",
        "contributed",
        "failures",
    }
    assert dumped["email"]["items"][0]["draft_reply"]["subject"] == "Re: Invoice overdue"
    assert dumped["calendar"]["events"][0]["prep_notes"] == ["Skim yesterday's diff"]
    assert dumped["tasks"]["items"][0]["verdict"] == "Do it before the standup."
    # Phase 9/12 link fields exist now and are carried through untouched.
    assert dumped["projects"]["projects"][0]["branch"] == "agent/2026-07-24"
    assert dumped["projects"]["projects"][0]["diff_path"].endswith(".diff")
    assert dumped["projects"]["projects"][0]["transcript_id"] == "run-2026-07-24-project"
    assert dumped["failures"] == []


def test_briefing_roundtrips_through_json():
    briefing = full_briefing()
    assert Briefing.model_validate(briefing.model_dump(mode="json")) == briefing


def test_add_failure_truncates_rather_than_raising():
    """Losing a failure is worse than showing a clipped one."""
    briefing = Briefing()
    briefing.add_failure("x" * 500, "y" * 900, "z" * 9000)

    failure = briefing.failures[0]
    assert len(failure.stage) == 120
    assert len(failure.message) == 500
    assert len(failure.detail) == 4000
    assert briefing.has_failures


def test_tasks_rank_most_urgent_first():
    section = full_briefing().tasks
    assert [t.urgency for t in section.ranked()] == [Urgency.HIGH, Urgency.LOW]


# ---------------------------------------------------------------------------------------
# Rendering — all sections present
# ---------------------------------------------------------------------------------------


def test_render_full_briefing_contains_every_section():
    html = render_briefing_html(full_briefing())

    for heading in (
        "Email",
        "Today&#x27;s calendar",
        "Task triage",
        "What I did last night",
        "From the agents",
        "Failures",
    ):
        assert heading in html, heading

    assert "Invoice overdue" in html
    assert "Standup" in html
    assert "Renew domain" in html
    assert "agent/2026-07-24" in html
    assert "Inbox notes" in html


def test_render_is_a_self_contained_document():
    """One file: no external CSS, fonts, images, scripts, or network references."""
    html = render_briefing_html(full_briefing())

    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    for forbidden in ("<script", "<link", "<style", "src=", "@import", "http://", "https://"):
        assert forbidden not in html.lower(), forbidden


def test_draft_replies_are_labeled_as_unsent():
    html = render_briefing_html(full_briefing())
    assert "Suggested draft — not sent" in html
    assert "nothing has been sent, merged, or run without your approval" in html


def test_project_links_are_plain_text_not_anchors():
    """A link is a side effect. Nothing an agent wrote becomes a clickable target."""
    html = render_briefing_html(full_briefing())
    assert "out/diffs/nightshift-2026-07-24.diff" in html
    assert "<a " not in html.lower()


# ---------------------------------------------------------------------------------------
# Rendering — degradation
# ---------------------------------------------------------------------------------------


def test_missing_sections_degrade_gracefully_and_stay_visible():
    """An empty briefing still renders every heading — an absent section is never silent."""
    html = render_briefing_html(Briefing(generated_at=FIXED_TIME))

    for heading in ("Email", "Today&#x27;s calendar", "Task triage", "What I did last night"):
        assert heading in html, heading
    assert html.count("Nothing to report.") == 4
    # No failures happened, and the artifact says so rather than omitting the section.
    assert "None — every step completed." in html


def test_contributed_section_is_omitted_when_no_agent_contributed():
    """The one section that may vanish: it has no promised content of its own."""
    html = render_briefing_html(Briefing(generated_at=FIXED_TIME))
    assert "From the agents" not in html


def test_failures_are_surfaced_prominently():
    briefing = Briefing(generated_at=FIXED_TIME)
    briefing.add_failure("project_agent", "Sandbox exited 137", "OOMKilled")
    html = render_briefing_html(briefing)

    assert "1 failure overnight" in html
    assert "project_agent" in html
    assert "Sandbox exited 137" in html
    assert "OOMKilled" in html
    assert "None — every step completed." not in html


def test_summariser_degradation_reaches_the_failures_section():
    briefing = Briefing(
        generated_at=FIXED_TIME,
        email=EmailDigest(degraded=["dropped 1 unparsable item"]),
    )
    html = render_briefing_html(briefing)

    assert "Summariser degraded" in html
    assert "dropped 1 unparsable item" in html
    assert "The summariser degraded on some mail" in html


def test_multiple_failures_are_all_listed():
    briefing = Briefing(generated_at=FIXED_TIME)
    briefing.add_failure("email_agent", "broker timeout")
    briefing.add_failure("calendar_agent", "no credential")
    html = render_briefing_html(briefing)

    assert "2 failures overnight" in html
    assert "broker timeout" in html
    assert "no credential" in html


# ---------------------------------------------------------------------------------------
# Inertness — the point of the artifact
# ---------------------------------------------------------------------------------------


def _injection_briefing() -> Briefing:
    """A briefing built from the hostile fixture, assuming the model complied fully."""
    hostile = next(e for e in mock_emails() if INJECTION_MARKER in e.body)
    return Briefing(
        generated_at=FIXED_TIME,
        email=EmailDigest(
            overview=hostile.body,
            items=[
                EmailSummaryItem(
                    email_id=hostile.id,
                    sender=hostile.sender,
                    subject=hostile.subject,
                    summary=hostile.body,
                    action_items=[INJECTION_CANARY, "<img src=x onerror=alert(1)>"],
                    draft_reply=DraftReply(subject=INJECTION_MARKER, body=hostile.body),
                )
            ],
            degraded=["<script>alert('degraded')</script>"],
        ),
        contributed=[
            BriefingSection(
                agent="email_agent",
                title="<script>alert('title')</script>",
                summary=INJECTION_CANARY,
                items=["<b>bold</b>"],
                taint=["email"],
            )
        ],
        failures=[Failure(stage="email_agent", message="<script>x</script>", detail=INJECTION_CANARY)],
    )


def test_injected_content_renders_as_inert_escaped_text():
    html = render_briefing_html(_injection_briefing())

    # No live markup survives, from any field, in any section. The payload text may well
    # still be present — that is the point — but never as a tag that a browser would open.
    lowered = html.lower()
    assert "<script" not in lowered
    assert "<img" not in lowered
    assert "<b>bold</b>" not in html
    # The handler payload only ever appears inside an escaped, inert tag.
    assert "&lt;img src=x onerror=alert(1)&gt;" in html

    # The text is still *visible* — escaped, not stripped. A human must be able to read
    # what the attacker tried, which is exactly why it has to be inert.
    assert "&lt;script&gt;" in html
    assert INJECTION_MARKER in html
    assert INJECTION_CANARY in html


def test_injection_survives_only_as_data_never_as_attribute_injection():
    html = render_briefing_html(_injection_briefing())
    # Quotes are escaped too, so nothing can break out of an inline style attribute.
    assert 'style="' in html
    assert '"><script' not in html
    assert "&quot;" in html or '"' not in INJECTION_MARKER


# ---------------------------------------------------------------------------------------
# add_to_briefing over the broker
# ---------------------------------------------------------------------------------------


@pytest.fixture
def broker() -> TestClient:
    api.reset_sections()
    client = TestClient(api.app)
    yield client
    api.reset_sections()


def _payload(**overrides) -> dict:
    payload = {
        "agent": "email_agent",
        "title": "Inbox notes",
        "summary": "Two senders waiting.",
        "items": ["Vendor wants payment"],
        "taint": ["email"],
    }
    payload.update(overrides)
    return payload


def test_broker_accepts_and_returns_a_valid_section(broker: TestClient):
    resp = broker.post("/briefing/sections", json=_payload())
    assert resp.status_code == 201

    listed = broker.get("/briefing/sections").json()
    assert len(listed) == 1
    assert listed[0]["title"] == "Inbox notes"
    assert listed[0]["taint"] == ["email"]


def test_broker_rejects_unknown_fields(broker: TestClient):
    """`extra="forbid"`: an agent cannot smuggle a field the renderer doesn't know."""
    resp = broker.post("/briefing/sections", json=_payload(html="<script>x</script>"))
    assert resp.status_code == 422
    assert broker.get("/briefing/sections").json() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"title": "x" * 121},
        {"summary": "x" * 4001},
        {"items": ["x"] * 51},
        {"agent": "a" * 61},
        {"taint": ["t"] * 11},
    ],
)
def test_broker_rejects_oversized_or_empty_sections(broker: TestClient, overrides):
    resp = broker.post("/briefing/sections", json=_payload(**overrides))
    assert resp.status_code == 422
    assert broker.get("/briefing/sections").json() == []


def test_broker_caps_total_sections_loudly(broker: TestClient):
    """A runaway agent is a failure to surface, not something to silently absorb."""
    for i in range(api._MAX_SECTIONS):
        assert broker.post("/briefing/sections", json=_payload(title=f"s{i}")).status_code == 201

    resp = broker.post("/briefing/sections", json=_payload(title="one too many"))
    assert resp.status_code == 429
    assert len(broker.get("/briefing/sections").json()) == api._MAX_SECTIONS


def test_broker_never_reaches_gmail_for_briefing_sections(broker: TestClient, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the briefing path must not touch Gmail")

    monkeypatch.setattr(api, "get_read_credentials", explode)
    monkeypatch.setattr(api, "fetch_emails_last_x_hours", explode)

    assert broker.post("/briefing/sections", json=_payload()).status_code == 201


def test_tool_stamps_agent_taint_not_what_the_model_claims():
    sink = BriefingSink()
    tool = add_to_briefing_tool(sink, agent="email_agent", taint=frozenset({"email"}))
    tool.handler(AddToBriefingArgs(title="Notes", summary="s", items=["a"]))

    assert sink.sections[0].agent == "email_agent"
    assert sink.sections[0].taint == ["email"]


def test_tool_rejects_oversized_section_recoverably():
    """Over-long input is the model's mistake to fix, not a reason to kill the run."""
    sink = BriefingSink()
    tool = add_to_briefing_tool(sink, agent="email_agent", taint=frozenset({"email"}))

    with pytest.raises(ToolError):
        tool.handler(AddToBriefingArgs(title="x" * 200, summary="s"))
    assert sink.sections == []
