"""Host-side git for nightly agent branches (Phase 9).

Every git operation that carries risk lives here, on the **host**, and nowhere else:

- **Branch naming.** One `agent/YYYY-MM-DD` branch per project per night, built from the
  project's configured `branch_prefix`. The name is generated host-side from a date, never
  from anything a model wrote.
- **Committing.** The sandboxed agent may commit as it goes (a local commit needs no
  credential), but the host sweeps whatever is left into one final commit so no work is
  lost when the disposable worktree is torn down.
- **The diff artifact.** `out/diffs/<project>-<date>.diff` is produced here and its path is
  what `ProjectWork.diff_path` points the briefing at. A diff on disk is what makes
  "mandatory human review" a real step rather than a slogan.
- **Pushing.** The deploy key is a secret, so it never enters the sandbox — the container
  has no git remote access at all and the push happens here. `push_branch` refuses any
  refname outside the agent prefix *before* invoking git, and pushes one explicit refspec
  (never `--force`, never `--mirror`, never a config-driven default).
- **Merging.** `merge_agent_branch` exists only so `approvals.py` can call it after a human
  approves. Nothing in the nightly path calls it.

**Two locks, not one.** The refname check below is the client-side lock and it is the one
that can be bypassed by anyone with the key and a shell. The lock that actually holds is
server-side: `hooks/pre-receive` in this repo rejects every ref update outside
`refs/heads/agent/`, and is installed on the real remote (see README). The tests exercise
both — the client refusal, and a real push to a local bare repo with the hook installed.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date as date_type
from pathlib import Path

DEFAULT_BRANCH_PREFIX = "agent/"
DEFAULT_REMOTE = "origin"
DEPLOY_KEY_ENV = "NIGHTSHIFT_DEPLOY_KEY"

# Authorship for the host's sweep commit. Fixed rather than inherited from the user's git
# config so a night's work is never mistakenly attributed to the human reviewing it.
COMMIT_NAME = "NightShift Agent"
COMMIT_EMAIL = "nightshift-agent@localhost"

# What a refname may contain after the prefix. Deliberately narrower than git's own rules:
# no spaces, no `~^:?*[\`, no `..`, nothing that could be read as an option.
_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


class GitError(RuntimeError):
    """A git command failed. Always carries the command's stderr."""


class RefusedRef(GitError):
    """A ref outside the agent prefix was offered to a pushing/merging operation.

    Refused before git is invoked. This is the client-side half of the deploy-key
    restriction, and it is fatal on purpose: an agent branch is the *only* thing this
    program is ever allowed to write to a remote.
    """


# --------------------------------------------------------------------------------------
# Refnames
# --------------------------------------------------------------------------------------


