"""Colima VM lifecycle helpers.

The orchestrator manages colima itself: it makes sure the VM is up and hands
back the Docker socket to talk to, rather than relying on the global
`docker context`.

It also owns the plumbing for the **host↔sandbox Unix-socket bridge**
(`forwarded_socket`). Containers live inside the colima VM, so a socket the host
broker binds is two hops away from them, and neither hop is free:

1. Bind-mounting the host socket straight into a container does *not* work.
   colima shares `$HOME` with the VM over virtiofs, which passes the socket
   *file* through but not the socket itself — connecting from the VM fails with
   ``connect(): Not supported``. Only a socket living on a VM-native filesystem
   can be connected to from a container.
2. So the host socket is reverse-forwarded (``ssh -R``) into the VM, where sshd
   binds a real, VM-native socket that Docker can bind-mount. Traffic tunnels
   over colima's existing SSH channel; no TCP port is opened on the host, and
   nothing but the sandbox's own mount can reach it.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Default colima profile socket. `colima start` (no --profile) creates this.
SOCKET_PATH = Path.home() / ".colima" / "default" / "docker.sock"


class ColimaError(RuntimeError):
    """Raised when colima is missing or cannot be brought up."""


def _require_colima() -> str:
    colima = shutil.which("colima")
    if colima is None:
        raise ColimaError(
            "colima is not installed. Install it with:\n\n    brew install colima docker\n"
        )
    return colima


def _is_running(colima: str) -> bool:
    # `colima status` exits 0 and prints to stderr when running, non-zero otherwise.
    result = subprocess.run(
        [colima, "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_colima_running() -> None:
    """Make sure the colima VM is up, starting it if needed."""
    colima = _require_colima()

    if _is_running(colima):
        print("colima is already running.")
    else:
        print("Starting colima (first boot can take a minute)...")
        # Stream output so the user sees VM provisioning progress.
        subprocess.run([colima, "start"], check=True)

    if not SOCKET_PATH.exists():
        raise ColimaError(
            f"colima reported running but its Docker socket was not found at {SOCKET_PATH}. "
            "Check `colima status`."
        )


def docker_base_url() -> str:
    """Return the docker-py base_url for the colima socket."""
    return f"unix://{SOCKET_PATH}"


def _ssh_config(tmp_dir: Path) -> Path:
    """Write colima's SSH config to `tmp_dir` and return the path.

    `colima ssh-config` prints a ready-made `Host colima` stanza (identity file,
    port, ControlMaster path). Regenerating it per run means we never guess at the
    VM's SSH port, which changes across restarts.
    """
    colima = _require_colima()
    result = subprocess.run([colima, "ssh-config"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ColimaError(f"`colima ssh-config` failed: {result.stderr.strip()}")

    path = tmp_dir / "colima_ssh_config"
    path.write_text(result.stdout, encoding="utf-8")
    path.chmod(0o600)
    return path


def _ssh(config: Path, command: str) -> subprocess.CompletedProcess[str]:
    """Run `command` in the colima VM over SSH (host name comes from the config)."""
    return subprocess.run(
        ["ssh", "-F", str(config), "colima", command],
        capture_output=True,
        text=True,
        check=False,
    )


@contextmanager
def forwarded_socket(host_socket: Path, *, tmp_dir: Path, timeout: float = 30.0) -> Iterator[str]:
    """Reverse-forward `host_socket` into the VM; yield the VM-side *directory*.

    The directory (not the socket) is what gets bind-mounted into the sandbox, so
    it holds exactly one entry: the forwarded broker socket, under the same
    basename it has on the host. It is created 0700 under the VM user's private
    `/run/user/<uid>` (tmpfs, wiped on VM restart) and removed on the way out,
    success or failure.
    """
    config = _ssh_config(tmp_dir)
    name = host_socket.name

    probe = _ssh(config, "id -u")
    if probe.returncode != 0:
        raise ColimaError(f"Could not SSH into the colima VM: {probe.stderr.strip()}")
    uid = probe.stdout.strip()

    vm_dir = f"/run/user/{uid}/nightshift-{secrets.token_hex(4)}"
    vm_socket = f"{vm_dir}/{name}"

    created = _ssh(config, f"mkdir -p {vm_dir} && chmod 700 {vm_dir}")
    if created.returncode != 0:
        raise ColimaError(f"Could not create {vm_dir} in the VM: {created.stderr.strip()}")

    # -N: no remote command, the tunnel is the whole point. StreamLocalBindUnlink
    # clears a stale socket rather than refusing to bind.
    tunnel = subprocess.Popen(
        [
            "ssh",
            "-F",
            str(config),
            "-o",
            "StreamLocalBindUnlink=yes",
            "-N",
            "-R",
            f"{vm_socket}:{host_socket}",
            "colima",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if tunnel.poll() is not None:
                stderr = (tunnel.stderr.read() or b"").decode(errors="replace")
                raise ColimaError(f"SSH socket forward exited early: {stderr.strip()}")
            if _ssh(config, f"test -S {vm_socket}").returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise ColimaError(f"Socket {vm_socket} never appeared in the VM.")

        print(f"Bridged host socket {host_socket} -> VM {vm_socket}.")
        yield vm_dir
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()
        # The tunnel dying does not always unlink the remote socket; make sure the
        # whole directory goes with it so nothing outlives the run.
        _ssh(config, f"rm -rf {vm_dir}")


def private_socket_dir(prefix: str = "nightshift-") -> Path:
    """Create a 0700 host directory for a Unix socket and return it.

    AF_UNIX paths are capped near 104 bytes on macOS, so this stays in the short
    per-user temp dir rather than anywhere nested.
    """
    import tempfile

    path = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(path, 0o700)
    return path
