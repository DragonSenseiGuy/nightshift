"""Git worktree management for sandbox runs.

Each sandbox run gets a disposable git worktree, mounted into the container so
the task is isolated from the main checkout. The context manager guarantees the
worktree is removed even if the run fails.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

WORKTREES_DIR = Path(__file__).resolve().parent / ".worktrees"


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_root), *args], check=True)


def _git_out(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], check=True, capture_output=True
    ).stdout


def _copy_working_tree_changes(repo_root: Path, path: Path) -> None:
    """Mirror the repo's *uncommitted* state into a fresh worktree.

    A plain worktree only has the committed HEAD; the nightly run needs to execute
    the code currently on disk. Applies tracked modifications as a patch, then copies
    untracked (non-ignored) files over.
    """
    diff = _git_out(repo_root, "diff", "HEAD", "--binary")
    if diff.strip():
        subprocess.run(
            ["git", "-C", str(path), "apply", "--whitespace=nowarn"],
            input=diff,
            check=True,
        )

    others = _git_out(repo_root, "ls-files", "--others", "--exclude-standard")
    for rel in others.decode().splitlines():
        if not rel:
            continue
        dest = path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_root / rel, dest)


@contextmanager
def worktree(
    repo_root: Path,
    branch: str | None = None,
    include_dirty: bool = False,
    new_branch: str | None = None,
    base: str = "HEAD",
) -> Iterator[Path]:
    """Create a disposable worktree, yield its path, then remove it.

    With ``branch`` the worktree checks out that ref; otherwise it detaches at
    HEAD so runs never disturb the current branch. With ``include_dirty`` the repo's
    uncommitted changes are mirrored in too, so the run sees the current working tree.

    ``new_branch`` (Phase 9) creates a *fresh* branch at ``base`` and checks it out here.
    The worktree is disposable; the branch is not — it survives the removal below, which
    is the point: the night's commits stay in the project repo for morning review while
    nothing about the human's checkout was touched. Creating it up front also means the
    agent's own commits land on the nightly branch directly, never on ``base``.
    """
    if new_branch and branch:
        raise ValueError("pass either `branch` (existing ref) or `new_branch`, not both")

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKTREES_DIR / uuid.uuid4().hex[:12]

    if new_branch:
        # No -B / --force: a name collision means tonight's run would silently reset
        # yesterday's unreviewed branch, so let git refuse instead.
        _git(repo_root, "worktree", "add", "-b", new_branch, str(path), base)
    elif branch:
        _git(repo_root, "worktree", "add", str(path), branch)
    else:
        _git(repo_root, "worktree", "add", "--detach", str(path), "HEAD")

    try:
        if include_dirty:
            _copy_working_tree_changes(repo_root, path)
        yield path
    finally:
        # --force handles the worktree being dirty from the task's writes.
        try:
            _git(repo_root, "worktree", "remove", "--force", str(path))
        except subprocess.CalledProcessError:
            # A sandboxed agent can scribble on anything in the mount, including the
            # `.git` pointer file that makes this a *linked* worktree. When that happens
            # `worktree remove` refuses to touch the directory, and leaving a stale
            # registration behind would block tomorrow's run too. Delete the directory and
            # let git reconcile its bookkeeping.
            shutil.rmtree(path, ignore_errors=True)
            _git(repo_root, "worktree", "prune")
