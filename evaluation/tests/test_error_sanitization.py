"""Error sanitization tests (REQ SAN-001).

Client responses must never leak stack traces, internal paths, credentials,
or fixture control port information.
"""

from tests.conftest import BAD_KEY

FORBIDDEN_SUBSTRINGS = [
    "Traceback",
    "traceback",
    "File \"",
    "/app/",
    "mcp_gateway",
    "reference/mcp_gateway",
    "site-packages",
    "key-a-123",
    "key-b-456",
    "key-admin-789",
    "9001",
    "9002",
    "control",
    "__pycache__",
    "<class ",
]


class TestErrorSanitization:
    def _assert_sanitized(self, response):
        text = response.text
        for sub in FORBIDDEN_SUBSTRINGS:
            assert sub not in text, f"leaked substring {sub!r} in: {text[:200]}"

    def test_auth_errors_sanitized(self, client):
        for headers in [{}, {"x-api-key": ""}, {"x-api-key": BAD_KEY}]:
            r = client.get("/tools/list", headers=headers)
            assert r.status_code in (401,)
            self._assert_sanitized(r)
            body = r.json()
            assert "code" in body
            assert "error" in body

    def test_not_found_sanitized(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.nonexistent", "arguments": {}},
        )
        assert r.status_code == 404
        self._assert_sanitized(r)

    def test_forbidden_sanitized(self, client, auth_headers_a):
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "jobs.execute_job", "arguments": {}},
        )
        assert r.status_code == 403
        self._assert_sanitized(r)

    def test_invalid_args_sanitized(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {}},
        )
        assert r.status_code == 400
        self._assert_sanitized(r)
        assert "details" in r.json()

    def test_payload_too_large_sanitized(self, client, auth_headers_admin):
        big = {"query": "x" * (1024 * 1024 + 1)}
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": big},
        )
        assert r.status_code == 413, r.text
        assert r.json()["code"] == "PAYLOAD_TOO_LARGE"
        self._assert_sanitized(r)

    def test_unhandled_exception_sanitized(self, client, auth_headers_admin):
        """Unhandled exceptions must be caught by the catch-all handler and
        never leak stack traces to the client (REQ SAN-001)."""
        r = client.post(
            "/_test/raise_error",
            headers=auth_headers_admin,
        )
        assert r.status_code == 500, r.text
        self._assert_sanitized(r)
        body = r.json()
        assert "code" in body
        assert "error" in body
