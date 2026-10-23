"""Revocation via SIGHUP config reload tests (REQ REV-001 through REV-003).

SIGHUP reloads /app/config/gateway.yaml without restarting. After revoking a
tool from a client's allowlist, new invocations get 403 and existing results
become inaccessible. Other clients are unaffected.

The gateway runs in Docker on Linux, so SIGHUP is available via docker exec.
"""

import time
import subprocess

import pytest

from evaluation.tests.conftest import (
    API_KEY_ADMIN, API_KEY_A, API_KEY_B,
    wait_for_state,
)

CONFIG_PATH = "/app/config/gateway.yaml"
CONTAINER_NAME = "secure-mcp-gateway"
DOCKER_EXE = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def _run_docker(*args, **kwargs):
    """Run docker command via PowerShell to ensure Docker Desktop context."""
    cmd = ["powershell", "-Command", f"& '{DOCKER_EXE}' {' '.join(args)}"]
    return subprocess.run(cmd, **kwargs)


def _send_sighup():
    """Send SIGHUP to the gateway process inside the Docker container.

    The uvicorn process runs as PID 1 in the container. The container doesn't
    have the 'kill' or 'pkill' commands, so we use Python to send the signal.
    We write a Python script to a temp file inside the container and execute it
    to avoid PowerShell quoting issues.
    """
    import tempfile
    import os
    script_content = "import os, signal\nos.kill(1, signal.SIGHUP)\n"

    # Write script to a temp file on the host, then copy it to the container
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(script_content)
        temp_script = f.name

    try:
        # Copy the script to the container
        copy_result = _run_docker("cp", temp_script, f"{CONTAINER_NAME}:/tmp/send_sighup.py", capture_output=True, text=True, timeout=10)
        if copy_result.returncode != 0:
            raise RuntimeError(f"Failed to copy script: {copy_result.stderr}")

        # Execute the script in the container
        exec_result = _run_docker("exec", CONTAINER_NAME, "python", "/tmp/send_sighup.py", capture_output=True, text=True, timeout=10)
        if exec_result.returncode != 0:
            raise RuntimeError(f"Failed to send SIGHUP: {exec_result.stderr}")
    finally:
        # Clean up the temp file on the host
        try:
            os.unlink(temp_script)
        except OSError:
            pass

    # Give the reload a moment
    time.sleep(0.5)


def _read_config():
    # Read config from the host path (bind-mounted to /app/config/gateway.yaml in container)
    # The config is at repo_root/config/gateway.yaml
    import os
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    config_path = repo_root / "config" / "gateway.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return f.read()


def _write_config(content):
    import os
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    config_path = repo_root / "config" / "gateway.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)


class TestRevocation:
    def test_sighup_revokes_tool_for_new_invocations(
        self, client, auth_headers_admin, auth_headers_a
    ):
        original = _read_config()
        try:
            # Revoke documents.search_documents from client_a.
            revoked = original.replace(
                'allowlist: ["documents.search_documents", "documents.get_document"]',
                'allowlist: ["documents.get_document"]',
            )
            assert revoked != original, "config replacement did not apply"
            _write_config(revoked)

            # Notify the gateway to reload config.
            _send_sighup()

            # New invocation of the revoked tool → 403 FORBIDDEN_TOOL.
            r = client.post(
                "/tools/call",
                headers=auth_headers_a,
                json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
            )
            assert r.status_code == 403, r.text
            assert r.json()["code"] == "FORBIDDEN_TOOL"

            # A tool still allowed → 202.
            r = client.post(
                "/tools/call",
                headers=auth_headers_a,
                json={"tool": "documents.get_document", "arguments": {"id": "doc-1"}},
            )
            assert r.status_code == 202, r.text
        finally:
            # Restore config and reload so other tests are unaffected.
            _write_config(original)
            _send_sighup()

    def test_revoked_client_loses_access_to_result(
        self, client, auth_headers_admin, auth_headers_a
    ):
        original = _read_config()
        try:
            # First, submit and complete an execution while permitted.
            r = client.post(
                "/tools/call",
                headers=auth_headers_a,
                json={"tool": "documents.search_documents", "arguments": {"query": "pre-revoke"}},
            )
            exec_id = r.json()["execution_id"]
            wait_for_state(client, exec_id, auth_headers_a, {"completed"})

            # Now revoke the tool.
            revoked = original.replace(
                'allowlist: ["documents.search_documents", "documents.get_document"]',
                'allowlist: ["documents.get_document"]',
            )
            _write_config(revoked)
            _send_sighup()

            # Result inaccessible → 403.
            r = client.get(f"/executions/{exec_id}/result", headers=auth_headers_a)
            assert r.status_code == 403, r.text
            assert r.json()["code"] == "FORBIDDEN_EXECUTION"

            # Status also 403.
            r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_a)
            assert r.status_code == 403, r.text
            assert r.json()["code"] == "FORBIDDEN_EXECUTION"
        finally:
            _write_config(original)
            _send_sighup()

    def test_other_clients_unaffected_by_revocation(
        self, client, auth_headers_admin, auth_headers_a, auth_headers_b
    ):
        original = _read_config()
        try:
            revoked = original.replace(
                'allowlist: ["documents.search_documents", "documents.get_document"]',
                'allowlist: ["documents.get_document"]',
            )
            _write_config(revoked)
            _send_sighup()

            # client_b (jobs) unaffected.
            r = client.post(
                "/tools/call",
                headers=auth_headers_b,
                json={"tool": "jobs.get_job_status", "arguments": {"job_id": "job-1"}},
            )
            assert r.status_code == 202, r.text

            # client_admin still allowed (its allowlist untouched).
            r = client.post(
                "/tools/call",
                headers=auth_headers_admin,
                json={"tool": "documents.search_documents", "arguments": {"query": "still ok"}},
            )
            assert r.status_code == 202, r.text
        finally:
            _write_config(original)
            _send_sighup()