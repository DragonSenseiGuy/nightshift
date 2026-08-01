"""The UI daemon surface (Phase 17), tested without a SwiftUI client.

`app/api.py` is the wire that lets a native client be a *client*: it serves exactly the
models `app/service.py` computes. The interesting properties are therefore not "does JSON
come back" but the ones the Swift code is entitled to rely on, and the ones a second
write-capable surface has to earn:

- **the token actually gates it.** This surface can send mail and merge branches, and
  loopback is not an authentication boundary on a shared Mac. Every route but `/health`
  must 401 without the bearer token;
- **approving is still the only thing that fires an effect**, and it fires exactly once —
  the same rule the queue enforces, asserted from the HTTP side of it;
- **the effect sentence crosses the wire**, because a client that had to compose it would
  be free to compose it differently (security rule 3);
- **the run list is not the full transcript.** A night of conversations is megabytes and a
  list view needs none of it;
- **a transcript keeps its taint labels**, so the viewer cannot present untrusted text as
  the agent's own trustworthy words.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app, default_token_path, ensure_token
from app.service import NightShiftService
from approvals import ApprovalQueue
from config import StandingInstructions
from models import Action, ActionStatus, ActionType, DraftReplyPayload, MergeBranchPayload
from orchestrator.power import PowerState
from runner.observe import AgentRunRecord
from runner.tools import ToolCallRecord
from transcripts import NightOutcome, TranscriptStore

TOKEN = "test-token-not-a-real-secret"
ON_AC = PowerState(on_ac=True, battery_percent=99)


@pytest.fixture
def sent() -> list[str]:
    """Every effect that actually fired. Empty is the default assertion."""
    return []


@pytest.fixture
def queue(tmp_path: Path, sent: list[str]) -> ApprovalQueue:
    def record(action: Action) -> str:
        sent.append(action.id)
        return "recorded"

    return ApprovalQueue(tmp_path / "approvals.db", effects={t: record for t in ActionType})


@pytest.fixture
def store(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "transcripts.db")


@pytest.fixture
def service(tmp_path: Path, queue: ApprovalQueue) -> NightShiftService:
    return NightShiftService(
        queue=queue,
        briefing_path=tmp_path / "briefing.html",
        config_loader=lambda _path: StandingInstructions(),
        spawn=lambda argv, *, log_path: _FakeProcess(argv, log_path),
        power_reader=lambda: ON_AC,
        opener=lambda url: True,
        clock=lambda: datetime(2026, 7, 24, 12, 0),
        run_log=tmp_path / "run.log",
        relaunch_state=tmp_path / "relaunch.json",
    )


class _FakeProcess:
    """A spawned night that never finishes, so `run_now` looks the way it does at 3am."""

    def __init__(self, argv, log_path) -> None:
        self.argv = list(argv)
        self.log_path = log_path

    def poll(self):
        return None


@pytest.fixture
def client(service: NightShiftService, store: TranscriptStore) -> TestClient:
    return TestClient(create_app(service, token=TOKEN, store=store))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def enqueue_draft(queue: ApprovalQueue, *, to: str = "sam@example.com") -> Action:
    return queue.enqueue(
        ActionType.DRAFT_REPLY,
        DraftReplyPayload(email_id="m1", to=to, subject="Re: launch", body="Sounds good."),
        origin="email_agent",
        taint=["email"],
        summary=f"Reply to {to}",
    )


# --------------------------------------------------------------------------------------
# The token
# --------------------------------------------------------------------------------------


def test_every_route_but_health_needs_the_token(client: TestClient, queue: ApprovalQueue):
    action = enqueue_draft(queue)
    assert client.get("/health").status_code == 200

    for method, path in (
        ("get", "/state"),
        ("get", "/actions"),
        ("get", "/nights"),
        ("get", "/runs"),
        ("post", f"/actions/{action.id}/approve"),
        ("post", f"/actions/{action.id}/reject"),
        ("post", "/run"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path} answered unauthenticated"


def test_a_wrong_token_cannot_approve(client: TestClient, queue, sent):
    action = enqueue_draft(queue)
    response = client.post(
        f"/actions/{action.id}/approve", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401
    assert sent == []  # and nothing was sent on the way to being refused
    assert queue.get(action.id).status is ActionStatus.PENDING


def test_the_token_file_is_private_and_stable(tmp_path: Path):
    path = tmp_path / "ui-token"
    token = ensure_token(path)
    assert len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Re-reading returns the same secret: a client that re-reads must not be locked out by
    # a daemon that merely restarted.
    assert ensure_token(path) == token


def test_a_world_readable_token_is_replaced_not_trusted(tmp_path: Path):
    path = tmp_path / "ui-token"
    first = ensure_token(path)
    os.chmod(path, 0o644)
    second = ensure_token(path)
    assert second != first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_token_path_is_state_not_run_output(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_UI_TOKEN_FILE", raising=False)
    path = default_token_path()
    assert path.parent.name == "NightShift"
    assert "out" not in path.parts  # `out/` is regenerated nightly; this must survive


# --------------------------------------------------------------------------------------
# State and previews
# --------------------------------------------------------------------------------------


def test_state_is_the_services_state(client: TestClient, queue: ApprovalQueue):
    enqueue_draft(queue)
    body = client.get("/state", headers=auth()).json()
    assert body["status"] == "attention"
    assert body["pending"] == 1
    assert "1 action" in body["summary"]
    # The icon is decided daemon-side so both clients show the same one.
    assert body["icon"]


def test_previews_carry_the_effect_sentence_and_the_taint(client: TestClient, queue):
    enqueue_draft(queue, to="kim@example.com")
    [preview] = client.get("/actions", headers=auth()).json()
    assert preview["type"] == "draft_reply"
    assert "SENDS" in preview["effect"] and "kim@example.com" in preview["effect"]
    assert preview["tainted"] is True
    assert preview["detail"] == "Sounds good."


def test_a_merge_preview_names_both_branches(client: TestClient, queue: ApprovalQueue):
    queue.enqueue(
        ActionType.MERGE_BRANCH,
        MergeBranchPayload(
            project="nightshift", branch="agent/2026-07-24", into="main", diff_path="out/d.diff"
        ),
        origin="nightly_project",
        summary="Merge tonight's work",
    )
    [preview] = client.get("/actions", headers=auth()).json()
    assert "MERGES" in preview["effect"]
    assert "agent/2026-07-24" in preview["effect"] and "main" in preview["effect"]


# --------------------------------------------------------------------------------------
# Approving — the only place an effect happens
# --------------------------------------------------------------------------------------


def test_approve_fires_the_effect_once(client: TestClient, queue: ApprovalQueue, sent):
    action = enqueue_draft(queue)
    response = client.post(f"/actions/{action.id}/approve", headers=auth())
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert sent == [action.id]

    # A second approve — a double-click, a retried request — must not send twice.
    again = client.post(f"/actions/{action.id}/approve", headers=auth())
    assert again.status_code == 409
    assert sent == [action.id]


def test_listing_and_rejecting_never_fire_an_effect(client: TestClient, queue, sent):
    action = enqueue_draft(queue)
    client.get("/state", headers=auth())
    client.get("/actions", headers=auth())
    response = client.post(
        f"/actions/{action.id}/reject", params={"reason": "not in my voice"}, headers=auth()
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["reason"] == "not in my voice"
    assert sent == []


def test_an_unknown_action_is_a_404(client: TestClient):
    assert client.post("/actions/nope/approve", headers=auth()).status_code == 404


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


def test_run_spawns_and_returns_immediately(client: TestClient):
    body = client.post("/run", headers=auth()).json()
    assert body["phase"] == "running"
    # A second request while it is running is a conflict, not a second night.
    assert client.post("/run", headers=auth()).status_code == 409


def test_the_briefing_is_served_only_once_it_exists(client: TestClient, service):
    assert client.get("/briefing", headers=auth()).status_code == 404
    service.briefing_path.write_text("<html><body>Good morning</body></html>", encoding="utf-8")
    response = client.get("/briefing", headers=auth())
    assert response.status_code == 200
    assert "Good morning" in response.text


# --------------------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------------------


def record(**kwargs) -> AgentRunRecord:
    defaults = dict(
        id="run-1",
        night_id="2026-07-24",
        agent="email_agent",
        model="test/model",
        steps=2,
        taint=["email"],
        text="Two urgent, one draft reply.",
        transcript=[
            ToolCallRecord(
                step=1, tool="read_emails", arguments={"since": "2h"}, result="3 emails",
                taint=["email"],
            )
        ],
        messages=[{"role": "system", "content": "you are"}],
        started_at=datetime(2026, 7, 24, 3, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 24, 3, 1, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return AgentRunRecord(**defaults)


def test_the_run_list_omits_the_conversation(client: TestClient, store: TranscriptStore):
    store.save(record())
    [row] = client.get("/runs", headers=auth()).json()
    assert row["id"] == "run-1"
    assert row["agent"] == "email_agent"
    assert "messages" not in row and "transcript" not in row


def test_one_run_comes_back_whole_with_its_taint(client: TestClient, store: TranscriptStore):
    store.save(record())
    body = client.get("/runs/run-1", headers=auth()).json()
    assert body["taint"] == ["email"]
    assert body["transcript"][0]["tool"] == "read_emails"
    assert body["messages"]


def test_a_replay_keeps_its_untrusted_banner(client: TestClient, store: TranscriptStore):
    store.save(record())
    text = client.get("/runs/run-1/replay", headers=auth()).text
    assert "derived from untrusted sources" in text
    assert "Never paste it into an agent prompt" in text


def test_nights_carry_the_spend(client: TestClient, store: TranscriptStore):
    night = store.start_night("2026-07-24")
    store.save(record(cost_usd=0.25))
    store.finish_night(night.id, outcome=NightOutcome.COMPLETED)
    [row] = client.get("/nights", headers=auth()).json()
    assert row["id"] == "2026-07-24"
    assert row["outcome"] == "completed"
    # `NightRunRecord` carries no cost; the daemon sums the night's agent runs so the
    # client never has to fetch every run to show one number.
    assert row["cost_usd"] == pytest.approx(0.25)


def test_an_unknown_run_is_a_404(client: TestClient):
    assert client.get("/runs/nope", headers=auth()).status_code == 404
