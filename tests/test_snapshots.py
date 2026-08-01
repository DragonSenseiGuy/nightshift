"""Phase 13 — pre-run snapshots and one-command rollback.

The claims under test, in the order the phase makes them:

1. **A snapshot captures everything a rollback needs**: the branch, HEAD, the staging area,
   and — the part most likely to be lost — uncommitted and untracked work.
2. **Rollback genuinely restores.** Mutate committed, uncommitted and untracked state, roll
   back, and the tree matches the snapshot byte for byte.
3. **Rollback protects you from itself.** It snapshots the current state first, so a
   rollback is undoable; disabling that on a dirty tree is refused without `--force`.
4. **Rollback never reaches outside the project path**, and refuses a snapshot taken from a
   different repository.
5. **The id is recorded and retrievable**, and reaches the briefing.
6. **Retention prunes, but never the last escape hatch.**

Every test runs against a scratch repo in `tmp_path`. Nothing here points at a real
project, and in particular nothing points at the NightShift checkout itself.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import gitops
import snapshots as snap
from config import ProjectConfig, RetentionConfig, StandingInstructions
from models import Briefing, ProjectWork

TODAY = date(2026, 7, 24)


# --------------------------------------------------------------------------------------
# Scratch repositories
# --------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def make_repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path.parent, "init", "-q", "-b", branch, str(path))
    _git(path, "config", "user.name", "Test Human")
    _git(path, "config", "user.email", "human@example.test")
    (path / "README.md").write_text("scratch project\n", encoding="utf-8")
    (path / ".gitignore").write_text("secrets.env\nbuild/\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    return path


def tree_state(repo: Path) -> dict[str, str]:
    """Every non-ignored file's content, relative to the repo. The thing a rollback restores."""
    listing = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard")
    state = {}
    for rel in sorted(line for line in listing.splitlines() if line.strip()):
        target = repo / rel
        if target.is_file():
            state[rel] = target.read_text(encoding="utf-8", errors="replace")
    return state


def status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain").strip()


@pytest.fixture
def store(tmp_path: Path) -> snap.SnapshotStore:
    return snap.SnapshotStore(tmp_path / "snapshots.db")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "project")


# --------------------------------------------------------------------------------------
# 1. Taking a snapshot
# --------------------------------------------------------------------------------------


def test_snapshot_records_branch_head_and_dirtiness(repo: Path, store) -> None:
    (repo / "work.py").write_text("half done\n", encoding="utf-8")
    taken = snap.take_snapshot(repo, project="scratch", store=store)

    assert taken.branch == "main"
    assert taken.head_sha == gitops.head_sha(repo)
    assert taken.dirty is True
    assert taken.project == "scratch"
    assert Path(taken.repo) == repo.resolve()
    assert taken.branches["main"] == taken.head_sha


