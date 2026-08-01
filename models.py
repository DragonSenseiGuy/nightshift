"""Shared Pydantic types that cross a boundary (broker ↔ client, LLM ↔ briefing).

Three families live here:

- `Email` / `EmailsResponse` — the broker's wire shape.
- The **email-summary schema** (`EmailDigest` and friends) — the structured output the
  summariser agent must produce. Nothing downstream consumes freeform LLM prose: the
  digest is data, and the briefing renderer (`briefing.py`) turns it into HTML with every
  model- and email-derived string escaped. That is the summary-as-data rule
  made concrete — a prompt injection can at worst set a wrong urgency, never emit markup
  or reach another agent's prompt.
- The **approval queue** types (`Action` and its per-type payloads) — the proposals a human
  approves in the morning. Nothing here executes anything; `approvals.py` does, and only
  after an explicit approve. That is the third security rule made concrete.
"""

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# One highlight bullet. Capped per *item*, not just per list: a 50-item cap does nothing
# if one item can be a megabyte, and this shape crosses the sandbox boundary as JSON.
Highlight = Annotated[str, StringConstraints(max_length=500)]


class Email(BaseModel):
    """A single fetched email, normalised for downstream use."""

    id: str = Field(description="Gmail message id")
    sender: str = Field(description="Raw From header")
    subject: str = Field(description="Subject header, or '(no subject)'")
    snippet: str | None = Field(default=None, description="Gmail-provided preview snippet")
    body: str = Field(default="", description="Decoded text/plain (or text/html) body")


class EmailsResponse(BaseModel):
    """Response envelope for GET /emails."""

    since: str = Field(description="The window that was requested, e.g. '8h'")
    hours: float = Field(description="The window resolved to hours")
    count: int = Field(description="Number of emails returned")
    emails: list[Email]


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_HOURS = {
    "s": 1 / 3600,
    "m": 1 / 60,
    "h": 1.0,
    "d": 24.0,
    "w": 24.0 * 7,
    "": 1.0,  # bare number defaults to hours
}


def parse_since(since: str) -> float:
    """Parse a duration string like '8h', '30m', '2d', '1w' into hours.

    A bare number (e.g. '8') is treated as hours.
    """
    match = _DURATION_RE.match(since)
    if not match:
        raise ValueError(
            f"Invalid duration {since!r}. Use formats like '8h', '30m', '2d', '1w'."
        )
    value, unit = match.groups()
    return float(value) * _UNIT_HOURS[unit.lower()]


# --------------------------------------------------------------------------------------
# Calendar & tasks — the broker's wire shapes (Phase 14)
# --------------------------------------------------------------------------------------
#
# These are the *raw* reads, deliberately separate from the `CalendarSection` /`TaskSection`
# the briefing renders further down. Same split as `Email` vs `EmailSummaryItem`, and for
# the same reason: an event title is attacker-controllable text (anyone who can send you an
# invite writes it), so it is untrusted input that happens to arrive from Google rather than
# from Gmail. It is fetched as data, labelled `TAINT_CALENDAR`, and only ever rendered.
#
# Every string is capped: these cross the broker bridge as JSON, and a 4MB event description
# is a denial-of-service on the token budget as much as on the renderer.


class CalendarEntry(BaseModel):
    """One event as the broker read it from Google Calendar. Untrusted text throughout."""

    id: str = Field(default="", max_length=1024, description="Google Calendar event id")
    start: str = Field(default="", max_length=64, description="Local start, e.g. '09:30'")
    end: str = Field(default="", max_length=64)
    all_day: bool = Field(default=False)
    title: str = Field(default="(no title)", max_length=300, description="Event summary")
    location: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=4000, description="Untrusted free text")
    attendees: list[str] = Field(default_factory=list, max_length=50)
    organiser: str = Field(default="", max_length=320)
    status: str = Field(default="", max_length=40, description="confirmed/tentative/…")


class CalendarResponse(BaseModel):
    """Response envelope for GET /calendar.

    `degraded` rather than an error status: a night where the calendar is unreadable must
    still produce a briefing that says so. The broker answers 200 with an empty day and a
    reason, and the reason reaches the Failures section (see `orchestrator/nightly.py`).
    """

    day: str = Field(default="", max_length=64, description="Which day was requested")
    date: str = Field(default="", max_length=32, description="Resolved ISO date")
    count: int = Field(default=0, ge=0)
    events: list[CalendarEntry] = Field(default_factory=list, max_length=200)
    degraded: list[str] = Field(default_factory=list, max_length=20)


