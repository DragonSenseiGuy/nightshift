"""Phase 12: run history and replayable transcripts.

What is on trial:

1. **Round trip.** Run an agent → store it → read it back → replay it, with every tool
   call present, in order, arguments and results intact. "Replayable" has to mean the
   stored row alone is enough; nothing here keeps the original result object around.
2. **The sandbox route.** A container writes JSON lines into its drop directory and the
   host imports them, re-stamping night id, project and source host-side. A sandboxed
   agent does not get to decide which night it belongs to.
3. **Run history.** One row per night, with the outcome the night actually had —
   completed, failed, refused — written even when the night went badly.
4. **Retention.** Old rows are pruned; recent ones are not.
5. **No laundering.** A stored transcript is display and audit only. Its taint survives
   storage, and the agent that accepts no taint refuses it. There is no API anywhere in
   the transcript layer that turns a stored run back into a prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

import briefing as briefing_module
import transcripts as transcripts_module
from config import StandingInstructions
from models import Briefing, ProjectSection, ProjectWork, TokenUsage
from orchestrator import nightly, power
from orchestrator.caffeinate import keep_awake
from runner.agent_runner import (
    AgentRunner,
    AgentSpec,
    CompletionResponse,
    RequestedToolCall,
)
from runner.agents import project_agent
from runner.observe import JsonlRecorder, parse_jsonl, use_recorder
from runner.taint import TAINT_EMAIL, PromptPart, TaintViolation
from runner.tools import Tool, ToolRegistry
from runner.tools_project import WorktreeScope
from transcripts import (
    NightOutcome,
    RunNotFound,
    SqliteRecorder,
    TranscriptStore,
    replay,
    replay_text,
)

AC = "Now drawing from 'AC Power'"


# --- doubles ----------------------------------------------------------------------------


class Args(BaseModel):
    note: str = ""


def _agent(sink: list[str]) -> AgentSpec:
    def handler(args: Args) -> str:
        sink.append(args.note)
        return f"read {args.note}"

    return AgentSpec(
        name="email_agent",
        system_prompt="triage the mail",
        model="cheap/model",
        tools=ToolRegistry(
            [Tool(
                name="read_emails",
                description="read",
                parameters=Args,
                handler=handler,
                taint=frozenset({TAINT_EMAIL}),
            )],
            owner="email_agent",
        ),
        accepts_taint=frozenset({TAINT_EMAIL}),
        max_steps=5,
    )


class Scripted:
    def __init__(self, *responses: CompletionResponse) -> None:
        self.responses = list(responses)

    def complete(self, request):
        return self.responses.pop(0) if self.responses else CompletionResponse(text="done")


def _call(index: int, note: str) -> CompletionResponse:
    return CompletionResponse(
        tool_calls=(
            RequestedToolCall(id=f"c{index}", name="read_emails", arguments=f'{{"note": "{note}"}}'),
        ),
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
    )


@pytest.fixture
def store(tmp_path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "transcripts.db")


def _run_a_recorded_agent(store: TranscriptStore, *, night_id: str = "") -> str:
    """Run a two-tool-call agent through a SQLite recorder; return its transcript id."""
    sink: list[str] = []
    runner = AgentRunner(
        Scripted(
            _call(1, "alpha"),
            _call(2, "beta"),
            CompletionResponse(
                text="triaged", usage=TokenUsage(prompt_tokens=100, completion_tokens=50)
            ),
        ),
        recorder=SqliteRecorder(store, night_id=night_id),
    )
    runner.run(
        _agent(sink),
        [PromptPart.tainted("subject: hello", {TAINT_EMAIL}, label="emails")],
    )
    return store.runs(limit=1)[0].id


# --------------------------------------------------------------------------------------
# 1. Round trip
# --------------------------------------------------------------------------------------


def test_a_run_is_stored_and_replays_with_its_tool_calls_in_order(store):
    run_id = _run_a_recorded_agent(store)

    record = store.get(run_id)
    assert record.agent == "email_agent"
    assert record.model == "cheap/model"
    assert record.stop_reason == "completed"
    assert record.steps == 3
    assert record.text == "triaged"
    assert [call.tool for call in record.transcript] == ["read_emails", "read_emails"]
    assert [call.arguments["note"] for call in record.transcript] == ["alpha", "beta"]
    assert [call.result for call in record.transcript] == ["read alpha", "read beta"]
    assert [call.step for call in record.transcript] == [1, 2]
    # The whole conversation is kept too, so a replay can show what it was told.
    assert any("subject: hello" in str(message) for message in record.messages)

    text = replay(run_id, store=store, full=True)
    assert run_id in text
    assert text.index("alpha") < text.index("beta"), "tool calls replay in order"
    assert "read alpha" in text and "read beta" in text
    assert "triaged" in text


def test_usage_and_cost_survive_the_round_trip(store):
    run_id = _run_a_recorded_agent(store)
    record = store.get(run_id)

    assert record.usage.prompt_tokens == 300  # three metered calls of 100
    assert record.usage.completion_tokens == 150
    assert record.usage.estimated is False
    assert record.cost_usd >= 0.0
    assert record.seconds >= 0.0


def test_a_capped_run_is_stored_with_the_reason_it_stopped(store):
    sink: list[str] = []
    spec = _agent(sink)
    capped = AgentSpec(
        name=spec.name,
        system_prompt=spec.system_prompt,
        model=spec.model,
        tools=spec.tools,
        accepts_taint=spec.accepts_taint,
        max_steps=2,
    )
    AgentRunner(
        Scripted(_call(1, "a"), _call(2, "b"), _call(3, "c")),
        recorder=SqliteRecorder(store),
    ).run(capped, [PromptPart.tainted("mail", {TAINT_EMAIL})])

    record = store.runs(limit=1)[0]
    assert record.stop_reason == "step_limit"
    assert len(record.transcript) == 2, "the partial work is what makes the record useful"
    assert "step_limit" in replay_text(record)


def test_an_unknown_id_is_an_error_not_an_empty_replay(store):
    with pytest.raises(RunNotFound):
        store.get("nope")


def test_a_corrupt_row_refuses_to_replay(store, tmp_path):
    run_id = _run_a_recorded_agent(store)
    import sqlite3

    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE agent_runs SET started_at = 'not a date' WHERE id = ?", (run_id,))
    conn.commit()
    conn.close()

    with pytest.raises(transcripts_module.CorruptRun):
        store.get(run_id)


# --------------------------------------------------------------------------------------
# 2. The sandbox route
# --------------------------------------------------------------------------------------


def test_a_sandbox_jsonl_drop_imports_and_is_restamped_host_side(store, tmp_path):
    drop = tmp_path / "out" / "agent_runs.jsonl"
    sink: list[str] = []
    # This is what happens inside the container: the recorder is a file, not a database.
    AgentRunner(
        Scripted(_call(1, "alpha"), CompletionResponse(text="worked")),
        recorder=JsonlRecorder(drop, night_id="a-night-the-container-made-up"),
    ).run(_agent(sink), [PromptPart.tainted("mail", {TAINT_EMAIL})])

    assert drop.exists()
    imported = store.import_jsonl(drop, night_id="the-real-night", project="nightshift")

    assert len(imported) == 1
    record = store.get(imported[0].id)
    assert record.night_id == "the-real-night", "the host decides which night a run joins"
    assert record.project == "nightshift"
    assert record.source == "sandbox"
    assert [call.tool for call in record.transcript] == ["read_emails"]


def test_importing_the_same_drop_twice_does_not_duplicate_runs(store, tmp_path):
    drop = tmp_path / "agent_runs.jsonl"
    sink: list[str] = []
    AgentRunner(Scripted(CompletionResponse(text="ok")), recorder=JsonlRecorder(drop)).run(
        _agent(sink), [PromptPart.tainted("mail", {TAINT_EMAIL})]
    )

    store.import_jsonl(drop)
    store.import_jsonl(drop)

    assert len(store.runs(limit=10)) == 1


def test_a_corrupt_transcript_line_costs_that_record_and_nothing_else(tmp_path):
    good = (
        '{"id": "abc", "agent": "email_agent", "started_at": "2026-07-01T00:00:00Z", '
        '"finished_at": "2026-07-01T00:00:01Z"}'
    )
    records = parse_jsonl("\n".join([good, "{not json", ""]))

    assert [record.id for record in records] == ["abc"]


def test_the_project_night_puts_the_transcript_id_on_the_briefing_card(store, tmp_path):
    """`ProjectWork.transcript_id` → the briefing prints the exact replay command."""
    import nightly_project

    drop = tmp_path / "agent_runs.jsonl"
    sink: list[str] = []
    AgentRunner(Scripted(CompletionResponse(text="ok")), recorder=JsonlRecorder(drop)).run(
        _agent(sink), [PromptPart.tainted("mail", {TAINT_EMAIL})]
    )
    outcome = nightly_project.SandboxOutcome(
        transcript_jsonl=drop.read_text(encoding="utf-8")
    )

    transcript_id = nightly_project._store_transcript(
        store, outcome, project="nightshift", night_id="n1"
    )

    assert transcript_id
    assert store.get(transcript_id).project == "nightshift"

    html = briefing_module.render_briefing_html(
        Briefing(
            projects=ProjectSection(
                projects=[ProjectWork(project="nightshift", transcript_id=transcript_id)]
            )
        )
    )
    assert transcript_id in html
    assert briefing_module.REPLAY_HINT in html


def test_the_briefing_replay_hint_matches_the_cli(store):
    """The two spellings of the replay command must not drift apart."""
    assert briefing_module.REPLAY_HINT == transcripts_module.REPLAY_COMMAND


def test_a_transcript_that_cannot_be_stored_does_not_fail_the_night(tmp_path):
    import nightly_project

    class BrokenStore:
        def import_records(self, *args, **kwargs):
            raise RuntimeError("disk full")

    outcome = nightly_project.SandboxOutcome(transcript_jsonl='{"bogus": true}')
    assert nightly_project._store_transcript(
        BrokenStore(), outcome, project="p", night_id="n"
    ) == ""


# --------------------------------------------------------------------------------------
# 3. Run history
# --------------------------------------------------------------------------------------


def _no_caffeinate(monkeypatch):
    monkeypatch.setattr(
        nightly, "keep_awake", lambda **kwargs: keep_awake(enabled=False, spawn=lambda cmd: None)
    )


def _on_ac():
    return power.read_power_state(pmset_text=AC, clamshell_text="", displays_text="")


def test_a_successful_night_writes_a_completed_history_row(tmp_path, monkeypatch, store):
    _no_caffeinate(monkeypatch)
    monkeypatch.setattr(nightly, "_load_emails", lambda *a, **k: [])

    result = nightly.run_night(
        StandingInstructions(),
        out=tmp_path / "briefing.html",
        send=False,
        projects=False,
        caffeinate=False,
        power_state=_on_ac(),
        store=store,
    )

    assert result.night_id
    night = store.night(result.night_id)
    assert night.outcome is NightOutcome.COMPLETED
    assert night.finished_at is not None
    assert night.briefing_path == str(tmp_path / "briefing.html")
    assert "email_agent" in night.stages


def test_a_failed_stage_writes_a_failed_history_row(tmp_path, monkeypatch, store):
    _no_caffeinate(monkeypatch)
    monkeypatch.setattr(
        nightly,
        "_load_emails",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    result = nightly.run_night(
        StandingInstructions(),
        out=tmp_path / "briefing.html",
        send=False,
        projects=False,
        caffeinate=False,
        power_state=_on_ac(),
        store=store,
    )

    night = store.night(result.night_id)
    assert night.outcome is NightOutcome.FAILED
    assert night.failures == 1


def test_a_refused_night_is_recorded_as_refused(tmp_path, monkeypatch, store):
    _no_caffeinate(monkeypatch)
    battery = power.read_power_state(
        pmset_text="Now drawing from 'Battery Power'", clamshell_text="", displays_text=""
    )

    result = nightly.run_night(
        StandingInstructions(),
        out=tmp_path / "briefing.html",
        send=False,
        projects=False,
        caffeinate=False,
        power_state=battery,
        store=store,
    )

    night = store.night(result.night_id)
    assert night.outcome is NightOutcome.REFUSED
    assert night.refused
    assert night.ran is False


def test_a_crashing_night_is_recorded_as_crashed(tmp_path, monkeypatch, store):
    _no_caffeinate(monkeypatch)
    monkeypatch.setattr(
        nightly, "_write_briefing", lambda *a, **k: (_ for _ in ()).throw(SystemExit(3))
    )
    monkeypatch.setattr(nightly, "_load_emails", lambda *a, **k: [])

    with pytest.raises(SystemExit):
        nightly.run_night(
            StandingInstructions(),
            out=tmp_path / "briefing.html",
            send=False,
            projects=False,
            caffeinate=False,
            power_state=_on_ac(),
            store=store,
        )

    night = store.nights(limit=1)[0]
    assert night.outcome is NightOutcome.CRASHED


def test_the_night_gathers_the_cost_of_its_agent_runs(store):
    night = store.start_night()
    _run_a_recorded_agent(store, night_id=night.id)
    _run_a_recorded_agent(store, night_id=night.id)

    assert len(store.runs(night_id=night.id)) == 2
    assert store.night_cost(night.id) == pytest.approx(
        sum(record.cost_usd for record in store.runs(night_id=night.id))
    )


def test_history_lists_newest_first(store):
    first = store.start_night()
    second = store.start_night()
    store.finish_night(first.id, outcome=NightOutcome.COMPLETED)
    store.finish_night(second.id, outcome=NightOutcome.FAILED, failures=2)

    listed = store.nights(limit=5)
    assert {night.id for night in listed} == {first.id, second.id}
    assert listed[0].started_at >= listed[1].started_at


# --------------------------------------------------------------------------------------
# 4. Retention
# --------------------------------------------------------------------------------------


def test_pruning_deletes_old_history_and_keeps_recent_history(store):
    fresh = _run_a_recorded_agent(store)
    old_night = store.start_night()
    stale = store.get(fresh).model_copy(
        update={
            "id": "stale-run",
            "night_id": old_night.id,
            "started_at": datetime.now(timezone.utc) - timedelta(days=90),
        }
    )
    store.save(stale)
    import sqlite3

    conn = sqlite3.connect(store.path)
    conn.execute(
        "UPDATE night_runs SET started_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(), old_night.id),
    )
    conn.commit()
    conn.close()

    runs, nights = store.prune(older_than_days=30)

    assert (runs, nights) == (1, 1)
    assert store.get(fresh).id == fresh
    with pytest.raises(RunNotFound):
        store.get("stale-run")


def test_retention_zero_keeps_everything(store):
    _run_a_recorded_agent(store)
    assert store.prune(older_than_days=0) == (0, 0)
    assert len(store.runs(limit=5)) == 1


def test_the_default_retention_is_finite():
    """Storage growth is bounded by default; unbounded has to be a deliberate choice."""
    assert StandingInstructions().retention.transcript_days > 0


# --------------------------------------------------------------------------------------
# 5. No laundering
# --------------------------------------------------------------------------------------


def test_a_stored_transcript_keeps_its_taint(store):
    run_id = _run_a_recorded_agent(store)
    record = store.get(run_id)

    assert record.taint == [TAINT_EMAIL]
    assert record.tainted is True
    assert "UNTRUSTED" in replay_text(record).upper()


def test_a_stored_transcript_cannot_be_laundered_into_the_project_agent(store, tmp_path):
    """The point of the taint column: storage does not wash email-derived text clean."""
    run_id = _run_a_recorded_agent(store)
    record = store.get(run_id)

    config = StandingInstructions()
    spec = project_agent(config, scope=WorktreeScope(tmp_path), goal="do work")
    assert spec.accepts_taint == frozenset()

    # Re-entering the stored text into a prompt *with the taint it was stored with* is the
    # only honest way to do it — and the runner refuses.
    with pytest.raises(TaintViolation):
        AgentRunner(Scripted(CompletionResponse(text="never")), recorder=None).run(
            spec,
            [PromptPart.tainted(record.text, record.taint, label="last night's transcript")],
        )


def test_the_transcript_layer_offers_no_way_to_build_a_prompt():
    """A negative asserted by construction: no prompt API, no taint import, no bypass."""
    from pathlib import Path

    import runner.observe as observe

    for module in (transcripts_module, observe):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "PromptPart" not in source.replace("`runner.taint.PromptPart`", "")
        assert "runner.taint" not in source.replace("`runner.taint.PromptPart`", "")
    assert not hasattr(transcripts_module.AgentRunRecord, "as_prompt_part")


def test_the_recorder_never_takes_the_run_down_with_it(store):
    """Observability is best-effort: a broken recorder loses a transcript, not the run."""

    class Broken:
        def record(self, result):
            raise RuntimeError("disk full")

    use_recorder(Broken())
    sink: list[str] = []
    result = AgentRunner(Scripted(CompletionResponse(text="fine"))).run(
        _agent(sink), [PromptPart.tainted("mail", {TAINT_EMAIL})]
    )

    assert result.text == "fine"
