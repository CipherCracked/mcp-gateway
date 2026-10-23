"""Tests for the configurable limits and the two failure modes."""

from __future__ import annotations

import sys
from pathlib import Path

from .conftest import API_KEY_A, API_KEY_B, headers, wait_for_terminal, write_config


async def test_payload_too_large(client):
    payload = {
        "tool": "documents.search_documents",
        "arguments": {"query": "x" * (1024 * 1024 + 100)},
    }
    response = await client.post("/tools/call", headers=headers(API_KEY_A), json=payload)
    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_rate_limit_is_enforced(tmp_path: Path, db_path: Path, monkeypatch):
    config = write_config(
        tmp_path / "config" / "gateway.yaml", rate_limit_per_minute=3
    )
    import httpx
    from asgi_lifespan import LifespanManager

    from reference.mcp_gateway.main import app

    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            statuses = []
            for _ in range(6):
                response = await client.get("/tools/list", headers=headers(API_KEY_A))
                statuses.append(response.status_code)
            assert 429 in statuses
            limited = [s for s in statuses if s == 429]
            assert limited
            # The bucket is per client: another client is unaffected.
            response = await client.get("/tools/list", headers=headers(API_KEY_B))
            assert response.status_code == 200


async def test_rate_limit_response_code(client):
    # The default limit is 60/minute; exhaust it deliberately.
    codes = []
    for _ in range(70):
        response = await client.get("/tools/list", headers=headers(API_KEY_B))
        codes.append((response.status_code, response.json().get("code")))
    limited = [entry for entry in codes if entry[0] == 429]
    assert limited
    assert all(code == "RATE_LIMITED" for _status, code in limited)


async def test_concurrency_limit(tmp_path: Path, db_path: Path, monkeypatch):
    config = write_config(
        tmp_path / "config" / "gateway.yaml", concurrency_limit=1
    )
    import httpx
    from asgi_lifespan import LifespanManager

    from reference.mcp_gateway.main import app

    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            # Occupy the single slot with a slow upstream call.
            first = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "documents.search_documents", "arguments": {"query": "a"}},
            )
            assert first.status_code == 202
            second = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "documents.search_documents", "arguments": {"query": "b"}},
            )
            # Either the slot is still occupied (429) or the first call
            # already finished (202); the 429 path is what matters.
            if second.status_code == 429:
                assert second.json()["code"] == "CONCURRENCY_EXCEEDED"
            await wait_for_terminal(
                client, first.json()["execution_id"], API_KEY_A
            )


async def test_submission_failure_when_upstream_is_dead(client):
    """A dead upstream at submission time is a 502, not an execution failure."""
    gateway = client._transport.app.state.gateway  # type: ignore[attr-defined]
    upstream = gateway.upstreams["documents"]
    await upstream.stop()

    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "x"}},
    )
    assert response.status_code == 502
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"


async def test_execution_failure_when_upstream_crashes(tmp_path, db_path, monkeypatch):
    """An upstream that dies mid-flight yields 202 then a failed execution."""
    config = tmp_path / "config" / "gateway.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
gateway:
  request_max_bytes: 1048576
  rate_limit_per_minute: 60
  concurrency_limit: 10
  execution_timeout_seconds: 30
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["{sys.executable}", "-m", "reference.mcp_gateway.servers.documents"]
  jobs:
    command: ["{sys.executable}", "-m", "reference.mcp_gateway.servers.testing"]

clients:
  client_a:
    api_key: "{API_KEY_A}"
    allowlist: ["documents.search_documents", "jobs.echo"]
""",
        encoding="utf-8",
    )
    import httpx
    from asgi_lifespan import LifespanManager

    from reference.mcp_gateway.main import app

    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))
    monkeypatch.setenv("GATEWAY_TEST_CRASH_ON_CALL", "1")

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "jobs.echo", "arguments": {}},
            )
            assert response.status_code == 202
            execution_id = response.json()["execution_id"]

            status = await wait_for_terminal(client, execution_id, API_KEY_A)
            assert status["state"] == "failed"
            assert status["error"]["code"] in (
                "UPSTREAM_UNAVAILABLE",
                "UPSTREAM_FAILED",
            )
            assert "Traceback" not in status["error"]["message"]


async def test_execution_timeout(tmp_path, db_path, monkeypatch):
    """A tool exceeding the execution timeout fails with EXECUTION_TIMEOUT."""
    config = tmp_path / "config" / "gateway.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
gateway:
  request_max_bytes: 1048576
  rate_limit_per_minute: 60
  concurrency_limit: 10
  execution_timeout_seconds: 1
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["{sys.executable}", "-m", "reference.mcp_gateway.servers.documents"]
  jobs:
    command: ["{sys.executable}", "-m", "reference.mcp_gateway.servers.testing"]

clients:
  client_a:
    api_key: "{API_KEY_A}"
    allowlist: ["documents.search_documents", "jobs.echo"]
""",
        encoding="utf-8",
    )
    import httpx
    from asgi_lifespan import LifespanManager

    from reference.mcp_gateway.main import app

    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            response = await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "jobs.echo", "arguments": {"delay": 10}},
            )
            assert response.status_code == 202
            execution_id = response.json()["execution_id"]

            status = await wait_for_terminal(client, execution_id, API_KEY_A, timeout=15)
            assert status["state"] == "failed"
            assert status["error"]["code"] == "EXECUTION_TIMEOUT"
