"""The approval queue: the only place a side effect may happen.

security rule 3 — *all side effects require morning approval* — is enforced here. An
agent can never send, merge, or run anything; it can only *propose*. A proposal is a row in
a SQLite queue, and the row's effect fires exactly once, host-side, after a human approves.

**Why this is a separate surface from `api.py`.** The broker is the read side: it holds a
`gmail.readonly` credential and, by construction, cannot import `send_emails`. Putting
`/approve` on that app would hand the read surface a route that emits mail, which is the
exact split Phase 1 exists to maintain — one compromised route on the process the sandbox
talks to would be enough. So the queue is its own module with its own FastAPI app
(`create_app`) on its own loopback port, and the sandbox has no bridge to it at all. The
Phase 11 menu-bar UI can equally well use `ApprovalQueue` directly, in-process; the HTTP
app is a thin shell over the same class, never a second implementation.

**Why SQLite (stdlib `sqlite3`).** The queue must survive a restart — a night's proposals
outlive the run that made them, and "approve tomorrow morning" is the whole point. No new
dependency, which matters because the sandbox bakes its deps at image build time.

**Ordering, so a crash is never a silent send.** `approve()` claims the row with a
conditional `UPDATE ... WHERE status='pending'`; only the writer that flipped exactly one
row runs the effect. A second approve of the same action finds zero rows and raises. If the
process dies mid-effect the row stays `approved` — visibly stuck, never re-fired
automatically — because an unsent mail is a smaller failure than a double-sent one.

**Taint.** Draft bodies are email-derived. They may be shown to a human and sent after an
explicit approve, but there is deliberately no path from an `Action` back into a
`runner.taint.PromptPart`: an approved draft is a side effect, not context for an agent.
"""

from __future__ import annotations

import html as html_module
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from pydantic import ValidationError

from models import (
    PAYLOAD_MODELS,
    Action,
    ActionPayload,
    ActionStatus,
    ActionType,
    DraftReplyPayload,
    EmailDigest,
    MergeBranchPayload,
    SendEmailPayload,
)

DB_ENV_VAR = "NIGHTSHIFT_APPROVALS_DB"

# State, not output: the queue outlives a run, so it does not belong in `out/` next to the
# regenerated briefing. macOS's per-user application-support dir is the conventional home.
DEFAULT_DB_PATH = (
    Path.home() / "Library" / "Application Support" / "NightShift" / "approvals.db"
)


