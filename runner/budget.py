"""Per-agent spend, step and wall-clock caps — the runner's stop conditions (Phase 12).

The step cap has existed since Phase 6; this module adds the two that actually protect
money and mornings, and puts all three behind one object so an agent's whole budget is a
single field on its `AgentSpec` rather than three loose ints.

Four decisions worth the words:

1. **Caps stop a run, they never raise.** A budget is not an error condition — it is the
   design working. `AgentRunner` records `stop_reason` (`cost_limit` / `time_limit` /
   `step_limit`) and returns everything the agent produced up to that point. A run that
   died with an exception loses its transcript at exactly the moment the transcript is
   most interesting.

2. **Caps are checked before spending, not after.** The ledger is consulted at the top of
   each loop iteration, so a run that has already blown its budget never pays for one more
   completion. The consequence is that the *final* call may take the total slightly past
   the cap — that is deliberate: a hard mid-flight abort would throw away work already paid
   for. The cap bounds the next request, not the last one.

3. **An unpriced model is priced as the most expensive thing we know.** Costs come from
   `[pricing]` in the standing instructions. A model slug we have no price for is billed at
   `[pricing].unknown_*`, which defaults to frontier-tier rates. Under-pricing an unknown
   model would silently disable the cost cap for exactly the case where you least want it
   disabled: someone swapped a slug in the config and nobody re-checked the price table.
   Over-pricing merely stops a run early and says so in the briefing.

4. **A completion with no `usage` is still billed.** Not every OpenAI-compatible proxy
   returns token counts (and no stubbed test backend does). Treating "no usage" as "free"
   would silently switch the cost cap off, so an unmetered call is charged against an
   estimate built from the bytes actually sent and received, at a deliberately low
   3 characters per token, and the run's usage is flagged `estimated` all the way into the
   stored transcript. Charging the full `max_tokens` allowance instead was the first
   attempt and it is *too* pessimistic: it bills a two-word answer like a novel, and a cap
   that stops every run on its second step is a cap nobody keeps switched on.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from models import TokenUsage

# Stop reasons. `completed` and `step_limit` predate this module and are spelled the same
# way on purpose — `project_step.py` and the briefing already read them.
STOP_COMPLETED = "completed"
STOP_STEP_LIMIT = "step_limit"
STOP_COST_LIMIT = "cost_limit"
STOP_TIME_LIMIT = "time_limit"

BUDGET_STOP_REASONS = frozenset({STOP_STEP_LIMIT, STOP_COST_LIMIT, STOP_TIME_LIMIT})

# What an unpriced model costs us, per million tokens, unless the config says otherwise.
# Deliberately frontier-tier (Opus/GPT-class list prices): see decision 3 above.
UNKNOWN_INPUT_PER_MTOK = 15.0
UNKNOWN_OUTPUT_PER_MTOK = 75.0

# Characters per token when we have to guess. The usual English rule of thumb is ~4, so
# dividing by 3 deliberately over-counts: an estimate a cost cap is computed from should
# err towards stopping the run, not towards spending more.
_CHARS_PER_TOKEN = 3


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens for one model."""

    input_per_mtok: float = UNKNOWN_INPUT_PER_MTOK
    output_per_mtok: float = UNKNOWN_OUTPUT_PER_MTOK
    known: bool = True

    def cost(self, usage: TokenUsage) -> float:
        return (
            usage.prompt_tokens * self.input_per_mtok
            + usage.completion_tokens * self.output_per_mtok
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class PriceList:
    """Model slug → price, with an explicit answer for slugs we do not know."""

    models: Mapping[str, ModelPrice] = field(default_factory=dict)
    unknown: ModelPrice = field(
        default_factory=lambda: ModelPrice(
            UNKNOWN_INPUT_PER_MTOK, UNKNOWN_OUTPUT_PER_MTOK, known=False
        )
    )

    def price(self, model: str) -> ModelPrice:
        return self.models.get(model, self.unknown)

    @classmethod
    def from_config(cls, config) -> PriceList:
        """Build the price list from `[pricing]` in the standing instructions.

        Takes the whole config rather than the section so callers can pass what they
        already have, and so a config object from an older file without the section still
        yields a usable (all-unknown) list.
        """
        pricing = getattr(config, "pricing", None)
        if pricing is None:
            return cls()
        return cls(
            models={
                slug: ModelPrice(entry.input_per_mtok, entry.output_per_mtok)
                for slug, entry in pricing.models.items()
            },
            unknown=ModelPrice(
                pricing.unknown_input_per_mtok,
                pricing.unknown_output_per_mtok,
                known=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class Budget:
    """The two caps that need money and a clock to evaluate, plus the price table.

    The *step* cap stays on `AgentSpec.max_steps`: it is a property of the agent's shape
    (the summariser is single-shot; the project agent gets forty turns) and every caller
    already sets it there. This object is what those callers could not express before.

    `0` disables a cap; it is spelled that way rather than `None` because the config file
    is hand-edited and "set it to zero to mean no limit" reads better in TOML than a key
    you have to delete. There is deliberately no way to disable the step cap — an
    unbounded agent loop is not a configuration we are willing to offer.
    """

    max_cost_usd: float = 0.0
    max_seconds: float = 0.0
    prices: PriceList = field(default_factory=PriceList)

    def describe(self) -> str:
        bits = [f"${self.max_cost_usd:.4f}" if self.max_cost_usd > 0 else "no cost cap"]
        bits.append(f"{self.max_seconds:g}s" if self.max_seconds > 0 else "no time cap")
        return ", ".join(bits)


def _characters(value) -> int:
    return len(value if isinstance(value, str) else str(value))


def estimate_usage(messages: list[dict], response) -> TokenUsage:
    """Usage for a completion the provider did not meter, from the bytes on the wire.

    Prompt tokens come from everything sent, completion tokens from the reply *and* the
    arguments of any tool calls it asked for — an agent that answers only in tool calls is
    not answering for free. The result is marked `estimated` so no stored cost figure can
    later be mistaken for a metered one.
    """
    prompt = sum(_characters(value) for message in messages for value in message.values())
    completion = _characters(getattr(response, "text", "") or "")
    for call in getattr(response, "tool_calls", ()) or ():
        completion += _characters(getattr(call, "name", "")) + _characters(
            getattr(call, "arguments", "")
        )
    return TokenUsage(
        prompt_tokens=prompt // _CHARS_PER_TOKEN,
        completion_tokens=completion // _CHARS_PER_TOKEN,
        estimated=True,
    )


class BudgetLedger:
    """One run's accounting. Mutable, lives exactly as long as an `AgentRunner.run` call.

    `clock` is injectable so the wall-clock cap can be tested without sleeping — a test
    that proves a timeout by actually waiting is a test nobody runs twice.
    """

    def __init__(
        self,
        budget: Budget,
        model: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self.price = budget.prices.price(model)
        self._clock = clock
        self._started = clock()
        self.usage = TokenUsage()
        self.cost_usd = 0.0
        self.calls = 0

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    @property
    def priced_by_guess(self) -> bool:
        """True if this run is being billed against the unknown-model fallback."""
        return not self.price.known

    def charge(self, usage: TokenUsage) -> None:
        """Add one completion's usage (metered or estimated) to the run's total."""
        self.usage = self.usage + usage
        self.cost_usd += self.price.cost(usage)
        self.calls += 1

    def exceeded(self) -> str:
        """The stop reason if a non-step cap has been blown, else `""`.

        Cost first: a run that has blown both caps is more usefully described by the one
        that cost money.
        """
        if self.budget.max_cost_usd > 0 and self.cost_usd >= self.budget.max_cost_usd:
            return STOP_COST_LIMIT
        if self.budget.max_seconds > 0 and self.elapsed >= self.budget.max_seconds:
            return STOP_TIME_LIMIT
        return ""

    def summary(self) -> str:
        """One line for a log or the briefing."""
        note = " (unpriced model — billed at the unknown-model rate)" if self.priced_by_guess else ""
        estimated = " estimated" if self.usage.estimated else ""
        return (
            f"{self.calls} call(s), {self.usage.total_tokens}{estimated} token(s), "
            f"${self.cost_usd:.4f}, {self.elapsed:.1f}s{note}"
        )
