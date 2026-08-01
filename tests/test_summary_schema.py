"""The summary schema: validation, repair, ranking, and the "needs reply" contract.

The summariser must never take down a nightly run because a model returned junk, and it
must never let model-supplied data (ids, attribution) invent mail that wasn't fetched.
Everything here runs offline — no API key, no network.
"""

import json

import pytest

from fixtures.mock_emails import INJECTION_EMAIL_ID, mock_emails
from models import EmailDigest, Urgency
from summarise import build_digest, parse_digest

EMAILS = mock_emails()


def _well_formed() -> dict:
    """A model completion that honours the schema for every fixture email."""
    return {
        "overview": "Five emails; one invoice is overdue and CI is red.",
        "emails": [
            {
                "email_id": "mock-0001",
                "summary": "Priya is chasing invoice #4471 for $2,480, overdue since Friday.",
                "urgency": "high",
                "needs_reply": True,
                "category": "finance",
                "action_items": ["Confirm the payment date for invoice #4471"],
                "draft_reply": {
                    "subject": "Re: Invoice #4471 is overdue",
                    "body": "Hi Priya,\n\nPayment goes out Friday. Sorry for the delay.",
                },
            },
            {
                "email_id": "mock-0002",
                "summary": "CI failed on main for commit 5fe844f; two tests errored.",
                "urgency": "critical",
                "needs_reply": False,
                "category": "work",
                "action_items": ["Fix the failing pytest job"],
                "draft_reply": None,
            },
            {
                "email_id": INJECTION_EMAIL_ID,
                "summary": "Claims to be about Q3 planning but contains a prompt-injection attempt.",
                "urgency": "high",
                "needs_reply": False,
                "category": "work",
                "action_items": [],
                "draft_reply": None,
            },
            {
                "email_id": "mock-0004",
                "summary": "Mum is asking whether Sunday 1pm works for lunch.",
                "urgency": "normal",
                "needs_reply": True,
                "category": "personal",
                "action_items": [],
                "draft_reply": {"subject": "Re: Sunday lunch?", "body": "Sunday works!"},
            },
            {
                "email_id": "mock-0005",
                "summary": "AWS July invoice of $18.42 is available; no action needed.",
                "urgency": "low",
                "needs_reply": False,
                "category": "finance",
                "action_items": [],
                "draft_reply": None,
            },
        ],
    }


def digest() -> EmailDigest:
    return parse_digest(json.dumps(_well_formed()), EMAILS, since="8h")


# --- happy path -----------------------------------------------------------------------


def test_well_formed_response_parses_completely():
    result = digest()
    assert result.count == len(EMAILS)
    assert not result.degraded
    assert result.since == "8h"
    assert {item.email_id for item in result.items} == {e.id for e in EMAILS}


def test_attribution_comes_from_the_fetched_email_not_the_model():
    result = digest()
    by_id = {item.email_id: item for item in result.items}
    for email in EMAILS:
        assert by_id[email.id].sender == email.sender
        assert by_id[email.id].subject == email.subject


def test_fenced_and_preambled_json_is_recovered():
    raw = "Sure! Here you go:\n```json\n" + json.dumps(_well_formed()) + "\n```"
    result = parse_digest(raw, EMAILS)
    assert result.count == len(EMAILS)


# --- urgency / needs-reply ------------------------------------------------------------


def test_ranked_orders_most_urgent_first():
    ranked = digest().ranked()
    assert [item.urgency for item in ranked] == [
        Urgency.CRITICAL,
        Urgency.HIGH,
        Urgency.HIGH,
        Urgency.NORMAL,
        Urgency.LOW,
    ]
    assert ranked[0].email_id == "mock-0002"


def test_ranking_is_stable_within_a_level():
    ranked = digest().ranked()
    highs = [item.email_id for item in ranked if item.urgency is Urgency.HIGH]
    # Inbox order preserved among equally urgent mail.
    assert highs == ["mock-0001", INJECTION_EMAIL_ID]