def test_snapshot_captures_untracked_and_staged_work_as_git_objects(repo: Path, store) -> None:
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("loose\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    taken = snap.take_snapshot(repo, store=store)

    listed = _git(repo, "ls-tree", "-r", "--name-only", taken.worktree_tree).split()
    assert {"README.md", "untracked.txt", "staged.txt"} <= set(listed)
    # The index tree is the staging area, which had `staged.txt` but not the loose file.
    staged = _git(repo, "ls-tree", "-r", "--name-only", taken.index_tree).split()
    assert "staged.txt" in staged and "untracked.txt" not in staged


def test_snapshot_excludes_ignored_files(repo: Path, store) -> None:
    (repo / "secrets.env").write_text("OPENROUTER_API_KEY=hunter2\n", encoding="utf-8")
    taken = snap.take_snapshot(repo, store=store)
    listed = _git(repo, "ls-tree", "-r", "--name-only", taken.worktree_tree).split()
    assert "secrets.env" not in listed


def test_snapshot_does_not_disturb_the_repository(repo: Path, store) -> None:
    """A backup that changes what it is backing up is not a backup."""
    (repo / "untracked.txt").write_text("loose\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    before_status, before_head, before_tree = status(repo), gitops.head_sha(repo), tree_state(repo)

    snap.take_snapshot(repo, store=store)

    assert status(repo) == before_status
    assert gitops.head_sha(repo) == before_head
    assert tree_state(repo) == before_tree


def test_snapshot_ref_lives_outside_refs_heads(repo: Path, store) -> None:
    """So `git branch` never shows it and `gitops.push_branch` can never send it."""
    taken = snap.take_snapshot(repo, store=store)
    assert taken.ref.startswith("refs/nightshift/")
    assert "main" == _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").strip()
    assert _git(repo, "rev-parse", "--verify", taken.ref).strip()
    with pytest.raises(gitops.RefusedRef):
        gitops.push_branch(repo, taken.ref)


def test_snapshot_survives_aggressive_gc(repo: Path, store) -> None:
    """The whole point of a ref: the objects cannot be collected out from under you."""
    (repo / "untracked.txt").write_text("loose\n", encoding="utf-8")
    taken = snap.take_snapshot(repo, store=store)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now", "--aggressive", "-q")
    assert _git(repo, "cat-file", "-e", f"{taken.worktree_tree}^{{tree}}") == ""


def test_snapshot_of_a_repo_without_commits_is_refused(tmp_path: Path, store) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(tmp_path, "init", "-q", str(empty))
    with pytest.raises(snap.SnapshotError, match="no commits"):
        snap.take_snapshot(empty, store=store)


# --------------------------------------------------------------------------------------
# 2. Rollback restores
# --------------------------------------------------------------------------------------


def test_rollback_restores_committed_uncommitted_and_untracked_work(repo: Path, store) -> None:
    """The acceptance criterion, in one test."""
    (repo / "README.md").write_text("about to change\n", encoding="utf-8")
    (repo / "notes.txt").write_text("uncommitted thought\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    before_tree, before_status, before_head = tree_state(repo), status(repo), gitops.head_sha(repo)

    taken = snap.take_snapshot(repo, project="scratch", store=store)

    # A bad night: a new commit, a modified file, a deleted file, a new untracked file.
    (repo / "agent.py").write_text("print('mischief')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "agent: nightly work")
    (repo / "README.md").write_text("clobbered by the agent\n", encoding="utf-8")
    (repo / "notes.txt").unlink()
    (repo / "extra.txt").write_text("debris\n", encoding="utf-8")

    assert tree_state(repo) != before_tree

    snap.restore(taken, store=store)

    assert tree_state(repo) == before_tree
    assert status(repo) == before_status
    assert gitops.head_sha(repo) == before_head
    assert gitops.current_branch(repo) == "main"
    assert not (repo / "agent.py").exists()
    assert not (repo / "extra.txt").exists()


def test_rollback_keeps_ignored_files(repo: Path, store) -> None:
    """`clean -fd`, never `-fx`: .env and build output are outside the snapshot's scope."""
    (repo / "secrets.env").write_text("KEY=1\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("artifact\n", encoding="utf-8")
    taken = snap.take_snapshot(repo, store=store)

    (repo / "junk.py").write_text("x\n", encoding="utf-8")
    snap.restore(taken, store=store)

    assert (repo / "secrets.env").read_text(encoding="utf-8") == "KEY=1\n"
    assert (repo / "build" / "out.bin").exists()
    assert not (repo / "junk.py").exists()


def test_rollback_restores_a_deleted_branch_and_a_detached_head(repo: Path, store) -> None:
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    detached = snap.take_snapshot(repo, store=store)
    assert detached.branch == ""

    _git(repo, "checkout", "-q", "main")
    snap.restore(detached, store=store)
    assert gitops.current_branch(repo) == ""
    assert gitops.head_sha(repo) == detached.head_sha


def test_rollback_deletes_the_nightly_branch_but_not_the_humans(repo: Path, store) -> None:
    taken = snap.take_snapshot(repo, store=store)
    _git(repo, "branch", "agent/2026-07-24")
    _git(repo, "branch", "my-feature")

    plan, _ = snap.restore(taken, store=store)

    assert plan.delete_branches == ["agent/2026-07-24"]
    branches = set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())
    assert "agent/2026-07-24" not in branches
    assert {"main", "my-feature"} <= branches


def test_rollback_restores_a_nightly_branch_it_had_recorded(repo: Path, store) -> None:
    """Rolling forward onto a safety snapshot puts back the branch the rollback deleted."""
    _git(repo, "branch", "agent/2026-07-24")
    tip = gitops.head_sha(repo)
    with_branch = snap.take_snapshot(repo, store=store)
    _git(repo, "branch", "-D", "agent/2026-07-24")

    plan, _ = snap.restore(with_branch, store=store)

    assert plan.reset_branches == ["agent/2026-07-24"]
    assert gitops.branch_exists(repo, "agent/2026-07-24")
    assert gitops.head_sha(repo, "agent/2026-07-24") == tip


def test_rollback_undoes_an_approved_merge(repo: Path, store) -> None:
    """The bad-night case that actually mutates the human's checkout."""
    taken = snap.take_snapshot(repo, store=store)
    _git(repo, "checkout", "-q", "-b", "agent/2026-07-24")
    (repo / "agent.py").write_text("regrettable\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "agent work")
    _git(repo, "checkout", "-q", "main")
    gitops.merge_agent_branch(repo, "agent/2026-07-24", into="main")
    assert (repo / "agent.py").exists()

    snap.restore(taken, store=store)

    assert not (repo / "agent.py").exists()
    assert gitops.head_sha(repo) == taken.head_sha
    assert gitops.current_branch(repo) == "main"


# --------------------------------------------------------------------------------------
# 3. Rollback protects you from itself
# --------------------------------------------------------------------------------------


def test_rollback_snapshots_the_current_state_first(repo: Path, store) -> None:
    """Undoing the undo. The rollback you ran at 8am half-awake is itself reversible."""
    first = snap.take_snapshot(repo, store=store)
    (repo / "important.txt").write_text("work I forgot I had\n", encoding="utf-8")

    _, safety = snap.restore(first, store=store)

    assert safety is not None
    assert not (repo / "important.txt").exists()
    assert "before rolling back" in safety.note

    snap.restore(safety, store=store)
    assert (repo / "important.txt").read_text(encoding="utf-8") == "work I forgot I had\n"


def test_rollback_refuses_a_dirty_tree_with_the_safety_snapshot_disabled(repo: Path, store) -> None:
    taken = snap.take_snapshot(repo, store=store)
    (repo / "unsaved.txt").write_text("hours of work\n", encoding="utf-8")

    with pytest.raises(snap.RollbackRefused, match="uncommitted changes"):
        snap.restore(taken, store=store, safety_snapshot=False)

    assert (repo / "unsaved.txt").exists()  # nothing was touched

    snap.restore(taken, store=store, safety_snapshot=False, force=True)
    assert not (repo / "unsaved.txt").exists()


def test_plan_states_the_destructive_parts_before_anything_runs(repo: Path, store) -> None:
    taken = snap.take_snapshot(repo, project="scratch", store=store)
    _git(repo, "branch", "agent/2026-07-24")
    (repo / "loose.txt").write_text("x\n", encoding="utf-8")

    plan = snap.plan_rollback(taken)
    rendered = plan.render()

    assert taken.id in rendered
    assert str(repo.resolve()) in rendered
    assert "safety snapshot" in rendered
    assert "delete untracked files" in rendered
    assert "agent/2026-07-24" in rendered
    assert plan.dirty_now is True
    # Planning is read-only.
    assert (repo / "loose.txt").exists()
    assert gitops.head_sha(repo) == taken.head_sha


def test_cli_dry_run_changes_nothing_and_prints_the_plan(repo: Path, tmp_path, capsys) -> None:
    db = tmp_path / "cli.db"
    store = snap.SnapshotStore(db)
    taken = snap.take_snapshot(repo, project="scratch", store=store)
    (repo / "after.txt").write_text("later work\n", encoding="utf-8")

    assert snap.main(["--db", str(db), "rollback", taken.id, "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "Rollback plan" in out and "nothing was changed" in out
    assert (repo / "after.txt").exists()


def test_cli_rollback_cancels_when_the_prompt_is_declined(repo: Path, tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "cli.db"
    store = snap.SnapshotStore(db)
    taken = snap.take_snapshot(repo, store=store)
    (repo / "after.txt").write_text("later work\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    assert snap.main(["--db", str(db), "rollback", taken.id]) == 1
    assert "Cancelled" in capsys.readouterr().out
    assert (repo / "after.txt").exists()


def test_cli_rollback_with_yes_restores_and_names_the_undo(repo: Path, tmp_path, capsys) -> None:
    db = tmp_path / "cli.db"
    store = snap.SnapshotStore(db)
    taken = snap.take_snapshot(repo, store=store)
    (repo / "after.txt").write_text("later work\n", encoding="utf-8")

    assert snap.main(["--db", str(db), "rollback", taken.id, "--yes"]) == 0

    out = capsys.readouterr().out
    assert not (repo / "after.txt").exists()
    assert snap.ROLLBACK_COMMAND in out  # the safety snapshot's own rollback command
    safety = [s for s in store.list(limit=10) if "before rolling back" in s.note]
    assert safety and safety[0].id in out


# --------------------------------------------------------------------------------------
# 4. Scope: nothing outside the project path
# --------------------------------------------------------------------------------------


def test_rollback_refuses_a_snapshot_from_another_repository(tmp_path: Path, store) -> None:
    mine = make_repo(tmp_path / "mine")
    theirs = make_repo(tmp_path / "theirs")
    taken = snap.take_snapshot(mine, project="mine", store=store)

    with pytest.raises(snap.RollbackRefused, match="Refusing to restore"):
        snap.restore(taken, repo=theirs, store=store)


def test_rollback_touches_nothing_outside_the_repository(tmp_path: Path, store) -> None:
    repo = make_repo(tmp_path / "project")
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "precious.txt").write_text("not yours\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("also not yours\n", encoding="utf-8")

    taken = snap.take_snapshot(repo, store=store)
    (repo / "junk.txt").write_text("x\n", encoding="utf-8")
    snap.restore(taken, store=store)

    assert (sibling / "precious.txt").read_text(encoding="utf-8") == "not yours\n"
    assert outside.read_text(encoding="utf-8") == "also not yours\n"
    assert not (repo / "junk.txt").exists()


def test_rollback_reports_a_snapshot_whose_objects_are_gone(repo: Path, store) -> None:
    taken = snap.take_snapshot(repo, store=store)
    broken = taken.model_copy(update={"worktree_tree": "0" * 40})
    with pytest.raises(snap.CorruptSnapshot, match="no longer in"):
        snap.restore(broken, store=store)


def test_resolve_repo_refuses_a_non_repository(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(snap.SnapshotError, match="not a git repository"):
        snap.resolve_repo(plain)


# --------------------------------------------------------------------------------------
# 5. The id is recorded, retrievable, and reaches the briefing
# --------------------------------------------------------------------------------------


def test_snapshot_id_round_trips_through_the_store(repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "snapshots.db"
    taken = snap.take_snapshot(repo, project="scratch", store=snap.SnapshotStore(db))

    reopened = snap.SnapshotStore(db)  # a different process would see exactly this
    assert reopened.get(taken.id) == taken
    assert reopened.latest("scratch").id == taken.id
    assert [s.id for s in reopened.list(project="scratch")] == [taken.id]
    with pytest.raises(snap.SnapshotNotFound):
        reopened.get("no-such-snapshot")


def test_a_night_snapshots_the_project_before_it_touches_it(tmp_path: Path, store) -> None:
    import nightly_project

    repo = make_repo(tmp_path / "project")
    (repo / "in-progress.txt").write_text("mid-refactor\n", encoding="utf-8")
    before = tree_state(repo)
    project = ProjectConfig(name="scratch", path=str(repo), push=False)
    config = StandingInstructions(projects=[project])

    def runner(_project, path, _config_path):
        (path / "agent-made-this.txt").write_text("nightly work\n", encoding="utf-8")
        from models import AgentWorkReport

        return AgentWorkReport(summary="did a thing")

    work = nightly_project.run_project_night(
        config,
        project,
        when=TODAY,
        runner=runner,
        snapshots=store,
        diff_dir=tmp_path / "diffs",
    )

    assert work.snapshot_id
    recorded = store.get(work.snapshot_id)
    assert recorded.project == "scratch"
    # Taken *before* the night: it has the mid-refactor file and not the agent's.
    listed = _git(repo, "ls-tree", "-r", "--name-only", recorded.worktree_tree).split()
    assert "in-progress.txt" in listed and "agent-made-this.txt" not in listed

    # And rolling back to it removes the nightly branch the run created.
    _git(repo, "checkout", "-q", "main")
    snap.restore(recorded, store=store)
    assert tree_state(repo) == before
    assert not gitops.branch_exists(repo, "agent/2026-07-24")


def test_a_failed_snapshot_does_not_fail_the_night(tmp_path: Path, monkeypatch, capsys) -> None:
    """Best-effort by contract: no rollback id is a missing line, not a missing night."""
    import nightly_project

    repo = make_repo(tmp_path / "project")
    project = ProjectConfig(name="scratch", path=str(repo), push=False)
    config = StandingInstructions(projects=[project])

    def explode(*_args, **_kwargs):
        raise snap.SnapshotError("no disk space for objects")

    monkeypatch.setattr(snap, "take_snapshot", explode)

    work = nightly_project.run_project_night(
        config,
        project,
        when=TODAY,
        runner=lambda *_: __import__("models").AgentWorkReport(summary="ok"),
        diff_dir=tmp_path / "diffs",
    )

    assert work.snapshot_id == ""
    assert work.branch == "agent/2026-07-24"
    assert "Could not snapshot" in capsys.readouterr().out


def test_the_briefing_prints_the_rollback_command() -> None:
    from briefing import ROLLBACK_HINT, render_briefing_html
    from models import ProjectSection

    assert ROLLBACK_HINT == snap.ROLLBACK_COMMAND  # pinned so the two cannot drift

    briefing = Briefing(date="Friday")
    briefing.projects = ProjectSection(
        projects=[
            ProjectWork(project="scratch", branch="agent/2026-07-24", snapshot_id="scratch-abc-123")
        ]
    )
    html = render_briefing_html(briefing)
    assert "uv run python snapshots.py rollback scratch-abc-123" in html


# --------------------------------------------------------------------------------------
# 6. Retention
# --------------------------------------------------------------------------------------


def _aged(repo: Path, store, *, project: str, days: int, index: int) -> snap.RepoSnapshot:
    when = datetime.now(timezone.utc) - timedelta(days=days, seconds=index)
    return snap.take_snapshot(repo, project=project, store=store, when=when)


def test_prune_deletes_aged_snapshots_and_their_refs(repo: Path, store) -> None:
    old = _aged(repo, store, project="scratch", days=90, index=0)
    for i in range(1, 6):
        _aged(repo, store, project="scratch", days=1, index=i)

    deleted = store.prune(older_than_days=30, keep_per_project=5)

    assert [s.id for s in deleted] == [old.id]
    with pytest.raises(snap.SnapshotNotFound):
        store.get(old.id)
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", old.ref],
            capture_output=True,
        ).returncode
        != 0
    )


def test_prune_never_deletes_the_last_escape_hatch(repo: Path, store) -> None:
    """Every snapshot is ancient, but `snapshot_keep` still spares the most recent ones."""
    for i in range(4):
        _aged(repo, store, project="scratch", days=365, index=i)

    assert store.prune(older_than_days=1, keep_per_project=5) == []
    assert len(store.list(project="scratch")) == 4

    deleted = store.prune(older_than_days=1, keep_per_project=2)
    assert len(deleted) == 2
    assert len(store.list(project="scratch")) == 2


def test_prune_keeps_everything_when_days_is_zero(repo: Path, store) -> None:
    _aged(repo, store, project="scratch", days=9999, index=0)
    assert store.prune(older_than_days=0, keep_per_project=0) == []
    assert len(store.list()) == 1


def test_prune_counts_keep_per_project_not_globally(tmp_path: Path, store) -> None:
    one = make_repo(tmp_path / "one")
    two = make_repo(tmp_path / "two")
    for i in range(3):
        _aged(one, store, project="one", days=100, index=i)
        _aged(two, store, project="two", days=100, index=i)

    store.prune(older_than_days=30, keep_per_project=2)

    assert len(store.list(project="one")) == 2
    assert len(store.list(project="two")) == 2


def test_prune_survives_a_repository_that_has_moved_away(tmp_path: Path, store) -> None:
    doomed = make_repo(tmp_path / "gone")
    for i in range(2):
        _aged(doomed, store, project="gone", days=100, index=i)
    import shutil

    shutil.rmtree(doomed)

    deleted = store.prune(older_than_days=30, keep_per_project=1)
    assert len(deleted) == 1
    assert len(store.list(project="gone")) == 1


def test_prune_snapshots_reads_the_retention_section(repo: Path, store) -> None:
    assert (RetentionConfig().snapshot_days, RetentionConfig().snapshot_keep) == (30, 5)
    for i in range(3):
        _aged(repo, store, project="scratch", days=100, index=i)

    config = StandingInstructions(retention=RetentionConfig(snapshot_days=30, snapshot_keep=1))
    assert len(snap.prune_snapshots(config, store=store)) == 2
    assert len(store.list(project="scratch")) == 1


def test_nightly_prune_is_wired_and_never_fails_the_night(monkeypatch, capsys) -> None:
    from orchestrator import nightly

    called: list[object] = []
    monkeypatch.setattr(
        snap, "prune_snapshots", lambda config, **kw: called.append(config) or [object()]
    )
    nightly._prune_snapshots(StandingInstructions())
    assert called and "Pruned 1 aged-out project snapshot(s)." in capsys.readouterr().out

    def explode(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(snap, "prune_snapshots", explode)
    nightly._prune_snapshots(StandingInstructions())  # must not raise
    assert "Could not prune the snapshot store" in capsys.readouterr().out
