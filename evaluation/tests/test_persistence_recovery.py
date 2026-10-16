"""Persistence and restart recovery tests (REQ PERS-001, PERS-002).

Terminal states survive gateway restart; non-terminal states become
EXECUTION_INTERRUPTED. Authorization still applies after restart.
"""

import os
import signal
import subprocess
import time

import httpx

from tests.conftest import (
    API_KEY_ADMIN, GATEWAY_URL, delay_documents,
    wait_for_state,
)


def _start_gateway():
    env = dict(os.environ)
    env["PYTHONPATH"] = "/app"
    return subprocess.Popen(
        ["uvicorn", "mcp_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd="/app",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _wait_ready(proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("gateway died during restart")
        try:
            r = httpx.get(
                f"{GATEWAY_URL}/tools/list",
                headers={"x-api-key": API_KEY_ADMIN},
                timeout=1.0,
            )
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.25)
    raise RuntimeError("gateway did not become ready after restart")


class TestPersistenceRecovery:
    def test_terminal_states_survive_restart_non_terminal_interrupted(
        self, client, auth_headers_admin, gateway_process
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

        # Restart the gateway.
        gateway_process.terminate()
        try:
            gateway_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway_process.kill()
        new_proc = _start_gateway()
        try:
            _wait_ready(new_proc)

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
        finally:
            new_proc.terminate()
            try:
                new_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                new_proc.kill()
