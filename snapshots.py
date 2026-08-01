"""Pre-run project snapshots and one-command rollback (Phase 13).

The night before is cheap to redo and expensive to lose. This module takes a snapshot of
every project repo *before* the agent touches it, and gives you one command that puts the
repo back:

    uv run python snapshots.py rollback <id>

**Why git-based and not an APFS snapshot.** Both were considered; git wins on every axis
that matters here:

- *Granularity.* `tmutil localsnapshot` snapshots a whole APFS **volume**. Restoring one
  is a volume-level operation, so "undo last night's work on one project" would mean
  reverting the user's mail, browser profile, and every other repo they touched at 2am.
  A git snapshot is scoped to exactly one repository — the same scope the project agent
  is scoped to.
- *Privilege.* `tmutil` needs Full Disk Access, and mounting/reverting a local snapshot
  needs `sudo` and (for the boot volume) a reboot into Recovery. A tool that runs at 3am
  unattended and gets driven at 8am half-awake must not need either. Git needs neither.
- *Cost.* Local snapshots are retained by macOS on its own schedule, are thinned out
  automatically under disk pressure, and cannot be relied on to still exist in the morning.
  A snapshot ref is a couple of objects in the repo that nothing else can reclaim.
- *Fidelity to what changed.* The project agent's entire surface is a git worktree. What
  it produces is commits, refs, and files. Recording HEAD, the branch, the index and the
  working tree captures that surface exactly, whereas a volume snapshot captures it plus
  everything irrelevant.

**What a snapshot contains.** Three things, all of them real git objects so nothing can be
garbage-collected out from under you:

1. `head_sha` and the checked-out `branch` (empty when detached).
2. `worktree_tree` — a tree object holding the working directory's *content*: tracked
   files as they are on disk, plus untracked, non-ignored files. Uncommitted work is not a
   footnote here, it is the thing most likely to be lost, so it is a first-class part of
   the snapshot.
3. `index_tree` — the staging area exactly as it was, so a restore does not silently
   unstage (or stage) anything.

Both trees are made reachable from one ref, `refs/nightshift/snapshots/<id>`, via a
two-commit chain whose ancestor is the original HEAD. That ref is deliberately outside
`refs/heads/`, so it is invisible to `git branch`, unreachable by `gitops.push_branch`
(which refuses anything outside `agent/*`) and not carried by any default push refspec.

**What a rollback does NOT restore — read this before trusting it.**

- **Ignored files.** Anything matched by `.gitignore` is neither snapshotted nor restored,
  and `git clean -fd` (never `-fx`) leaves it alone. That is a deliberate trade: it is what
  keeps `.env`, `node_modules/` and virtualenvs out of the snapshot, and it means a
  rollback will not resurrect a deleted build artifact.
- **Anything outside the project path.** Nothing here touches a byte outside the repo the
  snapshot names. Not the database, not `out/`, not another project.
- **Side effects that already left the machine.** A pushed branch stays pushed; a sent
  email stays sent. Rollback is a local-history tool, not a time machine. (The approval
  queue is what stands between a night and a side effect in the first place.)
- **Stash entries, reflog, submodule contents, and file modes beyond git's own
  executable bit.**

**Rollback is destructive, so it is built to be safe when you are not awake.** It always
takes a *safety snapshot of the current state first* (so a rollback is itself
rollback-able), it prints the exact plan before doing anything, and it asks before
proceeding unless `--yes`. It refuses outright if you disable the safety snapshot on a
dirty tree without `--force`. Branch cleanup is scoped to `agent/*` refs: a branch the
night created is deleted, a branch you created is never touched.

**Containers stay ephemeral.** Nothing here is snapshotted inside a container and nothing
is restored into one. The sandbox is still torn down at the end of every run
(`sandbox/worktree.py`); the snapshot lives in the host repo, which is the only place a
night's output persists.

**Retention.** Snapshots accumulate one per project per night. `[retention]
snapshot_days` (default 30) prunes by age and `snapshot_keep` (default 5) always keeps
that many most recent per project regardless of age — an aged-out policy that deletes your
last remaining escape hatch is not a policy anyone wants at 8am.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import gitops

DB_ENV_VAR = "NIGHTSHIFT_SNAPSHOTS_DB"

# Alongside the approval queue and the transcript store: state, not run output.
DEFAULT_DB_PATH = Path.home() / "Library" / "Application Support" / "NightShift" / "snapshots.db"

# Outside refs/heads/ on purpose — see the module docstring.
REF_NAMESPACE = "refs/nightshift/snapshots"

ROLLBACK_COMMAND = "uv run python snapshots.py rollback"

# Identity for the snapshot commit objects. Same reasoning as `gitops.COMMIT_NAME`: a
# snapshot is NightShift's bookkeeping, never the human's authorship.
SNAPSHOT_NAME = "NightShift Snapshots"
SNAPSHOT_EMAIL = "nightshift-snapshots@localhost"

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class SnapshotError(RuntimeError):
    """Base class for snapshot/rollback failures."""


class SnapshotNotFound(SnapshotError):
    pass


class CorruptSnapshot(SnapshotError):
    """A stored row no longer matches the schema, or its git objects are gone."""


class RollbackRefused(SnapshotError):
    """A rollback was declined on safety grounds before anything was changed."""


def default_db_path() -> Path:
    override = os.getenv(DB_ENV_VAR, "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(text: str) -> str:
    return _UNSAFE_ID.sub("-", text).strip("-.").lower() or "project"


# --------------------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------------------


class RepoSnapshot(BaseModel):
    """One repository, frozen at one instant, as git objects plus the metadata to use them.

    Validated on the way out of SQLite exactly like `AgentRunRecord` and `Action`: by the
    time you read a snapshot back you are about to run destructive git commands from it,
    which is the worst possible moment to trust an unchecked row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=120)
    project: str = Field(default="", max_length=120)
    repo: str = Field(max_length=1000, description="Absolute path of the repo, resolved")
    created_at: datetime = Field(default_factory=_now)
    branch: str = Field(default="", max_length=200, description="Empty when HEAD was detached")
    head_sha: str = Field(max_length=64)
    worktree_tree: str = Field(max_length=64, description="Tree of the working directory")
    index_tree: str = Field(max_length=64, description="Tree of the staging area")
    dirty: bool = Field(default=False, description="Whether the tree had uncommitted work")
    branches: dict[str, str] = Field(
        default_factory=dict, description="Local branch → sha at snapshot time"
    )
    note: str = Field(default="", max_length=500)

    @property
    def ref(self) -> str:
        return f"{REF_NAMESPACE}/{self.id}"

    def describe(self) -> str:
        state = "dirty" if self.dirty else "clean"
        where = self.branch or f"detached at {self.head_sha[:8]}"
        return (
            f"{self.id}  {self.project or '(no project)'}  {where} @ {self.head_sha[:8]} "
            f"({state})  {self.created_at.isoformat(timespec='seconds')}"
        )