class TaskEntry(BaseModel):
    """One open task as the broker read it from Google Tasks."""

    id: str = Field(default="", max_length=1024)
    title: str = Field(default="(untitled task)", max_length=300)
    notes: str = Field(default="", max_length=4000, description="Untrusted free text")
    due: str = Field(default="", max_length=64, description="ISO date, or '' if none")
    status: str = Field(default="needsAction", max_length=40)
    list_title: str = Field(default="", max_length=200, description="Which task list")


class TasksResponse(BaseModel):
    """Response envelope for GET /tasks. See `CalendarResponse.degraded`.

    Google Tasks is an *optional* scope (`google_auth.READ_SLOT.optional_scopes`), so
    `degraded` carrying "not authorised" is an ordinary, expected answer here.
    """

    count: int = Field(default=0, ge=0)
    tasks: list[TaskEntry] = Field(default_factory=list, max_length=500)
    degraded: list[str] = Field(default_factory=list, max_length=20)


# --------------------------------------------------------------------------------------
# Token accounting
# --------------------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """Tokens spent on one completion (or summed over a run).

    Lives here rather than in `runner/` because it crosses two boundaries: the LLM
    response on the way in, and the sandbox→host transcript file on the way out.

    `estimated` records that the provider did not report usage and the numbers were
    reconstructed worst-case (`runner.budget.estimate_usage`). Keeping that flag on the
    stored record means a cost figure in the briefing can never be mistaken for a metered
    one months later.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    estimated: bool = Field(
        default=False, description="True if these numbers were guessed, not metered"
    )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            # Any guessed component makes the total a guess. Rounding that away would be
            # the one lie this type exists to prevent.
            estimated=self.estimated or other.estimated,
        )


# --------------------------------------------------------------------------------------
# Email summary schema
# --------------------------------------------------------------------------------------


class Urgency(StrEnum):
    """How soon a human needs to look at an email.

    A closed vocabulary rather than a free string: the briefing sorts and colour-codes on
    it, so an unexpected value would have to be handled at render time in every consumer.
    """

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# Sort weight, most urgent first. Kept next to the enum so adding a level forces you to
# decide where it ranks.
URGENCY_RANK: dict[Urgency, int] = {
    Urgency.CRITICAL: 0,
    Urgency.HIGH: 1,
    Urgency.NORMAL: 2,
    Urgency.LOW: 3,
}


class DraftReply(BaseModel):
    """A *suggested* reply. Never sent — Phase 8 queues it for morning approval."""

    subject: str = Field(description="Suggested subject line, usually 'Re: ...'")
    body: str = Field(description="Plain-text reply body, ready for a human to edit")


class EmailSummaryItem(BaseModel):
    """The summariser's verdict on one email — the unit the briefing renders.

    `sender`/`subject` are copied from the fetched `Email` by the host, not taken from the
    model, so the briefing always attributes a summary to the mail it actually came from.
    """

    email_id: str = Field(description="Gmail message id this summary is about")
    sender: str = Field(default="", description="From header, filled in host-side")
    subject: str = Field(default="", description="Subject header, filled in host-side")
    summary: str = Field(description="One or two sentences: what this email is about")
    urgency: Urgency = Field(
        default=Urgency.NORMAL, description="How soon a human must act"
    )
    needs_reply: bool = Field(
        default=False, description="True if the sender is waiting on a response"
    )
    category: str = Field(
        default="other",
        description="Short bucket, e.g. 'finance', 'work', 'personal', 'notification'",
    )
    action_items: list[str] = Field(
        default_factory=list, description="Concrete things the human must do, if any"
    )
    draft_reply: DraftReply | None = Field(
        default=None,
        description="Suggested reply when needs_reply is true; null otherwise",
    )

    @property
    def rank(self) -> int:
        return URGENCY_RANK[self.urgency]


class EmailDigest(BaseModel):
    """The whole email section of a morning briefing, as data."""

    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this digest was produced",
    )
    since: str = Field(default="", description="Lookback window the emails came from")
    overview: str = Field(
        default="", description="One-line headline across the whole inbox"
    )
    items: list[EmailSummaryItem] = Field(default_factory=list)
    degraded: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal problems (unparsable model output, emails the model skipped). "
            "Surfaced in the briefing rather than swallowed — a bad night must be visible."
        ),
    )

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def needs_reply_count(self) -> int:
        return sum(1 for item in self.items if item.needs_reply)

    def ranked(self) -> list[EmailSummaryItem]:
        """Items most-urgent-first, stable within a level (keeps inbox order)."""
        return sorted(self.items, key=lambda item: item.rank)


