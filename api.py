"""The broker: the host-side process that owns Google access.

Everything else — including anything running in the sandbox — asks this API for clean,
Pydantic-shaped JSON instead of touching Google itself. The credential it uses is
read-only (`gmail.readonly` + `calendar.readonly` + optional `tasks.readonly`, one slot);
the send capability lives in `send_emails.py` and is deliberately not importable from here,
so no route on this surface can emit mail.

Routes: `/health`, `/emails?since=`, `/calendar?day=`, `/tasks`, and the briefing-section
pair agents contribute through.

`--mock` (or `NIGHTSHIFT_MOCK=1`) serves the canned inbox, day and task list from
`fixtures/` with no Google client constructed and no network call — the offline path for
tests, the sandbox, and the prompt-injection regression fixtures (one per untrusted
source: email *and* calendar).
"""

import argparse
import os

from fastapi import FastAPI, HTTPException, Query

from calendar_tasks import fetch_calendar, fetch_tasks, resolve_day
from emails import fetch_emails_last_x_hours, get_read_credentials
from fixtures.mock_calendar import mock_calendar_events, mock_tasks
from fixtures.mock_emails import mock_emails
from models import (
    BriefingSection,
    CalendarResponse,
    EmailsResponse,
    TasksResponse,
    parse_since,
)

MOCK_ENV_VAR = "NIGHTSHIFT_MOCK"

app = FastAPI(
    title="NightShift",
    description="Fetches your recent Gmail as clean, Pydantic-shaped JSON.",
    version="0.1.0",
)


def mock_enabled() -> bool:
    """Whether the broker serves canned data instead of calling Gmail.

    Read per-request from the environment (rather than captured at import) so tests can
    flip it with `monkeypatch.setenv` and the sandbox can set it like any other config.
    """
    return os.getenv(MOCK_ENV_VAR, "").strip().lower() not in ("", "0", "false", "no")


@app.get("/health")
def health() -> dict[str, str]:
    """Cheap readiness probe (no Gmail call), polled while the broker boots."""
    return {"status": "ok", "mode": "mock" if mock_enabled() else "live"}


@app.get("/emails", response_model=EmailsResponse)
def get_emails(
    since: str = Query(
        "8h",
        description="Lookback window: '8h', '30m', '2d', '1w', or a bare number of hours.",
        examples=["8h"],
    ),
) -> EmailsResponse:
    """Return emails received within the `since` window."""
    try:
        hours = parse_since(since)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if mock_enabled():
        # Canned data: the window is echoed back but never applied — the fixture set is
        # fixed so downstream tests stay deterministic whatever `since` they ask for.
        emails = mock_emails()
    else:
        emails = fetch_emails_last_x_hours(get_read_credentials(), hours=hours)

    return EmailsResponse(
        since=since,
        hours=round(hours, 4),
        count=len(emails),
        emails=emails,
    )


# --------------------------------------------------------------------------------------
# Calendar & tasks (Phase 14)
# --------------------------------------------------------------------------------------
#
# Same contract as `/emails`: the broker owns the Google credential, the caller gets
# validated JSON, and the caller is assumed to be untrusted. Two differences worth knowing:
#
# - **These routes answer 200 even when the source is unreadable**, with the reason in
#   `degraded`. A 5xx would make an unavailable calendar indistinguishable from a broken
#   broker, and the nightly run needs to tell those apart to keep going (see
#   `calendar_tasks.py`).
# - **Google Tasks is an optional scope.** "not authorised" is an expected `degraded`
#   answer here, not a failure.


