"""Error code catalog and status/cancel/result lifecycle tests.

Covers REQ ERR-001 (error format), LIF-001 (state machine), LIF-002 (cancel),
LIF-003 (result). Does NOT kill upstream servers — those tests live in
test_99_failure_modes.py which runs last.
"""

import time

from tests.conftest import (
    API_KEY_ADMIN, API_KEY_A, API_KEY_B,
    delay_documents, delay_jobs,
    wait_for_state,
)


class TestErrorCatalog:
    def test_not_found_execution_returns_404(self, client, auth_headers_admin):
        r = client.get("/executions/exec-doesnotexist/status", headers=auth_headers_admin)
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "NOT_FOUND"

        r = client.post("/executions/exec-doesnotexist/cancel", headers=auth_headers_admin)
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "NOT_FOUND"

        r = client.get("/executions/exec-doesnotexist/result", headers=auth_headers_admin)
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "NOT_FOUND"

    def test_result_unavailable_for_running(self, client, auth_headers_admin):
        delay_documents(5.0)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "running"}},
        )
        exec_id = r.json()["execution_id"]
        res = client.get(f"/executions/{exec_id}/result", headers=auth_headers_admin)
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "RESULT_UNAVAILABLE"

    def test_cancel_already_terminal_returns_409(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.get_document", "arguments": {"id": "doc-1"}},
        )
        exec_id = r.json()["execution_id"]
        wait_for_state(client, exec_id, auth_headers_admin, {"completed"})
        r = client.post(f"/executions/{exec_id}/cancel", headers=auth_headers_admin)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "RESULT_UNAVAILABLE"

    def test_cross_client_access_returns_403(self, client, auth_headers_a, auth_headers_b):
        """client_b may not access client_a's execution."""
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
        )
        exec_id = r.json()["execution_id"]
        wait_for_state(client, exec_id, auth_headers_a, {"completed"})

        for method, path in [("get", f"/executions/{exec_id}/status"),
                             ("post", f"/executions/{exec_id}/cancel"),
                             ("get", f"/executions/{exec_id}/result")]:
            r = getattr(client, method)(path, headers=auth_headers_b)
            assert r.status_code == 403, f"{method} {path}: {r.text}"
            assert r.json()["code"] == "FORBIDDEN_EXECUTION"


class TestExecutionLifecycle:
    def test_status_pending_then_completed(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "beta"}},
        )
        exec_id = r.json()["execution_id"]
        r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_admin)
        assert r.status_code == 200
        assert r.json()["state"] in ("pending", "running")
        record = wait_for_state(client, exec_id, auth_headers_admin, {"completed"})
        assert record["state"] == "completed"

    def test_cancel_pending_execution(self, client, auth_headers_admin):
        delay_documents(5.0)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "cancel-me"}},
        )
        exec_id = r.json()["execution_id"]
        time.sleep(0.5)
        r = client.post(f"/executions/{exec_id}/cancel", headers=auth_headers_admin)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "cancelled"
        assert r.json()["execution_id"] == exec_id

    def test_cancelled_status_shows_error(self, client, auth_headers_admin):
        delay_documents(5.0)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "cancel-me2"}},
        )
        exec_id = r.json()["execution_id"]
        time.sleep(0.5)
        client.post(f"/executions/{exec_id}/cancel", headers=auth_headers_admin)
        record = wait_for_state(client, exec_id, auth_headers_admin, {"cancelled"})
        assert record["error"] is not None
        assert record["error"]["code"] == "RESULT_UNAVAILABLE"

    def test_cancel_non_owner_returns_403(self, client, auth_headers_a, auth_headers_b):
        delay_documents(5.0)
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "documents.search_documents", "arguments": {"query": "cross"}},
        )
        exec_id = r.json()["execution_id"]
        time.sleep(0.5)
        r = client.post(f"/executions/{exec_id}/cancel", headers=auth_headers_b)
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "FORBIDDEN_EXECUTION"
