"""Sandbox orchestrator.

Three entry points:

- ``run_task`` — run an arbitrary shell command in a disposable container against a
  mounted git worktree (ad-hoc use, unchanged).
- ``run_project_step`` (Phase 9) — the project agent, against *another* repo's worktree.
  Same image and same egress proxy, but **no broker socket**: the project agent has no
  broker tools, so it gets no route to the process holding the Gmail credential, and no
  route to a git remote either (the push happens host-side under a restricted deploy key).
  NightShift's own modules cannot arrive via ``/workspace`` here — that mount is someone
  else's repo — so ``stage_runtime`` copies a named allowlist of files into a directory
  mounted read-only at ``/opt/nightshift``. Never the repo root: it contains ``.env``.
- ``run_summariser_step`` — the "mini nightly run": start the broker **on the host**
  behind a Unix socket, bridge that socket into the sandbox, stand up an allowlisting
  egress proxy, then run step 2 (summarise) inside a locked-down sandbox container. The
  sandbox writes the briefing into the mounted worktree, which the host reads back.

Topology::

    host                              colima VM
    ────────────────────────────      ─────────────────────────────────────────────
    [broker] --unix socket-- ssh -R --> /run/user/<uid>/nightshift-*/broker.sock
      │  (Keychain, Gmail)                            │ bind-mounted ro
      │                                    [sandbox] ─┘  (network: internal only)
      │                                    [sandbox] --HTTPS_PROXY--> [egress-proxy]
      ▼                                                                    │
    Gmail                                                                  ▼
    nightshift-egress (normal bridge, internet):  [egress-proxy]         LLM
    nightshift-internal (internal=True, no internet):  [sandbox], [egress-proxy]

The broker is no longer a container and is **not on any Docker network** — the sandbox
has no network route to it at all. Its only way to ask for email is the mounted socket,
which means the Google credential never leaves the host Keychain (previously the
read-only token had to be exported to a file for the broker container; that export is
gone). The internal network now carries exactly one thing: the sandbox's hop to the
egress proxy, which allowlists the LLM host and nothing else.

See ``sandbox/colima.py:forwarded_socket`` for why the socket needs an SSH hop rather
than a plain bind-mount of the host path.

Standing instructions (Phase 5) ride in as a plain file: whichever config the host
resolved is copied into the worktree at ``.nightshift/config.toml`` and pointed to by
``NIGHTSHIFT_CONFIG``. That is safe precisely because config is host-authored, trusted,
secret-free data — the opposite of email — so it needs no broker tool and opens no new
channel out of the sandbox.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import docker
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from config import load_config, resolve_config_path
from runner.observe import NIGHT_ID_ENV_VAR
from sandbox.colima import (
    docker_base_url,
    ensure_colima_running,
    forwarded_socket,
    private_socket_dir,
)
from sandbox.worktree import worktree

SANDBOX_DIR = Path(__file__).resolve().parent
REPO_ROOT = SANDBOX_DIR.parent
DEFAULT_IMAGE = "nightshift-sandbox:latest"
PROXY_IMAGE = "nightshift-egress-proxy:latest"
WORKSPACE = "/workspace"

INTERNAL_NET = "nightshift-internal"
EGRESS_NET = "nightshift-egress"

PROXY_NAME = "egress-proxy"
SANDBOX_NAME = "nightshift-sandbox"

# The bridge: a host-bound Unix socket, mounted into the sandbox at a fixed path.
BROKER_SOCKET_NAME = "broker.sock"
SANDBOX_SOCKET_DIR = "/run/nightshift"
SANDBOX_SOCKET_PATH = f"{SANDBOX_SOCKET_DIR}/{BROKER_SOCKET_NAME}"

# Where the host's standing instructions land inside the mounted worktree.
WORKTREE_CONFIG_REL = Path(".nightshift") / "config.toml"
SANDBOX_CONFIG_PATH = f"{WORKSPACE}/{WORKTREE_CONFIG_REL.as_posix()}"

# Phase 9: the project step mounts *someone else's* repo at /workspace, so NightShift's own
# modules cannot come in that way. They are staged into a private directory and mounted
# read-only here, alongside a writable directory for the structured work report.
PROJECT_NAME_CONTAINER = "nightshift-project"
RUNTIME_MOUNT = "/opt/nightshift"
REPORT_MOUNT = "/run/nightshift-out"
REPORT_FILE = "project_work.json"
SANDBOX_REPORT_PATH = f"{REPORT_MOUNT}/{REPORT_FILE}"

# Phase 12: the agent's transcript comes home the same way its work report does — as a
# file in the drop directory, read back by the host after the container exits. The sandbox
# gets no route to the host's transcript database; a writable channel to host state is
# exactly what this container is not allowed to have.
TRANSCRIPT_FILE = "agent_runs.jsonl"
SANDBOX_TRANSCRIPT_PATH = f"{REPORT_MOUNT}/{TRANSCRIPT_FILE}"

# Exactly what the project step needs, named one by one. An allowlist and not a directory
# copy: the repo root also holds `.env` (Google client secret, LLM key), and "copy the repo
# minus the secrets" is a rule that rots the moment someone adds a second secret file.
RUNTIME_FILES = ("config.py", "models.py")
RUNTIME_ENTRYPOINT = "project_step.py"
RUNTIME_PACKAGES = ("runner",)

# Staging dirs live inside the repo (and therefore under $HOME) because colima only shares
# $HOME with the VM — a mkdtemp under /var/folders cannot be bind-mounted into a container.
RUNS_DIR = SANDBOX_DIR / ".runs"

# Host derived from OPENROUTER_BASE_URL; the proxy allowlists exactly this.
DEFAULT_LLM_HOST = "ai.hackclub.com"

load_dotenv(REPO_ROOT / ".env")


def ensure_image(
    client: docker.DockerClient,
    tag: str,
    context: Path,
    dockerfile: str,
) -> None:
    """Build ``tag`` from ``context``/``dockerfile`` if it isn't present already."""
    try:
        client.images.get(tag)
        print(f"Image {tag} already present.")
        return
    except docker.errors.ImageNotFound:
        pass

    print(f"Building image {tag} from {context}/{dockerfile}...")
    _, logs = client.images.build(path=str(context), dockerfile=dockerfile, tag=tag, rm=True)
    for chunk in logs:
        line = chunk.get("stream", "")
        if line.strip():
            print(line, end="")


