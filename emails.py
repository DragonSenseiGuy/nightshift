import base64
import json
import os
from datetime import datetime, timedelta, timezone

# Google automatically adds the "openid" scope to userinfo requests, which makes
# oauthlib raise a "Scope has changed" error. Relax that check.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Where we cache the OAuth token so we don't have to sign in every run.
TOKEN_FILE = "token.json"
REDIRECT_PORT = 8765


def _client_config():
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


def get_credentials():
    """Return valid user credentials, running the OAuth flow if needed.

    On first run this opens a browser, lets the user grant access, and
    captures the auth code via a local redirect server. The resulting token
    is cached in TOKEN_FILE and refreshed automatically afterwards.
    """
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
        else:
            print("Starting OAuth flow — your browser will open to sign in...")
            flow = InstalledAppFlow.from_client_config(_client_config(), scopes=SCOPES)
            creds = flow.run_local_server(port=REDIRECT_PORT)
            print("Authorization complete.")

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return creds


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


def fetch_emails_last_x_hours(credentials, hours=8):
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

    detailed = []
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
            {
                "id": msg["id"],
                "sender": sender,
                "subject": subject,
                "snippet": msg.get("snippet"),
                "body": body,
            }
        )

    emails_json = json.dumps(detailed, indent=2)
    return detailed