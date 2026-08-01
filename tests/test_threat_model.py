"""Phase 16 — the threat-model review, as assertions.

The README's threat model makes three security claims and this file is where they stop being prose. Every
test here corresponds to a line in the "Threat model" section of README.md; where the
review found the claim *did not hold as written*, the test pins what is actually true
rather than what we would like to be true — a green suite that encodes a comfortable
fiction is worse than a red one.

The one finding worth reading before the rest: **the LLM API key does enter the sandbox.**
`sandbox/orchestrator.py:_llm_env` passes `OPENROUTER_API_KEY` into both containers,
because the summariser and the project agent both call the model from *inside* the box.
That is a real secret in an untrusted zone, and `test_the_llm_key_is_the_one_secret_in_the
_sandbox` exists to make sure it stays the *only* one and that nobody deletes the sentence
in the docs that admits it.

Coverage note: the pieces already pinned elsewhere are not duplicated here —
`tests/test_scopes.py` (read/send credential split), `tests/test_broker_bridge.py` (no
Google credential and no network route into the sandbox), `tests/test_agent_separation.py`
and `tests/test_calendar_tasks.py` (the two injection fixtures), `tests/test_project_
branches.py` (the `agent/*` refusal and the real pre-receive hook), and
`tests/test_end_to_end.py` (both injections through a whole night).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from sandbox import orchestrator

REPO_ROOT = Path(orchestrator.__file__).resolve().parent.parent


# --------------------------------------------------------------------------------------
# Rule 1 — secrets never enter the sandbox
# --------------------------------------------------------------------------------------


SECRET_ENV_MARKERS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "TOKEN",
    "KEYCHAIN",
    "DEPLOY_KEY",
    "SSH",
    "PASSWORD",
    "SECRET",
)


@pytest.fixture
def secretive_host(monkeypatch) -> None:
    """A host environment full of things that must not be inherited by a container."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "host-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "host-client-secret")
    monkeypatch.setenv("NIGHTSHIFT_DEPLOY_KEY", "/Users/someone/.ssh/nightshift_agent")
    monkeypatch.setenv("NIGHTSHIFT_TOKEN_FILE_SEND", "/Users/someone/token.json")
    monkeypatch.setenv("NIGHTSHIFT_APPROVALS_DB", "/Users/someone/approvals.db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")


def test_neither_container_inherits_the_host_environment(secretive_host) -> None:
    """Both container environments are *built*, not inherited — the whole point.

    `docker.containers.run(environment=...)` replaces the environment rather than adding
    to it, so what these two functions return is exactly what the container gets. A
    regression here would be someone passing `os.environ`.
    """
    for env in (
        orchestrator.sandbox_environment(since="2h", llm_env=orchestrator._llm_env()),
        orchestrator.project_environment(project="p", llm_env=orchestrator._llm_env()),
    ):
        leaked = [
            key
            for key in env
            if any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
        ]
        assert not leaked, f"a host secret reached the sandbox environment: {leaked}"
        assert "NIGHTSHIFT_APPROVALS_DB" not in env, "the sandbox has no path to the queue"
        assert "NIGHTSHIFT_TRANSCRIPTS_DB" not in env, "the sandbox gets no host database"


def test_the_llm_key_is_the_one_secret_in_the_sandbox(secretive_host) -> None:
    """The honest finding, pinned so it cannot drift in either direction.

    `OPENROUTER_API_KEY` *is* handed to both containers, because the agents call the model
    from inside them. Two consequences, both accepted deliberately and both documented in
    README's threat model: a compromised agent can spend the key (bounded by the provider
    cap, `[agents.*].max_cost_usd` and one night), and it can exfiltrate to the one host the
    egress proxy allows, which is the LLM endpoint itself.

    What this test defends is the *size* of that exception: exactly one key, and no other
    credential riding along beside it.
    """
    llm_env = orchestrator._llm_env()
    assert llm_env["OPENROUTER_API_KEY"] == "sk-test-not-real"
    assert set(llm_env) <= {"OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL"}

    for env in (
        orchestrator.sandbox_environment(since="2h", llm_env=llm_env),
        orchestrator.project_environment(project="p", llm_env=llm_env),
    ):
        secrets = [key for key in env if "KEY" in key.upper()]
        assert secrets == ["OPENROUTER_API_KEY"], (
            "the sandbox gained a second secret; if that is deliberate, update README's "
            "threat model and this test together"
        )


