"""Failure modes and upstream crash tests — RUNS LAST (crash kills jobs server).

Covers REQ FAIL-001 (submission failure 502) and FAIL-002 (execution failure
202 then failed). The crash test terminates the jobs fixture process, so this
file must execute after every test that needs jobs.execute_job.
"""

import time

import pytest

from tests.conftest import (
    API_KEY_ADMIN,
    crash_jobs, fail_next_jobs,
    wait_for_state,
)


class TestFailureModes:
    def test_submission_failure_502_when_upstream_down(self, client, auth_headers_admin):
        """Upstream unavailable BEFORE submission → 502 UPSTREAM_UNAVAILABLE."""
        crash_jobs()
        time.sleep(0.5)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        assert r.status_code == 502, r.text
        assert r.json()["code"] == "UPSTREAM_UNAVAILABLE"

    def test_execution_failure_202_then_failed(self, client, auth_headers_admin):
        """Job accepted (202), upstream raises during execution → 202 then failed UPSTREAM_FAILED."""
        fail_next_jobs("exception")
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        assert r.status_code == 202, r.text
        exec_id = r.json()["execution_id"]
        record = wait_for_state(client, exec_id, auth_headers_admin, {"failed"})
        assert record["state"] == "failed"
        assert record["error"]["code"] == "UPSTREAM_FAILED"

    def test_mcp_error_sanitized_to_upstream_failed(self, client, auth_headers_admin):
        """Upstream returns an MCP error payload → sanitized UPSTREAM_FAILED, no leak."""
        fail_next_jobs("mcp_error")
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        assert r.status_code == 202, r.text
        exec_id = r.json()["execution_id"]
        record = wait_for_state(client, exec_id, auth_headers_admin, {"failed"})
        assert record["state"] == "failed"
        assert record["error"]["code"] == "UPSTREAM_FAILED"
        text = str(record["error"])
        assert "Simulated MCP error" not in text
        assert "traceback" not in text.lower()
