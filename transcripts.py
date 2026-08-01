"""Run history and replayable agent transcripts (Phase 12).

Every agent run — email triage, project work, one a cap cut short — lands here as a row:
who ran, against which model, for how long, what it cost, why it stopped, and the full
ordered list of tool calls and messages. Every *night* lands in a second table with its
outcome. Together they answer the two questions you actually have at 8am: "what did it do
last night", and "why did it stop doing it".

**Why SQLite, per-operation connections, Pydantic on read.** Exactly the reasons in
`approvals.py`, and the same shape deliberately: stdlib only (the sandbox image bakes its
dependencies at build time), state that outlives the run that made it, and a database that
is untrusted input by the time you read it back, so every stored row is re-validated into
`AgentRunRecord` before anything renders it.

**Storage is not a prompt source.** A transcript contains email bodies, model prose written
after reading them, and tool output — the most thoroughly untrusted text in the system. It
is kept so a human can read it and so the briefing can point at it. There is deliberately
no function in this module that returns a `runner.taint.PromptPart`, and the taint labels
travel with the row so nothing downstream can mistake a replay for trusted text. Feeding a
stored run into another agent would launder untrusted data through the database, which is
security rule 2 with an extra hop.

**Replay from the briefing.** The briefing is a static HTML file that must survive being
emailed, so it cannot host a button. What it does instead is print the transcript id and
the exact command that replays it:

    uv run python transcripts.py replay <id>

`transcripts.py list` / `show` / `nights` / `prune` round out the CLI.

**Growth.** A night writes one row per agent run, each carrying its whole conversation —
a few hundred KB on a busy night with big email bodies. Left alone that is unbounded, so
`prune()` deletes runs older than `[retention].transcript_days` (30 by default) and the
nightly orchestrator calls it at the end of every run. Set the key to 0 to keep everything
and watch the file yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from models import TokenUsage
from runner.observe import (
    NIGHT_ID_ENV_VAR,
    AgentRunRecord,
    read_jsonl,
    record_from_result,
)

DB_ENV_VAR = "NIGHTSHIFT_TRANSCRIPTS_DB"

# Alongside the approval queue: state, not output. Both outlive the run that wrote them.
DEFAULT_DB_PATH = (
    Path.home() / "Library" / "Application Support" / "NightShift" / "transcripts.db"
)

REPLAY_COMMAND = "uv run python transcripts.py replay"


def default_db_path() -> Path:
    """Where the history lives, honouring `$NIGHTSHIFT_TRANSCRIPTS_DB` (tests, alt profiles)."""
    override = os.getenv(DB_ENV_VAR, "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


class TranscriptError(RuntimeError):
    """Base class for store misuse."""


class RunNotFound(TranscriptError):
    pass


class CorruptRun(TranscriptError):
    """A stored row no longer matches the schema and will not be replayed."""


# --------------------------------------------------------------------------------------
# The night, as history
# --------------------------------------------------------------------------------------


class NightOutcome(StrEnum):
    """How a night ended. A closed vocabulary so the UI can dispatch on it.

    `failed` means stages failed but the night finished and wrote a briefing; `crashed`
    means the run died and launchd may relaunch it. Keeping those apart is the difference
    between "read the Failures section" and "something is wrong with the machine".
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"
    CRASHED = "crashed"


class NightRunRecord(BaseModel):
    """One row of the run history: a whole night and what came of it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=64)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = Field(default=None)
    outcome: NightOutcome = Field(default=NightOutcome.RUNNING)
    refused: str = Field(default="", max_length=500, description="Power-guard reason, if any")
    failures: int = Field(default=0, ge=0)
    stages: list[str] = Field(default_factory=list, max_length=50)
    briefing_path: str = Field(default="", max_length=1000)
    seconds: float = Field(default=0.0, ge=0.0)
    note: str = Field(default="", max_length=1000)

    @property
    def ran(self) -> bool:
        return self.outcome is not NightOutcome.REFUSED


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id                TEXT PRIMARY KEY,
    night_id          TEXT NOT NULL DEFAULT '',
    agent             TEXT NOT NULL,
    model             TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'host',
    project           TEXT NOT NULL DEFAULT '',
    started_at        TEXT NOT NULL,
    finished_at       TEXT NOT NULL,
    stop_reason       TEXT NOT NULL DEFAULT 'completed',
    steps             INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    estimated         INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    taint             TEXT NOT NULL DEFAULT '',
    text              TEXT NOT NULL DEFAULT '',
    transcript        TEXT NOT NULL DEFAULT '[]',
    messages          TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS agent_runs_night ON agent_runs (night_id, started_at);
CREATE INDEX IF NOT EXISTS agent_runs_started ON agent_runs (started_at);

CREATE TABLE IF NOT EXISTS night_runs (
    id            TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT NOT NULL DEFAULT 'running',
    refused       TEXT NOT NULL DEFAULT '',
    failures      INTEGER NOT NULL DEFAULT 0,
    stages        TEXT NOT NULL DEFAULT '[]',
    briefing_path TEXT NOT NULL DEFAULT '',
    seconds       REAL NOT NULL DEFAULT 0.0,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS night_runs_started ON night_runs (started_at);
"""

