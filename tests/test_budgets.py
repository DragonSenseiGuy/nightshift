"""Phase 12: the stop conditions, asserted one cap at a time.

Three properties, and one thing that is *not* a property:

1. **Each cap stops the agent on its own** — steps, dollars, seconds — with the right
   `stop_reason` and with the work done so far still on the result. A cap that threw the
   transcript away would be worse than no cap, because the interesting question after a
   stopped run is always "what had it done".
2. **A cap is checked before the next call is paid for**, so a blown budget costs nothing
   extra. The scripted backend counts its own calls, which is the only way to prove that.
3. **An unpriced model is expensive by default**, and an unmetered completion is still
   billed. Both are the same rule: a cost cap that silently does not apply is not a cap.

Not a property: exact dollar amounts. The prices come from config, so the tests state
their own and assert the arithmetic, never a number baked into the source.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from config import StandingInstructions
from models import TokenUsage
from runner.agent_runner import (
    AgentRunner,
    AgentSpec,
    CompletionResponse,
    RequestedToolCall,
)
from runner.agents import EMAIL_AGENT, PROJECT_AGENT, budget_for
from runner.budget import (
    STOP_COMPLETED,
    STOP_COST_LIMIT,
    STOP_STEP_LIMIT,
    STOP_TIME_LIMIT,
    Budget,
    BudgetLedger,
    ModelPrice,
    PriceList,
    estimate_usage,
)
from runner.taint import PromptPart
from runner.tools import Tool, ToolRegistry

# --- doubles ----------------------------------------------------------------------------


class Ping(BaseModel):
    note: str = ""


def _toolset(calls: list[str]) -> ToolRegistry:
    def handler(args: Ping) -> str:
        calls.append(args.note)
        return f"pong {args.note}"

    return ToolRegistry(
        [Tool(name="read_file", description="ping", parameters=Ping, handler=handler)],
        owner="test_agent",
    )


class CountingBackend:
    """Replays responses, counts calls, and reports whatever usage the test wants."""

    def __init__(self, *responses: CompletionResponse) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return CompletionResponse(text="done")


def _tool_turn(note: str, usage: TokenUsage | None = None) -> CompletionResponse:
    return CompletionResponse(
        text="",
        tool_calls=(
            RequestedToolCall(id=f"c{note}", name="read_file", arguments=f'{{"note": "{note}"}}'),
        ),
        usage=usage,
    )


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


PRICES = PriceList(
    models={"cheap/model": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0)},
    unknown=ModelPrice(input_per_mtok=100.0, output_per_mtok=200.0, known=False),
)


def _spec(calls: list[str], *, budget: Budget, max_steps: int = 8, model: str = "cheap/model"):
    return AgentSpec(
        name="test_agent",
        system_prompt="you are a test",
        model=model,
        max_tokens=100,
        tools=_toolset(calls),
        max_steps=max_steps,
        budget=budget,
    )


PROMPT = [PromptPart.trusted("do the thing")]


# --------------------------------------------------------------------------------------
# 1. Step cap
# --------------------------------------------------------------------------------------


def test_the_step_cap_stops_the_agent_and_keeps_its_work():
    calls: list[str] = []
    backend = CountingBackend(*[_tool_turn(str(i)) for i in range(10)])

    result = AgentRunner(backend, recorder=None).run(
        _spec(calls, budget=Budget(prices=PRICES), max_steps=3), PROMPT
    )

    assert result.stop_reason == STOP_STEP_LIMIT
    assert result.steps == 3
    assert backend.calls == 3
    # Partial work survives: three tool calls happened and all three are on the transcript.
    assert calls == ["0", "1", "2"]
    assert [record.tool for record in result.transcript] == ["read_file"] * 3
    assert [record.step for record in result.transcript] == [1, 2, 3]


def test_an_agent_that_finishes_early_is_not_a_capped_run():
    calls: list[str] = []
    backend = CountingBackend(CompletionResponse(text="all done"))

    result = AgentRunner(backend, recorder=None).run(
        _spec(calls, budget=Budget(prices=PRICES), max_steps=5), PROMPT
    )

    assert result.stop_reason == STOP_COMPLETED
    assert result.stopped_early is False
    assert result.text == "all done"


# --------------------------------------------------------------------------------------
# 2. Cost cap
# --------------------------------------------------------------------------------------


def test_the_cost_cap_stops_the_agent_once_the_budget_is_spent():
    # 1M output tokens at $2/Mtok = $2.00 per turn, against a $3.00 budget: the second
    # turn takes the total to $4.00, so there must not be a third.
    usage = TokenUsage(prompt_tokens=0, completion_tokens=1_000_000)
    calls: list[str] = []
    backend = CountingBackend(*[_tool_turn(str(i), usage) for i in range(10)])

    result = AgentRunner(backend, recorder=None).run(
        _spec(calls, budget=Budget(max_cost_usd=3.0, prices=PRICES), max_steps=10), PROMPT
    )

    assert result.stop_reason == STOP_COST_LIMIT
    assert backend.calls == 2, "the cap must be checked before paying for another call"
    assert result.cost_usd == pytest.approx(4.0)
    assert result.steps == 2
    # Both turns' tool calls are preserved, in order.
    assert calls == ["0", "1"]
    assert len(result.transcript) == 2


def test_a_cost_cap_below_one_call_stops_after_the_first():
    usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=0)  # $1.00 at PRICES
    calls: list[str] = []
    backend = CountingBackend(*[_tool_turn(str(i), usage) for i in range(5)])

    result = AgentRunner(backend, recorder=None).run(
        _spec(calls, budget=Budget(max_cost_usd=0.01, prices=PRICES), max_steps=5), PROMPT
    )

    assert result.stop_reason == STOP_COST_LIMIT
    assert backend.calls == 1
    assert calls == ["0"]


def test_zero_means_no_cost_cap():
    usage = TokenUsage(prompt_tokens=5_000_000, completion_tokens=5_000_000)
    calls: list[str] = []
    backend = CountingBackend(_tool_turn("0", usage), CompletionResponse(text="done", usage=usage))

    result = AgentRunner(backend, recorder=None).run(
        _spec(calls, budget=Budget(max_cost_usd=0.0, prices=PRICES), max_steps=5), PROMPT
    )

    assert result.stop_reason == STOP_COMPLETED
    assert result.cost_usd > 0


def test_an_unknown_model_is_billed_at_the_expensive_fallback():
    """The whole point of the unknown-model rate: a slug nobody priced still trips a cap."""
    usage = TokenUsage(prompt_tokens=100_000, completion_tokens=0)  # $10 at the unknown rate
    calls: list[str] = []
    backend = CountingBackend(*[_tool_turn(str(i), usage) for i in range(5)])

    result = AgentRunner(backend, recorder=None).run(
        _spec(
            calls,
            budget=Budget(max_cost_usd=5.0, prices=PRICES),
            max_steps=5,
            model="who/knows",
        ),
        PROMPT,
    )

    assert result.stop_reason == STOP_COST_LIMIT
    assert result.cost_usd == pytest.approx(10.0)
    # The same usage against the *priced* model is a tenth of the cost and does not trip.
    assert PRICES.price("cheap/model").cost(usage) == pytest.approx(0.1)


def test_a_completion_with_no_usage_is_still_billed():
    """A proxy that omits `usage` must not hand the run a free pass past the cap."""
    calls: list[str] = []
    long_answer = "x" * 30_000  # ~10k estimated tokens
    backend = CountingBackend(
        CompletionResponse(text=long_answer),
        CompletionResponse(text=long_answer),
    )

    result = AgentRunner(backend, recorder=None).run(
        _spec(calls, budget=Budget(max_cost_usd=0.001, prices=PRICES), max_steps=5), PROMPT
    )

    assert result.cost_usd > 0
    assert result.usage.estimated is True
    assert result.usage.total_tokens > 0


def test_estimated_usage_counts_the_prompt_and_the_tool_arguments():
    response = CompletionResponse(
        text="ok",
        tool_calls=(RequestedToolCall(id="1", name="read_file", arguments='{"note": "a"}'),),
    )
    usage = estimate_usage([{"role": "user", "content": "abcdef"}], response)

    assert usage.estimated is True
    assert usage.prompt_tokens > 0
    assert usage.completion_tokens > 0


# --------------------------------------------------------------------------------------
# 3. Wall-clock cap
# --------------------------------------------------------------------------------------


def test_the_wall_clock_cap_stops_the_agent_without_sleeping():
    clock = FakeClock()
    calls: list[str] = []

    class SlowBackend(CountingBackend):
        def complete(self, request):
            clock.advance(30.0)  # every turn "takes" half a minute
            return super().complete(request)

    backend = SlowBackend(*[_tool_turn(str(i)) for i in range(10)])
    runner = AgentRunner(backend, recorder=None, clock=clock)

    result = runner.run(
        _spec(calls, budget=Budget(max_seconds=45.0, prices=PRICES), max_steps=10), PROMPT
    )

    assert result.stop_reason == STOP_TIME_LIMIT
    assert backend.calls == 2, "the clock is checked before another call is made"
    assert calls == ["0", "1"], "work done before the deadline is preserved"


def test_a_slow_tool_is_caught_at_the_top_of_the_next_turn():
    """The cap bounds the *next* request; a long tool call cannot buy another one."""
    clock = FakeClock()
    calls: list[str] = []

    def slow_handler(args: Ping) -> str:
        clock.advance(120.0)
        calls.append(args.note)
        return "slow"

    tools = ToolRegistry(
        [Tool(name="bash", description="slow", parameters=Ping, handler=slow_handler)],
        owner="test_agent",
    )
    spec = AgentSpec(
        name="test_agent",
        system_prompt="s",
        model="cheap/model",
        tools=tools,
        max_steps=10,
        budget=Budget(max_seconds=60.0, prices=PRICES),
    )
    backend = CountingBackend(
        *[
            CompletionResponse(
                tool_calls=(RequestedToolCall(id=f"c{i}", name="bash", arguments="{}"),)
            )
            for i in range(5)
        ]
    )

    result = AgentRunner(backend, recorder=None, clock=clock).run(spec, PROMPT)

    assert result.stop_reason == STOP_TIME_LIMIT
    assert backend.calls == 1


def test_cost_wins_over_time_when_both_are_blown():
    """Two blown caps, one stop reason: report the one that cost money."""
    clock = FakeClock()
    ledger = BudgetLedger(
        Budget(max_cost_usd=0.5, max_seconds=1.0, prices=PRICES), "cheap/model", clock=clock
    )
    ledger.charge(TokenUsage(prompt_tokens=1_000_000, completion_tokens=0))
    clock.advance(10.0)

    assert ledger.exceeded() == STOP_COST_LIMIT


# --------------------------------------------------------------------------------------
# 4. The caps come from config
# --------------------------------------------------------------------------------------


def test_budgets_are_read_from_the_standing_instructions():
    config = StandingInstructions.model_validate(
        {
            "agents": {
                "email_agent": {
                    "model": "cheap/model",
                    "max_steps": 2,
                    "max_cost_usd": 0.25,
                    "max_seconds": 30,
                }
            },
            "pricing": {
                "unknown_input_per_mtok": 99.0,
                "unknown_output_per_mtok": 99.0,
                "models": {"cheap/model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
            },
        }
    )

    budget = budget_for(config, EMAIL_AGENT)

    assert budget.max_cost_usd == 0.25
    assert budget.max_seconds == 30
    assert budget.prices.price("cheap/model").input_per_mtok == 1.0
    assert budget.prices.price("nobody/knows").known is False
    assert budget.prices.price("nobody/knows").input_per_mtok == 99.0


def test_a_config_change_alone_changes_where_an_agent_stops(tmp_path):
    """The acceptance criterion, through the real factory: edit config, agent stops sooner."""
    from runner.agents import project_agent
    from runner.tools_project import WorktreeScope

    config = StandingInstructions.model_validate(
        {
            "agents": {"project_agent": {"model": "cheap/model", "max_cost_usd": 0.5}},
            "pricing": {"models": {"cheap/model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}},
        }
    )
    spec = project_agent(config, scope=WorktreeScope(tmp_path), goal="tidy up", max_steps=10)
    assert spec.budget.max_cost_usd == 0.5

    usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=0)  # $1.00 per turn
    backend = CountingBackend(
        *[
            CompletionResponse(
                text="",
                tool_calls=(
                    RequestedToolCall(id=f"c{i}", name="read_file", arguments='{"path": "x"}'),
                ),
                usage=usage,
            )
            for i in range(5)
        ]
    )

    result = AgentRunner(backend, recorder=None).run(spec, [PromptPart.trusted("go")])

    assert result.stop_reason == STOP_COST_LIMIT
    assert backend.calls == 1
    assert result.agent == PROJECT_AGENT