def test_the_readme_admits_the_llm_key_is_in_the_sandbox() -> None:
    """Documentation as a control. The exception above is only acceptable while it is stated."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY" in readme
    assert "Threat model" in readme


def test_the_summariser_worktree_never_carries_an_ignored_file(tmp_path: Path) -> None:
    """`.env` is gitignored, and the mirror of a dirty tree must respect that.

    The summariser sandbox mounts a worktree of *this* repo, whose root holds `.env`
    (Google client secret, LLM key). `worktree(include_dirty=True)` copies uncommitted work
    in, so the question is whether "uncommitted" quietly includes "ignored". It does not —
    `git ls-files --others --exclude-standard` — and this is the test that says so, against
    a real repo rather than by reading the flag.
    """
    from sandbox.worktree import worktree

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@t.invalid")
    git("config", "user.name", "T")
    (repo / ".gitignore").write_text(".env\nsecrets/\n", encoding="utf-8")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "initial")

    # The dirty state a real checkout is in at 3am: an edit, a new file, and secrets.
    (repo / "app.py").write_text("print('edited')\n", encoding="utf-8")
    (repo / "new.py").write_text("# untracked but not ignored\n", encoding="utf-8")
    (repo / ".env").write_text("OPENROUTER_API_KEY=sk-real\n", encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "token.json").write_text("{}", encoding="utf-8")

    with worktree(repo, include_dirty=True) as path:
        assert (path / "new.py").exists(), "uncommitted work must reach the sandbox"
        assert "edited" in (path / "app.py").read_text(encoding="utf-8")
        assert not (path / ".env").exists(), "an ignored secret reached the sandbox"
        assert not (path / "secrets").exists(), "an ignored directory reached the sandbox"


def test_the_staged_runtime_carries_no_code_that_can_send_or_push(tmp_path: Path) -> None:
    """What the project agent can import is an allowlist, and it excludes every capability.

    `tests/test_project_branches.py` already asserts `.env` is not copied. This is the
    other half: even the *code* that could send mail or push a branch is absent, so a
    compromised agent has nothing to call even if it found a credential.
    """
    staged = orchestrator.stage_runtime(tmp_path / "runtime", None)
    names = {p.name for p in staged.rglob("*") if p.is_file()}

    for capability in ("send_emails.py", "google_auth.py", "emails.py", "gitops.py",
                       "approvals.py", "api.py", "snapshots.py", "transcripts.py",
                       "calendar_tasks.py", "broker_client.py"):
        assert capability not in names, f"{capability} was staged into the sandbox"
    assert "project_step.py" in names and "models.py" in names


def test_the_staged_config_is_the_only_file_that_crosses_and_holds_no_secret(
    tmp_path: Path, monkeypatch
) -> None:
    """Config reaches the sandbox as a file; `config.py` is what keeps secrets out of it."""
    from config import ConfigError, load_config

    bad = tmp_path / "bad.toml"
    bad.write_text('api_key = "sk-oops"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)  # extra="forbid" — a key-shaped field cannot be added by accident

    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    assert orchestrator.stage_config(worktree_path, REPO_ROOT / "config" / "standing_instructions.toml")
    staged = worktree_path / orchestrator.WORKTREE_CONFIG_REL
    text = staged.read_text(encoding="utf-8")
    for marker in ("sk-", "OPENROUTER_API_KEY", "BEGIN OPENSSH", "client_secret"):
        assert marker not in text


def _imports(path: Path) -> set[str]:
    """Every module name `path` imports, top-level names included."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return modules


# --------------------------------------------------------------------------------------
# Rule 2 — summary-as-data (the taint boundary)
# --------------------------------------------------------------------------------------


def test_there_is_no_declassify_anywhere_in_the_runner() -> None:
    """The absence of an escape hatch is the design; a grep is the cheapest way to keep it."""
    for module in (REPO_ROOT / "runner").glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "def declassify" not in source
        assert ".taint = " not in source.replace("self.taint = ", ""), (
            f"{module.name} mutates a taint label after construction"
        )


