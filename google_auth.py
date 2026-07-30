"""Host-side Google OAuth: least-privilege credential slots kept in the Keychain.

Two rules from `CLAUDE.md` shape this module.

*Secrets never enter the sandbox, and the broker only reads.* The broker owns the
read path, so it must never be holding a credential that can send mail. Credentials
are therefore split into independent **slots** — each slot has its own scope set and
its own Keychain entry, so authorising the send slot cannot widen the read slot and
vice versa. A slot also declares scopes it must *never* carry: if a stored token turns
out to be over-scoped (e.g. a legacy `gmail.modify` token landing in the read slot) we
refuse it and force a fresh, narrow authorisation rather than quietly using it.

*Tokens are secrets.* They live in the macOS Keychain via `keyring`, never in a
plaintext `token.json` beside the code. A legacy `token.json` is imported once and
then deleted (see `migrate_legacy_token_file`).

Keychain naming (stable, and what you'd search for in Keychain Access):

    service:  "NightShift"
    account:  "google-oauth:read"   — broker / read-only path
              "google-oauth:send"   — host-only sending path

A slot may also declare **optional** scopes: asked for at consent time, but not required
for the stored token to be usable. Google lets a user untick individual products on the
consent screen, and a declined optional scope must degrade one briefing section rather
than fail a night or re-prompt forever. `has_scope` is the gate callers check.

Re-consent: adding a required scope invalidates every stored token for that slot, on
purpose. Run `uv run python google_auth.py authorise read` to do it at a time you choose;
`... status read` shows what is currently granted.

Escape hatch: `NIGHTSHIFT_TOKEN_FILE_READ` / `NIGHTSHIFT_TOKEN_FILE_SEND` point a slot
at a JSON file instead of the Keychain, for environments with no Keychain. Nothing in
the nightly run uses it: since Phase 4 the broker is a host process behind a Unix
socket, so no container ever holds a Google credential. Leave it unset.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Google automatically adds the "openid" scope to userinfo requests, which makes
# oauthlib raise a "Scope has changed" error. Relax that check.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import keyring
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

KEYRING_SERVICE = "NightShift"
REDIRECT_PORT = 8765

# Pre-Phase-1 token cache. Imported into the Keychain once, then removed.
LEGACY_TOKEN_FILE = Path(__file__).resolve().parent / "token.json"

# Identity scopes both slots need to know *which* account is signed in.
IDENTITY_SCOPES = (
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
TASKS_READONLY = "https://www.googleapis.com/auth/tasks.readonly"

# Anything that can mutate or emit mail. Never allowed in the read slot.
WRITE_SCOPES = (
    GMAIL_SEND,
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    "https://mail.google.com/",  # full-access Gmail scope
)


@dataclass(frozen=True)
class CredentialSlot:
    """One independently-authorised Google credential with a fixed scope budget."""

    name: str
    scopes: tuple[str, ...]
    purpose: str
    # Scopes that must never appear on a token stored in this slot. A stored token
    # carrying one is treated as corrupt and re-authorised from scratch.
    forbidden_scopes: tuple[str, ...] = field(default=())
    # Scopes asked for at consent time but *not* required for the token to be usable.
    # Google's consent screen lets a user untick individual products, and a whole night
    # should not fail because they declined one optional API (Phase 14: Google Tasks).
    # Callers check for these with `has_scope` and degrade when they are absent.
    optional_scopes: tuple[str, ...] = field(default=())

    @property
    def requested_scopes(self) -> tuple[str, ...]:
        """What the OAuth flow asks for: required + optional, in a stable order."""
        return (*self.scopes, *self.optional_scopes)

    @property
    def keyring_username(self) -> str:
        return f"google-oauth:{self.name}"

    @property
    def file_override_env(self) -> str:
        return f"NIGHTSHIFT_TOKEN_FILE_{self.name.upper()}"


# The broker's slot. Read-only by construction: every scope here is a `*.readonly`, and
# `forbidden_scopes` makes that structural rather than a convention.
#
# Google Tasks is *optional* (Phase 14). It is a separate API from Calendar with its own
# consent tickbox, and it is the one a privacy-minded user is most likely to decline — so
# the broker asks for it, notices when it wasn't granted, and reports "Tasks unavailable"
# in the briefing instead of refusing the token and re-prompting every night.
READ_SLOT = CredentialSlot(
    name="read",
    scopes=(*IDENTITY_SCOPES, GMAIL_READONLY, CALENDAR_READONLY),
    purpose="Broker read path: fetch email, calendar and tasks as data.",
    forbidden_scopes=WRITE_SCOPES,
    optional_scopes=(TASKS_READONLY,),
)

# Host-only. Never imported by `api.py`; reachable only from `send_emails.py`.
SEND_SLOT = CredentialSlot(
    name="send",
    scopes=(*IDENTITY_SCOPES, GMAIL_SEND),
    purpose="Host-only sending capability (digest today, approval queue in Phase 8).",
)

SLOTS = {slot.name: slot for slot in (READ_SLOT, SEND_SLOT)}


def _client_config() -> dict:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in your .env file."
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{REDIRECT_PORT}/"],
        }
    }


def _file_override(slot: CredentialSlot) -> Path | None:
    path = os.getenv(slot.file_override_env)
    return Path(path) if path else None


def read_token(slot: CredentialSlot) -> dict | None:
    """Return the stored authorized-user JSON for `slot`, or None if unauthorised."""
    override = _file_override(slot)
    if override is not None:
        if not override.exists():
            return None
        return json.loads(override.read_text())

    raw = keyring.get_password(KEYRING_SERVICE, slot.keyring_username)
    return json.loads(raw) if raw else None


def write_token(slot: CredentialSlot, credentials: Credentials) -> None:
    """Persist `credentials` for `slot` (Keychain by default)."""
    payload = credentials.to_json()
    override = _file_override(slot)
    if override is not None:
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(payload)
        override.chmod(0o600)
        return

    keyring.set_password(KEYRING_SERVICE, slot.keyring_username, payload)


def clear_token(slot: CredentialSlot) -> None:
    """Forget `slot`'s token. Next use re-runs the OAuth flow."""
    override = _file_override(slot)
    if override is not None:
        override.unlink(missing_ok=True)
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, slot.keyring_username)
    except keyring.errors.PasswordDeleteError:
        pass