# --------------------------------------------------------------------------------------
# Taking a snapshot
# --------------------------------------------------------------------------------------


def _git_path(repo: Path, what: str) -> Path:
    """Resolve `git rev-parse --git-path <what>`, which is repo-relative for a linked worktree."""
    raw = gitops.git(repo, "rev-parse", "--git-path", what).strip()
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _write_worktree_tree(repo: Path, scratch: Path) -> str:
    """A tree of the working directory: tracked content on disk + untracked, non-ignored.

    Built against a *throwaway* index file so the repo's real index is not touched — the
    user may have a carefully staged change sitting there, and a backup that disturbs what
    it is backing up is not a backup.
    """
    index = scratch / "worktree.index"
    env = {"GIT_INDEX_FILE": str(index)}
    # An empty index plus `add -A` means "whatever is on disk right now", which is exactly
    # the definition we want; `-A` honours .gitignore, so ignored files stay out.
    gitops.git(repo, "add", "-A", env=env)
    return gitops.git(repo, "write-tree", env=env).strip()


def _write_index_tree(repo: Path, scratch: Path) -> str:
    """A tree of the staging area, from a *copy* of the index for the same reason as above.

    An unmerged index (a conflicted merge in progress) cannot be written as a tree. Rather
    than refuse to snapshot at the one moment the repo is most fragile, fall back to HEAD's
    tree and let the working-tree snapshot carry the content.
    """
    real = _git_path(repo, "index")
    if not real.exists():
        return gitops.git(repo, "rev-parse", "HEAD^{tree}").strip()
    copy = scratch / "staged.index"
    shutil.copy2(real, copy)
    env = {"GIT_INDEX_FILE": str(copy)}
    try:
        return gitops.git(repo, "write-tree", env=env).strip()
    except gitops.GitError:
        return gitops.git(repo, "rev-parse", "HEAD^{tree}").strip()


