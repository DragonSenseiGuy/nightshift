"""Host-only sending capability.

Sending is the one Google privilege the broker must never have, so it is isolated
here behind its own credential slot (`google_auth.SEND_SLOT`, scoped to `gmail.send`).
`api.py` does not import this module, and nothing in the sandbox can reach it. Since
Phase 8 it is the effect side of the approval queue: `approvals.py:approve()` is the
only code path that calls in here, and it runs only after a human taps approve.
"""

import base64
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from google_auth import SEND_SLOT, load_credentials

# Exported so tests (and readers) can assert the send scope lives only on this side.
SEND_SCOPES = SEND_SLOT.scopes


def get_send_credentials(*, interactive: bool = True):
  """Return the host-only credentials permitted to send mail."""
  return load_credentials(SEND_SLOT, interactive=interactive)


def _own_address(credentials) -> str:
  """The signed-in account's address, via OAuth userinfo.

  Gmail's `users.getProfile` would need a read scope, which this credential
  deliberately does not have — so identity comes from the userinfo endpoint instead.
  """
  service = build("oauth2", "v2", credentials=credentials)
  return service.userinfo().get().execute()["email"]


def send_email(credentials, to: str, subject: str, html_body: str, sender: str = "me"):
  """Send an HTML email via the Gmail API.

  `sender` can be "me" (the auth'ed account) or an explicit address
  that the account is allowed to send as.
  """
  service = build("gmail", "v1", credentials=credentials)

  message = MIMEText(html_body, "html")
  message["To"] = to
  message["From"] = sender
  message["Subject"] = subject

  raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

  sent = (
    service.users()
    .messages()
    .send(userId="me", body={"raw": raw})
    .execute()
  )
  print(f"Sent message id: {sent['id']}")
  return sent


def send_to_self(credentials, subject: str, html_body: str):
  """Send an HTML email from the authenticated account to itself.

  Looks up the account's own address via the Gmail profile endpoint so the
  recipient always matches whoever signed in. From == To == the account,
  so Gmail never rewrites the sender.
  """
  me = _own_address(credentials)
  service = build("gmail", "v1", credentials=credentials)

  message = MIMEText(html_body, "html")
  message["To"] = me
  message["From"] = me
  message["Subject"] = subject

  raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

  sent = (
    service.users()
    .messages()
    .send(userId="me", body={"raw": raw})
    .execute()
  )
  print(f"Sent digest to {me} (message id: {sent['id']})")
  return sent