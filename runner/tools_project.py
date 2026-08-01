"""The project agent's toolset: `bash`, `read_file`, `write_file`, scoped to a worktree.

The other half of per-agent scoping. The project agent writes code; it has no broker
tools at all, so email and calendar data are not merely "not passed to it" — they are
unreachable. That is what lets Phase 9 hand it a real repo and a real shell.

**Path scoping.** Every path argument is resolved (symlinks included) and must land under
the worktree root, which is itself fully resolved once at construction. Absolute paths,
`..` traversal, and symlinks pointing outside all fail with `PathScopeError`. Resolving
*before* comparing is the whole trick: string prefix checks are defeated by a symlink, by
`/worktree/../etc`, and by a sibling directory that merely starts with the same name.

**On defence in depth.** In Phase 9 this toolset runs inside the sandbox container, where
the worktree is the only thing mounted and the container is the real boundary. The checks
here are the second lock, not the first — they are what makes the tools safe to unit-test
and safe to run host-side during development, and they mean a container misconfiguration
is not immediately a filesystem escape.

**`report_work`** (Phase 9) is the agent's structured "what I did": prose and bullets into
an `AgentWorkReport` the host renders into the briefing. It is a *tool* rather than a parse
of the final message so the shape is validated at the boundary. Note what it cannot say —
branch, commits, diff path — all of which the host derives from git afterwards. The agent
describes; the host attests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from models import AgentWorkReport
from runner.tools import Tool, ToolError, ToolRegistry

# Cap what a tool can hand back, so one `cat` of a large file cannot blow the context
# budget (and, in Phase 12, the cost cap).
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 120


class PathScopeError(ToolError):
    """A path argument pointed outside the worktree. Reported to the model, never followed."""


class WorktreeScope:
    """A resolved worktree root, and the only way to turn agent input into a real path."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"worktree root is not a directory: {self.root}")

    def resolve(self, relative: str) -> Path:
        """Resolve a model-supplied path inside the worktree, or refuse it."""
        if not relative or relative.strip() in {"", "."}:
            raise PathScopeError("path must not be empty")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise PathScopeError(f"absolute paths are not allowed: {relative!r}")

        # strict=False: writing a new file is legitimate, so the leaf need not exist —
        # but every existing component (and any symlink among them) is still followed.
        resolved = (self.root / candidate).resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise PathScopeError(
                f"{relative!r} resolves outside the worktree ({resolved}); refusing."
            )
        return resolved


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path relative to the worktree root")


class WriteFileArgs(BaseModel):
    path: str = Field(description="Path relative to the worktree root")
    content: str = Field(description="Full new contents of the file")


class BashArgs(BaseModel):
    command: str = Field(min_length=1, description="Shell command, run inside the worktree")
    timeout: int = Field(default=DEFAULT_TIMEOUT, gt=0, le=900, description="Seconds")


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[truncated at {MAX_OUTPUT_CHARS} characters]"


def read_file_tool(scope: WorktreeScope) -> Tool:
    def handler(args: ReadFileArgs) -> str:
        path = scope.resolve(args.path)
        try:
            return _truncate(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise ToolError(f"could not read {args.path!r}: {exc}") from exc

    return Tool(
        name="read_file",
        description="Read a UTF-8 text file from the project worktree.",
        parameters=ReadFileArgs,
        handler=handler,
    )


def write_file_tool(scope: WorktreeScope) -> Tool:
    def handler(args: WriteFileArgs) -> str:
        path = scope.resolve(args.path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"could not write {args.path!r}: {exc}") from exc
        return f"wrote {len(args.content)} characters to {args.path}"

    return Tool(
        name="write_file",
        description="Write a UTF-8 text file in the project worktree, creating directories.",
        parameters=WriteFileArgs,
        handler=handler,
    )


def bash_tool(scope: WorktreeScope) -> Tool:
    def handler(args: BashArgs) -> str:
        try:
            completed = subprocess.run(
                ["bash", "-lc", args.command],
                cwd=str(scope.root),
                capture_output=True,
                text=True,
                timeout=args.timeout,
                # A non-zero exit is information for the agent, not an exception for us.
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"command timed out after {args.timeout}s") from exc
        except OSError as exc:
            raise ToolError(f"could not run command: {exc}") from exc

        parts = [f"exit code: {completed.returncode}"]
        if completed.stdout:
            parts.append(f"stdout:\n{completed.stdout}")
        if completed.stderr:
            parts.append(f"stderr:\n{completed.stderr}")
        return _truncate("\n".join(parts))

    return Tool(
        name="bash",
        description="Run a shell command with the project worktree as the working directory.",
        parameters=BashArgs,
        handler=handler,
    )


class ReportWorkArgs(BaseModel):
    summary: str = Field(
        default="",
        description="A few plain sentences on what you changed and why. No markup.",
    )
    highlights: list[str] = Field(
        default_factory=list, description="Short bullets, one per notable change"
    )
    completed: bool = Field(
        default=False, description="True only if tonight's goal is fully done"
    )


class WorkSink:
    """Collects the agent's `report_work` call for the host to read back.

    A sink object rather than a return value because the report arrives mid-loop, and a
    plain holder keeps `report_work` a normal tool. Last call wins: an agent that revises
    its summary after more work should not end up with two contradictory reports.
    """

    def __init__(self) -> None:
        self.report: AgentWorkReport | None = None

    def record(self, report: AgentWorkReport) -> None:
        self.report = report.clamp()


def report_work_tool(sink: WorkSink) -> Tool:
    def handler(args: ReportWorkArgs) -> str:
        sink.record(
            AgentWorkReport(
                summary=args.summary[:4000],
                highlights=[item[:500] for item in args.highlights[:50]],
                completed=args.completed,
            )
        )
        return "recorded; this will appear in the morning briefing"

    return Tool(
        name="report_work",
        description=(
            "Record what you did tonight for the morning briefing. Call this once, last. "
            "Do not describe the branch, commits or diff — the host fills those in."
        ),
        parameters=ReportWorkArgs,
        handler=handler,
    )


def project_toolset(
    scope: WorktreeScope, *, agent: str = "project_agent", sink: WorkSink | None = None
) -> ToolRegistry:
    """The project agent's complete allowlist. No broker tools, by design."""
    return ToolRegistry(
        [
            read_file_tool(scope),
            write_file_tool(scope),
            bash_tool(scope),
            report_work_tool(sink if sink is not None else WorkSink()),
        ],
        owner=agent,
    )
