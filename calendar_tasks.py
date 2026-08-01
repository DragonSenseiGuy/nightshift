"""Google Calendar and Google Tasks *reading*: the broker's other half.

The sibling of `emails.py`, and read-only for the same reason: it runs inside the broker,
which must hold no capability to change anything. It uses the same `google_auth.READ_SLOT`
credential — one Keychain entry, one consent, three read scopes — and nothing here can
create, move or delete an event or a task.

Two decisions worth stating, because both are load-bearing:

**"Tasks" means Google Tasks.** It is a separate API (`tasks.readonly`) from Calendar, not
a view of it. It is also the scope a user is most likely to decline, so it is *optional* on
the read slot: `fetch_tasks` checks the granted scopes first and returns an empty,
`degraded` response instead of letting a 403 take the night down. Everything else the user
might call a "task" — action items from email — already comes from the email agent and
stays there.

**Nothing raises for an unavailable source.** Both fetchers return their response model with
a `degraded` reason on the failure paths (no credential, declined scope, API error,
unparsable payload). A night must always produce a briefing, and a briefing that says
"Calendar unavailable: the token expired" is worth more than a traceback nobody was awake
to read. Programming errors still raise — only *external* failure degrades.

Every string is clipped to its model's cap before validation: Google will happily hand you
a 200KB event description, and that is a token-budget problem long before it is a rendering
one.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from googleapiclient.discovery import build

from google_auth import (
    CALENDAR_READONLY,
    READ_SLOT,
    TASKS_READONLY,
    has_scope,
    load_credentials,
)
from models import CalendarEntry, CalendarResponse, TaskEntry, TasksResponse

# Exported for callers that want to inspect what the read path asks for.
READ_SCOPES = READ_SLOT.scopes
OPTIONAL_SCOPES = READ_SLOT.optional_scopes

# Google returns paginated lists; one night's calendar and task list are small, and a cap
# here is cheaper than a runaway loop at 3am.
_MAX_EVENTS = 100
_MAX_TASKS = 200
_MAX_TASK_LISTS = 20


def get_read_credentials(*, interactive: bool = True):
    """The broker's read-only Google credentials — the same slot `emails.py` uses."""
    return load_credentials(READ_SLOT, interactive=interactive)


def _clip(value, limit: int) -> str:
    """Coerce whatever Google sent to a capped string. Never raises."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


# --------------------------------------------------------------------------------------
# Day resolution
# --------------------------------------------------------------------------------------


def resolve_day(day: str = "today", *, today: date | None = None) -> tuple[str, date]:
    """Turn a `day=` query into (label, date). Raises `ValueError` on anything else.

    Deliberately tiny vocabulary — `today`, `tomorrow`, `yesterday`, or an ISO date. The
    briefing only ever asks for one day, and a permissive parser here would mean the broker
    accepting date arithmetic from whatever ends up calling it.
    """
    base = today or date.today()
    key = (day or "today").strip().lower()
    if key in ("", "today"):
        return "today", base
    if key == "tomorrow":
        return "tomorrow", base + timedelta(days=1)
    if key == "yesterday":
        return "yesterday", base - timedelta(days=1)
    try:
        return key, date.fromisoformat(key)
    except ValueError as exc:
        raise ValueError(
            f"Invalid day {day!r}. Use 'today', 'tomorrow', 'yesterday' or 'YYYY-MM-DD'."
        ) from exc


def _local_window(on: date) -> tuple[str, str]:
    """RFC3339 [start, end) bounds covering `on` in the host's local timezone."""
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(on, time.min, tzinfo=tz)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _format_clock(raw: str) -> str:
    """'2026-07-24T09:30:00+01:00' → '09:30'. Falls back to the raw string."""
    try:
        return datetime.fromisoformat(raw).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return _clip(raw, 64)


# --------------------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------------------


def normalise_event(raw: dict) -> CalendarEntry:
    """One Google Calendar API event → one `CalendarEntry`. Never raises.

    Attendee identities are taken as-is and capped; they are display text in the briefing
    and are never resolved, contacted, or used to make a decision.
    """
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    all_day = "date" in start and "dateTime" not in start

    attendees = []
    for person in (raw.get("attendees") or [])[:50]:
        if not isinstance(person, dict):
            continue
        label = person.get("displayName") or person.get("email") or ""
        if label:
            attendees.append(_clip(label, 320))

    organiser = raw.get("organizer") or {}
    return CalendarEntry(
        id=_clip(raw.get("id"), 1024),
        start="" if all_day else _format_clock(start.get("dateTime", "")),
        end="" if all_day else _format_clock(end.get("dateTime", "")),
        all_day=all_day,
        title=_clip(raw.get("summary"), 300) or "(no title)",
        location=_clip(raw.get("location"), 300),
        description=_clip(raw.get("description"), 4000),
        attendees=attendees,
        organiser=_clip(organiser.get("email") if isinstance(organiser, dict) else "", 320),
        status=_clip(raw.get("status"), 40),
    )