def _get_or_create_network(client: docker.DockerClient, name: str, *, internal: bool):
    try:
        return client.networks.get(name)
    except docker.errors.NotFound:
        print(f"Creating network {name} (internal={internal}).")
        return client.networks.create(name, driver="bridge", internal=internal)


def _remove_if_exists(client: docker.DockerClient, name: str) -> None:
    try:
        existing = client.containers.get(name)
    except docker.errors.NotFound:
        return
    print(f"Removing stale container {name}.")
    existing.remove(force=True)


def _llm_host() -> str:
    base = os.getenv("OPENROUTER_BASE_URL", f"https://{DEFAULT_LLM_HOST}")
    # Strip scheme + path to leave the bare host.
    host = base.split("://", 1)[-1].split("/", 1)[0]
    return host or DEFAULT_LLM_HOST


def _start_host_broker(socket_path: Path) -> subprocess.Popen:
    """Start `api.py` on the host, listening on `socket_path`.

    The broker stays on the host precisely so it can reach the Keychain: it holds the
    read-only Google credential and makes the real Gmail calls, and the sandbox never
    sees anything but the socket. `NIGHTSHIFT_MOCK` is inherited from the caller, so a
    mock run needs no Gmail credential at all.
    """
    env = {**os.environ, "NIGHTSHIFT_BROKER_SOCKET": str(socket_path)}
    # Drop any TCP bind config so the host broker cannot also come up on a port.
    env.pop("NIGHTSHIFT_API_HOST", None)
    env.pop("NIGHTSHIFT_API_PORT", None)
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "api.py")],
        cwd=str(REPO_ROOT),
        env=env,
    )


