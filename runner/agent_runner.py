"""The agent loop, behind a thin interface so the SDK underneath stays swappable.

**Why there is no SDK here.** The spec pencilled in an "OpenRouter Agent SDK (pinned,
beta)". There isn't one: PyPI's `openrouter` package is a *client* SDK (HTTP + Pydantic,
no agent loop), and no `openrouter-agent-sdk` exists. On top of that this repo points at
an OpenAI-compatible endpoint (`[llm].base_url`, currently a proxy — not openrouter.ai),
and the sandbox image bakes its dependencies at build time, so an unnecessary dependency
is also an unnecessary rebuild. So the loop below is ours: a few dozen lines of
tool-calling over `chat.completions`. The point of this module was never the SDK, it was
the *seam* — nothing outside `runner/` imports an LLM client, so swapping in a real agent
SDK later means writing one `LLMBackend` and touching nothing else.

What the runner guarantees, and what the rest of the system leans on:

1. **Fresh context per run.** `run()` builds its message list from the spec every call and
   keeps nothing between calls. There is no cross-agent memory to poison, by construction.
2. **Tool scoping.** An agent can only reach its own `ToolRegistry`; asking for another
   agent's tool raises `ToolScopeError` and ends the run (see `tools.py`).
3. **Taint.** Prompts are `PromptPart`s carrying trust labels; an agent that did not
   declare a taint cannot be handed data bearing it, and the result inherits every taint
   it was exposed to (see `taint.py`). This is the summary-as-data rule, enforced.
4. **Bounded work.** Every run carries a `Budget` (steps, USD, wall-clock). Any cap that
   is reached stops the loop cleanly with a `stop_reason` — never an exception, so the
   partial work and the transcript survive. See `runner/budget.py`.
5. **A transcript.** Every tool call and result is recorded on the result, and handed to
   the installed recorder (`runner/observe.py`) so `transcripts.py` can persist and replay
   it. Recording is observation only: a stored transcript is never a prompt source.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, Field

from models import TokenUsage
from runner.budget import (
    STOP_COMPLETED,
    STOP_STEP_LIMIT,
    Budget,
    BudgetLedger,
    estimate_usage,
)
from runner.taint import PromptPart, check_accepts, combined_taint
from runner.tools import ToolCallRecord, ToolError, ToolRegistry

# --------------------------------------------------------------------------------------
# The swappable seam
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestedToolCall:
    """A tool call the model asked for, normalised away from any provider's shape."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """One turn's worth of input to whatever is generating tokens."""

    model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    text: str = ""
    tool_calls: tuple[RequestedToolCall, ...] = ()
    usage: TokenUsage | None = None
    """Tokens the provider says this call cost. `None` means it did not say, and the
    runner bills the call at its worst case rather than at zero (see `runner/budget.py`)."""


class LLMBackend(Protocol):
    """The only thing an agent needs from a model provider.

    Keep it this small on purpose: a backend does no tool dispatch, no retries against
    our schemas, and no prompt assembly. All the security-relevant behaviour lives in
    `AgentRunner`, so a swapped-in SDK cannot quietly opt out of it.
    """

    def complete(self, request: CompletionRequest) -> CompletionResponse: ...


# --------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Everything that defines one agent for one run.

    Built fresh per run by the factories in `runner/agents.py`. Frozen because an agent's
    tool allowlist and taint policy must not be edited by anything the agent then does.
    """

    name: str
    system_prompt: str
    model: str
    max_tokens: int = 16000
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    accepts_taint: frozenset[str] = field(default_factory=frozenset)
    response_format: dict[str, Any] | None = None
    max_steps: int = 8
    budget: Budget = field(default_factory=Budget)
    """Cost and wall-clock caps for this run (the step cap is `max_steps`, above).

    Frozen with the rest of the spec: a budget an agent could edit mid-run is not a budget.
    """
    advertise_tools: bool = True
    """Whether to *offer* the tools to the model this run.

    Scoping is enforced from `tools` either way; this only controls what is advertised, so
    a single-shot agent (the summariser already has its emails in hand) can skip the tool
    preamble without loosening any boundary.
    """


class AgentResult(BaseModel):
    """What an agent run produced, with the taint it accumulated riding along.

    `text` is the model's final message. It is *not* safe to concatenate into another
    prompt whenever `taint` is non-empty — and `as_prompt_part()` makes sure you cannot do
    so by accident, because the part it returns carries the same taint and the next
    agent's `accepts_taint` will reject it.
    """

    agent: str
    text: str = ""
    taint: frozenset[str] = Field(default_factory=frozenset)
    steps: int = 0
    stop_reason: str = STOP_COMPLETED
    transcript: list[ToolCallRecord] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Every message sent to or received from the model, in order",
    )
    model: str = Field(default="", description="Model slug this run was billed against")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = Field(
        default=0.0, ge=0.0, description="What this run cost at the configured prices"
    )
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def stopped_early(self) -> bool:
        """True when a cap ended the run rather than the agent finishing its work."""
        return self.stop_reason != STOP_COMPLETED

    def as_prompt_part(self, label: str = "prior agent output") -> PromptPart:
        """Re-enter another prompt *carrying the taint*. Usually this is what you must not do."""
        if self.taint:
            return PromptPart.tainted(self.text, self.taint, label=label)
        return PromptPart.trusted(self.text, label=label)


