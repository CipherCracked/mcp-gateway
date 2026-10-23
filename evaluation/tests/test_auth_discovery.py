"""Authentication and tool discovery tests (REQ AUTH-001, AUTH-002, DISC-001)."""

from evaluation.tests.conftest import (
    API_KEY_ADMIN, API_KEY_A, API_KEY_B, BAD_KEY,
)


class TestAuthentication:
    def test_missing_api_key_returns_401(self, client):
        r = client.get("/tools/list")
        assert r.status_code == 401
        body = r.json()
        assert body["code"] == "AUTH_MISSING"
        assert "stack" not in str(body).lower()
        assert "traceback" not in str(body).lower()

    def test_empty_api_key_returns_401(self, client):
        r = client.get("/tools/list", headers={"x-api-key": ""})
        assert r.status_code == 401
        assert r.json()["code"] == "AUTH_INVALID"

    def test_invalid_api_key_returns_401(self, client):
        r = client.get("/tools/list", headers={"x-api-key": BAD_KEY})
        assert r.status_code == 401
        assert r.json()["code"] == "AUTH_INVALID"

    def test_valid_admin_key_returns_200(self, client, auth_headers_admin):
        r = client.get("/tools/list", headers=auth_headers_admin)
        assert r.status_code == 200

    def test_error_response_has_no_stack_trace(self, client):
        r = client.get("/tools/list")
        text = r.text
        assert "Traceback" not in text
        assert "File \"" not in text
        assert "/app/" not in text


class TestToolDiscovery:
    def test_admin_sees_all_namespaced_tools(self, client, auth_headers_admin):
        r = client.get("/tools/list", headers=auth_headers_admin)
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert "documents.search_documents" in tools
        assert "documents.get_document" in tools
        assert "jobs.execute_job" in tools
        assert "jobs.get_job_status" in tools

    def test_client_a_sees_only_documents_tools(self, client, auth_headers_a):
        r = client.get("/tools/list", headers=auth_headers_a)
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert "documents.search_documents" in tools
        assert "documents.get_document" in tools
        assert "jobs.execute_job" not in tools
        assert "jobs.get_job_status" not in tools

    def test_client_b_sees_only_jobs_tools(self, client, auth_headers_b):
        r = client.get("/tools/list", headers=auth_headers_b)
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert "jobs.execute_job" in tools
        assert "jobs.get_job_status" in tools
        assert "documents.search_documents" not in tools
        assert "documents.get_document" not in tools

    def test_tool_list_is_filtered_not_full(self, client, auth_headers_a):
        """Authorization filters discovery — client_a must not even see job tools."""
        r = client.get("/tools/list", headers=auth_headers_a)
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert all("jobs." not in t for t in tools)
