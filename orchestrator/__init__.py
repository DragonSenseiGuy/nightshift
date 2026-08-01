"""The host-side daemon layer: scheduling, power, caffeinate, watchdog (Phase 10).

`orchestrator/` is the spec's home for everything that decides *when* and *whether* a
night runs, as opposed to `runner/` (how an agent runs), `sandbox/` (where it runs) and
the broker (what it may read). Nothing here talks to an LLM, and nothing here holds a
secret — it starts the run and gets out of the way.

Submodules are imported lazily by the CLI so `python -m orchestrator schedule install`
works on a machine whose Google/LLM dependencies are not installed yet.
"""

from orchestrator.caffeinate import keep_awake
from orchestrator.power import PowerState, power_refusal, read_power_state

__all__ = ["PowerState", "keep_awake", "power_refusal", "read_power_state"]