# --------------------------------------------------------------------------------------
# Morning briefing
# --------------------------------------------------------------------------------------
#
# The briefing is the single artifact a human reads at 8am, assembled from each agent's
# *structured* output. Nothing here holds HTML: `briefing.py` escapes every string on its
# way into the document, so a section built from a hostile email can at worst show wrong
# text — never markup, never a link the attacker chose.
#
# Sections whose data source doesn't exist yet (calendar, tasks, projects — Phases 9/14)
# are modelled in full and render as "nothing to report". An empty section is a *known*
# empty, which is different from a section that failed; failures get their own list so a
# 3am crash is visible rather than looking like a quiet night.


class Failure(BaseModel):
    """Something that went wrong during a run, surfaced in the briefing.

    Every field is plain text. `detail` is where an exception string lands, which means it
    may contain untrusted fragments — the renderer escapes it like everything else.
    """

    stage: str = Field(max_length=120, description="Where it broke, e.g. 'email_agent'")
    message: str = Field(max_length=500, description="One line a human can act on")
    detail: str = Field(default="", max_length=4000, description="Exception text, if any")
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CalendarEvent(BaseModel):
    """One of today's events, with agent-written prep notes. Populated in Phase 14."""

    start: str = Field(default="", max_length=64, description="Host-formatted start time")
    end: str = Field(default="", max_length=64)
    title: str = Field(max_length=300)
    location: str = Field(default="", max_length=300)
    attendees: list[str] = Field(default_factory=list, max_length=50)
    prep_notes: list[str] = Field(
        default_factory=list, max_length=20, description="What to do before this event"
    )


class CalendarSection(BaseModel):
    day: str = Field(default="", max_length=64, description="Which day this covers")
    events: list[CalendarEvent] = Field(default_factory=list, max_length=100)
    notes: list[str] = Field(default_factory=list, max_length=20)


class TaskItem(BaseModel):
    """A triaged task. Populated in Phase 14."""

    title: str = Field(max_length=300)
    due: str = Field(default="", max_length=64)
    urgency: Urgency = Field(default=Urgency.NORMAL)
    source: str = Field(default="", max_length=120, description="Where it came from")
    verdict: str = Field(default="", max_length=500, description="The triage call")

    @property
    def rank(self) -> int:
        return URGENCY_RANK[self.urgency]


class TaskSection(BaseModel):
    items: list[TaskItem] = Field(default_factory=list, max_length=200)

    def ranked(self) -> list["TaskItem"]:
        return sorted(self.items, key=lambda item: item.rank)


class ProjectWork(BaseModel):
    """"What I did last night" for one project.

    The link fields are defined now and filled in by Phase 9 (branches, diffs) and
    Phase 12 (transcripts). They are paths/identifiers we produce host-side, never URLs an
    agent chose — a link is a side effect, and side effects need approval.
    """

    project: str = Field(max_length=120)
    summary: str = Field(default="", max_length=4000)
    branch: str = Field(default="", max_length=200, description="agent/YYYY-MM-DD branch")
    diff_path: str = Field(default="", max_length=500, description="Local path to the diff")
    transcript_id: str = Field(default="", max_length=120, description="Phase 12 replay id")
    snapshot_id: str = Field(
        default="",
        max_length=120,
        description="Phase 13 pre-run snapshot; `snapshots.py rollback <id>` undoes the night",
    )
    commits: list[str] = Field(default_factory=list, max_length=100)
    highlights: list[str] = Field(default_factory=list, max_length=50)


