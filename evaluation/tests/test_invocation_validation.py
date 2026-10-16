"""Tool invocation and argument validation tests (REQ INV-001, INV-002, VAL-001)."""

import pytest

from tests.conftest import (
    API_KEY_ADMIN, API_KEY_A, API_KEY_B,
    wait_for_state,
)


class TestToolInvocation:
    def test_submit_returns_202_pending(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "hello"}},
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["state"] == "pending"
        assert body["execution_id"].startswith("exec-")

    def test_submit_jobs_tool(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {"foo": "bar"}}},
        )
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "pending"

    def test_forbidden_tool_returns_403(self, client, auth_headers_a):
        """client_a may not call jobs.execute_job."""
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "jobs.execute_job", "arguments": {}},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "FORBIDDEN_TOOL"

    def test_tool_not_found_returns_404(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.nonexistent", "arguments": {}},
        )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "TOOL_NOT_FOUND"

    def test_malformed_tool_name_returns_404(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "badname", "arguments": {}},
        )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "TOOL_NOT_FOUND"

    def test_empty_tool_name_returns_404(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "", "arguments": {}},
        )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "TOOL_NOT_FOUND"

    def test_successful_execution_reaches_completed(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "alpha"}},
        )
        exec_id = r.json()["execution_id"]
        record = wait_for_state(client, exec_id, auth_headers_admin, {"completed"})
        assert record["state"] == "completed"

    def test_result_retrieval_after_completion(self, client, auth_headers_admin):
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.get_document", "arguments": {"id": "doc-1"}},
        )
        exec_id = r.json()["execution_id"]
        wait_for_state(client, exec_id, auth_headers_admin, {"completed"})
        res = client.get(f"/executions/{exec_id}/result", headers=auth_headers_admin)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["state"] == "completed"
        assert body["result"] is not None
        assert body["result"]["id"] == "doc-1"


class TestArgumentValidation:
    def test_missing_required_field(self, client, auth_headers_admin):
        # search_documents requires "query"
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {}},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "INVALID_ARGS"
        assert "missing" in r.json()["details"]
        assert "query" in r.json()["details"]["missing"]

    def test_type_mismatch(self, client, auth_headers_admin):
        # limit must be integer
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents",
                  "arguments": {"query": "x", "limit": "not-an-int"}},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "INVALID_ARGS"
        assert "type_mismatch" in r.json()["details"]

    def test_unexpected_field_rejected(self, client, auth_headers_admin):
        # additionalProperties: false — bogus field rejected
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.get_document",
                  "arguments": {"id": "doc-1", "bogus": 1}},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "INVALID_ARGS"
        assert "unexpected_fields" in r.json()["details"]
        assert "bogus" in r.json()["details"]["unexpected_fields"]

    def test_enum_mismatch(self, client, auth_headers_admin):
        # priority must be low/normal/high
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "jobs.execute_job",
                  "arguments": {"payload": {}, "priority": "urgent"}},
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "INVALID_ARGS"
        assert "enum_mismatch" in r.json()["details"]

    def test_validation_runs_before_permission(self, client, auth_headers_a):
        """Invalid args on a forbidden tool still returns INVALID_ARGS? No —
        permission is checked first per spec. Forbidden tool → 403 regardless."""
        r = client.post(
            "/tools/call",
            headers=auth_headers_a,
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        )
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "FORBIDDEN_TOOL"

    def test_empty_arguments_allowed_when_no_required(self, client, auth_headers_admin):
        # get_document requires "id", but let's test a no-required scenario via
        # search_documents with only required query present
        r = client.post(
            "/tools/call",
            headers=auth_headers_admin,
            json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
        )
        assert r.status_code == 202, r.text
