"""Render the validated morning `Briefing` into one self-contained HTML artifact.

This module is the *only* place email-derived text becomes HTML, and it is deliberately
dumb: no LLM, no templating engine, no `mark_safe` escape hatch. Every string that came
from an email or from the model goes through `_esc()` before it reaches the output, so an
email body containing `<script>` or "IGNORE ALL PREVIOUS INSTRUCTIONS" lands in the
briefing as inert, visible text — the summary-as-data rule.

Styling is inline because the artifact is emailed: mail clients strip <style> blocks. For
the same reason there are no external assets at all — no CSS file, no font, no image, no
script. One file you can open, mail, or hand to the menu bar app (Phase 11).

Two entry points:

- `render_digest_html(digest)` — the email section on its own (used by the mail digest).
- `render_briefing_html(briefing)` — the whole artifact: email, calendar, tasks, project
  work, agent-contributed sections, and **Failures**.

A section with no data renders as "Nothing to report" rather than disappearing, because a
silently absent section and a genuinely quiet night look identical to a reader at 8am —
and only one of them is fine. Anything that actually broke goes in Failures, always last,
always shown.
"""

from __future__ import annotations

from html import escape

from models import (
    Briefing,
    BriefingSection,
    CalendarSection,
    EmailDigest,
    EmailSummaryItem,
    ProjectSection,
    TaskSection,
    Urgency,
)

# Badge colours per urgency level. Backgrounds are light enough for dark text in any
# client, since email clients ignore prefers-color-scheme in most cases.
_URGENCY_STYLE: dict[Urgency, tuple[str, str]] = {
    Urgency.CRITICAL: ("#7f1d1d", "#fee2e2"),
    Urgency.HIGH: ("#7c2d12", "#ffedd5"),
    Urgency.NORMAL: ("#1e3a8a", "#dbeafe"),
    Urgency.LOW: ("#374151", "#f3f4f6"),
}

# The command the briefing tells you to run to replay a stored transcript (Phase 12).
# Spelled out here rather than imported from `transcripts.py` on purpose: this module is
# rendered inside the sandbox too, and it has no business importing the host's database
# layer to print a string. A test pins it to `transcripts.REPLAY_COMMAND` so it cannot drift.
REPLAY_HINT = "uv run python transcripts.py replay"

# The command that undoes a night (Phase 13), spelled out for the same reason and pinned to
# `snapshots.ROLLBACK_COMMAND` by the same kind of test.
ROLLBACK_HINT = "uv run python snapshots.py rollback"

_CARD = (
    "border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;"
    "margin:0 0 12px 0;background:#ffffff;"
)
_MUTED = "color:#6b7280;font-size:13px;margin:0 0 6px 0;"


def _esc(value: object) -> str:
    """Escape anything on its way into the document. Never bypass this."""
    return escape(str(value), quote=True)


def _esc_multiline(value: str) -> str:
    """Escape, then turn newlines into <br> — the only markup we ever synthesise."""
    return _esc(value).replace("\n", "<br>")


def _badge(urgency: Urgency) -> str:
    fg, bg = _URGENCY_STYLE.get(urgency, _URGENCY_STYLE[Urgency.NORMAL])
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f"font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;"
        f'color:{fg};background:{bg};">{_esc(urgency.value)}</span>'
    )


def _reply_flag() -> str:
    return (
        '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        "font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;"
        'color:#065f46;background:#d1fae5;margin-left:6px;">needs reply</span>'
    )


def _render_item(item: EmailSummaryItem) -> str:
    parts = [f'<div style="{_CARD}">']
    parts.append(
        '<div style="margin:0 0 6px 0;">'
        + _badge(item.urgency)
        + (_reply_flag() if item.needs_reply else "")
        + f'<span style="color:#9ca3af;font-size:11px;margin-left:8px;">'
        f"{_esc(item.category)}</span></div>"
    )
    parts.append(
        f'<div style="font-weight:600;font-size:15px;margin:0 0 2px 0;">'
        f"{_esc(item.subject or '(no subject)')}</div>"
    )
    parts.append(f'<p style="{_MUTED}">{_esc(item.sender)}</p>')
    parts.append(
        f'<p style="margin:0 0 8px 0;font-size:14px;line-height:1.5;">'
        f"{_esc(item.summary)}</p>"
    )

    if item.action_items:
        bullets = "".join(f"<li>{_esc(a)}</li>" for a in item.action_items)
        parts.append(
            '<p style="margin:0 0 4px 0;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">Actions</p>'
            f'<ul style="margin:0 0 8px 18px;padding:0;font-size:14px;">{bullets}</ul>'
        )

    if item.draft_reply is not None:
        parts.append(
            '<div style="border-left:3px solid #d1d5db;padding:6px 0 6px 12px;'
            'margin-top:10px;background:#fafafa;">'
            '<p style="margin:0 0 4px 0;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">'
            "Suggested draft — not sent</p>"
            f'<p style="{_MUTED}">{_esc(item.draft_reply.subject)}</p>'
            f'<div style="font-size:14px;line-height:1.5;white-space:normal;">'
            f"{_esc_multiline(item.draft_reply.body)}</div></div>"
        )

    parts.append("</div>")
    return "".join(parts)


