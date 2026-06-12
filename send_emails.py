import base64
from email.mime.text import MIMEText

from googleapiclient.discovery import build

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
  service = build("gmail", "v1", credentials=credentials)

  me = service.users().getProfile(userId="me").execute()["emailAddress"]

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