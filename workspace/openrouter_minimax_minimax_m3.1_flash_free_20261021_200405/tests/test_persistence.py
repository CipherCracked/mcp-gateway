"""Tests for persistence, restart recovery and revocation semantics."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from .conftest import (
    API_KEY_A,
    API_KEY_ADMIN,
    API_KEY_B,
    headers,
    wait_for_terminal,
    write_config,
)


async def test_terminal_state_survives_restart(tmp_path: Path, db_path: Path, monkeypatch):
    config = write_config(tmp_path / "config" / "gateway.yaml")
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

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
            execution_id = response.json()["execution_id"]
            status = await wait_for_terminal(client, execution_id, API_KEY_A)
            assert status["state"] == "completed"

    # Restart the gateway against the same database.
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.get(
                f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
            )
            assert response.status_code == 200
            assert response.json()["state"] == "completed"

            response = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert response.status_code == 200
            assert response.json()["state"] == "completed"
            assert "documents" in response.json()["result"]


async def test_non_terminal_state_becomes_interrupted_after_restart(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """Executions left running by a crash are marked EXECUTION_INTERRUPTED."""
    config = write_config(tmp_path / "config" / "gateway.yaml")
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

    from reference.mcp_gateway.main import app
    from reference.mcp_gateway.persistence import ExecutionStore

    # Seed the database with an execution that never finished.
    async def seed() -> str:
        store = ExecutionStore(db_path)
        await store.connect()
        from reference.mcp_gateway.persistence import ExecutionRecord

        record = ExecutionRecord(
            execution_id="11111111-2222-3333-4444-555555555555",
            client_id="client_a",
            tool_name="documents.search_documents",
            server_name="documents",
            state="running",
            arguments={},
            created_at=1.0,
            updated_at=1.0,
        )
        await store.create(record)
        await store.close()
        return record.execution_id

    execution_id = await seed()

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.get(
                f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
            )
            assert response.status_code == 200
            body = response.json()
            assert body["state"] == "failed"
            assert body["error"]["code"] == "EXECUTION_INTERRUPTED"

            # The result of a failed execution is still retrievable.
            response = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert response.status_code == 200
            assert response.json()["error"]["code"] == "EXECUTION_INTERRUPTED"


async def test_revocation_blocks_new_calls_and_hides_results(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """SIGHUP revokes a permission: new calls fail and results are hidden."""
    config = write_config(tmp_path / "config" / "gateway.yaml")
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

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
            execution_id = response.json()["execution_id"]
            status = await wait_for_terminal(client, execution_id, API_KEY_A)
            assert status["state"] == "completed"

            # Before the reload the client may read its own result.
            response = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert response.status_code == 200

            # Revoke the permission and reload the configuration.
            write_config(
                config, allowlist_a=["documents.get_document"]
            )
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(0.5)

            # New calls to the revoked tool are refused.
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={
                    "tool": "documents.search_documents",
                    "arguments": {"query": "gateway"},
                },
            )
            assert response.status_code == 403
            assert response.json()["code"] == "FORBIDDEN_TOOL"

            # The result of the existing execution is hidden from the client.
            response = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert response.status_code == 403
            assert response.json()["code"] == "FORBIDDEN_EXECUTION"

            response = await client.get(
                f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
            )
            assert response.status_code == 403

            # Other clients are unaffected.
            response = await client.get(
                f"/executions/{execution_id}/status", headers=headers(API_KEY_ADMIN)
            )
            # The admin is not the owner either, so it stays forbidden.
            assert response.status_code == 403


async def test_revocation_does_not_affect_other_clients(
    tmp_path: Path, db_path: Path, monkeypatch
):
    config = write_config(tmp_path / "config" / "gateway.yaml")
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_B),
                json={"tool": "jobs.execute_job", "arguments": {"payload": {"a": 1}}},
            )
            execution_id = response.json()["execution_id"]
            await wait_for_terminal(client, execution_id, API_KEY_B)

            write_config(config, allowlist_a=[])
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(0.5)

            response = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_B)
            )
            assert response.status_code == 200
            assert response.json()["state"] == "completed"