def render_digest_html(digest: EmailDigest) -> str:
    """Render the digest as an inline-styled HTML fragment (no <html>/<head> wrapper)."""
    header_bits = [f"{digest.count} email{'s' if digest.count != 1 else ''}"]
    if digest.needs_reply_count:
        header_bits.append(f"{digest.needs_reply_count} needing a reply")
    if digest.since:
        header_bits.append(f"last {digest.since}")

    out = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'Helvetica,Arial,sans-serif;color:#111827;max-width:680px;">',
        '<h2 style="margin:0 0 2px 0;font-size:20px;">Your morning digest</h2>',
        f'<p style="{_MUTED}">{_esc(" · ".join(header_bits))}</p>',
    ]

    if digest.overview:
        out.append(
            f'<p style="margin:0 0 16px 0;font-size:15px;line-height:1.5;">'
            f"{_esc(digest.overview)}</p>"
        )

    if not digest.items:
        out.append(f'<p style="{_MUTED}">Nothing to report.</p>')
    else:
        out.extend(_render_item(item) for item in digest.ranked())

    if digest.degraded:
        issues = "".join(f"<li>{_esc(note)}</li>" for note in digest.degraded)
        out.append(
            '<div style="border:1px solid #fde68a;background:#fffbeb;border-radius:10px;'
            'padding:12px 16px;margin-top:8px;">'
            '<p style="margin:0 0 4px 0;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:.04em;color:#92400e;">'
            "Summariser issues</p>"
            f'<ul style="margin:0 0 0 18px;padding:0;font-size:13px;color:#92400e;">'
            f"{issues}</ul></div>"
        )

    out.append(
        f'<p style="{_MUTED}margin-top:16px;">Generated '
        f"{_esc(digest.generated_at.strftime('%Y-%m-%d %H:%M UTC'))} by NightShift. "
        "Draft replies are suggestions only; nothing has been sent.</p>"
    )
    out.append("</div>")
    return "".join(out)


# --------------------------------------------------------------------------------------
# The full briefing artifact
# --------------------------------------------------------------------------------------

_H2 = "margin:28px 0 10px 0;font-size:17px;font-weight:700;color:#111827;"
_EMPTY = f'<p style="{_MUTED}">Nothing to report.</p>'


def _section(title: str, body: str) -> str:
    """A titled section. Titles are ours, bodies are already-escaped HTML."""
    return f'<h2 style="{_H2}">{_esc(title)}</h2>{body}'


def _bullets(values: list[str], *, style: str = "") -> str:
    if not values:
        return ""
    items = "".join(f"<li>{_esc(v)}</li>" for v in values)
    return f'<ul style="margin:0 0 8px 18px;padding:0;font-size:14px;{style}">{items}</ul>'


def _render_email_section(digest: EmailDigest | None) -> str:
    if digest is None or not digest.items:
        body = _EMPTY
        if digest is not None and digest.degraded:
            # An empty digest *with* degradations is not a quiet night — say so.
            body += _bullets(digest.degraded, style="color:#92400e;")
        return _section("Email", body)

    head_bits = [f"{digest.count} email{'s' if digest.count != 1 else ''}"]
    if digest.needs_reply_count:
        head_bits.append(f"{digest.needs_reply_count} needing a reply")
    if digest.since:
        head_bits.append(f"last {digest.since}")

    body = [f'<p style="{_MUTED}">{_esc(" · ".join(head_bits))}</p>']
    if digest.overview:
        body.append(
            f'<p style="margin:0 0 14px 0;font-size:15px;line-height:1.5;">'
            f"{_esc(digest.overview)}</p>"
        )
    body.extend(_render_item(item) for item in digest.ranked())
    return _section("Email", "".join(body))


