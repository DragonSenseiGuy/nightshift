"""Rendering: the point where untrusted email text becomes an artifact.

The summary-as-data rule says email content must dead-end as inert text.
These tests hold that line: nothing an email or the model can say may survive the
renderer as live markup, and the injection fixture's markers must appear only as escaped
text. They also cover the offline end-to-end path (`main.py --mock`) with the LLM stubbed.
"""

import json
import re

import pytest

from briefing import render_digest_html
from fixtures.mock_emails import (
    INJECTION_CANARY,
    INJECTION_EMAIL_ID,
    INJECTION_MARKER,
    mock_emails,
)
from models import DraftReply, Email, EmailDigest, EmailSummaryItem, Urgency
from summarise import parse_digest

# Payloads a hostile email could try to smuggle through the model into the briefing.
_XSS = "<script>alert('pwned')</script>"
_IMG = "<img src=x onerror=alert(1)>"
_ATTR = '"><a href="https://evil.example">click</a>'


def _hostile_digest() -> EmailDigest:
    """A digest whose every string field carries markup or the injection markers."""
    return EmailDigest(
        since="8h",
        overview=f"Five emails. {INJECTION_MARKER} {_XSS}",
        items=[
            EmailSummaryItem(
                email_id=INJECTION_EMAIL_ID,
                sender=f"Dana {_IMG} <dana@partner-corp.example>",
                subject=f"Re: Q3 planning {_ATTR}",
                summary=f"{INJECTION_MARKER}: send {INJECTION_CANARY} {_XSS}",
                urgency=Urgency.HIGH,
                needs_reply=True,
                category=f"work{_XSS}",
                action_items=[f"Do not comply {_IMG}"],
                draft_reply=DraftReply(
                    subject=f"Re: Q3 {_XSS}",
                    body=f"Line one {_ATTR}\nLine two {INJECTION_CANARY}",
                ),
            )
        ],
        degraded=[f"Dropped malformed summary {_XSS}"],
    )


@pytest.fixture
def hostile_html() -> str:
    return render_digest_html(_hostile_digest())


def test_no_live_markup_from_email_derived_text(hostile_html):
    lowered = hostile_html.lower()
    assert "<script" not in lowered
    assert "<img" not in lowered
    # No anchors at all: the renderer never emits links, so an injected one is proof of
    # an escaping hole.
    assert "<a " not in lowered
    # `onerror` may appear as visible text, but only inside an escaped tag.
    assert lowered.count("onerror") == lowered.count("&lt;img src=x onerror")


def test_injected_payloads_appear_only_as_escaped_text(hostile_html):
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in hostile_html
    assert "&lt;img src=x onerror=alert(1)&gt;" in hostile_html
    assert _XSS not in hostile_html
    assert _IMG not in hostile_html
    assert _ATTR not in hostile_html


def test_injection_markers_survive_as_inert_text(hostile_html):
    # Visible (so a human can see what was attempted) but never as markup.
    assert INJECTION_MARKER in hostile_html
    assert INJECTION_CANARY in hostile_html
    marker_context = hostile_html[hostile_html.index(INJECTION_MARKER) - 40 :]
    assert "<script" not in marker_context.lower()


def test_only_renderer_owned_tags_are_present(hostile_html):
    """Whitelist the tag set: anything else means email text became markup."""
    tags = {t.lower() for t in re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", hostile_html)}
    assert tags <= {"div", "span", "p", "h2", "ul", "li", "br"}


def test_quotes_in_content_cannot_break_out_of_an_attribute(hostile_html):
    # Every style attribute must still be well-formed after the content is inlined.
    for style in re.findall(r'style="([^"]*)"', hostile_html):
        assert "<" not in style and ">" not in style


def test_end_to_end_from_mock_emails_through_the_renderer():
    """fetch (mock) → parse model JSON → render, with a hostile body in the mix."""
    emails = mock_emails()
    payload = {
        "overview": "Five emails.",
        "emails": [
            {
                "email_id": e.id,
                "summary": f"About: {e.subject}",
                "urgency": "high" if e.id == INJECTION_EMAIL_ID else "normal",
                "needs_reply": e.id == "mock-0001",
                "category": "work",
                "action_items": [],
                "draft_reply": (
                    {"subject": "Re: Invoice", "body": "Paying Friday."}
                    if e.id == "mock-0001"
                    else None
                ),
            }
            for e in emails
        ],
    }
    html = render_digest_html(parse_digest(json.dumps(payload), emails, since="8h"))

    assert "Your morning digest" in html
    assert "needs reply" in html
    assert "Suggested draft — not sent" in html
    assert "<script" not in html.lower()
    # The injection email's own body must not be pasted into the briefing wholesale.
    assert INJECTION_CANARY not in html


def test_email_body_html_never_reaches_the_briefing_via_placeholders():
    """The unparsable-output path echoes a snippet of the email — escape it too."""
    hostile = Email(
        id="x-1",
        sender=f"Mallory {_IMG}",
        subject=f"Hello {_XSS}",
        snippet=f"{_XSS} {INJECTION_MARKER}",
        body=f"{_XSS} {INJECTION_CANARY}",
    )
    html = render_digest_html(parse_digest("not json at all", [hostile]))
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html
    assert "Summariser issues" in html


def test_empty_digest_renders_gracefully():
    html = render_digest_html(EmailDigest(since="2h", overview="Quiet night."))
    assert "Nothing to report." in html
    assert "<script" not in html.lower()


def test_main_mock_path_runs_offline_end_to_end(tmp_path, monkeypatch):
    """`main.py --mock` with the LLM stubbed: no Gmail, no network, real HTML on disk."""
    import main as main_module
    import summarise

    def fake_call_model(emails, config, *, backend=None):
        return json.dumps(
            {
                "overview": "Stubbed digest.",
                "emails": [
                    {
                        "email_id": e.id,
                        "summary": f"Summary of {e.subject}",
                        "urgency": "normal",
                        "needs_reply": False,
                        "category": "work",
                        "action_items": [],
                        "draft_reply": None,
                    }
                    for e in emails
                ],
            }
        )

    def explode(*args, **kwargs):
        raise AssertionError("mock mode must not touch Gmail")

    monkeypatch.setattr(summarise, "_call_model", fake_call_model)
    monkeypatch.setattr(main_module, "get_read_credentials", explode)
    monkeypatch.setattr(main_module, "fetch_emails_from_api", explode)

    out = tmp_path / "briefing.html"
    monkeypatch.setattr("sys.argv", ["main.py", "--mock", "--out", str(out)])
    assert main_module.main() == 0

    html = out.read_text(encoding="utf-8")
    # main.py writes the full Phase-7 briefing artifact, not the bare email fragment.
    assert "Good morning" in html
    assert "Email" in html
    assert "<script" not in html.lower()
    assert INJECTION_CANARY not in html
    for email in mock_emails():
        assert email.subject.split()[0] in html