def _wait_for_broker(process: subprocess.Popen, socket_path: Path, timeout: float = 60.0) -> None:
    """Poll /health over the Unix socket until the host broker answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Broker exited before becoming ready (code {process.returncode}).")
        if socket_path.exists():
            try:
                transport = httpx.HTTPTransport(uds=str(socket_path))
                with httpx.Client(transport=transport, base_url="http://broker") as client:
                    if client.get("/health", timeout=5.0).status_code == 200:
                        # 0600: only the owner (and container root through the mount)
                        # can speak to the broker. Set after bind — uvicorn creates it.
                        socket_path.chmod(0o600)
                        print("Broker is ready on the socket.")
                        return
            except httpx.HTTPError:
                pass
        time.sleep(0.5)
    raise RuntimeError("Broker did not become ready in time.")


def _stop_host_broker(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def sandbox_environment(
    *,
    since: str,
    llm_env: dict[str, str] | None = None,
    config_in_worktree: bool = False,
) -> dict[str, str]:
    """Environment for the sandbox container.

    Deliberately contains **no** `NIGHTSHIFT_API_URL`: the broker is not reachable over
    any network, so the only configured route is the mounted socket. Everything else
    goes through the egress proxy — `NO_PROXY` covers loopback only, since there is no
    in-network peer to exempt any more.

    `NIGHTSHIFT_CONFIG` is set only when the host actually placed a config file in the
    worktree; otherwise the sandbox falls back to the repo's committed default (or, if
    that is missing too, to built-in defaults).
    """
    return {
        "NIGHTSHIFT_BROKER_SOCKET": SANDBOX_SOCKET_PATH,
        "NIGHTSHIFT_SINCE": since,
        # The summariser's transcript lands in the worktree beside the briefing it wrote,
        # and the host imports it after the container exits — same route as the briefing,
        # no new channel.
        "NIGHTSHIFT_TRANSCRIPT_JSONL": f"{WORKSPACE}/out/{TRANSCRIPT_FILE}",
        **(
            {"NIGHTSHIFT_NIGHT_ID": os.environ[NIGHT_ID_ENV_VAR]}
            if os.getenv(NIGHT_ID_ENV_VAR)
            else {}
        ),
        **({"NIGHTSHIFT_CONFIG": SANDBOX_CONFIG_PATH} if config_in_worktree else {}),
        "HTTP_PROXY": f"http://{PROXY_NAME}:3128",
        "HTTPS_PROXY": f"http://{PROXY_NAME}:3128",
        "NO_PROXY": "localhost,127.0.0.1",
        "PYTHONPATH": WORKSPACE,
        **(llm_env or {}),
    }


def sandbox_volumes(worktree_path: Path | str, vm_socket_dir: str) -> dict[str, dict[str, str]]:
    """Mounts for the sandbox: the worktree (rw) and the broker socket dir (ro).

    Read-only on the socket directory means the container can connect to the socket but
    cannot replace it, delete it, or drop anything else beside it.
    """
    return {
        str(worktree_path): {"bind": WORKSPACE, "mode": "rw"},
        vm_socket_dir: {"bind": SANDBOX_SOCKET_DIR, "mode": "ro"},
    }


def stage_config(worktree_path: Path, config_path: Path | str | None) -> bool:
    """Copy the host's resolved standing instructions into the worktree.

    Returns True if a file was staged. Validating first means a broken config fails on
    the host, where the error is visible, instead of inside a container at 3am. Only this
    one file is copied — no secrets, no `.env`.
    """
    source = resolve_config_path(config_path)
    if not source.exists():
        return False

    load_config(source)  # raises ConfigError here, on the host, if it is malformed
    destination = worktree_path / WORKTREE_CONFIG_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    print(f"Staged standing instructions {source} -> {SANDBOX_CONFIG_PATH}.")
    return True


def stage_runtime(destination: Path, config_path: Path | str | None) -> Path:
    """Copy the NightShift modules the project step needs into `destination`.

    Returns the directory to mount read-only at `RUNTIME_MOUNT`. Only `RUNTIME_FILES`,
    `RUNTIME_ENTRYPOINT` and `RUNTIME_PACKAGES` are copied — never the repo root, which
    holds `.env`. `__pycache__` is skipped so a host-compiled artefact never rides in.

    The resolved standing instructions land at `config/standing_instructions.toml` inside
    the staged tree, which is where `config.py`'s default lookup finds them. Config is
    host-authored, trusted, secret-free data, so shipping it as a file needs no new channel.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for name in (*RUNTIME_FILES, RUNTIME_ENTRYPOINT):
        shutil.copy2(REPO_ROOT / name, destination / name)
    for package in RUNTIME_PACKAGES:
        shutil.copytree(
            REPO_ROOT / package,
            destination / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )

    source = resolve_config_path(config_path)
    if source.exists():
        load_config(source)  # fail on the host, where the error is visible
        target = destination / "config" / "standing_instructions.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"Staged standing instructions {source} -> {RUNTIME_MOUNT}/config/.")
    return destination


