"""Typed tools and per-agent tool scoping.

A `Tool` is a name, a Pydantic parameter model, and a handler. The Pydantic model is not
decoration: it is what gets advertised to the model as a JSON schema *and* what validates
the arguments coming back, so a hallucinated argument shape fails here rather than three
frames deeper inside a subprocess call.

Scoping is the security half. Each agent gets its own `ToolRegistry` holding only the
tools it is allowed, and the runner can invoke nothing else. Two failure modes are
deliberately distinguished:

- **Unknown tool** — a name no tool in this process defines. That is a model mistake
  (fumbled function name); the runner hands the error back and lets it retry.
- **Out-of-scope tool** — a name that *does* exist somewhere in NightShift but is not in
  this agent's registry, e.g. the email agent reaching for `bash`. That is a security
  event: `ToolScopeError`, fatal, loud, never a silent no-op. Something either went
  wrong in our wiring or an injection is steering the agent, and both deserve a stopped
  run and a Failures entry in the briefing.

Every invocation is recorded as a `ToolCallRecord` (Pydantic, so Phase 12 can persist the
transcript to SQLite without inventing a shape).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

# Every tool name NightShift defines, declared statically rather than discovered.
#
# This set decides whether a denied call is a security event or a model typo, so it must
# not depend on which toolsets happen to have been constructed yet: on a night where no
# project agent ever runs, the email agent reaching for `bash` must *still* be a scope
# violation and not a shrugged-off "no such tool". Add new tools here as they land.
RESERVED_TOOL_NAMES = frozenset(
    {
        # broker read tools + briefing contribution (email/calendar agents)
        "read_emails",
        "read_calendar",
        "read_tasks",
        "add_to_briefing",
        # worktree tools (project agent)
        "bash",
        "read_file",
        "write_file",
        "report_work",
    }
)

# Names actually instantiated in this process, unioned with the reserved set above so a
# tool added in a later phase is classified correctly even before it is listed.
_KNOWN_TOOL_NAMES: set[str] = set(RESERVED_TOOL_NAMES)


def known_tool_names() -> frozenset[str]:
    return frozenset(_KNOWN_TOOL_NAMES)


class ToolError(RuntimeError):
    """The tool ran and failed for an ordinary reason; reported back to the model."""


class ToolScopeError(RuntimeError):
    """An agent reached for a tool outside its allowlist. Fatal by design."""


class ToolCallRecord(BaseModel):
    """One tool call and its outcome — the unit of an agent transcript."""

    step: int = Field(description="1-based agent loop iteration this call happened in")
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: str = Field(default="", description="What the tool returned, as the model saw it")
    error: str | None = Field(default=None, description="Failure message, if the call failed")
    taint: list[str] = Field(
        default_factory=list, description="Taint labels this result introduced"
    )


@dataclass(frozen=True, slots=True)
class Tool:
    """A callable the model may invoke, with a validated argument schema."""

    name: str
    description: str
    parameters: type[BaseModel]
    handler: Callable[[Any], str]
    taint: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _KNOWN_TOOL_NAMES.add(self.name)

    def schema(self) -> dict[str, Any]:
        """OpenAI-style function-tool declaration."""
        schema = self.parameters.model_json_schema()
        schema.pop("title", None)
        schema["additionalProperties"] = False
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def invoke(self, raw_arguments: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Validate arguments, run the handler, return (result_text, parsed_arguments)."""
        if isinstance(raw_arguments, str):
            try:
                raw = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ToolError(f"arguments were not valid JSON: {exc}") from exc
        else:
            raw = raw_arguments
        if not isinstance(raw, dict):
            raise ToolError("arguments must be a JSON object")

        try:
            parsed = self.parameters.model_validate(raw)
        except ValidationError as exc:
            raise ToolError(f"invalid arguments: {exc.errors()}") from exc

        return self.handler(parsed), parsed.model_dump(mode="json")


class ToolRegistry:
    """One agent's tool allowlist. Immutable once built — scope cannot widen mid-run."""

    def __init__(self, tools: Iterable[Tool] = (), *, owner: str = "") -> None:
        self._tools: dict[str, Tool] = {}
        self.owner = owner
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool {tool.name!r} in {owner or 'registry'}")
            self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def get(self, name: str) -> Tool:
        """Resolve a tool, failing closed on anything not in this agent's allowlist."""
        tool = self._tools.get(name)
        if tool is not None:
            return tool
        if name in _KNOWN_TOOL_NAMES:
            raise ToolScopeError(
                f"Agent {self.owner or '<unnamed>'} tried to call {name!r}, which is not "
                f"in its allowlist ({sorted(self.names) or 'no tools'}). Refusing: "
                "per-agent tool scoping is a security boundary, not a preference."
            )
        raise ToolError(f"no such tool {name!r}; available tools: {sorted(self.names)}")

    def taint_of(self, name: str) -> frozenset[str]:
        return self.get(name).taint
