"""Cancellation semantics of the execution lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from .conftest import API_KEY_A, API_KEY_B, headers


async def test_cancel_running_execution(tmp_path: Path, db_path: Path, monkeypatch):
    """Cancelling a running execution yields the cancelled state."""
    _config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "testing.echo", "arguments": {"delay": 20}},
            )
            assert response.status_code == 202
            execution_id = response.json()["execution_id"]

            # Wait until the execution is actually running.
            for _ in range(100):
                status = await client.get(
                    f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
                )
                if status.json()["state"] == "running":
                    break
                await asyncio.sleep(0.05)
            assert status.json()["state"] == "running", status.json()

            cancelled = await client.post(
                f"/executions/{execution_id}/cancel", headers=headers(API_KEY_A)
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["state"] == "cancelled"

            after = await client.get(
                f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
            )
            assert after.json()["state"] == "cancelled"
            assert after.json()["error"]["code"] == "CANCELLED"

            # A cancelled execution has no result.
            result = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert result.status_code == 409
            assert result.json()["code"] == "RESULT_UNAVAILABLE"


async def test_cancel_twice_returns_409(tmp_path: Path, db_path: Path, monkeypatch):
    """Cancelling an already terminal execution returns 409."""
    _config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "testing.echo", "arguments": {"delay": 20}},
            )
            execution_id = response.json()["execution_id"]
            first = await client.post(
                f"/executions/{execution_id}/cancel", headers=headers(API_KEY_A)
            )
            assert first.status_code == 200
            second = await client.post(
                f"/executions/{execution_id}/cancel", headers=headers(API_KEY_A)
            )
            assert second.status_code == 409, second.text
            assert second.json()["code"] == "RESULT_UNAVAILABLE"


async def test_cancel_unknown_execution_is_404(tmp_path: Path, db_path: Path, monkeypatch):
    """Cancelling an unknown execution returns 404."""
    _config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/executions/11111111-2222-3333-4444-555555555555/cancel",
                headers=headers(API_KEY_A),
            )
            assert response.status_code == 404
            assert response.json()["code"] == "NOT_FOUND"


async def test_cancel_of_other_clients_execution_is_403(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """A client cannot cancel another client's execution."""
    _config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "testing.echo", "arguments": {"delay": 20}},
            )
            execution_id = response.json()["execution_id"]

            other = await client.post(
                f"/executions/{execution_id}/cancel", headers=headers(API_KEY_B)
            )
            assert other.status_code == 403, other.text
            assert other.json()["code"] == "FORBIDDEN_EXECUTION"

            # The execution is still alive for its owner.
            status = await client.get(
                f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
            )
            assert status.json()["state"] in ("pending", "running")


def _config(tmp_path: Path, db_path: Path, monkeypatch) -> Path:
    config_path = tmp_path / "config" / "gateway.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""
gateway:
  rate_limit_per_minute: 600
  concurrency_limit: 10
  execution_timeout_seconds: 30
servers:
  testing:
    command: ["python3", "-m", "reference.mcp_gateway.servers.testing"]
clients:
  client_a:
    api_key: "{API_KEY_A}"
    allowlist: ["testing.echo"]
  client_b:
    api_key: "{API_KEY_B}"
    allowlist: ["testing.echo"]
"""
    )
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))
    return config_path