def _render_calendar_section(calendar: CalendarSection | None) -> str:
    if calendar is None or not calendar.events:
        return _section("Today's calendar", _EMPTY)

    body = []
    if calendar.day:
        body.append(f'<p style="{_MUTED}">{_esc(calendar.day)}</p>')
    for event in calendar.events:
        when = " – ".join(part for part in (event.start, event.end) if part)
        meta = " · ".join(
            part
            for part in (
                event.location,
                f"{len(event.attendees)} attendees" if event.attendees else "",
            )
            if part
        )
        card = [f'<div style="{_CARD}">']
        if when:
            card.append(
                f'<p style="{_MUTED}font-weight:600;color:#374151;">{_esc(when)}</p>'
            )
        card.append(
            f'<div style="font-weight:600;font-size:15px;margin:0 0 2px 0;">'
            f"{_esc(event.title)}</div>"
        )
        if meta:
            card.append(f'<p style="{_MUTED}">{_esc(meta)}</p>')
        if event.prep_notes:
            card.append(
                '<p style="margin:6px 0 4px 0;font-size:12px;font-weight:700;'
                'text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">Prep</p>'
                + _bullets(event.prep_notes)
            )
        card.append("</div>")
        body.append("".join(card))
    if calendar.notes:
        body.append(_bullets(calendar.notes))
    return _section("Today's calendar", "".join(body))


def _render_tasks_section(tasks: TaskSection | None) -> str:
    if tasks is None or not tasks.items:
        return _section("Task triage", _EMPTY)

    rows = []
    for task in tasks.ranked():
        meta = " · ".join(part for part in (task.due, task.source) if part)
        row = [f'<div style="{_CARD}">', f'<div style="margin:0 0 6px 0;">{_badge(task.urgency)}</div>']
        row.append(
            f'<div style="font-weight:600;font-size:15px;margin:0 0 2px 0;">'
            f"{_esc(task.title)}</div>"
        )
        if meta:
            row.append(f'<p style="{_MUTED}">{_esc(meta)}</p>')
        if task.verdict:
            row.append(
                f'<p style="margin:0;font-size:14px;line-height:1.5;">'
                f"{_esc(task.verdict)}</p>"
            )
        row.append("</div>")
        rows.append("".join(row))
    return _section("Task triage", "".join(rows))


def _render_projects_section(projects: ProjectSection | None) -> str:
    if projects is None or not projects.projects:
        return _section("What I did last night", _EMPTY)

    cards = []
    for work in projects.projects:
        card = [f'<div style="{_CARD}">']
        card.append(
            f'<div style="font-weight:600;font-size:15px;margin:0 0 2px 0;">'
            f"{_esc(work.project)}</div>"
        )
        # Branch / diff / transcript are host-produced identifiers, rendered as plain
        # text rather than anchors: nothing an agent wrote becomes a clickable target.
        meta = " · ".join(
            part
            for part in (
                f"branch {work.branch}" if work.branch else "",
                f"diff {work.diff_path}" if work.diff_path else "",
            )
            if part
        )
        if meta:
            card.append(
                f'<p style="{_MUTED}font-family:ui-monospace,SFMono-Regular,Menlo,'
                f'monospace;">{_esc(meta)}</p>'
            )
        if work.transcript_id:
            # "Replayable from the briefing" for an artifact that is a static file you may
            # read in a mail client: the id plus the exact command that replays it. No
            # link, because a link in an emailed briefing is a click we did not earn.
            card.append(
                f'<p style="{_MUTED}font-family:ui-monospace,SFMono-Regular,Menlo,'
                f'monospace;">Replay this run: {_esc(REPLAY_HINT)} '
                f"{_esc(work.transcript_id)}</p>"
            )
        if work.snapshot_id:
            # The escape hatch, printed next to the work it undoes. A morning reader who
            # does not like what they see should not have to go looking for the id of the
            # state they had before it.
            card.append(
                f'<p style="{_MUTED}font-family:ui-monospace,SFMono-Regular,Menlo,'
                f'monospace;">Undo this night: {_esc(ROLLBACK_HINT)} '
                f"{_esc(work.snapshot_id)}</p>"
            )
        if work.summary:
            card.append(
                f'<p style="margin:0 0 8px 0;font-size:14px;line-height:1.5;">'
                f"{_esc(work.summary)}</p>"
            )
        if work.highlights:
            card.append(_bullets(work.highlights))
        if work.commits:
            card.append(
                '<p style="margin:6px 0 4px 0;font-size:12px;font-weight:700;'
                'text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">'
                "Commits</p>" + _bullets(work.commits, style="font-family:ui-monospace,"
                "SFMono-Regular,Menlo,monospace;font-size:13px;")
            )
        card.append(
            f'<p style="{_MUTED}margin-top:8px;">Review the diff before merging — '
            "nothing is merged without your approval.</p>"
        )
        card.append("</div>")
        cards.append("".join(card))
    return _section("What I did last night", "".join(cards))