def normalise_prefix(prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """A branch prefix, guaranteed to end in `/` so `agent/x` can never become `agentx`."""
    prefix = (prefix or DEFAULT_BRANCH_PREFIX).strip()
    if not prefix:
        prefix = DEFAULT_BRANCH_PREFIX
    return prefix if prefix.endswith("/") else prefix + "/"


def nightly_branch(
    prefix: str = DEFAULT_BRANCH_PREFIX, when: date_type | None = None
) -> str:
    """`agent/YYYY-MM-DD` for tonight (or a given date). Host-generated, always."""
    when = when or date_type.today()
    return f"{normalise_prefix(prefix)}{when.isoformat()}"


def is_agent_ref(branch: str, prefix: str = DEFAULT_BRANCH_PREFIX) -> bool:
    """True only for a well-formed branch under the agent prefix.

    Checks the *whole* name, not just the prefix: `agent/../main` starts with `agent/` and
    is exactly the sort of thing that must not reach a refspec.
    """
    prefix = normalise_prefix(prefix)
    if not branch or not branch.startswith(prefix):
        return False
    rest = branch[len(prefix) :]
    if not rest or ".." in rest or rest.endswith((".lock", "/", ".")):
        return False
    return bool(_SEGMENT.match(rest))


def require_agent_ref(branch: str, prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    if not is_agent_ref(branch, prefix):
        raise RefusedRef(
            f"refusing to operate on {branch!r}: only well-formed branches under "
            f"{normalise_prefix(prefix)!r} may be pushed or merged by NightShift."
        )
    return branch


# --------------------------------------------------------------------------------------
# Running git
# --------------------------------------------------------------------------------------


def git(
    repo: Path | str,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    """Run a git command in `repo` and return stdout. Raises `GitError` on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo} (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def head_sha(repo: Path | str, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).strip()


def current_branch(repo: Path | str) -> str:
    """The checked-out branch, or "" when detached."""
    name = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return "" if name == "HEAD" else name


def is_dirty(repo: Path | str) -> bool:
    return bool(git(repo, "status", "--porcelain").strip())


def branch_exists(repo: Path | str, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# --------------------------------------------------------------------------------------
# Committing a night's work
# --------------------------------------------------------------------------------------


def commit_all(repo: Path | str, message: str) -> str | None:
    """Stage everything and commit. Returns the new sha, or None if nothing changed.

    Called after the sandbox exits, so it captures whatever the agent left uncommitted.
    Authorship is pinned with `-c` rather than written into the repo's config: the
    disposable worktree shares its config with the real project, and a nightly run must
    not edit the human's `user.email`.
    """
    if not is_dirty(repo):
        return None
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        f"user.name={COMMIT_NAME}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        "commit",
        "--no-verify",
        "-m",
        message,
    )
    return head_sha(repo)


def commits_between(repo: Path | str, base: str, head: str = "HEAD") -> list[str]:
    """One-line subjects of the commits `base..head`, oldest first."""
    out = git(repo, "log", "--reverse", "--pretty=format:%h %s", f"{base}..{head}")
    return [line for line in out.splitlines() if line.strip()]


def write_diff(repo: Path | str, base: str, dest: Path, head: str = "HEAD") -> Path:
    """Write `base..head` as a unified diff and return the path.

    The artifact exists so the morning review is reading the *actual* change rather than
    the agent's description of it. Written even when empty, so a briefing link never
    points at a missing file.
    """
    diff = git(repo, "diff", f"{base}..{head}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(diff, encoding="utf-8")
    return dest


# --------------------------------------------------------------------------------------
# Pushing under the restricted deploy key
# --------------------------------------------------------------------------------------


def resolve_deploy_key(path: str | Path | None = None) -> Path | None:
    """The restricted deploy key: explicit path, else `$NIGHTSHIFT_DEPLOY_KEY`, else none.

    A *path* to a key is not itself a secret, so it may live in the standing instructions;
    the key material stays on the host filesystem and never enters the sandbox.
    """
    raw = str(path or "").strip() or os.getenv(DEPLOY_KEY_ENV, "").strip()
    if not raw:
        return None
    resolved = Path(raw).expanduser()
    if not resolved.exists():
        raise GitError(f"deploy key not found at {resolved}")
    return resolved


def ssh_command(deploy_key: Path | None) -> str | None:
    """`GIT_SSH_COMMAND` pinning ssh to exactly this key.

    `IdentitiesOnly=yes` plus `IdentityAgent=none` matter as much as `-i`: without them
    ssh will happily offer every key in the running agent first, and the push would
    succeed under the human's full-access key while looking like it used the restricted
    one. Failing the push is the correct outcome if the deploy key is wrong.
    """
    if deploy_key is None:
        return None
    return (
        f"ssh -i {deploy_key} -o IdentitiesOnly=yes -o IdentityAgent=none "
        "-o StrictHostKeyChecking=yes"
    )


def push_branch(
    repo: Path | str,
    branch: str,
    *,
    remote: str = DEFAULT_REMOTE,
    deploy_key: str | Path | None = None,
    prefix: str = DEFAULT_BRANCH_PREFIX,
) -> str:
    """Push one agent branch under the restricted key. Refuses anything else.

    Note the explicit `refs/heads/x:refs/heads/x` refspec: it removes every way a
    `push.default`, a `remote.<name>.push` config or a matching-refs default could widen
    what leaves the machine. And there is deliberately no `force` parameter — an agent
    branch is append-only, and rewriting history on a remote is not a thing this program
    should be able to do at 3am.
    """
    require_agent_ref(branch, prefix)
    key = resolve_deploy_key(deploy_key)
    env = {}
    command = ssh_command(key)
    if command:
        env["GIT_SSH_COMMAND"] = command
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    git(repo, "push", remote, refspec, env=env or None)
    return refspec


# --------------------------------------------------------------------------------------
# Merging — approval-queue only
# --------------------------------------------------------------------------------------


def merge_agent_branch(
    repo: Path | str,
    branch: str,
    *,
    into: str = "main",
    prefix: str = DEFAULT_BRANCH_PREFIX,
) -> str:
    """Merge a reviewed agent branch into `into`. Called **only** by an approved action.

    Preconditions are strict and checked rather than fixed up: `into` must be the checked
    out branch and the tree must be clean. Stashing or switching branches under a human's
    working copy is the kind of helpfulness that loses work, and this runs on the machine
    the user codes on.

    `--no-ff` so the merge is one revertable commit even when the branch fast-forwards.
    """
    require_agent_ref(branch, prefix)
    if not branch_exists(repo, branch):
        raise GitError(f"branch {branch!r} does not exist in {repo}")

    on = current_branch(repo)
    if on != into:
        raise GitError(
            f"{repo} has {on or 'a detached HEAD'} checked out, not {into!r}; "
            "check out the target branch before approving the merge."
        )
    if is_dirty(repo):
        raise GitError(f"{repo} has uncommitted changes; refusing to merge into {into!r}.")

    before = head_sha(repo)
    git(
        repo,
        "-c",
        f"user.name={COMMIT_NAME}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        f"Merge reviewed nightly branch {branch}",
        branch,
    )
    after = head_sha(repo)
    return f"merged {branch} into {into} ({before[:8]}..{after[:8]})"