def _granted_scopes(token: dict) -> set[str]:
    scopes = token.get("scopes")
    if scopes is None:
        # Google's own token JSON sometimes stores a space-delimited "scope" string.
        scopes = token.get("scope", "").split()
    return set(scopes)


def token_problem(slot: CredentialSlot, token: dict) -> str:
    """Why `token` cannot be used for `slot`, as a sentence — or "" if it can.

    Rejects a token that is missing what the slot needs *or* that carries a scope the
    slot forbids. The second half is the security-relevant one: an over-scoped token
    is never silently accepted onto the read path. Optional scopes are ignored here: a
    token without them is perfectly usable, just less capable (see `READ_SLOT`).

    Returning a *reason* rather than a bool is what lets `load_credentials` tell the user
    "Calendar was added, consent once more" instead of silently reopening a browser.
    """
    granted = _granted_scopes(token)
    if not granted:
        return "it records no scopes"
    over = sorted(granted & set(slot.forbidden_scopes))
    if over:
        return f"it carries scope(s) this slot must never hold: {', '.join(over)}"
    missing = sorted(set(slot.scopes) - granted)
    if missing:
        return f"it is missing required scope(s): {', '.join(missing)}"
    return ""


def _token_is_usable(slot: CredentialSlot, token: dict) -> bool:
    """True when the stored token fits the slot's scope budget exactly enough to use."""
    return not token_problem(slot, token)


def has_scope(credentials, scope: str) -> bool:
    """Whether a live credential actually carries `scope`.

    The gate for optional capabilities: a user who unticked Google Tasks gets a valid
    credential that simply cannot read tasks, and the caller degrades rather than 403s.
    """
    return scope in set(getattr(credentials, "scopes", None) or ())


def _is_invalid_grant(exc: RefreshError) -> bool:
    """Whether Google says the stored refresh token can never be used again."""
    return any(
        arg.get("error") == "invalid_grant"
        if isinstance(arg, dict)
        else "invalid_grant" in str(arg)
        for arg in exc.args
    )


def migrate_legacy_token_file(path: Path = LEGACY_TOKEN_FILE) -> bool:
    """Import a pre-Phase-1 `token.json` into the Keychain, once, then delete it.

    The legacy token was minted with `gmail.modify` + `gmail.send`, so the only slot it
    can legitimately fill is the send slot; the read path deliberately starts empty and
    asks for a fresh, read-only grant. Returns True if a migration happened.
    """
    if not path.exists():
        return False

    try:
        token = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    if read_token(SEND_SLOT) is None and _granted_scopes(token) & {GMAIL_SEND}:
        keyring.set_password(
            KEYRING_SERVICE, SEND_SLOT.keyring_username, json.dumps(token)
        )
        print(
            f"Migrated {path.name} into the Keychain "
            f"({KEYRING_SERVICE}/{SEND_SLOT.keyring_username}); removing the plaintext copy."
        )

    path.unlink(missing_ok=True)
    print(
        "Google scopes changed in Phase 1: the broker now needs a fresh read-only "
        "authorisation. It will open a browser on next use."
    )
    return True


