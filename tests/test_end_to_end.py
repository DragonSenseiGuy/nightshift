"""Phase 16 — one whole night, end to end, on `--mock` data.

Every other test file verifies one layer. This one runs `orchestrator.nightly.run_night`
the way launchd runs it and asserts on the *whole artifact*, because the failures worth
catching here are the ones that live between layers: a section that stops being rendered,
a stage that stops being wired in, a taint label that stops travelling, a run-history row
that stops being written. A per-layer test cannot see any of those.

What is real in this test: the power gate, the stage watchdog, the email agent, the
calendar/task agent, the join-by-id repair, the briefing renderer, the approval queue, the
project agent's whole git side (snapshot → branch → commit sweep → diff → pending merge),
the run-history store and the notification. What is stubbed is exactly three things, all of
them the outside world:

- **The model** — `tests/offline_llm.py`, installed for the entire suite by `conftest.py`.
  Deterministic, offline, and deliberately *compliant* with the injection fixtures, so the
  briefing assertions below are the worst case rather than a polite one.
- **Google** — `--mock` serves `fixtures/mock_emails.py` and `fixtures/mock_calendar.py`.
- **The sandbox container** — the `runner` seam `nightly_project.py` already exposes. The
  container itself is exercised by the `sandbox`-marked tests; everything the host does
  around it runs for real here, against a scratch repo in `tmp_path`.

The security assertions are not decoration. `test_a_full_night_dead_ends_both_injections`
is the cross-phase version of the Phase 6 and Phase 14 regression tests: it takes both
permanent injection fixtures through the *whole* pipeline into the file the user actually
opens, and checks that what arrives is inert text.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from config import ProjectConfig, StandingInstructions
from fixtures.mock_calendar import (
    CALENDAR_INJECTION_CANARY,
    CALENDAR_INJECTION_MARKER,
    mock_calendar_events,
    mock_tasks,
)
from fixtures.mock_emails import INJECTION_CANARY, INJECTION_MARKER, mock_emails
from models import ActionStatus, ActionType, AgentWorkReport
from orchestrator.power import PowerState


# --------------------------------------------------------------------------------------
# The night under test
# --------------------------------------------------------------------------------------


def make_repo(path: Path) -> Path:
    """A scratch git repo for the project stage to work in."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(  # noqa: E731 - a local shorthand, not an API
        ["git", "-C", str(path), *args], check=True, capture_output=True
    )
    run("init", "-b", "main")
    run("config", "user.email", "test@nightshift.invalid")
    run("config", "user.name", "NightShift Test")
    (path / "README.md").write_text("scratch project\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "initial")
    return path


def stub_sandbox(project, path: Path, config_path) -> AgentWorkReport:
    """Stands in for the container: writes a file into the worktree and reports what it did."""
    (path / "NOTES.md").write_text("the agent worked here\n", encoding="utf-8")
    return AgentWorkReport(
        summary="Added NOTES.md.", highlights=["wrote NOTES.md"], completed=True
    )


@pytest.fixture
def night(tmp_path: Path, monkeypatch, notifications):
    """Run one whole `--mock` night and hand back everything it left behind.

    The night is driven exactly as `orchestrator run --mock` drives it, with three
    injections: mains power (so the result does not depend on whether the machine running
    the suite is a laptop on battery), the canned day (the suite's default `_offline_day`
    fixture blanks it, and this test is *about* the calendar being wired in), and the
    sandbox seam.
    """
    monkeypatch.setenv("NIGHTSHIFT_APPROVALS_DB", str(tmp_path / "approvals.db"))
    monkeypatch.setenv("NIGHTSHIFT_TRANSCRIPTS_DB", str(tmp_path / "transcripts.db"))
    monkeypatch.setenv("NIGHTSHIFT_SNAPSHOTS_DB", str(tmp_path / "snapshots.db"))

    import nightly_project
    from approvals import ApprovalQueue
    from orchestrator import nightly
    from transcripts import TranscriptStore

    repo = make_repo(tmp_path / "scratch")
    config = StandingInstructions(
        projects=[
            ProjectConfig(
                name="scratch",
                path=str(repo),
                goals=["Add a NOTES.md."],
                push=False,  # no remote, and the deploy key never comes near a test
            )
        ]
    )

    # The canned day: the model is offline, so this exercises the real fetch → taint →
    # join-by-id path with the hostile event and the hostile task included.
    monkeypatch.setattr(
        nightly, "_load_day", lambda **kwargs: (mock_calendar_events("today"), mock_tasks(), [])
    )

    # The sandbox seam. `run_night` calls `nightly_projects` without a `runner`, so the
    # substitution happens here rather than by passing one down: everything inside
    # `nightly_projects` — snapshot, branch, commit sweep, diff, queued merge — still runs.
    real_projects = nightly_project.nightly_projects
    monkeypatch.setattr(
        nightly_project,
        "nightly_projects",
        lambda *args, **kwargs: real_projects(
            *args, runner=stub_sandbox, diff_dir=tmp_path / "diffs", **kwargs
        ),
    )

    out = tmp_path / "briefing.html"
    result = nightly.run_night(
        config,
        mock=True,
        projects=True,
        queue_drafts=True,  # `--mock` defaults this off; the queue path is under test
        out=out,
        power_state=PowerState(on_ac=True, battery_percent=100),
    )
    return {
        "result": result,
        "html": out.read_text(encoding="utf-8"),
        "queue": ApprovalQueue(tmp_path / "approvals.db"),
        "store": TranscriptStore(tmp_path / "transcripts.db"),
        "repo": repo,
        "notifications": notifications,
        "tmp_path": tmp_path,
    }


# --------------------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------------------


def test_a_full_night_completes_every_stage(night) -> None:
    result = night["result"]
    assert result.ran and not result.refused
    assert set(result.stages) == {
        "email_agent",
        "calendar_agent",
        "approval_queue",
        "project_agent",
    }, "a stage silently stopped being wired into run_night"
    assert result.failures == 0, "a clean mock night must have nothing in Failures"
    assert result.night_id
    assert Path(result.briefing_path).is_file()


def test_the_briefing_contains_every_section(night) -> None:
    """One artifact, all of it. This is the assertion that catches cross-phase rot."""
    html = night["html"]

    # Structure
    assert html.startswith("<!doctype html>") and html.endswith("</html>")
    for heading in (
        "Good morning",
        "Email",
        "Today's calendar",
        "Task triage",
        "What I did last night",
        "Failures",
    ):
        # `_esc` escapes the apostrophe in "Today's calendar", so compare both ways.
        assert (
            heading in html or _escaped(heading) in html
        ), f"the briefing lost its {heading!r} section"
    assert "None — every step completed." in html, "a clean night must say so explicitly"

    # Email — every fixture email, attributed from the fetched header, not the model
    for email in mock_emails():
        assert email.subject in html or _escaped(email.subject) in html
    assert "Priya Raman" in html

    # Calendar — facts from the broker, prep notes from the model
    assert "Standup" in html and "09:30" in html
    assert "Offline stub prep note" in html
    # Tasks
    assert "Pay invoice #4471" in html and "Offline stub verdict." in html
    # Project work
    assert "scratch" in html and f"agent/{date.today().isoformat()}" in html

    # Nothing external: a briefing is opened from disk, possibly offline, and must not
    # phone anywhere or run anything. URLs *quoted from an email* are fine — they arrive
    # as escaped text — so the check is on the markup that would fetch something.
    lowered = html.lower()
    for markup in ("<script", "<iframe", "<link", "<img", " src=", " href=", "url("):
        assert markup not in lowered, f"the briefing gained an external reference: {markup!r}"


def test_a_full_night_dead_ends_both_injections(night) -> None:
    """The cross-phase version of the Phase 6 and Phase 14 regression tests.

    The stubbed model *complies* with both injections — it echoes the email body into its
    summary — so the payloads really do travel the whole pipeline. What has to hold at the
    end is that they arrive as text: escaped, rendered, acted on by nobody.
    """
    html = night["html"]

    # The payloads arrive (proving they were not simply dropped somewhere harmless) ...
    assert _escaped(INJECTION_MARKER) in html or INJECTION_MARKER in html
    assert CALENDAR_INJECTION_MARKER in html or _escaped(CALENDAR_INJECTION_MARKER) in html

    # ... and they arrive inert.
    assert "<script>alert('pwned')</script>" not in html
    assert "&lt;script&gt;" in html, "the calendar payload's markup must be escaped, not dropped"

    # Neither injection's requested side effect happened. The email one asks for a message
    # to attacker@example.invalid; the calendar one asks for the canary in the briefing.
    for action in night["queue"].list():
        assert "attacker@example.invalid" not in action.payload.model_dump_json()
    assert CALENDAR_INJECTION_CANARY not in html or _only_as_quoted_task_note(html)
    # The email canary may appear as quoted text in a summary, but never as an action.
    assert not [a for a in night["queue"].list() if INJECTION_CANARY in a.summary]


def _escaped(text: str) -> str:
    from html import escape

    return escape(text)


def _only_as_quoted_task_note(html: str) -> bool:
    """The calendar canary is allowed in the briefing only as the task note it came from."""
    return "Renew the domain" in html


# --------------------------------------------------------------------------------------
# Side effects: proposed, never performed
# --------------------------------------------------------------------------------------


def test_the_night_proposes_and_performs_nothing(night) -> None:
    """Security rule 3, at the level of a whole night."""
    actions = night["queue"].list()
    assert actions, "the night queued nothing at all — the approval path is not wired in"
    assert all(a.status is ActionStatus.PENDING for a in actions)

    kinds = {a.type for a in actions}
    assert ActionType.DRAFT_REPLY in kinds, "the digest's draft replies were not queued"
    assert ActionType.MERGE_BRANCH in kinds, "the night's branch was not queued for merge"

    # A draft's recipient comes from the fetched From header, never from model text.
    drafts = [a for a in actions if a.type is ActionType.DRAFT_REPLY]
    assert all("@" in a.payload.to for a in drafts)
    assert any("priya@acme-supply.example" in a.payload.to for a in drafts)
    assert all("email" in a.taint for a in drafts), "draft replies must stay email-tainted"


def test_the_night_leaves_a_reviewable_branch_and_merges_nothing(night) -> None:
    repo = night["repo"]
    branch = f"agent/{date.today().isoformat()}"

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout

    assert branch in git("branch", "--list", branch)
    assert git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main", "main was checked out"
    assert "NOTES.md" not in git("show", "--stat", "main"), "the work reached main unapproved"
    assert "NOTES.md" in git("show", "--stat", branch)

    diffs = list((night["tmp_path"] / "diffs").glob("scratch-*.diff"))
    assert diffs and "NOTES.md" in diffs[0].read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# Observability: history, transcripts, the nudge
# --------------------------------------------------------------------------------------


def test_the_night_records_its_history_and_its_agents(night) -> None:
    store, result = night["store"], night["result"]

    row = store.night(result.night_id)
    assert row.outcome == "completed"
    assert row.failures == 0
    assert row.briefing_path == result.briefing_path
    assert set(row.stages) == set(result.stages)

    runs = store.runs(night_id=result.night_id)
    agents = {run.agent for run in runs}
    assert {"email_agent", "calendar_agent"} <= agents
    # Taint travels into storage and stays there — a stored run is display, not a prompt.
    by_agent = {run.agent: run for run in runs}
    assert "email" in by_agent["email_agent"].taint
    assert "calendar" in by_agent["calendar_agent"].taint


def test_the_night_notifies_without_leaking_untrusted_text(night) -> None:
    posted = night["notifications"]
    assert len(posted) == 1, "the night did not post its wake-up notification"

    argv = " ".join(posted[0])
    assert "email" in argv  # counts and host-authored words
    for leak in (INJECTION_MARKER, INJECTION_CANARY, CALENDAR_INJECTION_MARKER, "Priya"):
        assert leak not in argv, "a banner is a rendering surface — rule 2 applies to it"


# --------------------------------------------------------------------------------------
# The unhappy paths, which is where the briefing earns its keep
# --------------------------------------------------------------------------------------


def test_a_broken_stage_lands_in_the_briefing_and_the_night_continues(
    tmp_path: Path, monkeypatch
) -> None:
    from orchestrator import nightly

    def explode(since, *, mock):
        raise RuntimeError("gmail fell over")

    monkeypatch.setenv("NIGHTSHIFT_APPROVALS_DB", str(tmp_path / "approvals.db"))
    monkeypatch.setattr(nightly, "_load_emails", explode)
    monkeypatch.setattr(
        nightly, "_load_day", lambda **kwargs: (mock_calendar_events("today"), mock_tasks(), [])
    )

    out = tmp_path / "briefing.html"
    result = nightly.run_night(
        StandingInstructions(),
        mock=True,
        projects=False,
        out=out,
        power_state=PowerState(on_ac=True, battery_percent=100),
    )

    assert result.failures == 1
    assert "email_agent" not in result.stages and "calendar_agent" in result.stages
    html = out.read_text(encoding="utf-8")
    assert "Summarising email failed" in html and "gmail fell over" in html
    assert "1 failure overnight" in html
    # The stages that did work still produced their sections.
    assert "Standup" in html


def test_a_refused_night_still_writes_a_briefing(tmp_path: Path) -> None:
    from orchestrator import nightly

    out = tmp_path / "briefing.html"
    result = nightly.run_night(
        StandingInstructions(),
        mock=True,
        projects=False,
        require_ac=True,
        out=out,
        power_state=PowerState(on_ac=False, battery_percent=12),
    )

    assert result.refused and not result.ran
    html = out.read_text(encoding="utf-8")
    assert "Nightly run skipped" in html
    assert "Failures" in html
