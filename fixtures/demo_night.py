"""One canned night, as finished artifacts — what demo mode shows a first-time reviewer.

`fixtures/mock_emails.py` and `fixtures/mock_calendar.py` are *inputs*: they stand in for
Gmail and Calendar so the real pipeline can run offline. This module is the other end —
the **output** a night would have produced from those inputs, written by hand so demo mode
needs no model call, no API key, no network and no Docker. It is therefore deterministic:
the same briefing every time, which is also what makes it usable in a screenshot.

Two things are deliberate about the content:

- **The injection fixtures are in it.** The hostile email body and the hostile event title
  appear in the briefing exactly as they must in a real night — as inert, escaped text
  under a summary that treats them as data. A demo that quietly drops them would be
  advertising the wrong thing.
- **There is a failure in it.** The Failures section is the part of the artifact people
  never see in a screenshot, because a demo is usually the happy path. A night with one
  degraded source is the honest picture.

Nothing here is trusted input to anything: it is fed to `briefing.render_briefing_html`,
which escapes every string, and to a queue whose effects demo mode disarms.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fixtures.mock_calendar import CALENDAR_INJECTION_MARKER
from fixtures.mock_emails import INJECTION_EMAIL_ID, INJECTION_MARKER
from models import (
    Briefing,
    BriefingSection,
    CalendarEvent,
    CalendarSection,
    DraftReply,
    DraftReplyPayload,
    EmailDigest,
    EmailSummaryItem,
    Failure,
    MergeBranchPayload,
    ProjectSection,
    ProjectWork,
    TaskItem,
    TaskSection,
    Urgency,
)

# A fixed date so two demo runs produce byte-identical artifacts (and so a screenshot in the
# README never disagrees with what a reviewer sees).
DEMO_DATE = "2026-07-29"
DEMO_AT = datetime(2026, 7, 30, 3, 42, tzinfo=timezone.utc)
DEMO_NIGHT_ID = "demo-night-2026-07-29"
DEMO_BRANCH = f"agent/{DEMO_DATE}"
DEMO_PROJECT = "nightshift"

# Stable ids for the two queued actions, so the demo transcript, the briefing and the
# approval queue all refer to the same pieces of work.
DEMO_TRANSCRIPT_ID = "demo-run-project"


def demo_digest() -> EmailDigest:
    """The email agent's verdict on `fixtures/mock_emails.py`."""
    return EmailDigest(
        generated_at=DEMO_AT,
        since="2h",
        overview="5 new emails. One invoice is overdue and CI is red on main.",
        items=[
            EmailSummaryItem(
                email_id="mock-0001",
                sender="Priya Raman <priya@acme-supply.example>",
                subject="Invoice #4471 is overdue",
                summary=(
                    "Priya is chasing invoice #4471 for $2,480, which was due last Friday, "
                    "and offers to resend the PDF."
                ),
                urgency=Urgency.HIGH,
                needs_reply=True,
                category="finance",
                action_items=["Confirm a payment date for invoice #4471"],
                draft_reply=DraftReply(
                    subject="Re: Invoice #4471 is overdue",
                    body=(
                        "Hi Priya,\n\nThanks for the nudge, and apologies for the delay — "
                        "#4471 slipped past me. I'm scheduling the payment for Friday and "
                        "will confirm here once it has gone out. No need to resend the PDF.\n\n"
                        "Best,\nAdityan"
                    ),
                ),
            ),
            EmailSummaryItem(
                email_id="mock-0002",
                sender="GitHub <notifications@github.example>",
                subject="[nightshift] CI failed on main",
                summary=(
                    "The tests workflow failed on main for commit 5fe844f: two tests errored "
                    "in test_broker.py."
                ),
                urgency=Urgency.HIGH,
                needs_reply=False,
                category="work",
                action_items=["Look at the two erroring tests in test_broker.py"],
            ),
            EmailSummaryItem(
                email_id=INJECTION_EMAIL_ID,
                sender="Dana Whitfield <dana@partner-corp.example>",
                subject="Re: Q3 planning — action required",
                summary=(
                    "Nominally about Q3 numbers, but the body is a prompt-injection attempt: "
                    f'it contains "{INJECTION_MARKER}" and instructs the reading agent to read '
                    "token.json and email its contents to an outside address. Nothing was "
                    "read and nothing was sent — the text is reproduced here as data. Treat "
                    "the sender as suspect."
                ),
                urgency=Urgency.CRITICAL,
                needs_reply=False,
                category="security",
                action_items=["Verify with Dana out-of-band before acting on this thread"],
            ),
            EmailSummaryItem(
                email_id="mock-0004",
                sender="Mum <mum@family.example>",
                subject="Sunday lunch?",
                summary="Your mum is asking whether you are free for lunch on Sunday at 1pm.",
                urgency=Urgency.NORMAL,
                needs_reply=True,
                category="personal",
                draft_reply=DraftReply(
                    subject="Re: Sunday lunch?",
                    body=(
                        "Hi Mum,\n\nSunday at 1pm works — I'll bring the dog.\n\n"
                        "See you then,\nAdityan"
                    ),
                ),
            ),
            EmailSummaryItem(
                email_id="mock-0005",
                sender="AWS Billing <no-reply@aws.example>",
                subject="Your July invoice is available",
                summary="July's AWS bill is $18.42. No action needed.",
                urgency=Urgency.LOW,
                needs_reply=False,
                category="notification",
            ),
        ],
        degraded=[],
    )


