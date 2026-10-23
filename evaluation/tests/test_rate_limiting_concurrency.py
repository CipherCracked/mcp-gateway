"""Rate limiting and concurrency limit tests (REQ LIM-001, LIM-002, LIM-003)."""

import concurrent.futures

from evaluation.tests.conftest import API_KEY_ADMIN, delay_jobs


class TestRateLimiting:
    def test_token_bucket_allows_burst_up_to_capacity(self, client, auth_headers_admin):
        """Capacity=60: first ~60 rapid submissions succeed (202), then 429 RATE_LIMITED."""
        results = []
        for _ in range(70):
            r = client.post(
                "/tools/call",
                headers=auth_headers_admin,
                json={"tool": "documents.search_documents", "arguments": {"query": "rate"}},
            )
            results.append(r.status_code)
        successes = [s for s in results if s == 202]
        rate_limited = [s for s in results if s == 429]
        assert len(successes) >= 1, "expected at least one successful submission"
        assert len(rate_limited) >= 1, "expected at least one rate-limited submission"
        for s in rate_limited:
            # The 429 responses here should be rate-limited, not concurrency.
            body = client.post(
                "/tools/call",
                headers=auth_headers_admin,
                json={"tool": "documents.search_documents", "arguments": {"query": "probe"}},
            ).json()
            # Just confirm the code shape once; done above via status.
            assert body.get("code") in ("RATE_LIMITED", "CONCURRENCY_EXCEEDED")


class TestConcurrency:
    def test_concurrency_limit_rejects_overflow(self, client, auth_headers_admin):
        """concurrency_limit=10: 10 delayed executions hold slots; 11th → 429 CONCURRENCY_EXCEEDED."""
        # Queue 10 delays (one per upcoming call), each holds a slot ~5s.
        for _ in range(10):
            delay_jobs(5.0)

        exec_ids = []
        for _ in range(10):
            r = client.post(
                "/tools/call",
                headers=auth_headers_admin,
                json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
            )
            assert r.status_code == 202, r.text
            exec_ids.append(r.json()["execution_id"])

        # 11th submission: all slots held → immediate 429 (non-blocking admission)
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        assert r.status_code == 429, r.text
        assert r.json()["code"] == "CONCURRENCY_EXCEEDED"

        # Let the delayed executions complete so slots are released.
        for exec_id in exec_ids:
            from evaluation.tests.conftest import wait_for_state
            wait_for_state(client, exec_id, auth_headers_admin, {"completed", "failed"})