class AgentRunner:
    """Runs one `AgentSpec` against one prompt. Holds no state between runs.

    `recorder` is the Phase 12 observation seam: every finished run — including one a cap
    stopped — is handed to it for storage. It is *write-only* by design. Nothing in this
    module ever reads a stored run back, because a transcript is email-derived text and
    the one thing it must never become is another agent's prompt (security rule 2).
    `clock` is injectable so the wall-clock cap is testable without sleeping.
    """

    def __init__(
        self,
        backend: LLMBackend,
        *,
        recorder: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._clock = clock
        if recorder is None:
            from runner.observe import active_recorder

            recorder = active_recorder()
        self._recorder = recorder

    def run(self, spec: AgentSpec, prompt: Sequence[PromptPart]) -> AgentResult:
        """Execute the agent loop and return its typed result.

        Raises `TaintViolation` if the prompt (or a tool result) carries a taint the agent
        never declared, and `ToolScopeError` if it reaches outside its allowlist. Both are
        fatal on purpose: this is the one place where failing open would quietly undo the
        isolation the whole design rests on.

        A blown budget is the opposite kind of event: the loop stops, `stop_reason` says
        which cap did it, and everything produced so far is returned and recorded.
        """
        carried = combined_taint(prompt)
        check_accepts(
            agent=spec.name, accepts=spec.accepts_taint, carried=carried, what="its prompt"
        )

        # Fresh every run — no shared memory between agents, and none between two runs of
        # the same agent either.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": spec.system_prompt},
            {"role": "user", "content": "\n\n".join(part.render() for part in prompt)},
        ]
        transcript: list[ToolCallRecord] = []
        tools = spec.tools.schemas() if spec.advertise_tools and len(spec.tools) else None

        text = ""
        stop_reason = STOP_COMPLETED
        step = 0
        started_at = datetime.now(timezone.utc)
        ledger = BudgetLedger(spec.budget, spec.model, clock=self._clock)

        for step in range(1, spec.max_steps + 1):
            # Caps are checked *before* paying for the next completion, so a run that has
            # already blown its budget never buys one more turn. The run keeps whatever it
            # produced up to here; `step - 1` is what it actually completed.
            blown = ledger.exceeded()
            if blown:
                stop_reason = blown
                step -= 1
                break

            request = CompletionRequest(
                model=spec.model,
                messages=list(messages),
                max_tokens=spec.max_tokens,
                tools=tools,
                response_format=spec.response_format,
            )
            response = self._backend.complete(request)
            # An unmetered call is billed at its worst case rather than at zero: see
            # `runner/budget.py`, decision 4.
            ledger.charge(
                response.usage
                if response.usage is not None
                else estimate_usage(request.messages, response)
            )
            text = response.text

            if not response.tool_calls:
                messages.append({"role": "assistant", "content": text})
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                record, output, new_taint = self._invoke(spec, call, step)
                transcript.append(record)
                # A tool may only hand back taint the agent was cleared for. Its own
                # registry should make this unreachable; assert it anyway, because "should
                # be unreachable" is how boundaries rot.
                check_accepts(
                    agent=spec.name,
                    accepts=spec.accepts_taint,
                    carried=new_taint,
                    what=f"the result of tool {call.name!r}",
                )
                carried |= new_taint
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        else:
            # Loop finished without a tool-free turn: we hit the cap.
            stop_reason = STOP_STEP_LIMIT

        result = AgentResult(
            agent=spec.name,
            text=text,
            taint=carried,
            steps=step,
            stop_reason=stop_reason,
            transcript=transcript,
            messages=messages,
            model=spec.model,
            usage=ledger.usage,
            cost_usd=round(ledger.cost_usd, 6),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        self._record(result)
        return result

    def _record(self, result: AgentResult) -> None:
        """Hand the finished run to the recorder. Never fails the run.

        Observability that can take the night down with it is worse than no observability:
        a full disk at 3am must cost you the transcript, not the briefing.
        """
        if self._recorder is None:
            return
        try:
            self._recorder.record(result)
        except Exception as exc:  # noqa: BLE001 - recording is best-effort by contract
            print(f"Could not record the transcript for {result.agent!r}: {exc!r}")

    def _invoke(
        self, spec: AgentSpec, call: RequestedToolCall, step: int
    ) -> tuple[ToolCallRecord, str, frozenset[str]]:
        """Dispatch one tool call. `ToolScopeError` escapes; ordinary failures don't."""
        try:
            tool = spec.tools.get(call.name)  # raises ToolScopeError if out of scope
            output, arguments = tool.invoke(call.arguments)
        except ToolError as exc:
            # Recoverable: a bad name or bad arguments is the model fumbling, not a breach.
            # Hand the error back and let it try again within the step budget.
            return (
                ToolCallRecord(step=step, tool=call.name, ok=False, error=str(exc)),
                f"error: {exc}",
                frozenset(),
            )

        record = ToolCallRecord(
            step=step,
            tool=call.name,
            arguments=arguments,
            ok=True,
            result=output,
            taint=sorted(tool.taint),
        )
        return record, output, tool.taint


def run_agent(
    spec: AgentSpec,
    prompt: Sequence[PromptPart],
    *,
    backend: LLMBackend,
    recorder: Any | None = None,
) -> AgentResult:
    """One-shot convenience wrapper. Explicit `backend` keeps tests off the network."""
    return AgentRunner(backend, recorder=recorder).run(spec, prompt)