def _local_branches(repo: Path) -> dict[str, str]:
    out = gitops.git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    branches: dict[str, str] = {}
    for line in out.splitlines():
        name, _, sha = line.strip().partition(" ")
        if name and sha:
            branches[name] = sha
    return branches


def _commit_tree(repo: Path, tree: str, message: str, parent: str | None) -> str:
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return gitops.git(
        repo,
        "-c",
        f"user.name={SNAPSHOT_NAME}",
        "-c",
        f"user.email={SNAPSHOT_EMAIL}",
        *args,
        env={
            "GIT_AUTHOR_NAME": SNAPSHOT_NAME,
            "GIT_AUTHOR_EMAIL": SNAPSHOT_EMAIL,
            "GIT_COMMITTER_NAME": SNAPSHOT_NAME,
            "GIT_COMMITTER_EMAIL": SNAPSHOT_EMAIL,
        },
    ).strip()


def resolve_repo(path: str | Path) -> Path:
    """The repo a snapshot may operate on: an existing git working tree, fully resolved.

    Resolved (symlinks and all) because every later safety check — "is this the repo the
    snapshot names", "is this path inside the project" — compares strings, and two names
    for one directory would defeat all of them.
    """
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SnapshotError(f"{repo} is not a git repository")
    return repo


def take_snapshot(
    repo: Path | str,
    *,
    project: str = "",
    note: str = "",
    store: SnapshotStore | None = None,
    when: datetime | None = None,
) -> RepoSnapshot:
    """Freeze `repo` and record it. Cheap enough to run before every night, always.

    Read-only with respect to the working tree, the index and every existing ref: it adds
    objects and one new ref under `refs/nightshift/`, and nothing else.
    """
    repo = resolve_repo(repo)
    try:
        head = gitops.head_sha(repo)
    except gitops.GitError as exc:
        raise SnapshotError(
            f"{repo} has no commits yet; there is nothing to snapshot or roll back to."
        ) from exc

    created = when or _now()
    identifier = (
        f"{_slug(project or repo.name)}-{created.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    )

    scratch = Path(_git_path(repo, "nightshift-snapshot-scratch"))
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        worktree_tree = _write_worktree_tree(repo, scratch)
        index_tree = _write_index_tree(repo, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # Two commits, one ref: the ref keeps both trees *and* the original HEAD reachable, so
    # a rollback still works after the branch that held it has been deleted and gc has run.
    index_commit = _commit_tree(repo, index_tree, f"nightshift snapshot {identifier} (index)", head)
    tip = _commit_tree(repo, worktree_tree, f"nightshift snapshot {identifier}", index_commit)

    snapshot = RepoSnapshot(
        id=identifier,
        project=project[:120],
        repo=str(repo),
        created_at=created,
        branch=gitops.current_branch(repo),
        head_sha=head,
        worktree_tree=worktree_tree,
        index_tree=index_tree,
        dirty=gitops.is_dirty(repo),
        branches=_local_branches(repo),
        note=note[:500],
    )
    gitops.git(repo, "update-ref", snapshot.ref, tip)
    (store or SnapshotStore()).save(snapshot)
    return snapshot


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL DEFAULT '',
    repo          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    branch        TEXT NOT NULL DEFAULT '',
    head_sha      TEXT NOT NULL,
    worktree_tree TEXT NOT NULL,
    index_tree    TEXT NOT NULL,
    dirty         INTEGER NOT NULL DEFAULT 0,
    branches      TEXT NOT NULL DEFAULT '{}',
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS snapshots_created ON snapshots (created_at);
CREATE INDEX IF NOT EXISTS snapshots_project ON snapshots (project, created_at);
"""

_COLUMNS = (
    "id, project, repo, created_at, branch, head_sha, worktree_tree, index_tree, "
    "dirty, branches, note"
)


class SnapshotStore:
    """Durable snapshot metadata. A connection per operation, like `ApprovalQueue`.

    The *content* lives in each project's own repository as git objects; this database
    only remembers which object is which. That split is on purpose: the thing you need in
    an emergency is inside the repo you are recovering, and survives losing this file.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def save(self, snapshot: RepoSnapshot) -> RepoSnapshot:
        import json

        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO snapshots ({_COLUMNS}) VALUES ({', '.join('?' * 11)})",
                (
                    snapshot.id,
                    snapshot.project,
                    snapshot.repo,
                    snapshot.created_at.isoformat(),
                    snapshot.branch,
                    snapshot.head_sha,
                    snapshot.worktree_tree,
                    snapshot.index_tree,
                    int(snapshot.dirty),
                    json.dumps(snapshot.branches),
                    snapshot.note,
                ),
            )
        return snapshot

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> RepoSnapshot:
        import json

        try:
            return RepoSnapshot(
                id=row["id"],
                project=row["project"],
                repo=row["repo"],
                created_at=datetime.fromisoformat(row["created_at"]),
                branch=row["branch"],
                head_sha=row["head_sha"],
                worktree_tree=row["worktree_tree"],
                index_tree=row["index_tree"],
                dirty=bool(row["dirty"]),
                branches=json.loads(row["branches"] or "{}"),
                note=row["note"],
            )
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            raise CorruptSnapshot(
                f"stored snapshot {row['id']!r} does not match the current schema: {exc}"
            ) from exc

    def get(self, snapshot_id: str) -> RepoSnapshot:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no snapshot with id {snapshot_id!r}")
        return self._row_to_snapshot(row)

    def list(self, *, project: str | None = None, limit: int = 50) -> list[RepoSnapshot]:
        """Snapshots, newest first."""
        query = f"SELECT {_COLUMNS} FROM snapshots"
        params: list[object] = []
        if project is not None:
            query += " WHERE project = ?"
            params.append(project)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(limit, 1))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def latest(self, project: str) -> RepoSnapshot | None:
        found = self.list(project=project, limit=1)
        return found[0] if found else None

    def delete(self, snapshot_id: str) -> bool:
        with self._connect() as conn:
            return bool(
                conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,)).rowcount
            )

    # -- retention -------------------------------------------------------------------

    def prune(
        self, *, older_than_days: int, keep_per_project: int = 5, now: datetime | None = None
    ) -> list[RepoSnapshot]:
        """Delete aged-out snapshots, and their refs. Returns what was deleted.

        Two guards, because the failure mode here is "the escape hatch expired":
        `older_than_days <= 0` keeps everything, and `keep_per_project` always spares that
        many most-recent snapshots per project no matter how old they are. Deleting the
        row is not enough — the git objects are only reclaimable once the snapshot ref is
        gone, so the ref goes first and a repo that has since moved or vanished is not
        allowed to block the row's removal.
        """
        if older_than_days <= 0:
            return []
        cutoff = (now or _now()) - timedelta(days=older_than_days)

        kept: dict[str, int] = {}
        doomed: list[RepoSnapshot] = []
        for snapshot in self.list(limit=100_000):  # newest first, so counting is per-project
            seen = kept.get(snapshot.project, 0)
            if seen < max(keep_per_project, 0):
                kept[snapshot.project] = seen + 1
                continue
            if snapshot.created_at < cutoff:
                doomed.append(snapshot)

        for snapshot in doomed:
            try:
                gitops.git(Path(snapshot.repo), "update-ref", "-d", snapshot.ref, check=False)
            except (gitops.GitError, OSError):
                pass  # a moved or deleted repo must not strand the row forever
            self.delete(snapshot.id)
        return doomed

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


