"""The mini nightly run: summarise inside the sandbox, send from the host.

Step 2 (summarise) runs in the locked-down sandbox container — it reads email from
the host broker over the mounted Unix socket and calls the LLM through the egress
proxy, then writes the briefing into the worktree. The host reads that briefing back
and emails it.

`--mock` runs the broker on canned fixtures (no Gmail, no OAuth); `--no-send` stops
after the briefing is read back instead of emailing it. Together they give a fully
offline-credential end-to-end exercise of the bridge.
"""

import argparse
import os
import sys

from config import ConfigError, load_config
from sandbox.orchestrator import run_summariser_step

SINCE = os.getenv("NIGHTSHIFT_SINCE", "2h")


def main() -> int:
    parser = argparse.ArgumentParser(description="NightShift mini nightly run")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run the broker on canned fixtures (no Gmail call).",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Write/print the briefing but do not email it.",
    )
    parser.add_argument("--since", default=SINCE, help="Email lookback window.")
    parser.add_argument(
        "--config",
        default=None,
        help="Standing-instructions TOML staged into the sandbox for this run.",
    )
    args = parser.parse_args()

    try:
        # Load host-side purely to fail fast and loudly on a broken config; the
        # orchestrator stages the same file into the worktree for the sandbox to read.
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error:\n{exc}")
        return 2
    print(
        "Config: "
        + (config.source_path or "built-in defaults (no config file)")
        + f" — email agent model {config.agent('email_agent').model}"
    )

    if args.mock:
        # The broker subprocess inherits this, so the flag needs no plumbing of its own.
        os.environ["NIGHTSHIFT_MOCK"] = "1"

    print("NightShift — running step 2 inside the sandbox\n")
    html = run_summariser_step(since=args.since, config_path=args.config)

    if not html:
        print("No briefing to send.")
        return 0

    if args.no_send:
        print(f"\n--no-send: briefing is {len(html)} bytes; not emailing it.")
        return 0

    # Imported late: the send credential is host-only and must not be touched on
    # paths that never send.
    from send_emails import get_send_credentials, send_to_self

    print("\nSending digest from the host...")
    send_to_self(get_send_credentials(), subject="Your morning digest", html_body=html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