def project_environment(*, project: str, llm_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the project sandbox.

    No `NIGHTSHIFT_BROKER_SOCKET` and no `NIGHTSHIFT_API_URL`: the project agent has no
    broker tools, so it gets no bridge to the process holding the Gmail credential. The
    egress proxy remains its only route off the internal network, and the only host it
    allowlists is the LLM — in particular there is no route to a git remote, which is why
    the push happens on the host under the restricted deploy key.
    """
    return {
        "NIGHTSHIFT_PROJECT": project,
        "NIGHTSHIFT_WORKSPACE": WORKSPACE,
        "NIGHTSHIFT_WORK_REPORT": SANDBOX_REPORT_PATH,
        "NIGHTSHIFT_TRANSCRIPT_JSONL": SANDBOX_TRANSCRIPT_PATH,
        **(
            {"NIGHTSHIFT_NIGHT_ID": os.environ[NIGHT_ID_ENV_VAR]}
            if os.getenv(NIGHT_ID_ENV_VAR)
            else {}
        ),
        "HTTP_PROXY": f"http://{PROXY_NAME}:3128",
        "HTTPS_PROXY": f"http://{PROXY_NAME}:3128",
        "NO_PROXY": "localhost,127.0.0.1",
        "PYTHONPATH": RUNTIME_MOUNT,
        "HOME": "/tmp",
        **(llm_env or {}),
    }


def project_volumes(
    worktree_path: Path | str, runtime_dir: Path | str, report_dir: Path | str
) -> dict[str, dict[str, str]]:
    """Mounts for the project sandbox: work (rw), our code (ro), the report drop (rw)."""
    return {
        str(worktree_path): {"bind": WORKSPACE, "mode": "rw"},
        str(runtime_dir): {"bind": RUNTIME_MOUNT, "mode": "ro"},
        str(report_dir): {"bind": REPORT_MOUNT, "mode": "rw"},
    }


class ProjectStepOutput(BaseModel):
    """What a project container left behind, as text, for the host to validate.

    Both fields crossed a trust boundary as files written by an untrusted container, so
    neither is parsed here: `nightly_project.py` validates the report into an
    `AgentWorkReport` and `transcripts.py` re-validates every transcript line.
    """

    model_config = ConfigDict(extra="forbid")

    report: str = Field(default="", description="project_work.json, verbatim")
    transcript_jsonl: str = Field(default="", description="agent_runs.jsonl, verbatim")


def run_project_step(
    *,
    worktree_path: Path,
    project: str,
    config_path: Path | str | None = None,
    image: str = DEFAULT_IMAGE,
    proxy_image: str = PROXY_IMAGE,
    keep: bool = False,
) -> ProjectStepOutput:
    """Run the project agent in the sandbox against an already-created worktree.

    The caller (`nightly_project.py`) owns the git side entirely: it creates tonight's
    `agent/<date>` branch, hands the worktree here, and does the commit sweep, diff and
    push afterwards. This function's whole job is "run that agent, in a box, with nothing
    it shouldn't have", and it returns what the container wrote: the JSON work report and
    the agent's transcript, both as text for the host to validate.
    """
    ensure_colima_running()
    client = docker.DockerClient(base_url=docker_base_url())

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="project-", dir=RUNS_DIR))
    runtime_dir = run_dir / "runtime"
    report_dir = run_dir / "out"
    report_dir.mkdir(parents=True, exist_ok=True)

    proxy = None
    try:
        ensure_image(client, image, REPO_ROOT, "sandbox/Dockerfile")
        ensure_image(client, proxy_image, SANDBOX_DIR / "proxy", "Dockerfile")

        internal_net = _get_or_create_network(client, INTERNAL_NET, internal=True)
        _get_or_create_network(client, EGRESS_NET, internal=False)

        allow_host = _llm_host()
        _remove_if_exists(client, PROXY_NAME)
        proxy = client.containers.run(
            proxy_image,
            name=PROXY_NAME,
            environment={"ALLOW_HOST": allow_host},
            network=EGRESS_NET,
            detach=True,
        )
        internal_net.connect(proxy, aliases=[PROXY_NAME])
        print(f"Egress proxy up; allowlisting {allow_host}.")

        stage_runtime(runtime_dir, config_path)
        _remove_if_exists(client, PROJECT_NAME_CONTAINER)
        print(f"Mounting project worktree {worktree_path} -> {WORKSPACE}; running agent.")
        sandbox = client.containers.run(
            image,
            command=["/opt/venv/bin/python", f"{RUNTIME_MOUNT}/{RUNTIME_ENTRYPOINT}"],
            name=PROJECT_NAME_CONTAINER,
            environment=project_environment(project=project, llm_env=_llm_env()),
            volumes=project_volumes(worktree_path, runtime_dir, report_dir),
            working_dir=WORKSPACE,
            network=INTERNAL_NET,
            detach=True,
        )
        try:
            print(f"Sandbox {sandbox.short_id} started; streaming output:\n")
            for chunk in sandbox.logs(stream=True, follow=True):
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
            exit_code = int(sandbox.wait().get("StatusCode", 1))
            print(f"\nProject sandbox exited with code {exit_code}.")
            if exit_code != 0:
                raise RuntimeError(f"Project step failed (exit {exit_code}).")
            report = report_dir / REPORT_FILE
            if not report.exists():
                raise RuntimeError("Project step produced no work report.")
            transcript = report_dir / TRANSCRIPT_FILE
            return ProjectStepOutput(
                report=report.read_text(encoding="utf-8"),
                # A missing transcript is not fatal: the night's *work* is the artifact
                # that matters, and losing the observability of a run that otherwise
                # succeeded must not throw the run away.
                transcript_jsonl=(
                    transcript.read_text(encoding="utf-8") if transcript.exists() else ""
                ),
            )
        finally:
            if not keep:
                sandbox.remove(force=True)
    finally:
        # Staged code and the report drop both go: the report has been read into memory by
        # now, and nothing about a run should outlive it on disk.
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)
        if not keep and proxy is not None:
            proxy.remove(force=True)
        client.close()


def _llm_env() -> dict[str, str]:
    """OpenRouter/LLM settings passed through to the sandbox (the host holds the key)."""
    env: dict[str, str] = {}
    for key in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL"):
        value = os.getenv(key)
        if value:
            env[key] = value
    return env


def _import_sandbox_transcripts(path: Path) -> None:
    """Pull the summariser container's transcript into the host store (Phase 12).

    Best-effort: the worktree is about to be destroyed, so this is the only moment the
    file exists — but a night whose briefing is ready must not fail because a database
    would not open.
    """
    if not path.exists():
        return
    try:
        from transcripts import TranscriptStore

        imported = TranscriptStore().import_jsonl(path)
        print(f"Imported {len(imported)} sandbox agent transcript(s).")
    except Exception as exc:  # noqa: BLE001 - observability never fails the run
        print(f"Could not import the sandbox transcripts: {exc!r}")


def run_summariser_step(
    *,
    since: str = "2h",
    image: str = DEFAULT_IMAGE,
    proxy_image: str = PROXY_IMAGE,
    keep: bool = False,
    config_path: Path | str | None = None,
) -> str:
    """Run step 2 in the sandbox and return the briefing HTML it wrote.

    Returns an empty string if there were no emails to summarise.
    """
    ensure_colima_running()
    client = docker.DockerClient(base_url=docker_base_url())

    # The socket lives in a 0700 dir of its own so nothing else on the host can reach
    # the broker, and the whole dir is shredded in the `finally` below.
    socket_dir = private_socket_dir()
    socket_path = socket_dir / BROKER_SOCKET_NAME

    proxy = None
    broker_process = None
    try:
        ensure_image(client, image, REPO_ROOT, "sandbox/Dockerfile")
        ensure_image(client, proxy_image, SANDBOX_DIR / "proxy", "Dockerfile")

        internal_net = _get_or_create_network(client, INTERNAL_NET, internal=True)
        _get_or_create_network(client, EGRESS_NET, internal=False)

        # Egress proxy: on egress (internet to the LLM) + internal (reachable by sandbox).
        # It is the *only* peer on the internal network besides the sandbox.
        allow_host = _llm_host()
        _remove_if_exists(client, PROXY_NAME)
        proxy = client.containers.run(
            proxy_image,
            name=PROXY_NAME,
            environment={"ALLOW_HOST": allow_host},
            network=EGRESS_NET,
            detach=True,
        )
        internal_net.connect(proxy, aliases=[PROXY_NAME])
        print(f"Egress proxy up; allowlisting {allow_host}.")

        # Broker: a host process behind a Unix socket. No container, no network, no
        # exported credential — it reads the Keychain directly and answers over the
        # socket that gets bridged into the sandbox below.
        broker_process = _start_host_broker(socket_path)
        _wait_for_broker(broker_process, socket_path)

        with (
            forwarded_socket(socket_path, tmp_dir=socket_dir) as vm_socket_dir,
            worktree(REPO_ROOT, include_dirty=True) as path,
        ):
            _remove_if_exists(client, SANDBOX_NAME)
            staged = stage_config(path, config_path)
            print(f"Mounting worktree {path} -> {WORKSPACE}; running summariser step.")
            sandbox = client.containers.run(
                image,
                command=["/opt/venv/bin/python", "-m", "summarise_step"],
                name=SANDBOX_NAME,
                environment=sandbox_environment(
                    since=since, llm_env=_llm_env(), config_in_worktree=staged
                ),
                volumes=sandbox_volumes(path, vm_socket_dir),
                working_dir=WORKSPACE,
                network=INTERNAL_NET,
                detach=True,
            )
            try:
                print(f"Sandbox {sandbox.short_id} started; streaming output:\n")
                for chunk in sandbox.logs(stream=True, follow=True):
                    sys.stdout.write(chunk.decode(errors="replace"))
                    sys.stdout.flush()

                exit_code = int(sandbox.wait().get("StatusCode", 1))
                print(f"\nSandbox exited with code {exit_code}.")
                if exit_code != 0:
                    raise RuntimeError(f"Summariser step failed (exit {exit_code}).")

                _import_sandbox_transcripts(path / "out" / TRANSCRIPT_FILE)
                briefing = path / "out" / "briefing.html"
                if briefing.exists():
                    return briefing.read_text(encoding="utf-8")
                print("No briefing produced (no emails in window).")
                return ""
            finally:
                if not keep:
                    sandbox.remove(force=True)
    finally:
        if broker_process is not None:
            _stop_host_broker(broker_process)
        # Socket, ssh config and the private dir all go together; the bridge must not
        # outlive the run even if it failed half-way through.
        shutil.rmtree(socket_dir, ignore_errors=True)
        if not keep and proxy is not None:
            proxy.remove(force=True)
        client.close()


def run_task(
    command: str,
    *,
    branch: str | None = None,
    image: str = DEFAULT_IMAGE,
    keep: bool = False,
) -> int:
    """Run ``command`` in a sandbox container against a mounted worktree.

    Returns the container's exit code.
    """
    ensure_colima_running()
    client = docker.DockerClient(base_url=docker_base_url())

    try:
        ensure_image(client, image, REPO_ROOT, "sandbox/Dockerfile")

        with worktree(REPO_ROOT, branch=branch) as path:
            print(f"Mounting worktree {path} -> {WORKSPACE}")
            container = client.containers.run(
                image,
                command=["bash", "-lc", command],
                volumes={str(path): {"bind": WORKSPACE, "mode": "rw"}},
                working_dir=WORKSPACE,
                detach=True,
            )
            try:
                print(f"Container {container.short_id} started; streaming output:\n")
                for chunk in container.logs(stream=True, follow=True):
                    sys.stdout.write(chunk.decode(errors="replace"))
                    sys.stdout.flush()

                result = container.wait()
                exit_code = int(result.get("StatusCode", 1))
                print(f"\nContainer exited with code {exit_code}.")
                return exit_code
            finally:
                if keep:
                    print(f"Keeping container {container.short_id} (--keep).")
                else:
                    container.remove(force=True)
                    print(f"Removed container {container.short_id}.")
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sandbox.orchestrator",
        description="Run a sandbox task in a disposable colima container.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="Shell command to run in the sandbox. Omit to run the summariser step.",
    )
    parser.add_argument("--branch", default=None, help="Ref to check out in the worktree.")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="App image tag.")
    parser.add_argument("--since", default="2h", help="Email lookback for the summariser.")
    parser.add_argument("--keep", action="store_true", help="Keep containers after exit.")
    parser.add_argument(
        "--config", default=None, help="Standing-instructions TOML for the summariser."
    )
    args = parser.parse_args()

    if args.command:
        sys.exit(run_task(args.command, branch=args.branch, image=args.image, keep=args.keep))

    html = run_summariser_step(
        since=args.since, image=args.image, keep=args.keep, config_path=args.config
    )
    print(f"\nBriefing HTML: {len(html)} bytes.")


if __name__ == "__main__":
    main()
