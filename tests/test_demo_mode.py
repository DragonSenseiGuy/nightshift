"""Demo mode is the version of NightShift a stranger runs, so it gets the strictest test.

Everything else in this suite protects the user's data. This file protects someone who has
never seen the project, double-clicked a downloaded app, and is about to press a button
labelled "Approve". Three properties, in the order they matter:

- **Nothing it can do reaches the world.** The demo queue's effects are replaced, so an
  approve moves a row and returns a sentence; `send_emails` and `gitops` are never called,
  and demo mode cannot even start a night.
- **Nothing it touches is real.** Briefing, queue and run history all live under the demo
  root, which is rebuilt on every launch. The real approvals database and the real briefing
  are somewhere else and stay untouched.
- **It shows the honest picture.** The prompt-injection email and the hostile calendar
  invite are in the canned night, escaped, and the Failures section is populated. A demo
  that quietly served the happy path would be advertising a system we did not build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.demo import DemoService, demo_service, inert_effects, seed
from app.service import ServiceError
from approvals import ApprovalQueue
from fixtures.demo_night import DEMO_NIGHT_ID, DEMO_TRANSCRIPT_ID
from fixtures.mock_calendar import CALENDAR_INJECTION_MARKER
from fixtures.mock_emails import INJECTION_CANARY, INJECTION_MARKER
from models import ActionStatus, ActionType
from transcripts import TranscriptStore

TOKEN = "demo-test-token"


@pytest.fixture
def env(tmp_path: Path):
    return seed(tmp_path / "demo")


def test_seed_builds_a_whole_morning(env) -> None:
    assert env.briefing_path.is_file()
    assert env.queue_path.is_file()
    assert env.transcripts_path.is_file()

    pending = env.queue.pending()
    assert {action.type for action in pending} == {
        ActionType.DRAFT_REPLY,
        ActionType.MERGE_BRANCH,
    }
    # Two drafts and a merge: enough to show both shapes of confirmation dialog.
    assert len(pending) == 3

    store = TranscriptStore(env.transcripts_path)
    assert store.night(DEMO_NIGHT_ID).briefing_path == str(env.briefing_path)
    assert {run.agent for run in store.runs(night_id=DEMO_NIGHT_ID)} == {
        "email_agent",
        "calendar_agent",
        "project_agent",
    }
    assert store.get(DEMO_TRANSCRIPT_ID).transcript, "the project run has tool calls to read"


def test_seed_is_idempotent_and_self_contained(tmp_path: Path) -> None:
    """A reviewer who approved everything must still find a full queue next launch."""
    root = tmp_path / "demo"
    first = seed(root)
    first.queue.approve(first.queue.pending()[0].id, by="test")
    assert len(first.queue.pending()) == 2

    second = seed(root)
    assert len(second.queue.pending()) == 3
    # And it wrote nowhere else: the whole demo is one directory.
    assert {path.parent for path in root.rglob("*") if path.is_file()} <= {root}


def test_approving_in_demo_mode_sends_nothing(env) -> None:
    draft = next(a for a in env.queue.pending() if a.type is ActionType.DRAFT_REPLY)
    queue = env.queue
    done = queue.approve(draft.id, by="test")

    assert done.status is ActionStatus.DONE
    assert "Demo mode" in (done.result or "")
    assert "nothing was sent" in (done.result or "")


def test_inert_effects_cover_every_action_type() -> None:
    """A type without a demo effect would fall through to the *real* executor."""
    assert set(inert_effects()) == set(ActionType)


def test_demo_never_reaches_the_real_queue(env, tmp_path: Path, monkeypatch) -> None:
    """The demo service reads the demo database, whatever the environment says."""
    real = tmp_path / "real.db"
    monkeypatch.setenv("NIGHTSHIFT_APPROVALS_DB", str(real))
    service = demo_service(env)

    assert len(service.previews()) == 3
    assert not real.exists(), "demo mode must not open the real approvals database"


def test_demo_mode_refuses_to_start_a_night(env) -> None:
    service = demo_service(env)
    assert isinstance(service, DemoService)
    with pytest.raises(ServiceError) as exc:
        service.run_now()
    # The refusal has to explain itself: "nothing happened" on a demo button is a bug report.
    assert "Google account" in str(exc.value)


def test_the_canned_briefing_shows_the_attacks_as_inert_text(env) -> None:
    html = env.briefing_path.read_text(encoding="utf-8")

    # Present, so the demo tells the truth about what a night contains …
    assert INJECTION_MARKER in html
    assert CALENDAR_INJECTION_MARKER in html
    # … and inert: the payload's canary was never acted on, and nothing was smuggled in as
    # markup (the renderer escapes every model- and email-derived string).
    assert INJECTION_CANARY not in html
    assert "<script" not in html.lower()
    assert "Failures" in html


def test_the_api_serves_the_demo_environment(env) -> None:
    """End to end over HTTP: what the SwiftUI client actually renders on first launch."""
    app = create_app(
        demo_service(env), token=TOKEN, store=TranscriptStore(env.transcripts_path)
    )
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {TOKEN}"}

    state = client.get("/state", headers=auth).json()
    assert state["pending"] == 3
    assert state["briefing_available"] is True

    actions = client.get("/actions", headers=auth).json()
    # The effect sentence is the daemon's, in demo mode as in a real one.
    assert any(action["effect"].startswith("Approving SENDS") for action in actions)
    assert any(action["effect"].startswith("Approving MERGES") for action in actions)
    assert any(action["tainted"] for action in actions)

    assert client.post("/run", headers=auth).status_code == 409
    assert client.get("/briefing", headers=auth).status_code == 200
    assert client.get(f"/runs/{DEMO_TRANSCRIPT_ID}", headers=auth).json()["agent"] == (
        "project_agent"
    )


def test_a_real_queue_is_still_armed(tmp_path: Path) -> None:
    """The disarming is per-queue and must not leak into the module defaults."""
    from approvals import DEFAULT_EFFECTS

    ApprovalQueue(tmp_path / "demo.db", effects=inert_effects())
    assert DEFAULT_EFFECTS[ActionType.DRAFT_REPLY].__module__ == "approvals"
