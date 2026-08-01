"""Client for the NightShift broker (the read API in `api.py`).

The broker holds the Google OAuth token and is the only component that talks to
Google directly. Everything else asks the broker for clean, Pydantic-shaped JSON —
email, calendar, tasks — instead of touching Gmail, Calendar or Tasks itself.

The broker can be reached two ways, chosen entirely by configuration:

- **Unix socket** (`NIGHTSHIFT_BROKER_SOCKET`) — used from inside the sandbox
  container, which has no network route to the host. The socket file is mounted
  in, so the container can ask for email without any network access.
- **TCP** (`NIGHTSHIFT_API_URL`) — used for ordinary local runs on the host.

Because the choice is config-only, the summariser can move into the sandbox
without any code change beyond setting an env var.
"""

from __future__ import annotations

import os

import httpx

from models import (
    BriefingSection,
    CalendarResponse,
    Email,
    EmailsResponse,
    TasksResponse,
)

# Dummy authority used for Unix-socket requests; httpx still needs a host in the
# URL even though routing happens over the socket, not DNS.
_UDS_BASE_URL = "http://broker"
_DEFAULT_API_URL = "http://localhost:8400"


class BrokerClient:
    """Fetches emails from the broker over either a Unix socket or TCP."""

    def __init__(
        self,
        *,
        socket: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if socket:
            self._client = httpx.Client(
                transport=httpx.HTTPTransport(uds=socket),
                base_url=_UDS_BASE_URL,
                timeout=timeout,
            )
        else:
            self._client = httpx.Client(
                base_url=base_url or _DEFAULT_API_URL,
                timeout=timeout,
            )

    @classmethod
    def from_env(cls) -> "BrokerClient":
        """Build a client from the environment.

        Prefers the Unix socket when `NIGHTSHIFT_BROKER_SOCKET` is set, otherwise
        falls back to TCP via `NIGHTSHIFT_API_URL`.
        """
        return cls(
            socket=os.getenv("NIGHTSHIFT_BROKER_SOCKET"),
            base_url=os.getenv("NIGHTSHIFT_API_URL"),
        )

    def fetch_emails(self, since: str = "8h") -> list[Email]:
        """Pull emails from the broker (`GET /emails?since=...`)."""
        resp = self._client.get("/emails", params={"since": since})
        resp.raise_for_status()
        return EmailsResponse.model_validate(resp.json()).emails

    def fetch_calendar(self, day: str = "today") -> CalendarResponse:
        """Pull one day's events from the broker (`GET /calendar?day=...`).

        Returns the whole envelope rather than the event list, because `degraded` is part
        of the answer: "the calendar could not be read" has to reach the briefing, and a
        bare list would quietly look like a free day.
        """
        resp = self._client.get("/calendar", params={"day": day})
        resp.raise_for_status()
        return CalendarResponse.model_validate(resp.json())

    def fetch_tasks(self) -> TasksResponse:
        """Pull open tasks from the broker (`GET /tasks`). See `fetch_calendar`."""
        resp = self._client.get("/tasks")
        resp.raise_for_status()
        return TasksResponse.model_validate(resp.json())

    def add_briefing_section(self, section: BriefingSection) -> BriefingSection:
        """Contribute a structured section to the morning briefing.

        This is the sandbox's only write path to the host, and it carries data rather than
        markup: the broker re-validates the section, and the host escapes every string at
        render time. Nothing here is a side effect the user has to approve — the briefing
        is what they read *before* approving anything.
        """
        resp = self._client.post("/briefing/sections", json=section.model_dump(mode="json"))
        resp.raise_for_status()
        return BriefingSection.model_validate(resp.json())

    def briefing_sections(self) -> list[BriefingSection]:
        """Read back everything agents contributed during this run."""
        resp = self._client.get("/briefing/sections")
        resp.raise_for_status()
        return [BriefingSection.model_validate(item) for item in resp.json()]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BrokerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
