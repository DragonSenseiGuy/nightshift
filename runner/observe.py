"""What a finished agent run looks like as a record, and where records go (Phase 12).

Split from `transcripts.py` on purpose. This module is *staged into the sandbox* with the
rest of `runner/`, so it must contain no database, no host paths and no notion of where
the user's Application Support directory is. It knows two things: the shape of a run
record, and how to append one to a file. The host's SQLite store, the run history and the
replay CLI live in `transcripts.py`, which the sandbox never sees.

That split is also how the project agent's transcript gets home. The container has no
route to the host's database — giving it one would be a writable channel out of the
sandbox — so it writes JSON lines into the same mounted drop directory it already uses for
`project_work.json`, and the host imports the file after the container exits. Untrusted
output crossing a boundary as validated data, exactly like the work report.

**A record is storage and display, never a prompt.** `AgentRunRecord` carries the taint
its run accumulated, and there is deliberately no method here (or in `transcripts.py`)
that turns one back into a `runner.taint.PromptPart`. Replay renders a transcript for a
human to read. Feeding one to an agent would be laundering untrusted text through the
database, which is the exact move security rule 2 forbids.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from models import TokenUsage
from runner.tools import ToolCallRecord

# Env var naming a JSON-lines file to append run records to. Set by the sandbox
# orchestrator for a containerised run; unset on the host, where the SQLite recorder in
# `transcripts.py` is installed explicitly by the entry point instead.
JSONL_ENV_VAR = "NIGHTSHIFT_TRANSCRIPT_JSONL"

# Env var carrying the id of the night this run belongs to, so a sandboxed agent's record
# can be joined to the right row of the run history without a channel back to the host.
NIGHT_ID_ENV_VAR = "NIGHTSHIFT_NIGHT_ID"

# Caps on what we are willing to store per run. A transcript is written by a model that
# may be under an attacker's influence; unbounded fields would make the database a
# denial-of-service target with a very long fuse.
MAX_TEXT = 100_000
MAX_MESSAGES = 400
MAX_TOOL_CALLS = 500


class AgentRunRecord(BaseModel):
    """One agent run, complete enough to replay without the process that made it.

    `messages` is the whole model conversation; `transcript` is the ordered tool calls and
    their results. Both are kept because they answer different questions in the morning:
    "what did it do" and "what was it told".
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=64, description="Stable replay id (also ProjectWork.transcript_id)")
    night_id: str = Field(default="", max_length=64, description="Which night this belongs to")
    agent: str = Field(max_length=60)
    model: str = Field(default="", max_length=200)
    source: str = Field(default="host", max_length=20, description="'host' or 'sandbox'")
    project: str = Field(default="", max_length=120, description="Project, for project runs")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stop_reason: str = Field(default="completed", max_length=40)
    steps: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = Field(default=0.0, ge=0.0)
    taint: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Untrusted sources this run touched. Kept on the stored row so a "
        "replay can never be mistaken for trusted text.",
    )
    text: str = Field(default="", max_length=MAX_TEXT, description="The final model message")
    transcript: list[ToolCallRecord] = Field(default_factory=list, max_length=MAX_TOOL_CALLS)
    messages: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_MESSAGES)

    @property
    def seconds(self) -> float:
        return max((self.finished_at - self.started_at).total_seconds(), 0.0)

    @property
    def tainted(self) -> bool:
        return bool(self.taint)


def _clip_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim a message list to what we are willing to store, oldest-first.

    The *system* message and the first user message are the ones a reader needs to make
    sense of the rest, so an over-long conversation loses its middle, not its head.
    """
    if len(messages) <= MAX_MESSAGES:
        return messages
    keep_head = 2
    trimmed = messages[:keep_head]
    trimmed.append(
        {"role": "system", "content": f"[{len(messages) - MAX_MESSAGES} message(s) elided]"}
    )
    trimmed.extend(messages[-(MAX_MESSAGES - keep_head - 1) :])
    return trimmed


def record_from_result(
    result,
    *,
    night_id: str = "",
    source: str = "host",
    project: str = "",
    run_id: str = "",
) -> AgentRunRecord:
    """Turn an `AgentResult` into the storable record. Pure; touches nothing."""
    return AgentRunRecord(
        id=run_id or uuid.uuid4().hex,
        night_id=night_id or os.getenv(NIGHT_ID_ENV_VAR, "")[:64],
        agent=result.agent[:60],
        model=result.model[:200],
        source=source,
        project=project[:120],
        started_at=result.started_at,
        finished_at=result.finished_at,
        stop_reason=result.stop_reason[:40],
        steps=result.steps,
        usage=result.usage,
        cost_usd=max(result.cost_usd, 0.0),
        taint=sorted(result.taint),
        text=result.text[:MAX_TEXT],
        transcript=list(result.transcript)[:MAX_TOOL_CALLS],
        messages=_clip_messages(list(result.messages)),
    )


class Recorder(Protocol):
    """Where a finished run goes. Write-only, by design — there is no `read` here."""

    def record(self, result) -> AgentRunRecord | None: ...


class JsonlRecorder:
    """Appends records to a JSON-lines file. The sandbox's half of the transcript path.

    One line per run, append-only, no database: the container must be able to write this
    with nothing but a mounted directory, and a partially written file must still yield
    every complete run before it.
    """

    def __init__(
        self, path: Path | str, *, night_id: str = "", source: str = "sandbox", project: str = ""
    ) -> None:
        self.path = Path(path)
        self.night_id = night_id or os.getenv(NIGHT_ID_ENV_VAR, "")
        self.source = source
        self.project = project

    def record(self, result) -> AgentRunRecord:
        record = record_from_result(
            result, night_id=self.night_id, source=self.source, project=self.project
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        return record


def parse_jsonl(raw: str) -> list[AgentRunRecord]:
    """Validate JSON-lines transcript text into records, skipping lines that do not fit.

    The text was written inside the sandbox, so it is untrusted input on the way back:
    every line is re-validated, and a corrupt one costs that record rather than the import.
    """
    records: list[AgentRunRecord] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(AgentRunRecord.model_validate_json(line))
        except Exception as exc:  # noqa: BLE001 - pydantic raises several shapes
            print(f"Skipping an unreadable transcript line: {exc!r}")
    return records


def read_jsonl(path: Path | str) -> list[AgentRunRecord]:
    """`parse_jsonl` over a file. A missing file is an empty list, not an error: a
    container that crashed before its first agent run wrote no transcript, and that is a
    fact about the night rather than a failure of the import."""
    try:
        return parse_jsonl(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


# --------------------------------------------------------------------------------------
# The installed recorder
# --------------------------------------------------------------------------------------
#
# Same pattern as `config.active_config`: an entry point installs one, and everything
# downstream stays free of plumbing. The default is *no* recorder rather than a database
# at a default path, because a library import must never create host state — a unit test
# that runs an agent should not silently start writing to the user's transcript store.

_active: Recorder | None = None


def use_recorder(recorder: Recorder | None) -> None:
    """Install the recorder every subsequent `AgentRunner` will use."""
    global _active
    _active = recorder


def active_recorder() -> Recorder | None:
    """The installed recorder, falling back to the sandbox's JSONL drop if configured."""
    global _active
    if _active is None:
        path = os.getenv(JSONL_ENV_VAR, "").strip()
        if path:
            _active = JsonlRecorder(path)
    return _active


def reset_recorder() -> None:
    """Forget the installed recorder (tests, and a long-lived UI between runs)."""
    global _active
    _active = None
