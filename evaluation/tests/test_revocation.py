"""Revocation via SIGHUP config reload tests (REQ REV-001 through REV-004).

SIGHUP reloads /app/config/gateway.yaml without restarting. After revoking a
tool from a client's allowlist, new invocations get 403 and existing results
become inaccessible. Other clients are unaffected.
"""

import signal

from tests.conftest import (
    API_KEY_ADMIN, API_KEY_A, API_KEY_B,
    wait_for_state,
)


CONFIG_PATH = "/app/config/gateway.yaml"


def _read_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _write_config(content):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


class TestRevocation:
    def test_sighup_revokes_tool_for_new_invocations(
        self, client, auth_headers_admin, auth_headers_a, gateway_process
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
            gateway_process.send_signal(signal.SIGHUP)

            # Give the reload a moment.
            import time
            time.sleep(0.5)

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
            gateway_process.send_signal(signal.SIGHUP)
            import time
            time.sleep(0.5)

    def test_revoked_client_loses_access_to_result(
        self, client, auth_headers_admin, auth_headers_a, gateway_process
    ):
        original = _read_config()
        try:
            revoked = original.replace(
                'allowlist: ["documents.search_documents", "documents.get_document"]',
                'allowlist: ["documents.get_document"]',
            )
            _write_config(revoked)
            gateway_process.send_signal(signal.SIGHUP)
            import time
            time.sleep(0.5)

            # Submit and complete an execution while permitted.
            r = client.post(
                "/tools/call",
                headers=auth_headers_a,
                json={"tool": "documents.search_documents", "arguments": {"query": "pre-revoke"}},
            )
            exec_id = r.json()["execution_id"]
            wait_for_state(client, exec_id, auth_headers_a, {"completed"})

            # Now revoke (already revoked above). Result inaccessible → 403.
            r = client.get(f"/executions/{exec_id}/result", headers=auth_headers_a)
            assert r.status_code == 403, r.text
            assert r.json()["code"] == "FORBIDDEN_EXECUTION"

            # Status also 403.
            r = client.get(f"/executions/{exec_id}/status", headers=auth_headers_a)
            assert r.status_code == 403, r.text
            assert r.json()["code"] == "FORBIDDEN_EXECUTION"
        finally:
            _write_config(original)
            gateway_process.send_signal(signal.SIGHUP)
            import time
            time.sleep(0.5)

    def test_other_clients_unaffected_by_revocation(
        self, client, auth_headers_a, auth_headers_b, gateway_process
    ):
        original = _read_config()
        try:
            revoked = original.replace(
                'allowlist: ["documents.search_documents", "documents.get_document"]',
                'allowlist: ["documents.get_document"]',
            )
            _write_config(revoked)
            gateway_process.send_signal(signal.SIGHUP)
            import time
            time.sleep(0.5)

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
            gateway_process.send_signal(signal.SIGHUP)
            import time
            time.sleep(0.5)
