"""Host one-shot digest: fetch → summarise → render → send.

The pipeline is now fetch → **validated `EmailDigest`** → rendered HTML (`briefing.py`),
so the mail we send is built host-side from structured data rather than from model prose.

`--mock` runs the whole thing against the canned inbox in `fixtures/mock_emails.py` with
no Gmail call at all (neither read nor send): it writes the rendered briefing to disk and
opens nothing. That is the offline end-to-end path — the only network it touches is the
LLM. Pair it with a stubbed client (see `tests/`) for a fully offline run.

`--queue-drafts` puts the digest's suggested replies into the Phase 8 approval queue. That
is not a send: they sit as `pending` actions until a human approves one (`approvals.py`).

`--config` selects the standing instructions (model slugs, priorities, style) for this
run; without it the loader falls back to `config/standing_instructions.toml`, then to
validated defaults. A broken config stops the run here, with a readable error.
"""

import argparse
import os
import webbrowser
from datetime import datetime
from pathlib import Path

import httpx

from briefing import render_briefing_html
from config import ConfigError, load_config, use_config
from emails import fetch_emails_last_x_hours, get_read_credentials
from models import Briefing
from summarise import build_digest, fetch_emails_from_api

SINCE = os.getenv("NIGHTSHIFT_SINCE", "2h")
DEFAULT_OUT = Path("out/briefing.html")


def load_emails(since: str):
    """Prefer the running API; fall back to a direct Gmail fetch if it's down.

    Public because the nightly daemon (`orchestrator/nightly.py`) needs the *same*
    fallback: an unattended run is exactly when the broker is most likely to be down.
    """
    try:
        emails = fetch_emails_from_api(since=since)
        print(f"Loaded {len(emails)} emails from the API ({since}).")
        return emails
    except httpx.HTTPError as exc:
        print(f"API unavailable ({exc}); fetching directly from Gmail.")
        from models import parse_since

        credentials = get_read_credentials()
        return fetch_emails_last_x_hours(credentials, hours=parse_since(since))


def main() -> int:
    parser = argparse.ArgumentParser(description="NightShift morning digest")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the canned inbox; never calls Gmail (implies --no-send).",
    )
    parser.add_argument(
        "--no-send", action="store_true", help="Write the briefing to disk instead."
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="Where to write the briefing."
    )
    parser.add_argument("--since", default=SINCE, help="Lookback window, e.g. '8h'.")
    parser.add_argument("--open", action="store_true", help="Open the written briefing.")
    parser.add_argument(
        "--queue-drafts",
        action="store_true",
        help="Queue the digest's draft replies for morning approval (sends nothing).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Standing-instructions TOML (default: config/standing_instructions.toml).",
    )
    parser.add_argument(
        "--projects",
        action="store_true",
        help="Also run the sandboxed project agent on each active project (needs colima).",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Loud and readable: a bad config is a human error worth stopping for.
        print(f"Config error:\n{exc}")
        return 2
    use_config(config)
    print(
        "Config: "
        + (config.source_path or "built-in defaults (no config file)")
        + f" — email agent model {config.agent('email_agent').model}"
    )

    # Phase 12: record this run's agent transcripts, but open no run-history row — a
    # one-shot digest you ran by hand is not a night, and the history table is about
    # nights. The transcripts still land, still replay, and still carry their taint.
    store = None
    try:
        from runner.observe import use_recorder
        from transcripts import SqliteRecorder, TranscriptStore

        store = TranscriptStore()
        use_recorder(SqliteRecorder(store))
    except Exception as exc:  # noqa: BLE001 - observability never blocks a run
        print(f"Transcripts unavailable ({exc!r}); this run will not be recorded.")

    print("NightShift — fetching your recent emails\n")
    if args.mock:
        from fixtures.mock_emails import mock_emails

        emails = mock_emails()
        print(f"Loaded {len(emails)} canned emails (mock mode — no Gmail).")
    else:
        emails = load_emails(args.since)

    if not emails and not args.projects:
        print("Nothing to summarise.")
        return 0

    briefing = Briefing(date=datetime.now().strftime("%A, %d %B %Y"))
    if emails:
        print("\nSummarising...")
        try:
            digest = build_digest(emails, since=args.since)
            briefing.email = digest
            print(
                f"Digest: {digest.count} item(s), {digest.needs_reply_count} needing a reply"
                + (f", {len(digest.degraded)} issue(s)" if digest.degraded else "")
            )
        except Exception as exc:
            # A dead summariser must still produce an artifact — one that says what broke.
            # Silence at 8am is indistinguishable from a quiet inbox, and that is the bug.
            briefing.add_failure("email_agent", "Summarising email failed", repr(exc))
            print(f"Summarising failed: {exc!r}")

    if args.queue_drafts and briefing.email is not None:
        # Queueing is not sending: every draft lands as `pending` and stays there until a
        # human approves it in the morning (`uv run python approvals.py`). Imported late so
        # the queue database is only created on runs that actually want one.
        from approvals import ApprovalQueue, enqueue_digest_drafts

        queue = ApprovalQueue()
        queued = enqueue_digest_drafts(queue, briefing.email)
        print(f"Queued {len(queued)} draft repl(ies) for approval at {queue.path}.")

    if args.projects:
        # Phase 9. Runs after the email agent and shares nothing with it: the project agent
        # accepts no taint, so no email-derived string can reach the one agent with a shell.
        # Every step is queued or written to disk — nothing here merges anything.
        from approvals import ApprovalQueue
        from nightly_project import nightly_projects

        print("\nRunning project work in the sandbox...")
        nightly_projects(
            config, briefing, config_path=args.config, queue=ApprovalQueue(), store=store
        )

    html = render_briefing_html(briefing)

    # Mock mode must not reach Google at all, and sending is a Google call.
    if args.mock or args.no_send:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(html, encoding="utf-8")
        print(f"Wrote briefing to {args.out} ({len(html)} bytes).")
        if args.open:
            webbrowser.open(args.out.resolve().as_uri())
        return 0

    # Sending uses a separate, host-only credential — the read path above never
    # holds a scope that can emit mail.
    from send_emails import get_send_credentials, send_to_self

    send_to_self(
        get_send_credentials(),
        subject="Your morning digest",
        html_body=html,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
