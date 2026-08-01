"""Step 2 (summarise), run *inside* the sandbox container.

The container has no general network access: it reaches the broker only through a
mounted Unix socket (`NIGHTSHIFT_BROKER_SOCKET`) and the LLM only through the
allowlisting egress proxy (`HTTPS_PROXY`). It fetches email, summarises it, and
writes the briefing into the mounted worktree at `/workspace/out/briefing.html`,
where the host picks it up after the container exits.

Standing instructions reach the sandbox as a *file in the mounted worktree*, named by
`NIGHTSHIFT_CONFIG` (the orchestrator copies whichever config the host resolved). No new
channel and no secret: the config is host-authored, trusted, non-secret data, so shipping
it in with the code is the simplest safe thing. If it is absent the run continues on
validated defaults rather than failing at 3am.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from broker_client import BrokerClient
from config import ConfigError, load_config, use_config
from summarise import summarise_emails

SINCE = os.getenv("NIGHTSHIFT_SINCE", "2h")
OUTPUT_PATH = Path(os.getenv("NIGHTSHIFT_BRIEFING_PATH", "/workspace/out/briefing.html"))


def _load_standing_instructions() -> None:
    """Install the standing instructions, degrading to defaults if they're unusable.

    The host has already validated this file; a failure here means the copy into the
    worktree went wrong, which is worth reporting but not worth losing the night over.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"summarise_step: config unusable ({exc}); falling back to defaults.")
        return
    print(
        "summarise_step: config "
        + (config.source_path or "defaults (no file)")
        + f"; model {config.agent('email_agent').model}"
    )
    use_config(config)


def main() -> int:
    _load_standing_instructions()
    print(f"summarise_step: fetching emails ({SINCE}) via broker socket...")
    with BrokerClient.from_env() as client:
        emails = client.fetch_emails(SINCE)
    print(f"summarise_step: fetched {len(emails)} emails.")

    if not emails:
        print("summarise_step: nothing to summarise; no briefing written.")
        return 0

    print("summarise_step: summarising via LLM (through egress proxy)...")
    # summarise_emails validates the model's JSON into an EmailDigest and renders the
    # HTML from that model — the briefing the host reads back is never raw LLM output.
    html = summarise_emails(emails, since=SINCE)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"summarise_step: wrote briefing to {OUTPUT_PATH} ({len(html)} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