_RUN_COLUMNS = (
    "id, night_id, agent, model, source, project, started_at, finished_at, stop_reason, "
    "steps, prompt_tokens, completion_tokens, estimated, cost_usd, taint, text, "
    "transcript, messages"
)

_NIGHT_COLUMNS = (
    "id, started_at, finished_at, outcome, refused, failures, stages, briefing_path, "
    "seconds, note"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TranscriptStore:
    """Durable agent-run transcripts and night history.

    A connection per operation, like `ApprovalQueue`: the writer is the nightly run and
    the readers are a CLI and (eventually) the UI, possibly in different processes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    # -- agent runs ------------------------------------------------------------------

    def save(self, record: AgentRunRecord) -> AgentRunRecord:
        """Store one run. Idempotent on id, so re-importing a JSONL file is safe."""
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO agent_runs ({_RUN_COLUMNS}) "
                f"VALUES ({', '.join('?' * 18)})",
                (
                    record.id,
                    record.night_id,
                    record.agent,
                    record.model,
                    record.source,
                    record.project,
                    record.started_at.isoformat(),
                    record.finished_at.isoformat(),
                    record.stop_reason,
                    record.steps,
                    record.usage.prompt_tokens,
                    record.usage.completion_tokens,
                    int(record.usage.estimated),
                    record.cost_usd,
                    ",".join(record.taint),
                    record.text,
                    json.dumps([call.model_dump(mode="json") for call in record.transcript]),
                    json.dumps(record.messages),
                ),
            )
        return record

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AgentRunRecord:
        """Re-validate a stored row. A row that no longer fits the schema is not replayed."""
        try:
            return AgentRunRecord(
                id=row["id"],
                night_id=row["night_id"],
                agent=row["agent"],
                model=row["model"],
                source=row["source"],
                project=row["project"],
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=datetime.fromisoformat(row["finished_at"]),
                stop_reason=row["stop_reason"],
                steps=row["steps"],
                usage=TokenUsage(
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    estimated=bool(row["estimated"]),
                ),
                cost_usd=row["cost_usd"],
                taint=[t for t in (row["taint"] or "").split(",") if t],
                text=row["text"],
                transcript=json.loads(row["transcript"] or "[]"),
                messages=json.loads(row["messages"] or "[]"),
            )
        except (ValueError, KeyError, ValidationError) as exc:
            raise CorruptRun(
                f"stored run {row['id']!r} does not match the current schema: {exc}"
            ) from exc

    def get(self, run_id: str) -> AgentRunRecord:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFound(f"no stored agent run with id {run_id!r}")
        return self._row_to_record(row)

    def runs(
        self,
        *,
        night_id: str | None = None,
        agent: str | None = None,
        limit: int = 50,
    ) -> list[AgentRunRecord]:
        """Stored runs, newest first."""
        query = f"SELECT {_RUN_COLUMNS} FROM agent_runs"
        clauses: list[str] = []
        params: list[object] = []
        if night_id is not None:
            clauses.append("night_id = ?")
            params.append(night_id)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at DESC, id LIMIT ?"
        params.append(max(limit, 1))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def import_records(
        self,
        records: Sequence[AgentRunRecord],
        *,
        night_id: str = "",
        project: str = "",
        source: str = "sandbox",
    ) -> list[AgentRunRecord]:
        """Store records that came from somewhere else (a sandbox's JSONL drop).

        The night id, project and source are re-stamped **host-side**: a sandboxed agent
        does not get to decide which night's history its record joins, or to claim it ran
        on the host.
        """
        imported: list[AgentRunRecord] = []
        for record in records:
            record = record.model_copy(
                update={
                    "night_id": night_id or record.night_id,
                    "project": project or record.project,
                    "source": source,
                }
            )
            imported.append(self.save(record))
        return imported

    def import_jsonl(self, path: Path | str, **stamps) -> list[AgentRunRecord]:
        """`import_records` over a JSON-lines file written by a sandbox run."""
        return self.import_records(read_jsonl(path), **stamps)

    # -- night history ---------------------------------------------------------------

    def start_night(self, night_id: str = "") -> NightRunRecord:
        """Open a run-history row. Written before any agent runs, so a crash still shows."""
        record = NightRunRecord(id=night_id or uuid.uuid4().hex, started_at=_now())
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO night_runs ({_NIGHT_COLUMNS}) "
                f"VALUES ({', '.join('?' * 10)})",
                (
                    record.id,
                    record.started_at.isoformat(),
                    None,
                    record.outcome.value,
                    "",
                    0,
                    "[]",
                    "",
                    0.0,
                    "",
                ),
            )
        return record

    def finish_night(
        self,
        night_id: str,
        *,
        outcome: NightOutcome,
        failures: int = 0,
        stages: Sequence[str] = (),
        briefing_path: str = "",
        seconds: float = 0.0,
        refused: str = "",
        note: str = "",
    ) -> NightRunRecord:
        """Close the run-history row with what actually happened."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE night_runs SET finished_at = ?, outcome = ?, refused = ?, "
                "failures = ?, stages = ?, briefing_path = ?, seconds = ?, note = ? "
                "WHERE id = ?",
                (
                    _now().isoformat(),
                    NightOutcome(outcome).value,
                    refused[:500],
                    max(failures, 0),
                    json.dumps(list(stages)[:50]),
                    briefing_path[:1000],
                    max(seconds, 0.0),
                    note[:1000],
                    night_id,
                ),
            )
        return self.night(night_id)

    @staticmethod
    def _row_to_night(row: sqlite3.Row) -> NightRunRecord:
        try:
            return NightRunRecord(
                id=row["id"],
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=(
                    datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
                ),
                outcome=NightOutcome(row["outcome"]),
                refused=row["refused"],
                failures=row["failures"],
                stages=json.loads(row["stages"] or "[]"),
                briefing_path=row["briefing_path"],
                seconds=row["seconds"],
                note=row["note"],
            )
        except (ValueError, KeyError, ValidationError) as exc:
            raise CorruptRun(
                f"stored night {row['id']!r} does not match the current schema: {exc}"
            ) from exc

    def night(self, night_id: str) -> NightRunRecord:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_NIGHT_COLUMNS} FROM night_runs WHERE id = ?", (night_id,)
            ).fetchone()
        if row is None:
            raise RunNotFound(f"no run history for night {night_id!r}")
        return self._row_to_night(row)

    def nights(self, limit: int = 30) -> list[NightRunRecord]:
        """Run history, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_NIGHT_COLUMNS} FROM night_runs ORDER BY started_at DESC LIMIT ?",
                (max(limit, 1),),
            ).fetchall()
        return [self._row_to_night(row) for row in rows]

    def night_cost(self, night_id: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM agent_runs WHERE night_id = ?",
                (night_id,),
            ).fetchone()
        return float(row["total"])

    def spend(self, *, days: int = 30, now: datetime | None = None) -> list[tuple[str, float, int]]:
        """`(agent, USD, runs)` over the last `days`, most expensive first.

        The host-side half of "defense in depth against runner bugs": the per-agent caps
        bound one run, the provider's hard key cap bounds the month, and this is how you
        see the number in between before either of them fires. Note that these are *our*
        figures, from `[pricing]` — the provider's dashboard is the authority.
        """
        cutoff = ((now or _now()) - timedelta(days=max(days, 0))).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent, SUM(cost_usd) AS total, COUNT(*) AS runs FROM agent_runs "
                "WHERE started_at >= ? GROUP BY agent ORDER BY total DESC",
                (cutoff,),
            ).fetchall()
        return [(row["agent"], float(row["total"] or 0.0), int(row["runs"])) for row in rows]

    # -- retention -------------------------------------------------------------------

    def prune(self, *, older_than_days: int, now: datetime | None = None) -> tuple[int, int]:
        """Delete history older than `older_than_days`. Returns (agent runs, nights).

        `0` (or less) keeps everything — an explicit "I will manage this myself" rather
        than a default that quietly deletes the evidence of a bad night. Transcripts are
        the largest thing NightShift stores, so the default policy is finite by design and
        the exact retention lives in `[retention]`.
        """
        if older_than_days <= 0:
            return (0, 0)
        cutoff = ((now or _now()) - timedelta(days=older_than_days)).isoformat()
        with self._connect() as conn:
            runs = conn.execute("DELETE FROM agent_runs WHERE started_at < ?", (cutoff,)).rowcount
            nights = conn.execute(
                "DELETE FROM night_runs WHERE started_at < ?", (cutoff,)
            ).rowcount
        return (runs, nights)

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


# --------------------------------------------------------------------------------------
# The recorder the runner writes through
# --------------------------------------------------------------------------------------


class SqliteRecorder:
    """`runner.observe.Recorder` over a `TranscriptStore`. Host-side only.

    Holds the night id so every run made during that night joins the right history row
    without each call site having to thread it through.
    """

    def __init__(
        self,
        store: TranscriptStore,
        *,
        night_id: str = "",
        source: str = "host",
        project: str = "",
    ) -> None:
        self.store = store
        self.night_id = night_id
        self.source = source
        self.project = project

    def record(self, result) -> AgentRunRecord:
        return self.store.save(
            record_from_result(
                result, night_id=self.night_id, source=self.source, project=self.project
            )
        )


@contextmanager
def recording_night(
    store: TranscriptStore | None = None, *, night_id: str = ""
) -> Iterator[tuple[TranscriptStore, str]]:
    """Install a SQLite recorder for the duration of a night and stamp the environment.

    The env stamp (`NIGHTSHIFT_NIGHT_ID`) is what carries the night id into the sandbox:
    the container writes it onto its own JSONL records, and the host re-stamps it on import
    anyway, so a captured agent can at worst mislabel a record it also wrote.
    """
    from runner.observe import reset_recorder, use_recorder

    store = store or TranscriptStore()
    night = store.start_night(night_id)
    previous = os.environ.get(NIGHT_ID_ENV_VAR)
    os.environ[NIGHT_ID_ENV_VAR] = night.id
    use_recorder(SqliteRecorder(store, night_id=night.id))
    try:
        yield store, night.id
    finally:
        reset_recorder()
        if previous is None:
            os.environ.pop(NIGHT_ID_ENV_VAR, None)
        else:
            os.environ[NIGHT_ID_ENV_VAR] = previous


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def replay_text(record: AgentRunRecord, *, full: bool = False) -> str:
    """Render a stored run for a human to read.

    Plain text, never HTML and never a prompt: every line here is email-derived model
    output, and the only safe thing to do with it is show it to a person who is expecting
    exactly that. The banner is not decoration — it is the reminder that everything below
    it is data.
    """
    lines = [
        f"Agent run {record.id}",
        f"  agent        {record.agent} ({record.source})"
        + (f" on project {record.project}" if record.project else ""),
        f"  model        {record.model or '(unrecorded)'}",
        f"  night        {record.night_id or '(none)'}",
        f"  started      {record.started_at.isoformat(timespec='seconds')}",
        f"  duration     {record.seconds:.1f}s over {record.steps} step(s)",
        f"  stop reason  {record.stop_reason}",
        f"  usage        {record.usage.total_tokens} token(s)"
        + (" (estimated)" if record.usage.estimated else "")
        + f", ${record.cost_usd:.4f}",
        f"  taint        {', '.join(record.taint) or 'none'}",
        "",
    ]
    if record.tainted:
        lines += [
            "!! This transcript contains text derived from untrusted sources "
            f"({', '.join(record.taint)}).",
            "!! It is shown as data. Never paste it into an agent prompt.",
            "",
        ]

    lines.append(f"Tool calls ({len(record.transcript)}):")
    if not record.transcript:
        lines.append("  (none — the agent used no tools)")
    for index, call in enumerate(record.transcript, 1):
        status = "ok" if call.ok else f"FAILED: {call.error}"
        lines.append(f"  {index:>3}. step {call.step} · {call.tool} · {status}")
        if call.arguments:
            lines.append(f"       args   {json.dumps(call.arguments)[:2000]}")
        if call.result:
            lines.append(f"       result {call.result if full else _clip(call.result, 500)}")
        if call.taint:
            lines.append(f"       taint  {', '.join(call.taint)}")

    lines += ["", f"Messages ({len(record.messages)}):"]
    for index, message in enumerate(record.messages, 1):
        role = str(message.get("role", "?"))
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content)
        if message.get("tool_calls"):
            content = (content + " " if content else "") + json.dumps(message["tool_calls"])
        lines.append(f"  {index:>3}. [{role}] {content if full else _clip(content, 800)}")

    lines += ["", "Final message:", record.text if full else record.text[:2000]]
    return "\n".join(lines)


def replay(run_id: str, *, store: TranscriptStore | None = None, full: bool = False) -> str:
    """Look a run up by id and render it. What "replayable from the briefing" means."""
    store = store or TranscriptStore()
    return replay_text(store.get(run_id), full=full)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _print_runs(records: list[AgentRunRecord]) -> None:
    if not records:
        print("No stored agent runs.")
        return
    print(f"{'id':<34}{'agent':<16}{'stop':<12}{'steps':>6}{'cost':>10}  started")
    for record in records:
        print(
            f"{record.id:<34}{record.agent[:15]:<16}{record.stop_reason[:11]:<12}"
            f"{record.steps:>6}{record.cost_usd:>10.4f}  "
            f"{record.started_at.isoformat(timespec='seconds')}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcripts", description="NightShift run history and agent transcripts."
    )
    parser.add_argument("--db", default=None, help="Transcript database path.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="Recent agent runs.")
    listing.add_argument("--night", default=None, help="Only runs from this night id.")
    listing.add_argument("--agent", default=None, help="Only runs by this agent.")
    listing.add_argument("--limit", type=int, default=25)

    nights = sub.add_parser("nights", help="Recent nights and how they ended.")
    nights.add_argument("--limit", type=int, default=20)

    for name, help_text in (
        ("show", "Print a stored run (summary form)."),
        ("replay", "Print a stored run in full, step by step."),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("id", help="Transcript id, as printed in the briefing.")

    spend = sub.add_parser("spend", help="What the agents have cost recently, per agent.")
    spend.add_argument("--days", type=int, default=30)

    prune = sub.add_parser("prune", help="Delete history older than N days.")
    prune.add_argument("--days", type=int, default=None, help="Default: [retention] in config.")

    args = parser.parse_args(argv)
    store = TranscriptStore(args.db)

    if args.command == "list":
        _print_runs(store.runs(night_id=args.night, agent=args.agent, limit=args.limit))
        return 0

    if args.command == "nights":
        history = store.nights(args.limit)
        if not history:
            print("No nights recorded yet.")
            return 0
        print(f"{'night id':<34}{'outcome':<12}{'fails':>6}{'secs':>8}{'cost':>10}  started")
        for night in history:
            print(
                f"{night.id:<34}{night.outcome.value:<12}{night.failures:>6}"
                f"{night.seconds:>8.1f}{store.night_cost(night.id):>10.4f}  "
                f"{night.started_at.isoformat(timespec='seconds')}"
            )
        return 0

    if args.command in {"show", "replay"}:
        try:
            print(replay(args.id, store=store, full=args.command == "replay"))
        except (RunNotFound, CorruptRun) as exc:
            print(exc)
            return 1
        return 0

    if args.command == "spend":
        rows = store.spend(days=args.days)
        if not rows:
            print(f"No agent runs in the last {args.days} day(s).")
            return 0
        print(f"{'agent':<20}{'runs':>6}{'USD':>12}")
        for agent, total, runs in rows:
            print(f"{agent[:19]:<20}{runs:>6}{total:>12.4f}")
        print(f"{'total':<20}{sum(r for _, _, r in rows):>6}{sum(c for _, c, _ in rows):>12.4f}")
        print(
            "\nThese are our own figures, computed from [pricing] in the standing "
            "instructions.\nYour provider's dashboard is the authority — and the place to "
            "set a hard spend cap."
        )
        return 0

    if args.command == "prune":
        days = args.days
        if days is None:
            from config import load_config

            days = load_config().retention.transcript_days
        runs, nights_deleted = store.prune(older_than_days=days)
        print(
            f"Deleted {runs} agent run(s) and {nights_deleted} night(s) older than "
            f"{days} day(s). Database is now {store.size_bytes() / 1024:.0f} KiB."
        )
        return 0

    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":
    sys.exit(main())