def _render_contributed(sections: list[BriefingSection]) -> str:
    if not sections:
        return ""
    out = []
    for section in sections:
        card = [f'<div style="{_CARD}">']
        card.append(
            f'<div style="font-weight:600;font-size:15px;margin:0 0 2px 0;">'
            f"{_esc(section.title)}</div>"
        )
        provenance = section.agent + (
            f" · from {', '.join(section.taint)}" if section.taint else ""
        )
        card.append(f'<p style="{_MUTED}">{_esc(provenance)}</p>')
        if section.summary:
            card.append(
                f'<p style="margin:0 0 8px 0;font-size:14px;line-height:1.5;">'
                f"{_esc(section.summary)}</p>"
            )
        card.append(_bullets(section.items))
        card.append("</div>")
        out.append("".join(card))
    return _section("From the agents", "".join(out))


def _render_failures(briefing: Briefing) -> str:
    """Always rendered. A run with nothing wrong says so explicitly, so the absence of
    this section can never be mistaken for the absence of problems."""
    rows = []
    for failure in briefing.failures:
        row = [
            '<div style="border:1px solid #fecaca;background:#fef2f2;border-radius:10px;'
            'padding:12px 16px;margin:0 0 10px 0;">',
            f'<p style="margin:0 0 2px 0;font-size:12px;font-weight:700;'
            f"text-transform:uppercase;letter-spacing:.04em;color:#991b1b;\">"
            f"{_esc(failure.stage)}</p>",
            f'<p style="margin:0;font-size:14px;color:#7f1d1d;">'
            f"{_esc(failure.message)}</p>",
        ]
        if failure.detail:
            row.append(
                f'<pre style="margin:8px 0 0 0;padding:8px;background:#fff1f2;'
                f"border-radius:6px;font-size:12px;line-height:1.4;overflow-x:auto;"
                f'white-space:pre-wrap;color:#7f1d1d;">{_esc(failure.detail)}</pre>'
            )
        row.append("</div>")
        rows.append("".join(row))

    # Summariser degradations are failures too — they just happen to be recoverable.
    if briefing.email and briefing.email.degraded:
        rows.append(
            '<div style="border:1px solid #fde68a;background:#fffbeb;border-radius:10px;'
            'padding:12px 16px;margin:0 0 10px 0;">'
            '<p style="margin:0 0 4px 0;font-size:12px;font-weight:700;'
            'text-transform:uppercase;letter-spacing:.04em;color:#92400e;">'
            "Summariser degraded</p>"
            + _bullets(briefing.email.degraded, style="color:#92400e;font-size:13px;")
            + "</div>"
        )

    if not rows:
        return _section(
            "Failures", f'<p style="{_MUTED}">None — every step completed.</p>'
        )
    return _section("Failures", "".join(rows))


def render_briefing_html(briefing: Briefing) -> str:
    """Render the whole briefing as one self-contained, inline-styled HTML document."""
    stamp = briefing.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    subtitle = briefing.date or stamp

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>NightShift briefing</title></head>",
        '<body style="margin:0;padding:24px 16px;background:#f9fafb;">',
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'Helvetica,Arial,sans-serif;color:#111827;max-width:680px;margin:0 auto;">',
        '<h1 style="margin:0 0 2px 0;font-size:22px;">Good morning</h1>',
        f'<p style="{_MUTED}">{_esc(subtitle)}</p>',
    ]

    if briefing.has_failures:
        count = len(briefing.failures)
        parts.append(
            '<p style="margin:12px 0 0 0;padding:10px 14px;border-radius:8px;'
            'background:#fef2f2;border:1px solid #fecaca;color:#991b1b;font-size:14px;">'
            f"{_esc(count)} failure{'s' if count != 1 else ''} overnight — see Failures "
            "below.</p>"
            if count
            else '<p style="margin:12px 0 0 0;padding:10px 14px;border-radius:8px;'
            'background:#fffbeb;border:1px solid #fde68a;color:#92400e;font-size:14px;">'
            "The summariser degraded on some mail — see Failures below.</p>"
        )

    parts.append(_render_email_section(briefing.email))
    parts.append(_render_calendar_section(briefing.calendar))
    parts.append(_render_tasks_section(briefing.tasks))
    parts.append(_render_projects_section(briefing.projects))
    parts.append(_render_contributed(briefing.contributed))
    parts.append(_render_failures(briefing))

    parts.append(
        f'<p style="{_MUTED}margin-top:24px;border-top:1px solid #e5e7eb;padding-top:12px;">'
        f"Generated {_esc(stamp)} by NightShift. Draft replies are suggestions only; "
        "nothing has been sent, merged, or run without your approval.</p>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)