@app.get("/calendar", response_model=CalendarResponse)
def get_calendar(
    day: str = Query(
        "today",
        description="Which day: 'today', 'tomorrow', 'yesterday' or 'YYYY-MM-DD'.",
        examples=["today"],
    ),
) -> CalendarResponse:
    """Return one day's calendar events as data. Event text is untrusted."""
    try:
        label, on = resolve_day(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if mock_enabled():
        # Canned data: the day is echoed back but never applied, exactly as `/emails`
        # ignores its window, so downstream tests stay deterministic.
        events = mock_calendar_events(label)
        return CalendarResponse(
            day=label, date=on.isoformat(), count=len(events), events=events
        )

    return fetch_calendar(get_read_credentials(), day=day)


@app.get("/tasks", response_model=TasksResponse)
def get_tasks() -> TasksResponse:
    """Return open Google Tasks as data. Task text is untrusted."""
    if mock_enabled():
        tasks = mock_tasks()
        return TasksResponse(count=len(tasks), tasks=tasks)

    return fetch_tasks(get_read_credentials())


# --------------------------------------------------------------------------------------
# Briefing sections
# --------------------------------------------------------------------------------------
#
# `add_to_briefing` is a broker tool so a sandboxed agent can contribute to the morning
# artifact without any filesystem or network path to the host: it posts a *validated*
# `BriefingSection` over the same bridge it reads email on. The section is data — plain
# strings with length caps and `extra="forbid"` — never HTML. The host renders and escapes
# it, so a captured agent cannot inject markup into what the human reads.
#
# The store is in-memory and per-process: the broker lives for exactly one nightly run,
# and a section that outlives its run would be a lie in tomorrow's briefing.

_MAX_SECTIONS = 50

_sections: list[BriefingSection] = []


def reset_sections() -> None:
    """Drop everything collected so far. Called at the start of a run (and by tests)."""
    _sections.clear()


@app.post("/briefing/sections", response_model=BriefingSection, status_code=201)
def add_briefing_section(section: BriefingSection) -> BriefingSection:
    """Accept one structured section from an agent.

    FastAPI validates against `BriefingSection` before we ever see it, so oversized or
    unknown fields are a 422 rather than something the renderer has to defend against.
    """
    if len(_sections) >= _MAX_SECTIONS:
        # Fail closed and loudly rather than silently dropping: a runaway agent flooding
        # the briefing is a failure worth surfacing, not something to quietly absorb.
        raise HTTPException(
            status_code=429,
            detail=f"briefing already holds {_MAX_SECTIONS} sections",
        )
    _sections.append(section)
    return section


@app.get("/briefing/sections", response_model=list[BriefingSection])
def list_briefing_sections() -> list[BriefingSection]:
    """Read the collected sections back host-side, to assemble the briefing."""
    return list(_sections)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="NightShift broker")
    parser.add_argument(
        "--mock",
        action="store_true",
        help=f"Serve canned emails, no Gmail call (same as {MOCK_ENV_VAR}=1).",
    )
    args = parser.parse_args()
    if args.mock:
        # Normalise the flag into the env var so there is exactly one source of truth.
        os.environ[MOCK_ENV_VAR] = "1"
    if mock_enabled():
        print("NightShift broker running in MOCK mode — no Gmail, no network.")

    # The nightly run starts the broker here, on the host, bound to a Unix socket that
    # is bridged into the sandbox — the container asks for email with no network route
    # to the broker at all. For ordinary local runs we keep listening on loopback TCP.
    socket_path = os.getenv("NIGHTSHIFT_BROKER_SOCKET")
    if socket_path:
        socket_dir = os.path.dirname(socket_path)
        if socket_dir:
            os.makedirs(socket_dir, exist_ok=True)
        # uvicorn won't bind if the socket file is already there from a prior run.
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        print(f"NightShift broker listening on unix socket {socket_path}")
        uvicorn.run(app, uds=socket_path)
    else:
        # Defaults to loopback, and should stay there: no container needs a TCP route
        # to the broker any more, so widening the bind only widens the attack surface.
        host = os.getenv("NIGHTSHIFT_API_HOST", "127.0.0.1")
        port = int(os.getenv("NIGHTSHIFT_API_PORT", "8400"))
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
