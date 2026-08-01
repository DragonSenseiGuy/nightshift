"""The one module in NightShift that imports an LLM client.

Everything else — tools, agents, orchestration, `summarise.py` — goes through
`LLMBackend`. Keeping the import here is what makes the "swappable SDK" claim in
`agent_runner.py` true rather than aspirational: replacing the provider means adding a
sibling of `ChatCompletionsBackend`, not grepping the repo for `openai`.

The endpoint comes from the standing instructions (`[llm].base_url`); the API key never
does — it stays in `.env`/Keychain, because config is a file that gets committed.
"""

from __future__ import annotations

import os

from openai import OpenAI

from config import StandingInstructions
from models import TokenUsage
from runner.agent_runner import CompletionRequest, CompletionResponse, RequestedToolCall


def build_client(config: StandingInstructions) -> OpenAI:
    """Build the OpenAI-compatible client lazily, so importing this needs no key (tests)."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY must be set in your .env file.")
    return OpenAI(base_url=config.llm.base_url, api_key=api_key)


def _usage_of(response) -> TokenUsage | None:
    """Read token counts off a completion, or `None` if the provider did not report them.

    `None` is not "zero": the runner bills an unmetered call at its worst case, so a proxy
    that omits `usage` costs the run a pessimistic estimate rather than a free pass past
    the cost cap (`runner/budget.py`). Written defensively because "OpenAI-compatible"
    endpoints vary most in exactly this field.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt is None and completion is None:
        return None
    return TokenUsage(prompt_tokens=int(prompt or 0), completion_tokens=int(completion or 0))


class ChatCompletionsBackend:
    """`LLMBackend` over an OpenAI-compatible `chat.completions` endpoint."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        kwargs = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": request.messages,
        }
        if request.tools:
            kwargs["tools"] = request.tools
        if request.response_format:
            kwargs["response_format"] = request.response_format

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception:
            # Not every routed model supports `json_schema` response formats. Retry in
            # plain JSON mode; callers validate and repair the result either way
            # (`summarise.parse_digest`), so a downgrade costs strictness, not the run.
            if not request.response_format:
                raise
            kwargs["response_format"] = {"type": "json_object"}
            response = self._client.chat.completions.create(**kwargs)

        message = response.choices[0].message
        usage = _usage_of(response)
        calls = tuple(
            RequestedToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments or "{}",
            )
            for call in (getattr(message, "tool_calls", None) or [])
        )
        return CompletionResponse(text=message.content or "", tool_calls=calls, usage=usage)


def backend_for(config: StandingInstructions) -> ChatCompletionsBackend:
    """The default backend for a run, wired from config."""
    return ChatCompletionsBackend(build_client(config))
