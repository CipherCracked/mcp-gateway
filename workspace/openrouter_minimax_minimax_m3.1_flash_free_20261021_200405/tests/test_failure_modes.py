"""Distinction between submission failures (502) and execution failures (202)."""

from __future__ import annotations

from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from .conftest import API_KEY_A, headers, wait_for_terminal


async def test_submission_failure_returns_502(tmp_path: Path, db_path: Path, monkeypatch):
    """A dead upstream at submission time is a 502, not a 202."""
    write_config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            # Kill the upstream so that the next submission fails.
            gateway = app.state.gateway
            for upstream in gateway.upstreams.values():
                await upstream.stop()

            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={
                    "tool": "documents.search_documents",
                    "arguments": {"query": "gateway"},
                },
            )
            assert response.status_code == 502, response.text
            assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"


async def test_execution_failure_is_202_then_failed(tmp_path: Path, db_path: Path, monkeypatch):
    """A failure during execution is accepted (202) and later reports failed."""
    write_config(tmp_path, db_path, monkeypatch)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "testing.fail", "arguments": {}},
            )
            assert response.status_code == 202, response.text
            execution_id = response.json()["execution_id"]

            status = await wait_for_terminal(client, execution_id, API_KEY_A)
            assert status["state"] == "failed", status
            assert status["error"]["code"] == "UPSTREAM_FAILED", status

            result = await client.get(
                f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
            )
            assert result.status_code == 200
            body = result.json()
            assert body["state"] == "failed"
            assert body["error"]["code"] == "UPSTREAM_FAILED"
            # The client never sees upstream internals.
            assert "Traceback" not in result.text
            assert "fixture failure" not in result.text


async def test_execution_timeout_is_failed_not_cancelled(
    tmp_path: Path, db_path: Path, monkeypatch
):
    """A slow upstream leads to failed/EXECUTION_TIMEOUT, not cancelled."""
    write_config(tmp_path, db_path, monkeypatch, timeout=1)
    from reference.mcp_gateway.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "testing.echo", "arguments": {"delay": 10}},
            )
            assert response.status_code == 202, response.text
            execution_id = response.json()["execution_id"]

            status = await wait_for_terminal(client, execution_id, API_KEY_A)
            assert status["state"] == "failed", status
            assert status["error"]["code"] == "EXECUTION_TIMEOUT", status


def write_config(
    tmp_path: Path, db_path: Path, monkeypatch, timeout: int = 30
) -> Path:
    """Write a config that uses the test fixture server for failure paths."""
    config_path = tmp_path / "config" / "gateway.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""
gateway:
  rate_limit_per_minute: 600
  concurrency_limit: 10
  execution_timeout_seconds: {timeout}
  gateway_timeout_seconds: {timeout + 5}
servers:
  documents:
    command: ["python3", "-m", "reference.mcp_gateway.servers.documents"]
  testing:
    command: ["python3", "-m", "reference.mcp_gateway.servers.testing"]
    env:
      GATEWAY_TEST_TOOL_FAIL: "1"
clients:
  client_a:
    api_key: "{API_KEY_A}"
    allowlist:
      - "documents.search_documents"
      - "testing.echo"
      - "testing.fail"
"""
    )
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))
    # The "testing" server must be able to fail as well as succeed.
    monkeypatch.delenv("GATEWAY_TEST_TOOL_FAIL", raising=False)
    return config_path
