"""Execution timeout test (REQ LIM-004).

execution_timeout_seconds=30: a delayed upstream call exceeds the timeout and
the execution is marked failed with EXECUTION_TIMEOUT.
"""

import time

from tests.conftest import API_KEY_ADMIN, delay_documents, wait_for_state


class TestTimeout:
    def test_execution_timeout_marks_failed(self, client, auth_headers_admin):
        """Delay the upstream call beyond the 30s execution timeout."""
        delay_documents(31.0)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "slow"}},
        )
        assert r.status_code == 202, r.text
        exec_id = r.json()["execution_id"]
        record = wait_for_state(client, exec_id, auth_headers_admin, {"failed"}, timeout=45.0)
        assert record["state"] == "failed"
        assert record["error"]["code"] == "EXECUTION_TIMEOUT"