def load_credentials(slot: CredentialSlot, *, interactive: bool = True) -> Credentials:
    """Return valid credentials for `slot`, authorising or refreshing as needed.

    Storage is the Keychain; the OAuth flow runs a local redirect server and only fires
    when there is no usable token. `interactive=False` makes a missing/expired token an
    error instead of opening a browser — used by anything that must not block (tests,
    headless runs).
    """
    migrate_legacy_token_file()

    token = read_token(slot)
    creds: Credentials | None = None
    problem = ""

    if token is not None:
        problem = token_problem(slot, token)
        if not problem:
            # Build the credential from what was actually *granted*, not from what the slot
            # asks for, so `has_scope` can tell a declined optional scope from a held one.
            creds = Credentials.from_authorized_user_info(
                token, sorted(_granted_scopes(token))
            )
        else:
            print(
                f"Stored '{slot.name}' Google token is no longer usable — {problem}.\n"
                "Discarding it and asking for a fresh, read-only authorisation."
            )
            clear_token(slot)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # invalid_grant means the refresh token was revoked, expired, or otherwise
            # invalidated by Google. Retrying it can never work; discard only this
            # permanently-bad token and fall through to the normal authorisation path.
            # Other refresh errors may be transient, so preserve the token and surface
            # them unchanged rather than unexpectedly revoking local access.
            if not _is_invalid_grant(exc):
                raise
            problem = "Google rejected its refresh token (invalid_grant)"
            print(
                f"Stored '{slot.name}' Google token is no longer usable — {problem}.\n"
                "Discarding it and asking for fresh authorisation."
            )
            clear_token(slot)
            creds = None
        else:
            write_token(slot, creds)
            return creds

    if not interactive:
        raise RuntimeError(
            f"No usable Google credential for the '{slot.name}' slot "
            f"({slot.purpose})"
            + (f" — {problem}" if problem else "")
            + ". Run `uv run python google_auth.py authorise "
            f"{slot.name}` on the host once to authorise it."
        )

    print(
        f"\nAuthorising Google '{slot.name}' access — your browser will open.\n"
        f"  purpose:  {slot.purpose}\n"
        f"  required: {', '.join(slot.scopes)}\n"
        + (
            f"  optional: {', '.join(slot.optional_scopes)}\n"
            if slot.optional_scopes
            else ""
        )
        + (
            "You can untick an optional scope and NightShift will simply report that "
            "section as unavailable."
            if slot.optional_scopes
            else "These permissions are used only for the purpose shown above."
        )
    )
    flow = InstalledAppFlow.from_client_config(
        _client_config(), scopes=list(slot.requested_scopes)
    )
    creds = flow.run_local_server(port=REDIRECT_PORT)
    write_token(slot, creds)
    granted = set(creds.scopes or ())
    declined = [scope for scope in slot.optional_scopes if scope not in granted]
    print(
        f"Authorisation complete; token stored in the Keychain as "
        f"{KEYRING_SERVICE}/{slot.keyring_username}."
    )
    if declined:
        print(
            "You declined: "
            + ", ".join(declined)
            + ". That is fine — the affected briefing section will say it is unavailable."
        )
    return creds


def main(argv: list[str] | None = None) -> int:
    """`google_auth.py authorise <slot>` — run one slot's consent flow on purpose.

    Exists so adding a scope (Phase 14 added Calendar and optional Tasks) is a thing the
    user can do deliberately at a convenient moment, rather than something that ambushes
    them the first time a nightly run happens to need it.
    """
    import argparse

    parser = argparse.ArgumentParser(description="NightShift Google credential slots")
    parser.add_argument(
        "command", choices=("authorise", "authorize", "status", "forget")
    )
    parser.add_argument(
        "slot", nargs="?", default="read", choices=sorted(SLOTS), help="Which slot."
    )
    args = parser.parse_args(argv)
    slot = SLOTS[args.slot]

    if args.command == "status":
        token = read_token(slot)
        if token is None:
            print(f"{slot.name}: not authorised ({slot.purpose})")
            return 1
        problem = token_problem(slot, token)
        granted = sorted(_granted_scopes(token))
        print(f"{slot.name}: {'UNUSABLE — ' + problem if problem else 'ok'}")
        for scope in granted:
            print(f"  granted: {scope}")
        for scope in slot.optional_scopes:
            if scope not in granted:
                print(f"  declined (optional): {scope}")
        return 1 if problem else 0

    if args.command == "forget":
        clear_token(slot)
        print(f"Forgot the '{slot.name}' token; the next use will re-authorise.")
        return 0

    load_credentials(slot, interactive=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
