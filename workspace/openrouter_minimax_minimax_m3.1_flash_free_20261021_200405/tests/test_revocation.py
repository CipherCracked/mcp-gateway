"""Revocation semantics triggered by a configuration reload."""

from __future__ import annotations

from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from .conftest import API_KEY_A, API_KEY_B, headers, wait_for_terminal


async def test_revocation_blocks_new_calls_and_hides_results(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """Revoking a tool blocks new calls and hides in-flight results."""
    config_path = write_config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={
                    "tool": "documents.search_documents",
                    "arguments": {"query": "gateway"},
                },
            )
            assert response.status_code == 202
            execution_id = response.json()["execution_id"]
            status = await wait_for_terminal(client, execution_id, API_KEY_A)
            assert status["state"] == "completed"

            # Before the revocation the result is visible.
            ok = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert ok.status_code == 200

            # Revoke the tool and reload the configuration.
            _write(config_path, revoked=True)
            await app.state.gateway.reload_config()

            blocked = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={
                    "tool": "documents.search_documents",
                    "arguments": {"query": "gateway"},
                },
            )
            assert blocked.status_code == 403
            assert blocked.json()["code"] == "FORBIDDEN_TOOL"

            hidden = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert hidden.status_code == 403
            assert hidden.json()["code"] == "FORBIDDEN_EXECUTION"

            # The other client is unaffected.
            other = await client.get("/tools/list", headers=headers(API_KEY_B))
            assert other.status_code == 200
            assert "documents.search_documents" in other.json()["tools"]


async def test_reload_replaces_the_allowlist(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """A reload replaces the client configuration."""
    config_path = write_config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            before = await client.get("/tools/list", headers=headers(API_KEY_A))
            assert before.json()["tools"] == [
                "documents.search_documents",
                "documents.get_document",
            ]

            _write(config_path, revoked=True)
            await app.state.gateway.reload_config()

            after = await client.get("/tools/list", headers=headers(API_KEY_A))
            assert after.json()["tools"] == ["documents.get_document"]


async def test_removed_client_key_is_rejected(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """A client removed from the config can no longer authenticate."""
    config_path = write_config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            # Remove client_a from the configuration entirely.
            _write_without_client_a(config_path)
            await app.state.gateway.reload_config()

            response = await client.get("/tools/list", headers=headers(API_KEY_A))
            assert response.status_code == 401
            assert response.json()["code"] == "AUTH_INVALID"

            # The remaining client is unaffected.
            other = await client.get("/tools/list", headers=headers(API_KEY_B))
            assert other.status_code == 200


def write_config(
    tmp_path: Path, db_path: Path, monkeypatch
) -> Path:
    config_path = tmp_path / "config" / "gateway.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write(config_path, revoked=False)
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))
    return config_path


def _write(config_path: Path, revoked: bool) -> None:
    allowlist = (
        '["documents.get_document"]'
        if revoked
        else '["documents.search_documents", "documents.get_document"]'
    )
    config_path.write_text(
        f"""
gateway:
  rate_limit_per_minute: 600
servers:
  documents:
    command: ["python3", "-m", "reference.mcp_gateway.servers.documents"]
clients:
  client_a:
    api_key: "{API_KEY_A}"
    allowlist: {allowlist}
  client_b:
    api_key: "{API_KEY_B}"
    allowlist: ["documents.search_documents", "documents.get_document"]
"""
    )


def _write_without_client_a(config_path: Path) -> None:
    config_path.write_text(
        f"""
gateway:
  rate_limit_per_minute: 600
servers:
  documents:
    command: ["python3", "-m", "reference.mcp_gateway.servers.documents"]
clients:
  client_b:
    api_key: "{API_KEY_B}"
    allowlist: ["documents.search_documents", "documents.get_document"]
"""
    )