class AgentWorkReport(BaseModel):
    """What the *project agent* itself says it did — the sandbox→host half of `ProjectWork`.

    Deliberately narrower than `ProjectWork`, exactly as `LLMEmailSummary` is narrower than
    `EmailSummaryItem`: the agent supplies prose, the host supplies every fact. Branch name,
    commit list and diff path are computed from git on the host (`gitops.py`) and joined in
    afterwards, so a model that misremembers — or is talked into misreporting — what it did
    cannot rewrite the metadata a human reviews the work by.

    `extra="forbid"` and hard length caps because this crosses the sandbox boundary as a
    JSON file written by an untrusted container.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=4000, description="Plain prose, no markup")
    highlights: list[Highlight] = Field(default_factory=list, max_length=50)
    completed: bool = Field(
        default=False, description="Whether the agent believes it finished the goal"
    )

    def clamp(self) -> "AgentWorkReport":
        """Trim to the caps instead of raising, for a report assembled in-process."""
        return AgentWorkReport(
            summary=self.summary[:4000],
            highlights=[item[:500] for item in self.highlights[:50]],
            completed=self.completed,
        )


class ProjectSection(BaseModel):
    projects: list[ProjectWork] = Field(default_factory=list, max_length=50)


class BriefingSection(BaseModel):
    """A free-form section contributed by an agent via `add_to_briefing`.

    This crosses the sandbox→host bridge, so it is the one briefing type that arrives from
    somewhere less trusted than the host. Hence `extra="forbid"` and a length cap on every
    field: a captured agent can waste its own tokens, but it cannot flood the artifact, and
    it cannot smuggle a field the renderer doesn't know about.

    `taint` records which untrusted sources are behind the section. It is set from the
    contributing *agent's* taint host-side, never from what the agent claims.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(max_length=60, description="Which agent contributed this")
    title: str = Field(min_length=1, max_length=120, description="Plain-text heading")
    summary: str = Field(default="", max_length=4000, description="Plain sentences")
    items: list[str] = Field(default_factory=list, max_length=50)
    taint: list[str] = Field(default_factory=list, max_length=10)


class Briefing(BaseModel):
    """The whole morning artifact. One run, one of these, one HTML file."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    date: str = Field(default="", max_length=64, description="The night this covers")
    email: EmailDigest | None = Field(default=None)
    calendar: CalendarSection | None = Field(default=None)
    tasks: TaskSection | None = Field(default=None)
    projects: ProjectSection | None = Field(default=None)
    contributed: list[BriefingSection] = Field(default_factory=list, max_length=50)
    failures: list[Failure] = Field(default_factory=list, max_length=200)

    def add_failure(self, stage: str, message: str, detail: str = "") -> None:
        """Record a failure. Truncates rather than raising — losing a failure is worse
        than showing a clipped one."""
        self.failures.append(
            Failure(stage=stage[:120], message=message[:500], detail=detail[:4000])
        )

    @property
    def has_failures(self) -> bool:
        return bool(self.failures) or bool(self.email and self.email.degraded)


# --------------------------------------------------------------------------------------
# Approval queue
# --------------------------------------------------------------------------------------
#
# security rule 3: all side effects require morning approval. These types are the shape of
# a queued action; `approvals.py` is the only thing that may execute one.
#
# Two deliberate choices:
#
# - **Closed enums, not strings.** A row whose type or status is not in the vocabulary is a
#   corrupt row, and the queue would rather refuse to load it than dispatch on a value no
#   executor knows about. `send_email` must never be one typo away from being unreviewable.
# - **One payload model per type, `extra="forbid"`.** The payload is stored as JSON, so the
#   database is untrusted input by the time it is read back (an old schema, a hand-edited
#   row). Re-validating on read means a mismatched payload surfaces as a loud error rather
#   than as a partially-populated send.


class ActionType(StrEnum):
    """What a queued action would do if approved. Every member has an executor."""

    SEND_EMAIL = "send_email"
    DRAFT_REPLY = "draft_reply"
    MERGE_BRANCH = "merge_branch"


class ActionStatus(StrEnum):
    """Lifecycle of a queued action.

    `pending → approved → done|failed` and `pending → rejected`. `approved` is a real,
    persisted state rather than a moment inside `approve()`: the row is claimed *before*
    the effect runs, so a crash mid-send leaves an action visibly stuck in `approved`
    instead of quietly re-firing on the next start.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    DONE = "done"


TERMINAL_STATUSES = frozenset(
    {ActionStatus.REJECTED, ActionStatus.FAILED, ActionStatus.DONE}
)


class SendEmailPayload(BaseModel):
    """A whole message the host would send. `html_body` is rendered host-side."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1, max_length=320, description="Single recipient address")
    subject: str = Field(default="", max_length=500)
    html_body: str = Field(default="", max_length=200_000)


class DraftReplyPayload(BaseModel):
    """A suggested reply to a specific email, queued but never sent on its own.

    The body is email-derived (the summariser wrote it after reading untrusted mail), so it
    carries the `email` taint. It may be shown to a human and — after an explicit approve —
    sent. It must never be fed into another agent's prompt; there is deliberately no path
    from an `Action` back into `runner.taint.PromptPart`.
    """

    model_config = ConfigDict(extra="forbid")

    email_id: str = Field(default="", max_length=200, description="Gmail id replied to")
    to: str = Field(min_length=1, max_length=320, description="Who the reply goes to")
    subject: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=100_000, description="Plain text, escaped on send")


class MergeBranchPayload(BaseModel):
    """A nightly `agent/*` branch a human has reviewed. Executed from Phase 9 on."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=200)
    branch: str = Field(min_length=1, max_length=300, description="e.g. agent/2026-07-24")
    into: str = Field(default="main", max_length=300)
    diff_path: str = Field(default="", max_length=500, description="What was reviewed")


