"""Tests that concurrency slots are released once executions finish."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from .conftest import API_KEY_A, headers, wait_for_terminal, write_config


async def _make_client(config: Path, db_path: Path, monkeypatch):
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))
    from reference.mcp_gateway.main import app

    return app


async def test_slots_are_released_after_completion(tmp_path: Path, db_path: Path, monkeypatch):
    """A client may submit again once its previous execution finished."""
    config = write_config(tmp_path / "config" / "gateway.yaml", concurrency_limit=1)
    app = await _make_client(config, db_path, monkeypatch)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            for _ in range(4):
                response = await client.post(
                    "/tools/call",
                    headers=headers(API_KEY_A),
                    json={
                        "tool": "documents.search_documents",
                        "arguments": {"query": "gateway"},
                    },
                )
                assert response.status_code == 202, response.json()
                execution_id = response.json()["execution_id"]
                status = await wait_for_terminal(client, execution_id, API_KEY_A)
                assert status["state"] == "completed"


async def test_concurrency_limit_blocks_burst(tmp_path: Path, db_path: Path, monkeypatch):
    """More simultaneous submissions than the limit are rejected."""
    config = write_config(tmp_path / "config" / "gateway.yaml", concurrency_limit=2)
    app = await _make_client(config, db_path, monkeypatch)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/tools/call",
                        headers=headers(API_KEY_A),
                        json={
                            "tool": "documents.search_documents",
                            "arguments": {"query": f"q{index}"},
                        },
                    )
                    for index in range(12)
                ]
            )
            codes = [response.status_code for response in responses]
            assert 429 in codes, codes
            for response in responses:
                if response.status_code == 429:
                    assert response.json()["code"] == "CONCURRENCY_EXCEEDED"


async def test_concurrency_limit_is_per_client(tmp_path: Path, db_path: Path, monkeypatch):
    """One client exhausting its slots does not affect the others."""
    config = write_config(tmp_path / "config" / "gateway.yaml", concurrency_limit=1)
    app = await _make_client(config, db_path, monkeypatch)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as client:
            await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={
                    "tool": "documents.search_documents",
                    "arguments": {"query": "gateway"},
                },
            )
            # A different client is still admitted.
            response = await client.post(
                "/tools/call",
                headers=headers("test-key-b"),
                json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
            )
            assert response.status_code == 202
