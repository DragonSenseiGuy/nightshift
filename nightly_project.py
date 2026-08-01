"""A night's project work, start to finish (Phase 9).

The host half of the project agent. Everything with a consequence is here, because
everything with a consequence needs a credential or a decision, and the sandbox gets
neither:

    host                                              sandbox
    ─────────────────────────────────────────         ────────────────────────────
    1. create agent/<date> in the project repo
    2. add a disposable worktree on that branch  ───▶  3. agent edits + commits
                                                       4. writes project_work.json
    5. sweep leftovers into a final commit  ◀──────────┘
    6. write out/diffs/<project>-<date>.diff
    7. push agent/<date> under the restricted key
    8. queue a *pending* merge_branch action
    9. hand back a ProjectWork for the briefing

Four rules this file exists to keep:

- **The branch is created before the agent runs**, so its commits can only land on
  ``agent/<date>``. ``main`` is never checked out, never committed to, never touched.
- **The deploy key never enters the sandbox.** Step 7 runs here; the container has no
  network route to a git remote at all.
- **Nothing merges.** Step 8 queues a `pending` action. Merging happens in
  ``approvals.py`` after a human approves it, and nowhere else.
- **Failures land in the briefing.** Every step is wrapped so a broken night produces a
  Failures entry rather than a silent gap — a missing "What I did" section reads exactly
  like a quiet night, and that is the bug.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date as date_type
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

import gitops
from config import ConfigError, ProjectConfig, StandingInstructions, load_config
from models import (
    ActionType,
    AgentWorkReport,
    Briefing,
    MergeBranchPayload,
    ProjectSection,
    ProjectWork,
)
from sandbox.worktree import worktree

REPO_ROOT = Path(__file__).resolve().parent
DIFF_DIR = REPO_ROOT / "out" / "diffs"

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    """A project name flattened into something safe to use as a filename."""
    return _SLUG.sub("-", name).strip("-").lower() or "project"


def diff_path_for(project: str, when: date_type, directory: Path = DIFF_DIR) -> Path:
    return directory / f"{_slug(project)}-{when.isoformat()}.diff"


def _resolve_repo(project: ProjectConfig) -> Path:
    if not project.path.strip():
        raise gitops.GitError(f"project {project.name!r} has no `path` configured")
    repo = Path(project.path).expanduser()
    if not (repo / ".git").exists():
        raise gitops.GitError(f"{repo} is not a git repository (project {project.name!r})")
    return repo


def _parse_report(raw: str) -> AgentWorkReport:
    """Validate the sandbox's JSON into an `AgentWorkReport`.

    It crossed a trust boundary as a file written by a container, so it is validated on
    the way in exactly like a broker response — and a malformed one degrades to a report
    that says so rather than failing the night after the work is already committed.
    """
    try:
        return AgentWorkReport.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic raises several shapes here
        return AgentWorkReport(summary=f"The agent's work report was unreadable: {exc!r}"[:4000])


class SandboxOutcome(BaseModel):
    """What one sandboxed project run produced, host-side.

    The runner seam may return either this or a bare `AgentWorkReport` (see
    `_normalise_outcome`): a stub that only cares about the work should not have to know
    that transcripts exist.
    """

    model_config = ConfigDict(extra="forbid")

    report: AgentWorkReport = Field(default_factory=AgentWorkReport)
    transcript_jsonl: str = Field(default="", description="Raw JSONL from the container")


def run_agent_in_sandbox(
    project: ProjectConfig, worktree_path: Path, config_path
) -> SandboxOutcome:
    """Default runner: the real container. Injected as a parameter so tests need no Docker."""
    from sandbox.orchestrator import run_project_step

    output = run_project_step(
        worktree_path=worktree_path, project=project.name, config_path=config_path
    )
    return SandboxOutcome(
        report=_parse_report(output.report), transcript_jsonl=output.transcript_jsonl
    )


def _normalise_outcome(returned) -> SandboxOutcome:
    """Accept either a `SandboxOutcome` or a plain `AgentWorkReport` from the runner seam."""
    if isinstance(returned, SandboxOutcome):
        return returned
    return SandboxOutcome(report=returned)


def _run_and_restore_git(runner, project: ProjectConfig, path: Path, config_path) -> SandboxOutcome:
    """Run the agent, then put the worktree's `.git` pointer back the way it was.

    A linked worktree's `.git` is a one-line file naming a gitdir **outside** the mount, so
    git inside the container is broken by construction — and an agent that tries anyway
    (`git init`, `git status` then "fixing" it) can replace that file with a directory,
    after which the host can neither commit the night's work nor tear the worktree down.
    The system prompt tells the agent not to use git; this is the part that does not depend
    on the agent having listened.

    Only the pointer is restored. Everything else the agent wrote is exactly what gets
    committed and reviewed — this repairs the plumbing, never the work.
    """
    pointer = path / ".git"
    original = pointer.read_bytes() if pointer.is_file() else None
    try:
        return _normalise_outcome(runner(project, path, config_path))
    finally:
        if original is not None and (not pointer.is_file() or pointer.read_bytes() != original):
            if pointer.is_dir():
                shutil.rmtree(pointer, ignore_errors=True)
            elif pointer.exists():
                pointer.unlink()
            pointer.write_bytes(original)
            print(f"Restored the worktree's .git pointer for {project.name!r}.")


def _store_transcript(store, outcome: SandboxOutcome, *, project: str, night_id: str) -> str:
    """Import the container's transcript into the host store; return its replay id.

    Best-effort on purpose (Phase 12): the branch, the diff and the queued merge are the
    night's real output, and losing the observability of a run that otherwise worked must
    never fail it. A missing or unreadable transcript costs the briefing one line.
    """
    if store is None or not outcome.transcript_jsonl.strip():
        return ""
    try:
        from runner.observe import parse_jsonl

        records = store.import_records(
            parse_jsonl(outcome.transcript_jsonl), night_id=night_id, project=project
        )
    except Exception as exc:  # noqa: BLE001 - observability is never worth the night
        print(f"Could not store the transcript for project {project!r}: {exc!r}")
        return ""
    return records[-1].id if records else ""


def run_project_night(
    config: StandingInstructions,
    project: ProjectConfig,
    *,
    when: date_type | None = None,
    config_path: Path | str | None = None,
    runner=run_agent_in_sandbox,
    queue=None,
    push: bool | None = None,
    diff_dir: Path = DIFF_DIR,
    store=None,
    snapshots=None,
    night_id: str = "",
) -> ProjectWork:
    """Do one project's night and return its "What I did" for the briefing.

    `runner` is the seam: it takes `(project, worktree_path, config_path)` and returns an
    `AgentWorkReport` having mutated the worktree. In production it is the sandbox; in
    tests it is a stub, which is what lets the branch/commit/diff/push machinery be
    verified end to end with no Docker and no network.

    Raises rather than swallowing: the caller (`nightly_projects`) turns a failure into a
    briefing entry, so this function stays honest about what happened.
    """
    when = when or date_type.today()
    repo = _resolve_repo(project)
    branch = gitops.nightly_branch(project.branch_prefix, when)
    gitops.require_agent_ref(branch, project.branch_prefix)

    if gitops.branch_exists(repo, branch):
        # Refusing beats `-B`: silently resetting yesterday's unreviewed branch would
        # destroy work a human has not looked at yet.
        raise gitops.GitError(
            f"branch {branch!r} already exists in {repo}; review or delete it first."
        )

    # Phase 13: freeze the repo *before* the first mutation — before the branch is created,
    # before the worktree exists, and long before the agent runs. Everything above this
    # line is a read or a refusal, so this is the last moment the repo is still untouched,
    # and a snapshot taken any later is a snapshot of the problem.
    #
    # Best-effort by contract: a repo that cannot be snapshotted still gets its night (see
    # `snapshots.snapshot_project`); it just gets no rollback id in the briefing.
    from snapshots import snapshot_project

    snapshot_id = snapshot_project(
        project, store=snapshots, note=f"pre-run snapshot for {when.isoformat()}"
    )

    base = gitops.head_sha(repo)
    work = ProjectWork(project=project.name[:120], branch=branch, snapshot_id=snapshot_id)

    with worktree(repo, new_branch=branch, base=base) as path:
        outcome = _run_and_restore_git(runner, project, path, config_path)
        report = outcome.report
        # The sweep: whatever the agent left uncommitted still becomes part of the branch,
        # because the worktree is about to be deleted and unreviewed work is better than
        # lost work.
        gitops.commit_all(path, f"agent: nightly work for {when.isoformat()}")
        head = gitops.head_sha(path)
        work.commits = gitops.commits_between(path, base, head)[:100]
        destination = diff_path_for(project.name, when, diff_dir)
        gitops.write_diff(path, base, destination, head)

    work.summary = report.summary
    work.highlights = list(report.highlights)
    work.diff_path = str(destination)
    work.transcript_id = _store_transcript(store, outcome, project=project.name, night_id=night_id)

    if not work.commits:
        # An empty night is a real outcome; say so plainly rather than shipping an empty card.
        work.highlights.insert(0, "No changes were committed tonight.")

    should_push = project.push if push is None else push
    if should_push and work.commits:
        gitops.push_branch(
            repo,
            branch,
            remote=project.remote,
            deploy_key=project.deploy_key,
            prefix=project.branch_prefix,
        )
        work.highlights.append(f"Pushed {branch} to {project.remote}.")

    if queue is not None and work.commits:
        # Pending, always. This is the only merge path that exists, and it does nothing
        # until a human approves it in the morning (security rule 3).
        queue.enqueue(
            ActionType.MERGE_BRANCH,
            MergeBranchPayload(
                project=project.name,
                branch=branch,
                into=project.merge_into,
                diff_path=str(destination),
            ),
            origin="project_agent",
            summary=f"Merge {branch} into {project.merge_into} ({len(work.commits)} commit(s))",
        )

    return work


def nightly_projects(
    config: StandingInstructions,
    briefing: Briefing,
    *,
    when: date_type | None = None,
    config_path: Path | str | None = None,
    runner=run_agent_in_sandbox,
    queue=None,
    push: bool | None = None,
    diff_dir: Path = DIFF_DIR,
    store=None,
    snapshots=None,
    night_id: str = "",
) -> ProjectSection:
    """Run every active project, highest priority first, into the briefing.

    One project's failure never stops the others, and never disappears: it becomes a
    `Failure` on the briefing with the project named in the stage.
    """
    section = briefing.projects or ProjectSection()
    for project in config.active_projects():
        try:
            section.projects.append(
                run_project_night(
                    config,
                    project,
                    when=when,
                    config_path=config_path,
                    runner=runner,
                    queue=queue,
                    push=push,
                    diff_dir=diff_dir,
                    store=store,
                    snapshots=snapshots,
                    night_id=night_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - every failure mode belongs in the briefing
            briefing.add_failure(
                f"project_agent:{project.name}",
                f"Nightly work on {project.name} failed",
                repr(exc),
            )
            print(f"Project {project.name!r} failed: {exc!r}")
    briefing.projects = section
    return section


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one night of project work.")
    parser.add_argument("--config", default=None, help="Standing-instructions TOML.")
    parser.add_argument("--project", default=None, help="Only this project (default: all active).")
    parser.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override the per-project `push` setting.",
    )
    parser.add_argument(
        "--queue-merge",
        action="store_true",
        help="Queue a pending merge_branch action (never merges; approval does that).",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error:\n{exc}")
        return 2

    queue = None
    if args.queue_merge:
        from approvals import ApprovalQueue

        queue = ApprovalQueue()

    briefing = Briefing(date=date_type.today().isoformat())
    if args.project:
        try:
            # Naming a project explicitly overrides `active`: "run this one now" is a
            # deliberate act, and silently doing nothing would be the wrong answer.
            chosen = config.project(args.project).model_copy(update={"active": True})
        except ConfigError as exc:
            print(exc)
            return 2
        config = config.model_copy(update={"projects": [chosen]})

    # A standalone project run is still a night as far as the history is concerned: it
    # opens a run-history row and stamps NIGHTSHIFT_NIGHT_ID so the container's transcript
    # comes back joined to it.
    from transcripts import REPLAY_COMMAND, NightOutcome, TranscriptStore, recording_night

    with recording_night(TranscriptStore()) as (store, night_id):
        section = nightly_projects(
            config,
            briefing,
            config_path=args.config,
            queue=queue,
            push=args.push,
            store=store,
            night_id=night_id,
        )
        store.finish_night(
            night_id,
            outcome=NightOutcome.FAILED if briefing.failures else NightOutcome.COMPLETED,
            failures=len(briefing.failures),
            stages=[work.project for work in section.projects],
            note="project run only",
        )

    for work in section.projects:
        print(f"\n{work.project}: {work.branch} — {len(work.commits)} commit(s)")
        print(f"  diff: {work.diff_path}")
        if work.transcript_id:
            print(f"  transcript: {REPLAY_COMMAND} {work.transcript_id}")
        if work.snapshot_id:
            from snapshots import ROLLBACK_COMMAND

            print(f"  undo:       {ROLLBACK_COMMAND} {work.snapshot_id}")
        print(f"  {work.summary[:400]}")
    for failure in briefing.failures:
        print(f"\nFAILED {failure.stage}: {failure.message}\n  {failure.detail}")
    return 1 if briefing.failures else 0


if __name__ == "__main__":
    sys.exit(main())