def test_both_untrusted_sources_stay_in_their_own_agent() -> None:
    """An injected email cannot ask for your calendar, and an injected invite cannot ask
    for your inbox: neither tool is in the other agent's registry, and the one agent with a
    shell accepts no taint at all."""
    from config import StandingInstructions
    from day_plan import calendar_agent_spec
    from runner.taint import TAINT_CALENDAR, TAINT_EMAIL
    from summarise import email_agent_spec

    config = StandingInstructions()
    email = email_agent_spec(config)
    calendar = calendar_agent_spec(config)

    assert "read_calendar" not in email.tools.names
    assert "read_emails" not in calendar.tools.names
    assert email.accepts_taint == frozenset({TAINT_EMAIL})
    assert calendar.accepts_taint == frozenset({TAINT_CALENDAR})

    from runner.agents import project_agent
    from runner.tools_project import WorkSink, WorktreeScope

    project = project_agent(
        config, scope=WorktreeScope(REPO_ROOT), goal="x", sink=WorkSink()
    )
    assert project.accepts_taint == frozenset()
    assert "read_emails" not in project.tools.names
    assert "read_calendar" not in project.tools.names


def test_storage_and_the_briefing_are_not_prompt_sources() -> None:
    """The two places untrusted text comes to rest must not import the prompt type.

    Asserted as a module-graph fact rather than a convention, because "feed last night's
    transcript back in" is a natural-sounding feature request that would be rule 2 with an
    extra hop.
    """
    for name in ("transcripts.py", "briefing.py", "approvals.py"):
        imported = _imports(REPO_ROOT / name)
        # `runner.observe` (the record *shape*) is allowed; the prompt half is not.
        forbidden = {"runner.taint", "runner.agent_runner", "runner.agents", "summarise",
                     "day_plan", "project_step"}
        assert not (imported & forbidden), f"{name} reaches into the agent layer: {imported}"


# --------------------------------------------------------------------------------------
# Rule 3 — every side effect waits for a human
# --------------------------------------------------------------------------------------