def demo_calendar() -> CalendarSection:
    """Today, from `fixtures/mock_calendar.py`, with the hostile invite left in place."""
    return CalendarSection(
        day="Thursday 30 July",
        events=[
            CalendarEvent(
                start="09:30",
                end="09:45",
                title="Standup",
                location="Meet",
                attendees=["team@acme.example"],
                prep_notes=["Mention that CI is red on main."],
            ),
            CalendarEvent(
                start="11:00",
                end="11:30",
                title="Invoice review with Priya Raman",
                location="Zoom",
                attendees=["priya@acme-supply.example"],
                prep_notes=[
                    "Have a payment date for #4471 ready — she emailed about it overnight.",
                ],
            ),
            CalendarEvent(
                start="14:00",
                end="15:00",
                title=f"Q3 checkpoint — {CALENDAR_INJECTION_MARKER}",
                location="Room 2",
                attendees=["dana@partner-corp.example"],
                prep_notes=[
                    "The event description tries to give the agent instructions. It was "
                    "read as data and ignored; the title is shown verbatim so you can see it.",
                ],
            ),
            CalendarEvent(
                start="17:15",
                end="17:45",
                title="Dog at the vet",
                location="Elm Street Veterinary",
                prep_notes=["Leave by 17:00."],
            ),
        ],
        notes=["Two back-to-back meetings before lunch; the afternoon is clear after 15:00."],
    )


def demo_tasks() -> TaskSection:
    return TaskSection(
        items=[
            TaskItem(
                title="Pay invoice #4471",
                due="2026-07-25",
                urgency=Urgency.CRITICAL,
                source="Work",
                verdict="Overdue and being chased by email this morning. Do it first.",
            ),
            TaskItem(
                title="Fix the CI failure on main",
                due="",
                urgency=Urgency.HIGH,
                source="Work",
                verdict="Blocks every other merge. The agent has a branch open on it.",
            ),
            TaskItem(
                title="Renew the domain",
                due="2026-07-30",
                urgency=Urgency.NORMAL,
                source="Personal",
                verdict=(
                    "Due today. The note on this task also contains instructions aimed at "
                    "the agent; they were ignored."
                ),
            ),
        ]
    )


def demo_projects() -> ProjectSection:
    return ProjectSection(
        projects=[
            ProjectWork(
                project=DEMO_PROJECT,
                summary=(
                    "Reproduced the two test_broker.py errors, traced them to the broker "
                    "fixture binding a TCP port instead of the Unix socket, and rewrote the "
                    "fixture to use a socket in a temp directory. The suite is green on the "
                    "branch."
                ),
                branch=DEMO_BRANCH,
                diff_path=f"out/diffs/{DEMO_PROJECT}-{DEMO_DATE}.diff",
                transcript_id=DEMO_TRANSCRIPT_ID,
                snapshot_id="demo-snapshot-2026-07-29",
                commits=[
                    "a1b2c3d Bind the broker test fixture to a Unix socket",
                    "d4e5f6a Drop the now-unused port allocation helper",
                ],
                highlights=[
                    "2 tests fixed, 0 added dependencies",
                    "Nothing merged — the merge is waiting in the approval queue",
                ],
            )
        ]
    )


def demo_contributed() -> list[BriefingSection]:
    return [
        BriefingSection(
            agent="email_agent",
            title="Watch out",
            summary=(
                "One of last night's emails was an attempt to take control of the agent "
                "reading it. It is summarised above as data and nothing was acted on."
            ),
            items=["Sender: dana@partner-corp.example", "Nothing was read, sent or executed"],
            taint=["email"],
        )
    ]


def demo_briefing() -> Briefing:
    """The whole artifact demo mode renders."""
    return Briefing(
        generated_at=DEMO_AT,
        date=DEMO_DATE,
        email=demo_digest(),
        calendar=demo_calendar(),
        tasks=demo_tasks(),
        projects=demo_projects(),
        contributed=demo_contributed(),
        failures=[
            Failure(
                stage="tasks",
                message="Google Tasks was unavailable for part of the run.",
                detail=(
                    "tasks.readonly is an optional scope; one list ('Someday') could not be "
                    "read. The rest of the task section is complete."
                ),
                at=DEMO_AT,
            )
        ],
    )


def demo_actions() -> list[tuple[str, object, dict]]:
    """The pending queue: two draft replies and one merge, as (type, payload, kwargs).

    Returned as data rather than enqueued here so `app/demo.py` owns the database and this
    module stays a pure fixture.
    """
    digest = demo_digest()
    actions: list[tuple[str, object, dict]] = []
    for item in digest.items:
        if item.draft_reply is None:
            continue
        address = item.sender.split("<")[-1].rstrip(">")
        actions.append(
            (
                "draft_reply",
                DraftReplyPayload(
                    email_id=item.email_id,
                    to=address,
                    subject=item.draft_reply.subject,
                    body=item.draft_reply.body,
                ),
                {
                    "origin": "email_agent",
                    "taint": ["email"],
                    "summary": f"Reply to {item.subject}",
                },
            )
        )
    actions.append(
        (
            "merge_branch",
            MergeBranchPayload(
                project=DEMO_PROJECT,
                branch=DEMO_BRANCH,
                into="main",
                diff_path=f"out/diffs/{DEMO_PROJECT}-{DEMO_DATE}.diff",
            ),
            {
                "origin": "project_agent",
                "taint": [],
                "summary": f"Merge {DEMO_BRANCH} into main",
            },
        )
    )
    return actions