# --------------------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------------------


class RollbackPlan(BaseModel):
    """Exactly what a rollback is about to do, in the order it will do it.

    Built before anything is touched and printed verbatim, because "one command undoes
    last night" is only safe if the command tells you what it means by *undo* first.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    repo: str
    branch: str = ""
    head_sha: str = ""
    currently_on: str = ""
    current_head: str = ""
    dirty_now: bool = False
    delete_branches: list[str] = Field(default_factory=list)
    reset_branches: list[str] = Field(default_factory=list)
    safety_snapshot: bool = True

    def lines(self) -> list[str]:
        where = self.branch or f"detached HEAD at {self.head_sha[:8]}"
        now = self.currently_on or f"detached HEAD at {self.current_head[:8]}"
        out = [
            f"Rollback plan for snapshot {self.snapshot_id}",
            f"  repository        {self.repo}",
            f"  currently         {now} @ {self.current_head[:8]}"
            + ("  (uncommitted changes present)" if self.dirty_now else ""),
            f"  will restore to   {where} @ {self.head_sha[:8]}",
            "",
            "This will:",
        ]
        if self.safety_snapshot:
            out.append("  · take a safety snapshot of the CURRENT state first (undo the undo)")
        else:
            out.append("  · NOT take a safety snapshot — the current state will be unrecoverable")
        out += [
            f"  · move {where} back to {self.head_sha[:8]}",
            "  · restore every tracked file, staged change and untracked file to the snapshot",
            "  · delete untracked files created since the snapshot (ignored files are kept)",
        ]
        for name in self.delete_branches:
            out.append(f"  · delete the nightly branch {name} (created after the snapshot)")
        for name in self.reset_branches:
            out.append(f"  · restore the nightly branch {name} to where it was")
        out += [
            "",
            "It will NOT touch anything outside that repository, and it cannot un-push a",
            "pushed branch or un-send a sent email.",
        ]
        return out

    def render(self) -> str:
        return "\n".join(self.lines())


def plan_rollback(
    snapshot: RepoSnapshot, *, repo: Path | None = None, safety_snapshot: bool = True
) -> RollbackPlan:
    """Work out what `restore` would do, touching nothing."""
    repo = resolve_repo(repo or snapshot.repo)
    _require_same_repo(snapshot, repo)

    current = _local_branches(repo)
    delete = sorted(
        name
        for name in current
        if name not in snapshot.branches and gitops.is_agent_ref(name)
    )
    # Recorded-but-missing counts too: rolling *forward* onto a safety snapshot has to put
    # back the nightly branch that the rollback it is undoing deleted.
    reset = sorted(
        name
        for name, sha in snapshot.branches.items()
        if gitops.is_agent_ref(name) and current.get(name) != sha
    )
    return RollbackPlan(
        snapshot_id=snapshot.id,
        repo=str(repo),
        branch=snapshot.branch,
        head_sha=snapshot.head_sha,
        currently_on=gitops.current_branch(repo),
        current_head=gitops.head_sha(repo),
        dirty_now=gitops.is_dirty(repo),
        delete_branches=delete,
        reset_branches=reset,
        safety_snapshot=safety_snapshot,
    )


def _require_same_repo(snapshot: RepoSnapshot, repo: Path) -> None:
    """A snapshot may only be applied to the repository it was taken from.

    The check is the whole reason `resolve_repo` resolves symlinks. Running
    `read-tree --reset` and `clean -fd` against the wrong checkout is the single worst
    thing this module could do, and "the path in the row differs from the path you passed"
    is precisely the signal that it is about to.
    """
    recorded = Path(snapshot.repo)
    if repo != recorded:
        raise RollbackRefused(
            f"snapshot {snapshot.id} was taken from {recorded}, not {repo}. "
            "Refusing to restore one repository's state into another."
        )


def _require_objects(snapshot: RepoSnapshot, repo: Path) -> None:
    for kind, oid in (
        ("commit", snapshot.head_sha),
        ("tree", snapshot.worktree_tree),
        ("tree", snapshot.index_tree),
    ):
        try:
            gitops.git(repo, "cat-file", "-e", f"{oid}^{{{kind}}}")
        except gitops.GitError as exc:
            raise CorruptSnapshot(
                f"snapshot {snapshot.id} refers to {kind} {oid} which is no longer in {repo}; "
                f"the ref {snapshot.ref} was probably deleted."
            ) from exc


def restore(
    snapshot: RepoSnapshot,
    *,
    repo: Path | None = None,
    store: SnapshotStore | None = None,
    safety_snapshot: bool = True,
    force: bool = False,
) -> tuple[RollbackPlan, RepoSnapshot | None]:
    """Put the repository back the way the snapshot found it. Destructive, by definition.

    Returns the plan that was executed and the safety snapshot taken beforehand (if any),
    so a caller can print `snapshots.py rollback <that id>` and mean it.

    The order below is not arbitrary. HEAD moves first (which discards modifications to
    tracked files), then untracked leftovers are cleaned, and only then is the recorded
    content laid down — restoring content before moving HEAD would have git compare the
    snapshot against the wrong baseline and delete the wrong files.
    """
    repo = resolve_repo(repo or snapshot.repo)
    _require_same_repo(snapshot, repo)
    _require_objects(snapshot, repo)

    plan = plan_rollback(snapshot, repo=repo, safety_snapshot=safety_snapshot)
    if plan.dirty_now and not safety_snapshot and not force:
        raise RollbackRefused(
            f"{repo} has uncommitted changes and the safety snapshot is disabled. "
            "Re-run without --no-safety-snapshot, or pass --force to discard them."
        )

    taken: RepoSnapshot | None = None
    if safety_snapshot:
        taken = take_snapshot(
            repo,
            project=snapshot.project,
            note=f"safety snapshot taken before rolling back to {snapshot.id}",
            store=store,
        )

    # 1. HEAD, detached first so the branch ref can be moved without git objecting that it
    #    is checked out.
    gitops.git(repo, "checkout", "--force", "--detach", snapshot.head_sha)
    if snapshot.branch:
        gitops.git(repo, "branch", "--force", snapshot.branch, snapshot.head_sha)
        gitops.git(repo, "checkout", "--force", snapshot.branch)

    # 2. Untracked leftovers. `-fd`, never `-fx`: ignored files (.env, node_modules, venvs)
    #    are outside the snapshot and must survive it.
    gitops.git(repo, "clean", "-fd")

    # 3. The working tree, then the index. `read-tree -u --reset` writes the content and
    #    removes files the snapshot did not have; the second, worktree-less `read-tree`
    #    puts the staging area back without disturbing what was just written.
    gitops.git(repo, "read-tree", "-u", "--reset", snapshot.worktree_tree)
    gitops.git(repo, "read-tree", snapshot.index_tree)

    # 4. Nightly branches. Only `agent/*` — a branch the human made after the snapshot is
    #    not this tool's business, however confusing it makes the history look.
    for name in plan.delete_branches:
        gitops.git(repo, "branch", "-D", name)
    for name in plan.reset_branches:
        sha = snapshot.branches.get(name, "")
        if not sha:
            continue
        try:
            # The commit may have been gc'd since (nothing keeps a deleted branch's tip
            # alive). Say so and carry on: a missing nightly branch is a smaller loss than
            # a rollback that aborts halfway through restoring the working tree.
            gitops.git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        except gitops.GitError:
            print(f"Cannot restore branch {name}: commit {sha[:8]} is no longer in {repo}.")
            continue
        gitops.git(repo, "branch", "--force", name, sha)

    return plan, taken


def rollback(
    snapshot_id: str,
    *,
    store: SnapshotStore | None = None,
    safety_snapshot: bool = True,
    force: bool = False,
) -> tuple[RollbackPlan, RepoSnapshot | None]:
    """`restore` by id — the one-command entrypoint the briefing points at."""
    store = store or SnapshotStore()
    return restore(
        store.get(snapshot_id), store=store, safety_snapshot=safety_snapshot, force=force
    )


# --------------------------------------------------------------------------------------
# Nightly wiring
# --------------------------------------------------------------------------------------


def snapshot_project(project, *, store: SnapshotStore | None = None, note: str = "") -> str:
    """Snapshot one configured project before its night starts; return the snapshot id.

    Best-effort by contract, like the transcript import: a repo that cannot be snapshotted
    is a repo whose night is *more* worth running, not less, and the failure is visible in
    the briefing as a missing rollback id rather than as a night that never happened.
    """
    try:
        snapshot = take_snapshot(
            Path(project.path).expanduser(),
            project=project.name,
            note=note or "pre-run snapshot",
            store=store,
        )
    except (SnapshotError, gitops.GitError, OSError) as exc:
        print(f"Could not snapshot {project.name!r} before the run: {exc!r}")
        return ""
    return snapshot.id


def prune_snapshots(config, *, store: SnapshotStore | None = None) -> list[RepoSnapshot]:
    """Apply `[retention]` to the snapshot store. Called at the end of every night."""
    store = store or SnapshotStore()
    return store.prune(
        older_than_days=config.retention.snapshot_days,
        keep_per_project=config.retention.snapshot_keep,
    )


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _print_snapshots(snapshots: Sequence[RepoSnapshot]) -> None:
    if not snapshots:
        print("No snapshots recorded.")
        return
    print(f"{'id':<44}{'project':<18}{'branch':<22}{'head':<10}created")
    for snapshot in snapshots:
        print(
            f"{snapshot.id:<44}{snapshot.project[:17]:<18}"
            f"{(snapshot.branch or '(detached)')[:21]:<22}{snapshot.head_sha[:8]:<10}"
            f"{snapshot.created_at.isoformat(timespec='seconds')}"
        )


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="snapshots", description="NightShift pre-run project snapshots and rollback."
    )
    parser.add_argument("--db", default=None, help="Snapshot database path.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="Recorded snapshots, newest first.")
    listing.add_argument("--project", default=None, help="Only this project.")
    listing.add_argument("--limit", type=int, default=25)

    show = sub.add_parser("show", help="One snapshot in detail.")
    show.add_argument("id")

    take = sub.add_parser("take", help="Snapshot a repository right now.")
    take.add_argument("repo", help="Path to the git repository.")
    take.add_argument("--project", default="", help="Project name to file it under.")
    take.add_argument("--note", default="", help="Why you took it.")

    back = sub.add_parser("rollback", help="Restore a repository to a snapshot. Destructive.")
    back.add_argument("id", help="Snapshot id, as printed in the briefing.")
    back.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    back.add_argument("--dry-run", action="store_true", help="Print the plan and stop.")
    back.add_argument(
        "--no-safety-snapshot",
        action="store_true",
        help="Do not snapshot the current state first (then a dirty tree needs --force).",
    )
    back.add_argument("--force", action="store_true", help="Discard uncommitted work knowingly.")

    prune = sub.add_parser("prune", help="Delete snapshots older than N days.")
    prune.add_argument("--days", type=int, default=None, help="Default: [retention] in config.")
    prune.add_argument("--keep", type=int, default=None, help="Always keep N per project.")

    args = parser.parse_args(argv)
    store = SnapshotStore(args.db)

    if args.command == "list":
        _print_snapshots(store.list(project=args.project, limit=args.limit))
        return 0

    if args.command == "show":
        try:
            snapshot = store.get(args.id)
        except (SnapshotNotFound, CorruptSnapshot) as exc:
            print(exc)
            return 1
        print(snapshot.describe())
        print(f"  repo       {snapshot.repo}")
        print(f"  ref        {snapshot.ref}")
        print(f"  worktree   {snapshot.worktree_tree}")
        print(f"  index      {snapshot.index_tree}")
        print(f"  branches   {len(snapshot.branches)} local")
        if snapshot.note:
            print(f"  note       {snapshot.note}")
        print(f"\nRoll back with: {ROLLBACK_COMMAND} {snapshot.id}")
        return 0

    if args.command == "take":
        try:
            snapshot = take_snapshot(
                args.repo, project=args.project, note=args.note, store=store
            )
        except (SnapshotError, gitops.GitError) as exc:
            print(exc)
            return 1
        print(snapshot.describe())
        print(f"\nRoll back with: {ROLLBACK_COMMAND} {snapshot.id}")
        return 0

    if args.command == "rollback":
        try:
            snapshot = store.get(args.id)
            plan = plan_rollback(
                snapshot, safety_snapshot=not args.no_safety_snapshot
            )
        except (SnapshotNotFound, CorruptSnapshot, RollbackRefused, SnapshotError) as exc:
            print(exc)
            return 1

        print(plan.render())
        if args.dry_run:
            print("\n(dry run — nothing was changed)")
            return 0
        if not args.yes and not _confirm("\nProceed? [y/N] "):
            print("Cancelled. Nothing was changed.")
            return 1

        try:
            _, taken = restore(
                snapshot,
                store=store,
                safety_snapshot=not args.no_safety_snapshot,
                force=args.force,
            )
        except (RollbackRefused, CorruptSnapshot, SnapshotError, gitops.GitError) as exc:
            print(f"\nRollback refused: {exc}")
            return 1

        print(f"\nRestored {plan.repo} to snapshot {snapshot.id}.")
        if taken is not None:
            print(f"The state you just replaced is snapshot {taken.id}:")
            print(f"  {ROLLBACK_COMMAND} {taken.id}")
        return 0

    if args.command == "prune":
        days, keep = args.days, args.keep
        if days is None or keep is None:
            from config import load_config

            retention = load_config().retention
            days = retention.snapshot_days if days is None else days
            keep = retention.snapshot_keep if keep is None else keep
        deleted = store.prune(older_than_days=days, keep_per_project=keep)
        print(
            f"Deleted {len(deleted)} snapshot(s) older than {days} day(s), keeping the "
            f"{keep} most recent per project. Database is now "
            f"{store.size_bytes() / 1024:.0f} KiB."
        )
        return 0

    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":
    sys.exit(main())