def default_db_path() -> Path:
    """Where the queue lives, honouring `$NIGHTSHIFT_APPROVALS_DB` (tests, alt profiles)."""
    override = os.getenv(DB_ENV_VAR, "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ApprovalError(RuntimeError):
    """Base class for queue misuse. Always fatal to the request that caused it."""


class ActionNotFound(ApprovalError):
    pass


class ActionNotPending(ApprovalError):
    """Someone tried to decide an action that was already decided.

    This is the double-fire guard surfacing. It is an error, not a no-op: a UI that thinks
    it just approved a send when it did not is worse than one that shows a stale-state
    message.
    """


class CorruptAction(ApprovalError):
    """A stored row no longer matches the schema and will not be executed."""


# --------------------------------------------------------------------------------------
# Effects — what approval actually *does*
# --------------------------------------------------------------------------------------
#
# An effect takes the validated action and returns a short human-readable result that is
# stored on the row. Raising is fine and expected: the queue records the error and moves the
# action to `failed`, so a broken effect is visible in the morning rather than lost.

Effect = Callable[[Action], str]


def _plain_to_html(text: str) -> str:
    """Render a plain-text body as HTML, escaping every character of it.

    Draft bodies are model-written after reading untrusted mail, so they are treated exactly
    like every other email-derived string in this repo: escaped, never interpreted. The
    worst a hostile email can do to an approved reply is make it read oddly.
    """
    return "<div>" + html_module.escape(text).replace("\n", "<br>\n") + "</div>"


def _payload_of[T: ActionPayload](action: Action, expected: type[T]) -> T:
    """Narrow an action's payload, checked rather than asserted.

    `Action` already validates payload-against-type, so this can only fire if an executor
    was registered for the wrong type. A real `raise` (not an `assert`, which `-O` strips)
    keeps that a loud recorded failure instead of a send with the wrong fields.
    """
    if not isinstance(action.payload, expected):
        raise CorruptAction(
            f"action {action.id!r} carries {type(action.payload).__name__}, "
            f"expected {expected.__name__}"
        )
    return action.payload


def send_email_effect(action: Action) -> str:
    """Actually send mail. The one function in the queue that reaches Google.

    `send_emails` is imported lazily so that merely importing this module (the UI, the
    tests, the enqueue path) never constructs a Google client or touches the Keychain.
    """
    from send_emails import get_send_credentials, send_email

    payload = _payload_of(action, SendEmailPayload)
    sent = send_email(
        get_send_credentials(),
        to=payload.to,
        subject=payload.subject,
        html_body=payload.html_body,
    )
    return f"sent to {payload.to} (message id {sent.get('id', '?')})"


def draft_reply_effect(action: Action) -> str:
    """Send an approved draft reply, escaped into HTML on the way out."""
    from send_emails import get_send_credentials, send_email

    payload = _payload_of(action, DraftReplyPayload)
    sent = send_email(
        get_send_credentials(),
        to=payload.to,
        subject=payload.subject,
        html_body=_plain_to_html(payload.body),
    )
    return f"replied to {payload.to} (message id {sent.get('id', '?')})"


def merge_branch_effect(action: Action) -> str:
    """Merge a reviewed nightly `agent/*` branch. The only merge path that exists.

    Three things this function is careful about, all of them because it runs `git merge` on
    the machine the human actually codes on:

    - **The repo comes from config, not the payload.** `MergeBranchPayload` carries a
      project *name*; the path is looked up in the standing instructions (host-authored).
      A queue row is untrusted by the time it is read back, and a payload that could name a
      directory would be a payload that could merge somewhere nobody asked for.
    - **The branch must still be an `agent/*` ref**, re-checked against the project's own
      prefix by `gitops.merge_agent_branch` — the same check the push path uses. Approval
      approves *this* branch, not an arbitrary refname that reached the database.
    - **It only runs after `approve()`.** There is no other caller, in this file or any
      other, and `run_project_night` only ever enqueues the action as `pending`.

    Imported lazily so importing the queue (the UI, tests, the enqueue path) never shells
    out to git.
    """
    import gitops
    from config import active_config

    payload = _payload_of(action, MergeBranchPayload)
    project = active_config().project(payload.project)
    repo = Path(project.path).expanduser()
    if not (repo / ".git").exists():
        raise gitops.GitError(f"{repo} is not a git repository (project {project.name!r})")
    return gitops.merge_agent_branch(
        repo, payload.branch, into=payload.into, prefix=project.branch_prefix
    )


DEFAULT_EFFECTS: dict[ActionType, Effect] = {
    ActionType.SEND_EMAIL: send_email_effect,
    ActionType.DRAFT_REPLY: draft_reply_effect,
    ActionType.MERGE_BRANCH: merge_branch_effect,
}


# --------------------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT '',
    taint        TEXT NOT NULL DEFAULT '[]',
    summary      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    decided_at   TEXT,
    decided_by   TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    result       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS actions_status ON actions (status, created_at);
"""

_COLUMNS = (
    "id, type, status, payload, origin, taint, summary, created_at, "
    "decided_at, decided_by, reason, completed_at, result, error"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ApprovalQueue:
    """Durable queue of proposed side effects, with the effects attached.

    A connection is opened per operation rather than held open: the queue is written by the
    nightly orchestrator and read by a long-lived UI, possibly in different processes, and
    per-operation connections keep that honest without a lock of our own.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        effects: dict[ActionType, Effect] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A copy, so a caller's stub registry (tests, dry runs) can never leak into the
        # module-level defaults and quietly disarm a later real queue.
        self._effects = dict(effects) if effects is not None else dict(DEFAULT_EFFECTS)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    # -- reading -------------------------------------------------------------------

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> Action:
        """Re-validate a stored row into an `Action`.

        The database is untrusted input by the time we read it back — an older schema, a
        hand-edited row, a payload written by a version that spelled a field differently.
        Anything that does not validate raises `CorruptAction` instead of being executed.
        """
        try:
            action_type = ActionType(row["type"])
            payload_model = PAYLOAD_MODELS[action_type]
            payload: ActionPayload = payload_model.model_validate_json(row["payload"])
            return Action(
                id=row["id"],
                type=action_type,
                status=ActionStatus(row["status"]),
                payload=payload,
                origin=row["origin"],
                taint=[t for t in (row["taint"] or "").split(",") if t],
                summary=row["summary"],
                created_at=datetime.fromisoformat(row["created_at"]),
                decided_at=(
                    datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None
                ),
                decided_by=row["decided_by"],
                reason=row["reason"],
                completed_at=(
                    datetime.fromisoformat(row["completed_at"])
                    if row["completed_at"]
                    else None
                ),
                result=row["result"],
                error=row["error"],
            )
        except (ValueError, KeyError, ValidationError) as exc:
            raise CorruptAction(
                f"stored action {row['id']!r} does not match the current schema: {exc}"
            ) from exc

    def get(self, action_id: str) -> Action:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise ActionNotFound(f"no queued action with id {action_id!r}")
        return self._row_to_action(row)

    def list(self, status: ActionStatus | None = None) -> list[Action]:
        """All actions, oldest first, optionally filtered by status."""
        query = f"SELECT {_COLUMNS} FROM actions"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status.value,)
        query += " ORDER BY created_at, id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_action(row) for row in rows]

    def pending(self) -> list[Action]:
        """What the morning UI shows. Nothing here has had any effect."""
        return self.list(ActionStatus.PENDING)

    # -- writing -------------------------------------------------------------------

    def enqueue(
        self,
        action_type: ActionType,
        payload: ActionPayload | dict,
        *,
        origin: str = "",
        taint: Iterable[str] = (),
        summary: str = "",
    ) -> Action:
        """Propose an action. Always lands as `pending` — enqueueing has no side effect.

        The payload is validated against the type here *and* again on every read, so a
        payload that never made sense cannot sit in the queue waiting to be approved.
        """
        action_type = ActionType(action_type)
        model = PAYLOAD_MODELS[action_type]
        validated: ActionPayload = (
            payload if isinstance(payload, model) else model.model_validate(payload)
        )
        action = Action(
            id=uuid.uuid4().hex,
            type=action_type,
            status=ActionStatus.PENDING,
            payload=validated,
            origin=origin,
            taint=sorted({t for t in taint}),
            summary=summary,
            created_at=_now(),
        )
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO actions ({_COLUMNS}) VALUES ({', '.join('?' * 14)})",
                (
                    action.id,
                    action.type.value,
                    action.status.value,
                    action.payload.model_dump_json(),
                    action.origin,
                    ",".join(action.taint),
                    action.summary,
                    action.created_at.isoformat(),
                    None,
                    "",
                    "",
                    None,
                    "",
                    "",
                ),
            )
        return action

    def _claim(self, action_id: str, status: ActionStatus, by: str, reason: str) -> Action:
        """Atomically move a *pending* action to a decided state, or raise.

        The `WHERE status='pending'` is the entire double-fire guard: two approvals racing
        (two UI clicks, a UI and a CLI) both run this, exactly one updates a row, and the
        loser gets `ActionNotPending` rather than a second send.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE actions SET status = ?, decided_at = ?, decided_by = ?, reason = ? "
                "WHERE id = ? AND status = ?",
                (
                    status.value,
                    _iso(_now()),
                    by,
                    reason,
                    action_id,
                    ActionStatus.PENDING.value,
                ),
            )
            claimed = cursor.rowcount == 1
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise ActionNotFound(f"no queued action with id {action_id!r}")
        if not claimed:
            raise ActionNotPending(
                f"action {action_id!r} is {row['status']}, not pending — refusing to "
                "decide it twice"
            )
        return self._row_to_action(row)

    def approve(self, action_id: str, *, by: str = "human") -> Action:
        """Approve an action **and perform its effect**. The only path to a side effect.

        Returns the action in its final state: `done` with a result, or `failed` with the
        error recorded. A failed effect never disappears and never silently retries.
        """
        action = self._claim(action_id, ActionStatus.APPROVED, by, reason="")
        effect = self._effects.get(action.type)
        if effect is None:
            # Fail closed: an action type with no executor must not look approved-and-done.
            return self._complete(
                action.id,
                ActionStatus.FAILED,
                error=f"no executor registered for {action.type}",
            )
        try:
            result = effect(action)
        except Exception as exc:
            return self._complete(action.id, ActionStatus.FAILED, error=repr(exc))
        return self._complete(action.id, ActionStatus.DONE, result=str(result or "done"))

    def reject(self, action_id: str, *, by: str = "human", reason: str = "") -> Action:
        """Discard an action. The effect is never constructed, let alone run.

        The row is kept (in `rejected`) rather than deleted: "I decided not to send that"
        is exactly the kind of thing you want to be able to check later. `purge_decided`
        cleans up when you actually want it gone.
        """
        return self._claim(action_id, ActionStatus.REJECTED, by, reason=reason)

    def _complete(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        result: str = "",
        error: str = "",
    ) -> Action:
        with self._connect() as conn:
            conn.execute(
                "UPDATE actions SET status = ?, completed_at = ?, result = ?, error = ? "
                "WHERE id = ? AND status = ?",
                (
                    status.value,
                    _iso(_now()),
                    result[:2000],
                    error[:4000],
                    action_id,
                    ActionStatus.APPROVED.value,
                ),
            )
        return self.get(action_id)

    def purge_decided(self, *, keep_failed: bool = True) -> int:
        """Delete finished rows. Failures are kept by default — they are the audit trail."""
        statuses = [ActionStatus.DONE.value, ActionStatus.REJECTED.value]
        if not keep_failed:
            statuses.append(ActionStatus.FAILED.value)
        placeholders = ", ".join("?" * len(statuses))
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM actions WHERE status IN ({placeholders})", statuses
            )
            return cursor.rowcount


# --------------------------------------------------------------------------------------
# Enqueueing from a night's work
# --------------------------------------------------------------------------------------


def enqueue_digest_drafts(
    queue: ApprovalQueue, digest: EmailDigest, *, origin: str = "email_agent"
) -> list[Action]:
    """Queue every draft reply in a digest. Generated by the agent, sent by nobody.

    The recipient is parsed from the *fetched* email's `From` header, which the host filled
    in — never from anything the model wrote — so an injected "reply to attacker@evil" in an
    email body cannot redirect an approved reply.
    """
    queued: list[Action] = []
    for item in digest.items:
        if item.draft_reply is None:
            continue
        _, address = parseaddr(item.sender)
        if not address:
            # No parseable sender means no safe recipient. Skipping is right: a draft with a
            # guessed address is a side effect waiting to go somewhere unintended.
            continue
        queued.append(
            queue.enqueue(
                ActionType.DRAFT_REPLY,
                DraftReplyPayload(
                    email_id=item.email_id,
                    to=address,
                    subject=item.draft_reply.subject[:500],
                    body=item.draft_reply.body[:100_000],
                ),
                origin=origin,
                taint=["email"],
                summary=f"Reply to {address}: {item.subject[:200]}",
            )
        )
    return queued


# --------------------------------------------------------------------------------------
# HTTP surface (host-only, separate app and port from the broker)
# --------------------------------------------------------------------------------------


def create_app(queue: ApprovalQueue | None = None):
    """Build the approvals API over a queue.

    A factory rather than a module-level `app`: importing this module must not create a
    database or a second global surface, and tests get their own app over a temp queue.
    Binds to loopback only (see `main`) — nothing in the sandbox has a route to it.
    """
    from fastapi import Body, FastAPI, HTTPException

    queue = queue or ApprovalQueue()
    app = FastAPI(
        title="NightShift approvals",
        description="Host-only approval queue. Approving is what performs a side effect.",
        version="0.1.0",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "pending": str(len(queue.pending()))}

    @app.get("/actions", response_model=list[Action])
    def list_actions(status: ActionStatus | None = None) -> list[Action]:
        return queue.list(status)

    @app.get("/actions/{action_id}", response_model=Action)
    def get_action(action_id: str) -> Action:
        try:
            return queue.get(action_id)
        except ActionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CorruptAction as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/actions/{action_id}/approve", response_model=Action)
    def approve_action(action_id: str, by: str = Body("human", embed=True)) -> Action:
        """Approve and fire. `by` is recorded so the decision is attributable."""
        try:
            return queue.approve(action_id, by=by)
        except ActionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionNotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/actions/{action_id}/reject", response_model=Action)
    def reject_action(
        action_id: str,
        by: str = Body("human", embed=True),
        reason: str = Body("", embed=True),
    ) -> Action:
        try:
            return queue.reject(action_id, by=by, reason=reason)
        except ActionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionNotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="NightShift approvals API (host-only)")
    parser.add_argument("--db", type=Path, default=None, help="Queue database path.")
    parser.add_argument("--port", type=int, default=8401, help="Loopback port.")
    args = parser.parse_args()

    queue = ApprovalQueue(args.db)
    print(f"Approvals queue at {queue.path} ({len(queue.pending())} pending)")
    # Loopback, always: this app can send mail, so it must never be reachable off-host.
    uvicorn.run(create_app(queue), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
