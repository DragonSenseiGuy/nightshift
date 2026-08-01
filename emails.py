"""Gmail *reading*: the broker's half of the Google integration.

This module is deliberately read-only. It holds the credential slot the broker uses
(`google_auth.READ_SLOT`, scoped to `gmail.readonly`) and nothing that can send or
mutate mail — sending lives in `send_emails.py` behind its own credential, so an
attacker who reaches the broker's API surface finds no path to an outbound message.
"""

import base64
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from google_auth import READ_SLOT, load_credentials
from models import Email

# Exported for callers that want to inspect what the read path asks for.
READ_SCOPES = READ_SLOT.scopes


def get_read_credentials(*, interactive: bool = True):
    """Return the broker's read-only Google credentials (Keychain-backed).

    First use opens a browser for consent; afterwards the token is refreshed silently.
    See `google_auth` for the storage and scope-budget rules.
    """
    return load_credentials(READ_SLOT, interactive=interactive)


def _extract_body(payload):
    """Walk a Gmail message payload and return its decoded text body.

    Prefers text/plain, falling back to text/html. Handles both single-part
    messages and nested multipart MIME trees.
    """
    plain = None
    html = None

    def walk(part):
        nonlocal plain, html
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if mime_type == "text/plain" and plain is None:
                plain = decoded
            elif mime_type == "text/html" and html is None:
                html = decoded
        for sub_part in part.get("parts", []):
            walk(sub_part)

    walk(payload)
    return plain or html or ""


def fetch_emails_last_x_hours(credentials, hours: float = 8) -> list[Email]:
    service = build("gmail", "v1", credentials=credentials)

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    unix_timestamp = int(cutoff_time.timestamp())

    search_query = f"after:{unix_timestamp}"
    print(f"Searching Gmail with query: '{search_query}'")

    results = service.users().messages().list(userId="me", q=search_query).execute()
    messages = results.get("messages", [])

    if not messages:
        print(f"No emails found in the last {hours} hours.")
        return []

    print(f"Found {len(messages)} emails. Fetching details...")

    detailed: list[Email] = []
    for message in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message["id"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "(unknown sender)")
        body = _extract_body(msg["payload"])
        detailed.append(
            Email(
                id=msg["id"],
                sender=sender,
                subject=subject,
                snippet=msg.get("snippet"),
                body=body,
            )
        )

    return detailed