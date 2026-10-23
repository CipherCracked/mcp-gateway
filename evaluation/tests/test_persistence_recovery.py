"""Persistence and restart recovery tests (REQ PERS-001, PERS-002).

Terminal states survive gateway restart; non-terminal states become
EXECUTION_INTERRUPTED. Authorization still applies after restart.
"""

import subprocess
import time

import httpx

from evaluation.tests.conftest import (
    API_KEY_ADMIN, GATEWAY_URL, delay_documents,
    wait_for_state,
)


CONTAINER_NAME = "secure-mcp-gateway"
DOCKER_EXE = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def _run_docker(*args, **kwargs):
    """Run docker command via PowerShell to ensure Docker Desktop context."""
    cmd = ["powershell", "-Command", f"& '{DOCKER_EXE}' {' '.join(args)}"]
    return subprocess.run(cmd, **kwargs)


def _restart_gateway_container():
    """Restart the gateway Docker container."""
    # Restart the container
    result = _run_docker("restart", CONTAINER_NAME, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to restart container: {result.stderr}")

    # Wait for gateway to be ready
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{GATEWAY_URL}/tools/list",
                headers={"x-api-key": API_KEY_ADMIN},
                timeout=1.0,
            )
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            pass
        time.sleep(0.5)
    raise RuntimeError("gateway did not become ready after container restart")


class TestPersistenceRecovery:
    def test_terminal_states_survive_restart_non_terminal_interrupted(
        self, client, auth_headers_admin
    ):
        # E1: delayed so it is non-terminal (pending/running) at restart time.
        delay_documents(6.0)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "e1"}},
        )
        e1 = r.json()["execution_id"]

        # E2: let it complete (terminal).
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.get_document", "arguments": {"id": "doc-2"}},
        )
        e2 = r.json()["execution_id"]
        wait_for_state(client, e2, auth_headers_admin, {"completed"})

        # Give E1 a moment to be running.
        time.sleep(0.5)

        # Restart the gateway by restarting the Docker container.
        _restart_gateway_container()

        # E2 (terminal) recovered faithfully.
        record = wait_for_state(
            httpx.Client(base_url=GATEWAY_URL, timeout=10.0),
            e2, auth_headers_admin, {"completed"},
        )
        assert record["state"] == "completed"

        # E1 (non-terminal) marked failed with EXECUTION_INTERRUPTED.
        with httpx.Client(base_url=GATEWAY_URL, timeout=10.0) as c2:
            record = wait_for_state(
                c2, e1, auth_headers_admin, {"failed"}, timeout=10.0
            )
            assert record["state"] == "failed"
            assert record["error"]["code"] == "EXECUTION_INTERRUPTED"