def fetch_calendar(credentials, day: str = "today") -> CalendarResponse:
    """Read one day's events. Returns a `degraded` response instead of raising."""
    try:
        label, on = resolve_day(day)
    except ValueError as exc:
        # The only caller-error path that reaches here; the broker turns it into a 422
        # before we are called, so treat it as a degradation rather than a crash.
        return CalendarResponse(day=day[:64], degraded=[str(exc)])

    response = CalendarResponse(day=label, date=on.isoformat())

    if not has_scope(credentials, CALENDAR_READONLY):
        response.degraded.append(
            "Calendar unavailable: the stored Google token does not grant "
            "calendar.readonly. Run `uv run python google_auth.py authorise read`."
        )
        return response

    time_min, time_max = _local_window(on)
    try:
        service = build("calendar", "v3", credentials=credentials)
        payload = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,  # expand recurrences into real instances
                orderBy="startTime",
                maxResults=_MAX_EVENTS,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable calendar is a degraded night
        response.degraded.append(f"Calendar unavailable: {exc!r}")
        return response

    for raw in (payload.get("items") or [])[:_MAX_EVENTS]:
        if not isinstance(raw, dict):
            continue
        if raw.get("status") == "cancelled":
            continue
        try:
            response.events.append(normalise_event(raw))
        except Exception as exc:  # noqa: BLE001 - one bad event must not lose the day
            response.degraded.append(f"Skipped an unreadable event ({type(exc).__name__}).")

    response.count = len(response.events)
    return response


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------


def normalise_task(raw: dict, list_title: str = "") -> TaskEntry:
    """One Google Tasks API task → one `TaskEntry`. Never raises."""
    due = _clip(raw.get("due"), 64)
    return TaskEntry(
        id=_clip(raw.get("id"), 1024),
        title=_clip(raw.get("title"), 300) or "(untitled task)",
        notes=_clip(raw.get("notes"), 4000),
        # Google returns an RFC3339 timestamp whose time part is always midnight UTC;
        # only the date half means anything, so only the date half is kept.
        due=due[:10] if len(due) >= 10 else due,
        status=_clip(raw.get("status"), 40) or "needsAction",
        list_title=_clip(list_title, 200),
    )


def fetch_tasks(credentials) -> TasksResponse:
    """Read open tasks across every task list. Degrades instead of raising.

    The declined-scope path is an ordinary outcome, not an error: `tasks.readonly` is
    optional on the read slot (see `google_auth.READ_SLOT`).
    """
    response = TasksResponse()

    if not has_scope(credentials, TASKS_READONLY):
        response.degraded.append(
            "Tasks unavailable: Google Tasks access was not granted. It is optional — "
            "run `uv run python google_auth.py authorise read` and tick it to enable "
            "task triage."
        )
        return response

    try:
        service = build("tasks", "v1", credentials=credentials)
        lists = (service.tasklists().list(maxResults=_MAX_TASK_LISTS).execute() or {}).get(
            "items"
        ) or []
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        response.degraded.append(f"Tasks unavailable: {exc!r}")
        return response

    for task_list in lists[:_MAX_TASK_LISTS]:
        if not isinstance(task_list, dict) or not task_list.get("id"):
            continue
        title = _clip(task_list.get("title"), 200)
        try:
            payload = (
                service.tasks()
                .list(
                    tasklist=task_list["id"],
                    showCompleted=False,  # triage is about what is still open
                    showHidden=False,
                    maxResults=_MAX_TASKS,
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - one unreadable list, not the whole set
            response.degraded.append(f"Could not read task list {title!r}: {exc!r}")
            continue

        for raw in (payload.get("items") or [])[:_MAX_TASKS]:
            if not isinstance(raw, dict) or raw.get("status") == "completed":
                continue
            try:
                response.tasks.append(normalise_task(raw, title))
            except Exception as exc:  # noqa: BLE001
                response.degraded.append(f"Skipped an unreadable task ({type(exc).__name__}).")
            if len(response.tasks) >= _MAX_TASKS:
                break

    response.count = len(response.tasks)
    return response