def test_send_and_merge_are_reachable_only_through_the_queue() -> None:
    """One caller each, and it is `approve()`.

    A grep, deliberately: the guarantee is about the *whole repo*, so a test that imported
    one module and checked it would be asking the wrong question.
    """
    callers_of_send: list[str] = []
    callers_of_merge: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in {".venv", "tests", "__pycache__", ".worktrees", ".runs"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "send_to_self(" in source or "get_send_credentials(" in source:
            callers_of_send.append(path.name)
        if "merge_agent_branch(" in source:
            callers_of_merge.append(path.name)

    # `send_emails.py` defines them; `approvals.py` is the queue; `main.py`/`nightly.py`
    # send the *briefing to yourself*, which is not an agent-proposed side effect.
    assert set(callers_of_send) <= {
        "send_emails.py",  # defines them
        "approvals.py",  # the queue: the only caller that acts on an agent's proposal
        "main.py",  # emails the briefing to yourself, at your own command
        "run_nightly.py",  # ditto
        "nightly.py",  # ditto, on the schedule you set
    }
    assert set(callers_of_merge) <= {"gitops.py", "approvals.py"}

    # And the broker — the only surface the sandbox can reach — cannot do either. Checked
    # as imports rather than as text, since the module *docstring* names `send_emails.py`
    # precisely to explain why it is absent.
    assert not (_imports(REPO_ROOT / "api.py") & {"send_emails", "approvals", "gitops"})


def test_the_night_never_approves_anything_it_queued() -> None:
    """`run_night` and the project driver enqueue; neither may call `approve`."""
    for name in ("orchestrator/nightly.py", "nightly_project.py", "main.py", "summarise.py"):
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert ".approve(" not in source, f"{name} approves its own proposal"


# --------------------------------------------------------------------------------------
# Deploy-key scoping
# --------------------------------------------------------------------------------------


def test_the_deploy_key_never_leaves_the_host() -> None:
    """Neither container gets the key, a path to it, or the code that would use it."""
    env = orchestrator.project_environment(project="p", llm_env=orchestrator._llm_env())
    assert not [k for k in env if "KEY" in k.upper() and k != "OPENROUTER_API_KEY"]
    assert "GIT_SSH_COMMAND" not in env

    source = Path(orchestrator.__file__).read_text(encoding="utf-8")
    assert "deploy_key" not in source and "GIT_SSH_COMMAND" not in source
    # The project container has no git remote to push to either way.
    volumes = orchestrator.project_volumes("/w", "/rt", "/out")
    assert set(volumes) == {"/w", "/rt", "/out"}
    assert volumes["/rt"]["mode"] == "ro"


def test_the_client_side_refusal_covers_the_ways_around_the_prefix() -> None:
    """`is_agent_ref` checks the whole refname, not just the prefix."""
    import gitops

    assert gitops.is_agent_ref("agent/2026-07-28")
    for hostile in (
        "main",
        "agent/../main",
        "agent/",
        "refs/heads/main",
        "agent/x.lock",
        "../agent/x",
        "",
    ):
        assert not gitops.is_agent_ref(hostile), f"{hostile!r} passed the agent/* check"
        with pytest.raises(gitops.RefusedRef):
            gitops.require_agent_ref(hostile)


def test_the_server_side_hook_is_shipped_and_executable() -> None:
    """The lock that actually holds is a file the user has to install; it must exist here."""
    hook = REPO_ROOT / "hooks" / "pre-receive"
    assert hook.is_file()
    assert os.access(hook, os.X_OK), "pre-receive must be executable when it is copied out"
    text = hook.read_text(encoding="utf-8")
    assert "refs/heads/agent/" in text
    assert "refusing to delete" in text


# --------------------------------------------------------------------------------------
# The egress allowlist
# --------------------------------------------------------------------------------------


def test_the_proxy_denies_by_default_and_allows_one_host() -> None:
    config = (REPO_ROOT / "sandbox" / "proxy" / "tinyproxy.conf").read_text(encoding="utf-8")
    assert "FilterDefaultDeny Yes" in config
    assert "ConnectPort 443" in config, "the sandbox must not tunnel to arbitrary ports"

    entrypoint = (REPO_ROOT / "sandbox" / "proxy" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "ALLOW_HOST" in entrypoint

    # Both containers are pointed at it, and neither is given an exemption beyond loopback.
    for env in (
        orchestrator.sandbox_environment(since="2h"),
        orchestrator.project_environment(project="p"),
    ):
        assert env["HTTPS_PROXY"] == f"http://{orchestrator.PROXY_NAME}:3128"
        assert env["NO_PROXY"] == "localhost,127.0.0.1"


# --------------------------------------------------------------------------------------
# Rule 3, from the UI's side — the surfaces stay separate (Phase 17)
# --------------------------------------------------------------------------------------


def test_the_ui_surface_is_neither_the_broker_nor_reachable_from_the_sandbox() -> None:
    """Phase 17 added a third host surface. Three things must stay true of it.

    The broker is the only surface a container can reach, so the property that matters is
    not "the UI API is careful" but "the UI API is somewhere the sandbox cannot go".
    """
    ui = (REPO_ROOT / "app" / "api.py").read_text(encoding="utf-8")

    # 1. It binds loopback, and nothing configurable can move it off-host.
    assert '"127.0.0.1"' in ui
    assert "0.0.0.0" not in ui

    # 2. It is its own port and its own app — never a route bolted onto the read broker.
    assert "8402" in ui
    broker = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    assert "approve" not in broker

    # 3. The sandbox is never told the token file or the port exists. `sandbox_environment`
    #    and `project_environment` are the complete list of what crosses into a container.
    for env in (
        orchestrator.sandbox_environment(since="2h"),
        orchestrator.project_environment(project="p"),
    ):
        assert not any("UI_TOKEN" in key for key in env)
        assert not any("8402" in str(value) for value in env.values())


def test_the_ui_can_only_reach_an_effect_through_the_queue() -> None:
    """The UI layer proposes nothing and performs nothing of its own.

    `app/api.py` and `app/service.py` may call `ApprovalQueue.approve`; they may not import
    `send_emails` or `gitops`, because a UI that could send directly would make the queue's
    single-executor guarantee a convention again.
    """
    for name in ("app/api.py", "app/service.py", "app/menubar.py"):
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "import send_emails" not in source
        assert "from send_emails" not in source
        assert "import gitops" not in source
        assert "from gitops" not in source


def test_the_swift_client_never_composes_its_own_effect_sentence() -> None:
    """Security rule 3's sentence is written once, host-side, in `app/service.py:preview`.

    A native client that built "Approving SENDS…" itself could quietly build it differently
    — or omit it — so the words must not exist in the Swift source at all: they arrive on
    `ActionPreview.effect` and are rendered verbatim.
    """
    swift_root = REPO_ROOT / "app" / "NightShiftUI" / "Sources"
    sources = list(swift_root.rglob("*.swift"))
    assert sources, "the SwiftUI client should be checked in"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for invented in ("Approving SENDS", "Approving MERGES"):
            assert invented not in text, f"{path.name} composes an effect sentence itself"
