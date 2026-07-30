"""Scope separation: the read path must never be able to send mail.

This is the security-critical half of Phase 1. The broker holds a `gmail.readonly`
credential in its own Keychain slot; sending is a separate host-only credential that
the broker's module graph cannot reach.
"""

import json

import pytest
from google.auth.exceptions import RefreshError

import api
import emails as emails_module
import google_auth
import send_emails
from google_auth import (
    GMAIL_READONLY,
    GMAIL_SEND,
    KEYRING_SERVICE,
    READ_SLOT,
    SEND_SLOT,
    WRITE_SCOPES,
    load_credentials,
)


def test_read_slot_requests_only_read_scopes():
    assert GMAIL_READONLY in READ_SLOT.scopes
    assert not set(READ_SLOT.scopes) & set(WRITE_SCOPES)


def test_send_scope_lives_only_on_the_send_slot():
    assert GMAIL_SEND in SEND_SLOT.scopes
    assert GMAIL_SEND not in READ_SLOT.scopes


def test_slots_have_distinct_keychain_entries():
    assert READ_SLOT.keyring_username != SEND_SLOT.keyring_username
    assert READ_SLOT.keyring_username == "google-oauth:read"
    assert SEND_SLOT.keyring_username == "google-oauth:send"
    assert KEYRING_SERVICE == "NightShift"


def test_broker_module_cannot_reach_the_send_capability():
    """`api.py` must not import the sending module, directly or transitively."""
    assert not hasattr(api, "send_email")
    assert not hasattr(api, "send_to_self")
    assert not hasattr(api, "get_send_credentials")
    # The read module the broker *does* import carries no sending helper either.
    assert not hasattr(emails_module, "send_email")
    assert GMAIL_SEND not in emails_module.READ_SCOPES
    # ...and the sending helpers only exist behind the send slot.
    assert GMAIL_SEND in send_emails.SEND_SCOPES


def test_over_scoped_stored_token_is_refused_on_the_read_path(monkeypatch, tmp_path):
    """A legacy send/modify-capable token must never be adopted by the read slot."""
    token_file = tmp_path / "read.json"
    token_file.write_text(
        json.dumps(
            {
                "token": "x",
                "refresh_token": "y",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": [*READ_SLOT.scopes, GMAIL_SEND],
            }
        )
    )
    monkeypatch.setenv(READ_SLOT.file_override_env, str(token_file))
    monkeypatch.setattr(google_auth, "migrate_legacy_token_file", lambda *a, **k: False)

    # interactive=False so a refusal surfaces as an error instead of a browser popup.
    with pytest.raises(RuntimeError, match="No usable Google credential"):
        load_credentials(READ_SLOT, interactive=False)

    # The offending token is discarded rather than left lying around.
    assert not token_file.exists()


def test_correctly_scoped_token_is_accepted(monkeypatch, tmp_path):
    token_file = tmp_path / "read.json"
    token_file.write_text(
        json.dumps(
            {
                "token": "x",
                "refresh_token": "y",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": list(READ_SLOT.scopes),
                # Far-future expiry so the credential is valid without a refresh call.
                "expiry": "2999-01-01T00:00:00Z",
            }
        )
    )
    monkeypatch.setenv(READ_SLOT.file_override_env, str(token_file))
    monkeypatch.setattr(google_auth, "migrate_legacy_token_file", lambda *a, **k: False)

    creds = load_credentials(READ_SLOT, interactive=False)
    assert set(creds.scopes) == set(READ_SLOT.scopes)
    assert GMAIL_SEND not in creds.scopes


def test_invalid_refresh_grant_is_discarded_and_reauthorised(monkeypatch, tmp_path):
    token_file = tmp_path / "send.json"
    token_file.write_text(json.dumps({"scopes": list(SEND_SLOT.scopes)}))
    monkeypatch.setenv(SEND_SLOT.file_override_env, str(token_file))
    monkeypatch.setattr(google_auth, "migrate_legacy_token_file", lambda *a, **k: False)

    class ExpiredCredentials:
        valid = False
        expired = True
        refresh_token = "revoked"

        def refresh(self, request):
            raise RefreshError("invalid_grant: Bad Request", {"error": "invalid_grant"})

    fresh = type("FreshCredentials", (), {"scopes": SEND_SLOT.scopes})()
    monkeypatch.setattr(
        google_auth.Credentials,
        "from_authorized_user_info",
        lambda *a, **k: ExpiredCredentials(),
    )
    monkeypatch.setattr(google_auth, "_client_config", lambda: {})
    monkeypatch.setattr(
        google_auth.InstalledAppFlow,
        "from_client_config",
        lambda *a, **k: type(
            "Flow", (), {"run_local_server": lambda self, **kwargs: fresh}
        )(),
    )
    monkeypatch.setattr(google_auth, "write_token", lambda slot, creds: None)

    assert load_credentials(SEND_SLOT) is fresh
    assert not token_file.exists()


def test_invalid_refresh_grant_in_noninteractive_run_has_actionable_error(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "send.json"
    token_file.write_text(json.dumps({"scopes": list(SEND_SLOT.scopes)}))
    monkeypatch.setenv(SEND_SLOT.file_override_env, str(token_file))
    monkeypatch.setattr(google_auth, "migrate_legacy_token_file", lambda *a, **k: False)

    class ExpiredCredentials:
        valid = False
        expired = True
        refresh_token = "revoked"

        def refresh(self, request):
            raise RefreshError("invalid_grant: Bad Request", {"error": "invalid_grant"})

    monkeypatch.setattr(
        google_auth.Credentials,
        "from_authorized_user_info",
        lambda *a, **k: ExpiredCredentials(),
    )

    with pytest.raises(RuntimeError, match=r"google_auth\.py authorise send"):
        load_credentials(SEND_SLOT, interactive=False)

    assert not token_file.exists()


def test_legacy_token_file_migrates_to_the_send_slot(monkeypatch, tmp_path):
    """A pre-Phase-1 `token.json` lands in the Keychain (send slot) and is deleted."""
    legacy = tmp_path / "token.json"
    legacy.write_text(
        json.dumps(
            {
                "token": "x",
                "refresh_token": "y",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": [GMAIL_SEND, "https://www.googleapis.com/auth/gmail.modify"],
            }
        )
    )

    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        google_auth.keyring,
        "set_password",
        lambda service, user, value: stored.__setitem__((service, user), value),
    )
    monkeypatch.setattr(
        google_auth.keyring,
        "get_password",
        lambda service, user: stored.get((service, user)),
    )

    assert google_auth.migrate_legacy_token_file(legacy) is True
    assert not legacy.exists()
    assert (KEYRING_SERVICE, SEND_SLOT.keyring_username) in stored
    # Crucially: nothing was written to the read slot.
    assert (KEYRING_SERVICE, READ_SLOT.keyring_username) not in stored
