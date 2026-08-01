"""The Unix-socket bridge between the sandbox and the host broker (Phase 4).

Two things are worth pinning down, and both can be checked without Docker:

1. The transport really works over a Unix socket — a `BrokerClient` pointed at a
   socket-bound broker gets the same Pydantic-shaped email back it would over TCP.
2. The orchestrator's *wiring decisions* keep the sandbox on the socket and nothing
   else: no broker URL, no broker on any network, no credential in the container.

The docker-driven half (containers, networks, the SSH hop into the colima VM) is
exercised by `uv run python run_nightly.py --mock --no-send`, not here — these tests
must stay green and fast on a machine with colima stopped.
"""

from __future__ import annotations

import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from api import app
from broker_client import BrokerClient
from fixtures.mock_emails import mock_emails
from sandbox import orchestrator
from sandbox.colima import private_socket_dir


@pytest.fixture
def socket_dir():
    path = private_socket_dir(prefix="nightshift-test-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def broker_socket(socket_dir, monkeypatch):
    """A real broker (mock mode) listening on a Unix socket, in a background thread."""
    monkeypatch.setenv("NIGHTSHIFT_MOCK", "1")
    path = socket_dir / "broker.sock"

    server = uvicorn.Server(uvicorn.Config(app, uds=str(path), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline and not (server.started and path.exists()):
        time.sleep(0.05)
    if not path.exists():
        pytest.fail("broker did not bind its Unix socket in time")

    try:
        yield path
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# --- the transport itself -------------------------------------------------------


def test_socket_is_a_real_unix_socket(broker_socket):
    assert broker_socket.is_socket()


def test_client_fetches_emails_over_the_socket(broker_socket):
    with BrokerClient(socket=str(broker_socket)) as client:
        fetched = client.fetch_emails("8h")

    expected = mock_emails()
    assert [e.id for e in fetched] == [e.id for e in expected]
    assert [e.subject for e in fetched] == [e.subject for e in expected]


def test_socket_client_never_resolves_a_host(broker_socket, monkeypatch):
    """The bridge must not depend on name resolution or an IP route.

    `http://broker` in the socket-mode base URL is only an HTTP authority header —
    if anything ever tried to *resolve* it, the sandbox would need a network route
    to the broker, which is exactly what this phase removed.
    """

    def explode(*args, **kwargs):
        raise AssertionError("socket-mode client must not resolve a hostname")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    with BrokerClient(socket=str(broker_socket)) as client:
        assert client.fetch_emails("8h")


# --- config resolution ----------------------------------------------------------


def test_from_env_prefers_the_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("NIGHTSHIFT_BROKER_SOCKET", str(tmp_path / "broker.sock"))
    monkeypatch.setenv("NIGHTSHIFT_API_URL", "http://broker:8400")

    with BrokerClient.from_env() as client:
        transport = client._client._transport
        assert transport._pool._uds == str(tmp_path / "broker.sock")


def test_from_env_falls_back_to_tcp(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_BROKER_SOCKET", raising=False)
    monkeypatch.setenv("NIGHTSHIFT_API_URL", "http://example.invalid:8400")

    with BrokerClient.from_env() as client:
        assert str(client._client.base_url) == "http://example.invalid:8400"
        assert client._client._transport._pool._uds is None


def test_empty_socket_var_is_not_a_socket(monkeypatch):
    """An empty env var must not be read as "use a socket at path ''"."""
    monkeypatch.setenv("NIGHTSHIFT_BROKER_SOCKET", "")
    monkeypatch.delenv("NIGHTSHIFT_API_URL", raising=False)

    with BrokerClient.from_env() as client:
        assert client._client._transport._pool._uds is None


# --- orchestrator wiring / topology ---------------------------------------------


def test_sandbox_env_routes_the_broker_over_the_socket():
    env = orchestrator.sandbox_environment(since="2h")

    assert env["NIGHTSHIFT_BROKER_SOCKET"] == orchestrator.SANDBOX_SOCKET_PATH
    # No network route to the broker: no URL, and nothing broker-shaped exempted
    # from the proxy.
    assert "NIGHTSHIFT_API_URL" not in env
    assert "broker" not in env["NO_PROXY"]
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_sandbox_env_forces_llm_traffic_through_the_proxy():
    env = orchestrator.sandbox_environment(since="2h", llm_env={"OPENROUTER_API_KEY": "k"})

    assert env["HTTP_PROXY"] == f"http://{orchestrator.PROXY_NAME}:3128"
    assert env["HTTPS_PROXY"] == f"http://{orchestrator.PROXY_NAME}:3128"
    assert env["NIGHTSHIFT_SINCE"] == "2h"


def test_sandbox_env_carries_no_google_credential():
    """Secrets never enter the sandbox: no token, no token file, no OAuth client."""
    env = orchestrator.sandbox_environment(since="2h", llm_env=orchestrator._llm_env())

    forbidden = ("TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "KEYCHAIN")
    assert not [key for key in env if any(word in key.upper() for word in forbidden)]


def test_sandbox_mounts_are_the_worktree_and_a_read_only_socket_dir(tmp_path):
    volumes = orchestrator.sandbox_volumes(tmp_path, "/run/user/501/nightshift-abc")

    assert volumes[str(tmp_path)] == {"bind": orchestrator.WORKSPACE, "mode": "rw"}
    socket_mount = volumes["/run/user/501/nightshift-abc"]
    assert socket_mount == {"bind": orchestrator.SANDBOX_SOCKET_DIR, "mode": "ro"}
    # Exactly two mounts — nothing else from the host is exposed.
    assert len(volumes) == 2
    assert orchestrator.SANDBOX_SOCKET_PATH.startswith(orchestrator.SANDBOX_SOCKET_DIR + "/")


def test_orchestrator_never_exports_a_credential():
    """Regression guard on the Phase-4 migration.

    The old topology ran the broker in a container and had to export the read-only
    token to a mounted file. The broker is a host process now, so that export must
    stay gone — its return would put a Google credential inside a container again.
    """
    source = Path(orchestrator.__file__).read_text(encoding="utf-8")

    assert "NIGHTSHIFT_TOKEN_FILE" not in source
    assert "_export_read_token" not in source
    assert not hasattr(orchestrator, "_export_read_token")
    # ...and no broker container to put it in.
    assert not hasattr(orchestrator, "BROKER_NAME")
