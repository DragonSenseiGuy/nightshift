"""The UI daemon surface (Phase 17) — the localhost API the SwiftUI client renders.

Phase 11 put every decision in `app/service.py` so the v2 client could be a *client*. This
module is the wire that makes that true: it exposes exactly `NightShiftService` — one
`AppState`, the `ActionPreview`s, approve/reject, "run now", the briefing, and the stored
transcripts — as JSON over loopback, so a SwiftUI app re-renders those models instead of
re-deriving when it is bedtime or what approving a merge does.

Three things about this surface, in the order they matter:

- **It is the third host process, and the second one that can cause a side effect.**
  `api.py` (broker) is read-only and is the only surface the sandbox can reach.
  `approvals.py` (:8401) performs effects. This (:8402) *asks* `approvals.py` to, through
  `NightShiftService.approve`, adding no effect of its own. It is never bridged into a
  container, and it must never merge into the broker — an approve route on the surface the
  sandbox talks to would hand an agent the ability to emit mail (security rule 3).
- **Loopback is not authentication.** Anything running as any user on this Mac can reach
  127.0.0.1, and this surface can send email and merge branches. So every route but
  `/health` requires a bearer token read from a 0600 file the daemon writes at startup
  (`~/Library/Application Support/NightShift/ui-token`); the SwiftUI client reads the same
  file. Compared with `hmac.compare_digest`, so a wrong token cannot be found a byte at a
  time.
- **Everything crossing the wire is already-validated Pydantic.** No new shapes were
  invented for the UI: `AppState`, `ActionPreview`, `RunSnapshot`, `AgentRunRecord`,
  `NightRunRecord`. A field the Swift client needs is a field one of those models grows,
  which keeps the rumps menu and the SwiftUI menu rendering the same truth.

Transcript text — the most thoroughly untrusted text in the system — is served as *data*
with its taint labels attached (`GET /runs/{id}` carries `taint`, and the replay text keeps
its UNTRUSTED banner). The client displays it; nothing here feeds it back to a model.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path

from app.service import ActionPreview, AppState, NightShiftService, RunSnapshot, ServiceError
from approvals import ActionNotFound, ActionNotPending, CorruptAction
from models import Action
from transcripts import RunNotFound, TranscriptStore, replay_text

DEFAULT_PORT = 8402

# The token lives beside the databases rather than in `out/`: like the approval queue it is
# state, and `out/` is regenerated every night.
TOKEN_ENV = "NIGHTSHIFT_UI_TOKEN_FILE"


def default_token_path() -> Path:
    override = os.environ.get(TOKEN_ENV)
    if override:
        return Path(override).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "NightShift"
        / "ui-token"
    )


def ensure_token(path: Path | str | None = None) -> str:
    """Read the UI token, creating a fresh one if there isn't a usable one yet.

    Written 0600 *before* the secret goes in it (`os.open` with the mode, not a chmod
    afterwards), so the token is never briefly world-readable on disk. A token file with
    looser permissions than that is rewritten rather than trusted: the point of the file is
    that only this user can read it.
    """
    path = Path(path) if path is not None else default_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        mode = stat.S_IMODE(path.stat().st_mode)
        if existing and not mode & (stat.S_IRWXG | stat.S_IRWXO):
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.chmod(path, 0o600)  # O_CREAT's mode is ignored when the file already existed
    return token


def create_app(
    service: NightShiftService | None = None,
    *,
    token: str = "",
    store: TranscriptStore | None = None,
):
    """Build the UI API over a service.

    A factory, like `approvals.create_app`: importing this module must not open a database,
    a token file or a second global surface, and the tests get their own app over a temp
    queue. Pass `token=""` only in tests — `main` always supplies one.
    """
    from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
    from fastapi.responses import FileResponse, PlainTextResponse

    service = service or NightShiftService()
    store = store or TranscriptStore()

    def require_token(authorization: str = Header(default="")) -> None:
        """Bearer check for every route but `/health`.

        `compare_digest` rather than `==`, and a 401 that says nothing about *why*: this
        endpoint can send mail, and a local process guessing at it should learn nothing.
        """
        if not token:
            return
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented, token):
            raise HTTPException(status_code=401, detail="unauthorised")

    app = FastAPI(
        title="NightShift UI",
        description="Host-only, loopback, token-gated. The SwiftUI client's whole backend.",
        version="0.1.0",
    )

    # Everything hangs off this router, so the token check is attached once rather than
    # remembered per route — a new endpoint is gated by where it is written, and forgetting
    # the dependency is not a thing you can do. `/health` is the one route on the bare app,
    # and it is the only one that may answer without a token.
    gated = APIRouter(dependencies=[Depends(require_token)])

    # -- status ------------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, str]:
        """Unauthenticated on purpose: it is how the client knows the daemon is up at all,
        and it reveals nothing a port scan would not."""
        return {"status": "ok", "service": "nightshift-ui"}

    @gated.get("/state", response_model=AppState)
    def state() -> AppState:
        return service.state()

    # -- approvals ---------------------------------------------------------------------

    @gated.get("/actions", response_model=list[ActionPreview])
    def actions() -> list[ActionPreview]:
        """Previews, not raw rows: the client must show the effect sentence, and deriving
        it twice is how the two UIs would come to disagree about what a click does."""
        return service.previews()

    @gated.post("/actions/{action_id}/approve", response_model=Action)
    def approve(action_id: str) -> Action:
        try:
            return service.approve(action_id)
        except ActionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionNotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CorruptAction as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @gated.post("/actions/{action_id}/reject", response_model=Action)
    def reject(action_id: str, reason: str = "") -> Action:
        try:
            return service.reject(action_id, reason=reason)
        except ActionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ActionNotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -- runs --------------------------------------------------------------------------

    @gated.post("/run", response_model=RunSnapshot)
    def run_now() -> RunSnapshot:
        """Spawn a night. Returns as soon as the subprocess exists — the run outlives both
        this request and the UI (see `app/service.py`)."""
        try:
            return service.run_now()
        except ServiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @gated.get("/briefing", response_class=FileResponse)
    def briefing():
        """The briefing HTML itself, so the client can show it in a WKWebView instead of
        bouncing the user to a browser. It is a self-contained, fully escaped artifact."""
        if not service.briefing_exists():
            raise HTTPException(status_code=404, detail="no briefing yet")
        return FileResponse(service.briefing_path, media_type="text/html")

    # -- transcripts -------------------------------------------------------------------

    @gated.get("/nights")
    def nights(limit: int = 30) -> list[dict]:
        """Run history, newest first, each with what it cost.

        `NightRunRecord` has no cost field — spend lives on the agent runs — so it is summed
        here rather than in the client: a history view without the number is the thing
        people open the history for.
        """
        return [
            {**record.model_dump(mode="json"), "cost_usd": store.night_cost(record.id)}
            for record in store.nights(limit=limit)
        ]

    @gated.get("/runs")
    def runs(night: str | None = None, agent: str | None = None, limit: int = 50) -> list[dict]:
        """Run summaries — no `messages`, no `transcript`.

        A night's worth of full conversations is megabytes, and a list view needs none of
        it; the client fetches one run in full when the user opens it.
        """
        records = store.runs(night_id=night, agent=agent, limit=limit)
        return [
            record.model_dump(mode="json", exclude={"messages", "transcript", "text"})
            for record in records
        ]

    @gated.get("/runs/{run_id}")
    def run(run_id: str) -> dict:
        try:
            return store.get(run_id).model_dump(mode="json")
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @gated.get("/runs/{run_id}/replay", response_class=PlainTextResponse)
    def replay(run_id: str, full: bool = False) -> str:
        """The same text `transcripts.py replay` prints, UNTRUSTED banner and all."""
        try:
            return replay_text(store.get(run_id), full=full)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    app.include_router(gated)
    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="NightShift UI API (host-only, loopback)")
    parser.add_argument("--config", type=Path, default=None, help="Standing instructions.")
    parser.add_argument("--db", type=Path, default=None, help="Approval queue database.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Loopback port.")
    parser.add_argument(
        "--token-file", type=Path, default=None, help="Where the bearer token lives."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Serve a canned night with every side effect disarmed (see app/demo.py).",
    )
    parser.add_argument(
        "--demo-dir", type=Path, default=None, help="Where demo state is built."
    )
    args = parser.parse_args(argv)

    from approvals import ApprovalQueue

    token_path = args.token_file or default_token_path()
    store: TranscriptStore | None = None
    if args.demo:
        # Demo mode swaps the *whole* environment — queue, briefing and run history — for
        # one built under `--demo-dir`, so it can never read or write the real ones.
        from app.demo import demo_service, seed

        # Seeding *first*: it clears the demo directory, and the token file usually lives
        # in there. Minting the token before the wipe leaves the daemon holding a secret
        # that is no longer on disk, and every request from the client 401s.
        env = seed(args.demo_dir)
        service = demo_service(env)
        store = TranscriptStore(env.transcripts_path)
        print(f"Demo mode: canned night in {env.root} (nothing here can send or merge)")
    else:
        service = NightShiftService(
            queue=ApprovalQueue(args.db) if args.db else None, config_path=args.config
        )
    token = ensure_token(token_path)
    print(f"NightShift UI API on http://127.0.0.1:{args.port} (token: {token_path})")
    # Loopback, always. This surface can approve a send; it must never be reachable off-host.
    uvicorn.run(
        create_app(service, token=token, store=store), host="127.0.0.1", port=args.port
    )
    return 0
