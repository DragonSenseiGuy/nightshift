"""The broker's `--mock` path: canned data, no Gmail, no network.

These tests double as the guard on the mock fixture itself — Phase 6's summary-as-data
regression test is built on the injection email staying exactly where it is.
"""

import pytest
from fastapi.testclient import TestClient

import api
import emails as emails_module
from api import MOCK_ENV_VAR, app
from fixtures.mock_emails import (
    INJECTION_CANARY,
    INJECTION_EMAIL_ID,
    INJECTION_MARKER,
    mock_emails,
)


@pytest.fixture
def mock_client(monkeypatch):
    """A broker client in mock mode, with every Gmail entry point booby-trapped."""

    def explode(*args, **kwargs):
        raise AssertionError("mock mode must not touch Gmail or the network")

    monkeypatch.setenv(MOCK_ENV_VAR, "1")
    monkeypatch.setattr(api, "get_read_credentials", explode)
    monkeypatch.setattr(api, "fetch_emails_last_x_hours", explode)
    monkeypatch.setattr(emails_module, "load_credentials", explode)
    monkeypatch.setattr(emails_module, "build", explode)
    return TestClient(app)


def test_health_reports_mock_mode(mock_client):
    body = mock_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "mock"


def test_mock_returns_canned_set(mock_client):
    response = mock_client.get("/emails", params={"since": "8h"})
    assert response.status_code == 200

    body = response.json()
    expected = mock_emails()
    assert body["since"] == "8h"
    assert body["hours"] == 8.0
    assert body["count"] == len(expected)
    assert [e["id"] for e in body["emails"]] == [e.id for e in expected]
    # Pydantic shape: every email carries the full Email field set.
    for email in body["emails"]:
        assert set(email) == {"id", "sender", "subject", "snippet", "body"}


def test_mock_includes_the_injection_fixture(mock_client):
    body = mock_client.get("/emails", params={"since": "8h"}).json()
    injected = next(e for e in body["emails"] if e["id"] == INJECTION_EMAIL_ID)

    # Both markers must survive verbatim: Phase 6 asserts on these exact strings.
    assert INJECTION_MARKER in injected["body"]
    assert INJECTION_CANARY in injected["body"]


def test_mock_mode_off_by_default(monkeypatch):
    monkeypatch.delenv(MOCK_ENV_VAR, raising=False)
    assert api.mock_enabled() is False
    for falsey in ("", "0", "false", "no"):
        monkeypatch.setenv(MOCK_ENV_VAR, falsey)
        assert api.mock_enabled() is False
    monkeypatch.setenv(MOCK_ENV_VAR, "1")
    assert api.mock_enabled() is True


def test_bad_since_is_rejected_before_any_fetch(mock_client):
    assert mock_client.get("/emails", params={"since": "banana"}).status_code == 422
