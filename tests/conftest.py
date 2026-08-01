"""Test-wide guards that keep the suite off the user's real state.

Three pieces of host state outlive a run: the approval queue, the transcript store
(Phase 12) and the snapshot store (Phase 13). All three default to `~/Library/Application
Support/NightShift/`, and all three are written by code paths the tests exercise for real —
`run_night` opens a run-history row before it does anything else, and `run_project_night`
snapshots the repo before it does anything else. So the databases are redirected into a
per-session temp directory here rather than in each test that happens to remember.

The transcript recorder is reset between tests for the same reason: it is process-global
(`runner.observe.use_recorder`), and a test that installs one must not silently record the
next test's agent runs into its own store.

Phase 16 added the fourth guard, and the one with teeth: **no test may reach a model.**
See `_offline_llm` below and `tests/offline_llm.py`.
"""

from __future__ import annotations

import pytest

from offline_llm import OFFLINE_API_KEY, offline_client
from runner.observe import reset_recorder


@pytest.fixture(autouse=True, scope="session")
def _isolated_state(tmp_path_factory) -> None:
    state = tmp_path_factory.mktemp("nightshift-state")
    import os

    os.environ.setdefault("NIGHTSHIFT_TRANSCRIPTS_DB", str(state / "transcripts.db"))
    os.environ.setdefault("NIGHTSHIFT_APPROVALS_DB", str(state / "approvals.db"))
    os.environ.setdefault("NIGHTSHIFT_SNAPSHOTS_DB", str(state / "snapshots.db"))


@pytest.fixture(autouse=True)
def _clean_recorder() -> None:
    reset_recorder()
    yield
    reset_recorder()


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch) -> None:
    """No test reaches a real model, and no test spends the user's key.

    `runner/backends.py` is the only module in the repo that constructs an LLM client, so
    replacing its `OpenAI` symbol closes every path at once — including the ones that do
    not thread a `backend=` through (`run_night` → `summarise.build_digest` →
    `runner.backends.backend_for`, which is how the suite was quietly making live calls
    until Phase 16). The key is overwritten too, so even a path that somehow built a real
    client would authenticate as nobody.

    A test that wants a specific completion patches `backends.OpenAI` (or injects a
    backend) itself; the later `monkeypatch.setattr` wins over this one.
    """
    from runner import backends

    monkeypatch.setenv("OPENROUTER_API_KEY", OFFLINE_API_KEY)
    monkeypatch.setattr(backends, "OpenAI", offline_client)


@pytest.fixture(autouse=True)
def notifications(monkeypatch) -> list[list[str]]:
    """No test puts a real banner on the developer's screen — and every test can see them.

    `run_night` calls `notify_night` from its outermost `finally` with no seams threaded
    through, so any test that runs a night was posting a genuine macOS notification. This
    wraps the *real* `notify_night` (headline building, backend choice and argv assembly
    all still run for real) and injects the `runner`/`which` seams `send()` already has, so
    the banner is recorded instead of posted.

    Returns the list of argv it captured, so a test can assert the night notified. A test
    that patches `notify_night` itself wins over this, as `tests/test_notifications.py` does.
    """
    from orchestrator import notify

    posted: list[list[str]] = []

    class _Completed:
        returncode = 0
        stderr = b""

    def record(command, **kwargs):
        posted.append(list(command))
        return _Completed()

    def fake_which(name: str) -> str | None:
        # Pin the backend so the assertion does not depend on whether the machine running
        # the suite happens to have `brew install terminal-notifier`ed.
        return "/usr/bin/osascript" if name == "osascript" else None

    real = notify.notify_night

    def wrapper(briefing, **kwargs):
        kwargs.setdefault("runner", record)
        kwargs.setdefault("which", fake_which)
        return real(briefing, **kwargs)

    monkeypatch.setattr(notify, "notify_night", wrapper)
    return posted


@pytest.fixture(autouse=True)
def _offline_day(monkeypatch) -> None:
    """Keep the calendar/task stage (Phase 14) offline by default.

    `run_night` now reads today's calendar and tasks through the broker. Every existing
    night test would otherwise try to open a socket to `localhost:8400` and gain a spurious
    "broker unreachable" entry in its briefing — and a `--mock` night would call a real LLM
    to plan a canned day. An empty day short-circuits `build_day_plan` before any model
    call, so this is the quiet default.

    Tests that are *about* the calendar (`tests/test_calendar_tasks.py`) simply patch
    `_load_day` again; the later `monkeypatch.setattr` wins.
    """
    from orchestrator import nightly

    monkeypatch.setattr(nightly, "_load_day", lambda **kwargs: ([], [], []))
