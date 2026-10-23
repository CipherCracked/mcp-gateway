"""Error response format validation tests (REQ GEN-001).

All error responses must follow the format: {"error": str, "code": str, "details"?: object}.
This test validates the format across all error types.
"""

import pytest

from evaluation.tests.conftest import API_KEY_ADMIN, API_KEY_A, BAD_KEY


class TestErrorCodes:
    """Verify all error responses follow the standard format."""

    def _assert_format(self, response, expected_code: str | None = None):
        """Assert response has {"error", "code", "details"?} format."""
        assert response.status_code >= 400
        body = response.json()
        assert "error" in body, f"Missing 'error' field in {body}"
        assert "code" in body, f"Missing 'code' field in {body}"
        assert isinstance(body["error"], str), f"'error' must be string: {body}"
        assert isinstance(body["code"], str), f"'code' must be string: {body}"
        if expected_code:
            assert body["code"] == expected_code, f"Expected code {expected_code}, got {body['code']}"
        # details is optional
        if "details" in body:
            assert body["details"] is not None

    def test_auth_missing_key_format(self, client):
        r = client.get("/tools/list")
        self._assert_format(r, "AUTH_MISSING")

    def test_auth_empty_key_format(self, client):
        r = client.get("/tools/list", headers={"x-api-key": ""})
        self._assert_format(r, "AUTH_INVALID")

    def test_auth_invalid_key_format(self, client):
        r = client.get("/tools/list", headers={"x-api-key": BAD_KEY})
        self._assert_format(r, "AUTH_INVALID")

    def test_forbidden_tool_format(self, client, auth_headers_a):
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "jobs.execute_job", "arguments": {}},
        )
        self._assert_format(r, "FORBIDDEN_TOOL")

    def test_tool_not_found_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.nonexistent", "arguments": {}},
        )
        self._assert_format(r, "TOOL_NOT_FOUND")

    def test_malformed_tool_name_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "invalid", "arguments": {}},
        )
        self._assert_format(r, "TOOL_NOT_FOUND")

    def test_empty_tool_name_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "", "arguments": {}},
        )
        self._assert_format(r, "TOOL_NOT_FOUND")

    def test_missing_required_field_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {}},
        )
        self._assert_format(r, "INVALID_ARGS")
        assert "details" in r.json()
        assert "missing" in r.json()["details"]

    def test_type_mismatch_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": 123}},
        )
        self._assert_format(r, "INVALID_ARGS")
        assert "details" in r.json()
        assert "type_mismatch" in r.json()["details"]

    def test_unexpected_field_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "x", "extra": 1}},
        )
        self._assert_format(r, "INVALID_ARGS")
        assert "details" in r.json()
        assert "unexpected_fields" in r.json()["details"]

    def test_enum_mismatch_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}, "priority": "urgent"}},
        )
        self._assert_format(r, "INVALID_ARGS")
        assert "details" in r.json()
        assert "enum_mismatch" in r.json()["details"]

    def test_payload_too_large_format(self, client, auth_headers_admin):
        big = {"query": "x" * (1024 * 1024 + 1)}
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": big},
        )
        self._assert_format(r, "PAYLOAD_TOO_LARGE")

    def test_not_found_execution_format(self, client, auth_headers_admin):
        r = client.get("/executions/exec-nonexistent/status", headers=auth_headers_admin)
        self._assert_format(r, "NOT_FOUND")

    def test_result_unavailable_running_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
        )
        exec_id = r.json()["execution_id"]
        r = client.get(f"/executions/{exec_id}/result", headers=auth_headers_admin)
        self._assert_format(r, "RESULT_UNAVAILABLE")

    def test_cancel_already_terminal_format(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
        )
        exec_id = r.json()["execution_id"]
        # Wait for completion
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_admin)
            if r.json()["state"] == "completed":
                break
            time.sleep(0.25)
        # Now try to cancel completed
        r = client.post(f"/executions/{exec_id}/cancel", headers=auth_headers_admin)
        self._assert_format(r, "RESULT_UNAVAILABLE")

    def test_cross_client_access_format(self, client, auth_headers_a, auth_headers_b):
        # client_a creates execution
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
        )
        exec_id = r.json()["execution_id"]
        # client_b tries to access
        r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_b)
        self._assert_format(r, "FORBIDDEN_EXECUTION")

    def test_cancel_non_owner_format(self, client, auth_headers_a, auth_headers_b):
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
        )
        exec_id = r.json()["execution_id"]
        r = client.post(f"/executions/{exec_id}/cancel", headers=auth_headers_b)
        self._assert_format(r, "FORBIDDEN_EXECUTION")

    def test_unhandled_exception_format(self, client, auth_headers_admin):
        r = client.post("/_test/raise_error", headers=auth_headers_admin)
        self._assert_format(r, "INTERNAL")

    def test_rate_limited_format(self, client, auth_headers_admin):
        # Exhaust rate limit by making many requests
        for _ in range(70):
            client.get("/tools/list", headers=auth_headers_admin)
        r = client.get("/tools/list", headers=auth_headers_admin)
        if r.status_code == 429:
            self._assert_format(r, "RATE_LIMITED")

    def test_concurrency_exceeded_format(self, client, auth_headers_admin):
        # Submit many concurrent requests to exceed concurrency limit (default 10)
        # Use delay to make them overlap - apply delay to all 15 requests
        from evaluation.tests.conftest import delay_documents
        delay_documents(2.0, count=15)  # 2s delay for next 15 requests
        try:
            import threading
            import httpx

            results = []
            def make_request():
                with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30.0) as c:
                    r = c.post(
                        "/tools/call",
                        headers={"x-api-key": API_KEY_ADMIN},
                        json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
                    )
                    results.append(r)

            # Submit 15 concurrent requests (limit is 10)
            threads = [threading.Thread(target=make_request) for _ in range(15)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            # At least one should get 429
            rate_limited = [r for r in results if r.status_code == 429]
            assert len(rate_limited) > 0, f"Expected at least one 429, got statuses: {[r.status_code for r in results]}"
            self._assert_format(rate_limited[0], "CONCURRENCY_EXCEEDED")
        finally:
            delay_documents(0.0)

    def test_execution_timeout_format(self, client, auth_headers_admin):
        # Use delay control to make documents server slow
        from evaluation.tests.conftest import delay_documents
        delay_documents(40.0, count=1)  # Longer than execution_timeout (30s)
        try:
            r = client.post(
                "/tools/call",
                headers=auth_headers_admin,
                json={"tool": "documents.search_documents", "arguments": {"query": "slow"}},
            )
            assert r.status_code == 202, f"Expected 202, got {r.status_code}: {r.text}"
            exec_id = r.json()["execution_id"]
            # Poll until failed
            import time
            deadline = time.time() + 50
            last_state = None
            while time.time() < deadline:
                r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_admin)
                assert r.status_code == 200, f"Status poll failed: {r.status_code}"
                body = r.json()
                last_state = body["state"]
                if last_state == "failed":
                    # Verify the error code in the status response
                    assert "error" in body, f"Missing error in failed status: {body}"
                    assert body["error"]["code"] == "EXECUTION_TIMEOUT", f"Expected EXECUTION_TIMEOUT, got {body['error']['code']}"
                    return
                time.sleep(0.5)
            assert False, f"Execution did not reach failed state, last state: {last_state}"
        finally:
            delay_documents(0.0)

    def test_upstream_unavailable_format(self, client, auth_headers_admin):
        from evaluation.tests.conftest import crash_jobs
        crash_jobs()
        import time
        time.sleep(0.5)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        if r.status_code == 502:
            self._assert_format(r, "UPSTREAM_UNAVAILABLE")

    def test_upstream_failed_format(self, client, auth_headers_admin):
        from evaluation.tests.conftest import fail_next_jobs
        import time

        # The previous test (test_upstream_unavailable_format) crashes the jobs server.
        # Gateway restarts it on-demand when a jobs tool is invoked (after ~1s cooldown).
        # First, trigger a gateway request to a jobs tool to record dead_since.
        trigger_r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        # This returns 502 (UPSTREAM_UNAVAILABLE) and records dead_since=now.
        # Gateway only restarts on next request AFTER cooldown (1s).
        # Wait for cooldown to pass, then make another request to trigger actual restart.
        time.sleep(1.5)  # Wait for RESTART_COOLDOWN_SECONDS (1.0) + buffer

        # Second request triggers the restart
        restart_trigger = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        # This may return 502 (if restart in progress) or 202 (if fast).
        # Either way, the restart has been initiated.

        # Now wait for jobs server control endpoint to be ready.
        # Use fail_next_jobs helper which handles connection errors during restart.
        for _ in range(40):  # 40 * 0.25 = 10s max wait
            r = fail_next_jobs("exception")
            if r.status_code == 200:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Jobs control endpoint not ready after 10s")

        # Now the jobs server is up and fail_next is queued. Make the actual test request.
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        assert r.status_code == 202, f"Expected 202, got {r.status_code}: {r.text}"
        exec_id = r.json()["execution_id"]
        # Poll until failed
        deadline = time.time() + 15
        last_state = None
        while time.time() < deadline:
            r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_admin)
            assert r.status_code == 200, f"Status poll failed: {r.status_code}"
            body = r.json()
            last_state = body["state"]
            if last_state == "failed":
                # Verify the error code in the status response
                assert "error" in body, f"Missing error in failed status: {body}"
                assert body["error"]["code"] == "UPSTREAM_FAILED", f"Expected UPSTREAM_FAILED, got {body['error']['code']}"
                return
            time.sleep(0.25)
        assert False, f"Execution did not reach failed state, last state: {last_state}"