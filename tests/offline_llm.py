"""A deterministic stand-in for the LLM, installed for the whole test suite.

Phase 16 found that `uv run pytest` was **not** offline. Three tests in
`tests/test_notifications.py` drive a real `run_night`, and `run_night`'s email stage calls
`summarise.build_digest` with no injected backend — so every run of the suite opened a TLS
connection to `[llm].base_url`, spent tokens against the user's key, and took 10-16s per
test waiting for a completion whose content nothing asserted on. A test suite that quietly
bills the person running it, needs the network to pass, and returns different text every
run is three bad things at once.

The fix is installed at the one seam that exists for it: `runner/backends.py` is the only
module in the repo that constructs an LLM client, so `conftest.py` replaces its `OpenAI`
symbol with `offline_client` for every test. Nothing else has to remember, and a future
code path that reaches for a model gets this instead of the internet.

What comes back is *shaped like a real completion*, not a fixed string: the reply is
derived from the request, so the digest/day-plan join-by-id logic, the placeholder
backfill and the escaping in `briefing.py` are all exercised for real. In particular the
summary text **echoes the email body**, which is deliberately the worst case — it is how a
fully-compliant model would behave in front of the injection fixture, so an end-to-end
test can assert the payload lands in the briefing escaped and inert rather than assert on
a stub that politely refused it.

A test that needs a specific completion still installs its own backend or its own fake
client; `monkeypatch` applies in order, so the later `setattr` wins.
"""

from __future__ import annotations

import json
from typing import Any

OFFLINE_API_KEY = "test-key-offline-suite"
"""What `OPENROUTER_API_KEY` is set to under test — so a real key is never in play."""


# --------------------------------------------------------------------------------------
# Working out what was asked
# --------------------------------------------------------------------------------------


def _json_lists(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Every JSON array of objects embedded in the conversation, in order.

    The agents pass their fetched data as JSON inside a single user message, fenced by the
    `PromptPart` label (`--- emails (UNTRUSTED, source: email) --- …`), so the arrays are
    substrings rather than whole messages. Scanned with `raw_decode` rather than matched on
    the fence text, so a reworded banner does not turn this stub into one that silently
    answers about nothing.
    """
    decoder = json.JSONDecoder()
    found: list[list[dict[str, Any]]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        index = content.find("[")
        while index != -1:
            try:
                parsed, end = decoder.raw_decode(content, index)
            except ValueError:
                index = content.find("[", index + 1)
                continue
            if isinstance(parsed, list) and parsed and all(isinstance(i, dict) for i in parsed):
                found.append(parsed)
            index = content.find("[", max(end, index + 1))
    return found


def _schema_name(response_format: dict[str, Any] | None) -> str:
    if not response_format:
        return ""
    schema = response_format.get("json_schema")
    return str(schema.get("name", "")) if isinstance(schema, dict) else ""


# --------------------------------------------------------------------------------------
# The canned replies, derived from the request
# --------------------------------------------------------------------------------------


def email_digest_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """An `LLMDigest`-shaped reply covering every email in the prompt.

    Echoes each body into its summary on purpose (see the module docstring), and marks the
    first email as needing a reply so the draft-reply → approval-queue path has something
    to carry.
    """
    emails: list[dict[str, Any]] = []
    for payload in _json_lists(messages):
        if "subject" in payload[0] and "id" in payload[0]:
            emails = payload
            break
    items = []
    for index, email in enumerate(emails):
        text = str(email.get("body") or email.get("snippet") or "")
        needs_reply = index == 0
        items.append(
            {
                "email_id": str(email.get("id", "")),
                "summary": f"Offline stub summary: {text[:400]}",
                "urgency": "high" if needs_reply else "normal",
                "needs_reply": needs_reply,
                "category": "stub",
                "action_items": ["Read it properly in the morning."] if needs_reply else [],
                "draft_reply": (
                    {
                        "subject": f"Re: {email.get('subject', '')}"[:200],
                        "body": "Thanks — I will come back to you on this tomorrow.",
                    }
                    if needs_reply
                    else None
                ),
            }
        )
    return {"overview": f"Offline stub: {len(items)} email(s) in this window.", "emails": items}


def day_plan_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """An `LLMDayPlan`-shaped reply covering every event and task in the prompt.

    The calendar agent is sent two arrays (events, then tasks); which is which is decided
    by the fields on the entries rather than by position, so a future prompt reorder does
    not silently turn this into an empty plan.
    """
    events: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for payload in _json_lists(messages):
        if not payload:
            continue
        first = payload[0]
        if "all_day" in first or "attendees" in first:
            events = payload
        elif "due" in first or "list_title" in first:
            tasks = payload
    return {
        "notes": ["Offline stub: this day plan came from the test suite, not a model."],
        "events": [
            {
                "event_id": str(event.get("id", "")),
                # Echoes the description as well as the title: the injection fixture's
                # payload lives in the description, and a stub that quietly dropped it
                # would make the end-to-end escaping assertion prove nothing.
                "prep_notes": [
                    f"Offline stub prep note for {str(event.get('title', ''))[:120]}",
                    f"Offline stub read: {str(event.get('description', ''))[:400]}",
                ],
            }
            for event in events
        ],
        "tasks": [
            {
                "task_id": str(task.get("id", "")),
                "urgency": "normal",
                "verdict": "Offline stub verdict.",
            }
            for task in tasks
        ],
    }


def reply_for(*, messages: list[dict[str, Any]], response_format: dict[str, Any] | None) -> str:
    """The completion text this request gets."""
    name = _schema_name(response_format)
    if name == "email_digest":
        return json.dumps(email_digest_reply(messages))
    if name == "day_plan":
        return json.dumps(day_plan_reply(messages))
    # Any other agent (the project agent, an ad-hoc run) gets plain text and no tool call,
    # which ends its loop immediately. Tests that need tool calls inject their own backend.
    return "Offline stub: no model was called during this test."


# --------------------------------------------------------------------------------------
# The fake client, shaped like the sliver of the OpenAI SDK `backends.py` uses
# --------------------------------------------------------------------------------------


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls: list[Any] = []


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Response:
    def __init__(self, content: str, prompt_tokens: int) -> None:
        self.choices = [_Choice(content)]
        # Reported, and reported deterministically: an unmetered reply would be billed
        # against `runner/budget.py`'s pessimistic estimate and make costs vary by prompt.
        self.usage = _Usage(prompt_tokens, len(content) // 4)


class OfflineCompletions:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def create(self, **kwargs: Any) -> _Response:
        self._calls.append(kwargs)
        messages = kwargs.get("messages") or []
        content = reply_for(
            messages=messages, response_format=kwargs.get("response_format")
        )
        prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
        return _Response(content, prompt_tokens)


class OfflineClient:
    """Stands in for `openai.OpenAI`. Records every request it was asked to make."""

    def __init__(self, *, base_url: str = "", api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.calls: list[dict[str, Any]] = []
        self.chat = type("_Chat", (), {"completions": OfflineCompletions(self.calls)})()


def offline_client(*, base_url: str, api_key: str) -> OfflineClient:
    """Drop-in for `runner.backends.OpenAI`, installed by the autouse conftest fixture."""
    return OfflineClient(base_url=base_url, api_key=api_key)