ActionPayload = SendEmailPayload | DraftReplyPayload | MergeBranchPayload

# The single source of truth for "which model validates which type". The queue, the API and
# the executors all read this, so adding an action type is one entry plus one executor.
PAYLOAD_MODELS: dict[ActionType, type[BaseModel]] = {
    ActionType.SEND_EMAIL: SendEmailPayload,
    ActionType.DRAFT_REPLY: DraftReplyPayload,
    ActionType.MERGE_BRANCH: MergeBranchPayload,
}


class Action(BaseModel):
    """One row of the approval queue: what was proposed, by whom, and what came of it.

    Every decision field is filled host-side. Nothing an agent produced is trusted to say
    who approved something or whether it succeeded.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=64)
    type: ActionType
    status: ActionStatus = Field(default=ActionStatus.PENDING)
    payload: ActionPayload
    origin: str = Field(
        default="", max_length=120, description="Which agent/run proposed this"
    )
    taint: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Untrusted sources behind this action, e.g. ['email']",
    )
    summary: str = Field(
        default="", max_length=500, description="One line for the approval UI"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = Field(default=None)
    decided_by: str = Field(default="", max_length=120, description="Who approved/rejected")
    reason: str = Field(default="", max_length=1000, description="Why it was rejected")
    completed_at: datetime | None = Field(default=None)
    result: str = Field(default="", max_length=2000, description="What the effect returned")
    error: str = Field(default="", max_length=4000, description="Why the effect failed")

    @model_validator(mode="after")
    def _payload_matches_type(self) -> "Action":
        """The payload must be the model this action type declares.

        Pydantic's union would happily accept a `MergeBranchPayload` on a `send_email`
        action if the fields happened to fit. Pinning it here means a row that drifted from
        the schema fails loudly on read instead of reaching an executor.
        """
        expected = PAYLOAD_MODELS[self.type]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"action type {self.type} requires {expected.__name__}, "
                f"got {type(self.payload).__name__}"
            )
        return self

    @property
    def is_pending(self) -> bool:
        return self.status is ActionStatus.PENDING


class LLMEmailSummary(BaseModel):
    """What the *model* is asked to return per email.

    Deliberately narrower than `EmailSummaryItem`: the model never restates the sender or
    subject, it only references an `email_id`. The host joins that id back to the email it
    fetched, so untrusted text cannot rewrite the attribution line in the briefing.
    """

    email_id: str
    summary: str
    urgency: Urgency
    needs_reply: bool
    category: str
    action_items: list[str]
    draft_reply: DraftReply | None


class LLMDigest(BaseModel):
    """Top-level object the model returns; validated before anything renders it."""

    overview: str
    emails: list[LLMEmailSummary]


class LLMEventPrep(BaseModel):
    """What the calendar agent may say about one event: prep notes, and nothing else.

    Same discipline as `LLMEmailSummary`. The model never restates the time, title,
    location or attendees — the host joins `event_id` back to the `CalendarEntry` it
    fetched. So an event whose title is an injection payload cannot also rewrite *when*
    the briefing says your day starts.
    """

    event_id: str
    prep_notes: list[str]


class LLMTaskTriage(BaseModel):
    """The calendar agent's verdict on one task. Title and due date stay host-supplied."""

    task_id: str
    urgency: Urgency
    verdict: str


class LLMDayPlan(BaseModel):
    """Top-level object the calendar/task agent returns."""

    notes: list[str]
    events: list[LLMEventPrep]
    tasks: list[LLMTaskTriage]


class DayPlan(BaseModel):
    """The calendar agent's whole contribution, as the two sections the briefing renders.

    A container rather than two return values so the degradations travel with the data:
    "Tasks unavailable (not authorised)" is part of the answer, not an exception someone
    downstream has to remember to catch.
    """

    calendar: CalendarSection = Field(default_factory=CalendarSection)
    tasks: TaskSection = Field(default_factory=TaskSection)
    degraded: list[str] = Field(default_factory=list)
