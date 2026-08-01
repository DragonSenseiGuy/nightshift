"""NightShift agent runner (Phase 6).

The layer between "we have a model endpoint" and "we have agents that are safe to point
at an inbox". Three things live here and nowhere else:

- `agent_runner` — the agent loop, behind an `LLMBackend` seam so the SDK is swappable.
- `taint` — the summary-as-data rule, enforced by types instead of good intentions.
- `tools`/`tools_email`/`tools_project` — typed tools and per-agent allowlists.

Import from the submodules; this file only re-exports the handful of names that callers
outside `runner/` legitimately need.
"""

from runner.agent_runner import (
    AgentResult,
    AgentRunner,
    AgentSpec,
    CompletionRequest,
    CompletionResponse,
    LLMBackend,
    RequestedToolCall,
    run_agent,
)
from runner.taint import (
    TAINT_CALENDAR,
    TAINT_EMAIL,
    TAINT_WEB,
    PromptPart,
    TaintViolation,
)
from runner.tools import Tool, ToolCallRecord, ToolError, ToolRegistry, ToolScopeError

__all__ = [
    "TAINT_CALENDAR",
    "TAINT_EMAIL",
    "TAINT_WEB",
    "AgentResult",
    "AgentRunner",
    "AgentSpec",
    "CompletionRequest",
    "CompletionResponse",
    "LLMBackend",
    "PromptPart",
    "RequestedToolCall",
    "TaintViolation",
    "Tool",
    "ToolCallRecord",
    "ToolError",
    "ToolRegistry",
    "ToolScopeError",
    "run_agent",
]