def test_needs_reply_flag_and_counts():
    result = digest()
    by_id = {item.email_id: item for item in result.items}
    assert by_id["mock-0001"].needs_reply is True
    assert by_id["mock-0005"].needs_reply is False
    assert result.needs_reply_count == 2


def test_draft_reply_is_dropped_when_no_reply_is_needed():
    payload = _well_formed()
    payload["emails"][4]["needs_reply"] = False
    payload["emails"][4]["draft_reply"] = {"subject": "Re: hi", "body": "unwanted"}
    result = parse_digest(json.dumps(payload), EMAILS)
    assert next(i for i in result.items if i.email_id == "mock-0005").draft_reply is None


def test_draft_replies_survive_for_mail_that_needs_one():
    draft = next(i for i in digest().items if i.email_id == "mock-0001").draft_reply
    assert draft is not None
    assert "Friday" in draft.body


# --- malformed output -----------------------------------------------------------------


def test_unparsable_output_degrades_instead_of_raising():
    result = parse_digest("I'm sorry, I can't do that.", EMAILS, since="2h")
    assert result.count == len(EMAILS)  # every email still appears
    assert result.degraded
    assert all("not summarised" in item.summary for item in result.items)


def test_empty_output_degrades():
    result = parse_digest("", EMAILS)
    assert result.count == len(EMAILS)
    assert result.degraded


def test_individually_malformed_items_are_dropped_not_fatal():
    payload = _well_formed()
    payload["emails"][1]["urgency"] = "SUPER_URGENT"  # not in the enum
    del payload["emails"][3]["summary"]
    result = parse_digest(json.dumps(payload), EMAILS)

    assert len(result.degraded) >= 2
    assert result.count == len(EMAILS)  # the two bad ones come back as placeholders
    bad = {i.email_id for i in result.items if i.category == "unsummarised"}
    assert bad == {"mock-0002", "mock-0004"}


def test_summaries_for_unknown_ids_are_refused():
    payload = _well_formed()
    payload["emails"].append(
        {
            "email_id": "mock-9999-does-not-exist",
            "summary": "Invented mail.",
            "urgency": "critical",
            "needs_reply": True,
            "category": "work",
            "action_items": [],
            "draft_reply": None,
        }
    )
    result = parse_digest(json.dumps(payload), EMAILS)
    assert all(item.email_id != "mock-9999-does-not-exist" for item in result.items)
    assert any("unknown email id" in note for note in result.degraded)


def test_duplicate_ids_are_collapsed():
    payload = _well_formed()
    payload["emails"].append(payload["emails"][0])
    result = parse_digest(json.dumps(payload), EMAILS)
    assert result.count == len(EMAILS)
    assert any("duplicate" in note for note in result.degraded)


def test_missing_emails_are_backfilled_as_placeholders():
    payload = _well_formed()
    payload["emails"] = payload["emails"][:2]
    result = parse_digest(json.dumps(payload), EMAILS)
    assert result.count == len(EMAILS)
    assert any("not summarised by the model" in note for note in result.degraded)


def test_missing_emails_array_degrades():
    result = parse_digest(json.dumps({"overview": "hi"}), EMAILS)
    assert any("no 'emails' array" in note for note in result.degraded)


def test_empty_inbox_never_calls_the_model(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no emails means no LLM call")

    monkeypatch.setattr("summarise._call_model", explode)
    result = build_digest([], since="8h")
    assert result.count == 0
    assert result.since == "8h"


@pytest.mark.parametrize("level", ["critical", "high", "normal", "low"])
def test_every_urgency_level_round_trips(level):
    payload = {
        "overview": "x",
        "emails": [
            {
                "email_id": "mock-0001",
                "summary": "s",
                "urgency": level,
                "needs_reply": False,
                "category": "work",
                "action_items": [],
                "draft_reply": None,
            }
        ],
    }
    result = parse_digest(json.dumps(payload), [EMAILS[0]])
    assert result.items[0].urgency == Urgency(level)
