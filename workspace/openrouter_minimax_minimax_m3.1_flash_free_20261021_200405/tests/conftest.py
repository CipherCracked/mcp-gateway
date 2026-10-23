"""Shared fixtures for the Secure MCP Gateway test suite."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

API_KEY_A = "test-key-a"
API_KEY_B = "test-key-b"
API_KEY_ADMIN = "test-key-admin"

CONFIG_TEMPLATE = """
gateway:
  host: "0.0.0.0"
  port: 8000
  request_max_bytes: {request_max_bytes}
  rate_limit_per_minute: {rate_limit_per_minute}
  concurrency_limit: {concurrency_limit}
  execution_timeout_seconds: {execution_timeout_seconds}
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["{python}", "-m", "reference.mcp_gateway.servers.documents"]
  jobs:
    command: ["{python}", "-m", "reference.mcp_gateway.servers.jobs"]

clients:
  client_a:
    api_key: "{key_a}"
    allowlist: ["documents.search_documents", "documents.get_document"]
  client_b:
    api_key: "{key_b}"
    allowlist: ["jobs.execute_job", "jobs.get_job_status"]
  client_admin:
    api_key: "{key_admin}"
    allowlist:
      - "documents.search_documents"
      - "documents.get_document"
      - "jobs.execute_job"
      - "jobs.get_job_status"
"""


def write_config(
    path: Path,
    *,
    request_max_bytes: int = 1048576,
    rate_limit_per_minute: int = 60,
    concurrency_limit: int = 10,
    execution_timeout_seconds: float = 30,
    key_a: str = API_KEY_A,
    key_b: str = API_KEY_B,
    key_admin: str = API_KEY_ADMIN,
    allowlist_a: list[str] | None = None,
) -> Path:
    """Write a gateway configuration file for the tests."""
    text = CONFIG_TEMPLATE.format(
        request_max_bytes=request_max_bytes,
        rate_limit_per_minute=rate_limit_per_minute,
        concurrency_limit=concurrency_limit,
        execution_timeout_seconds=execution_timeout_seconds,
        python=sys.executable,
        key_a=key_a,
        key_b=key_b,
        key_admin=key_admin,
    )
    if allowlist_a is not None:
        rendered = ", ".join(f'"{item}"' for item in allowlist_a)
        text = text.replace(
            'allowlist: ["documents.search_documents", "documents.get_document"]',
            f"allowlist: [{rendered}]",
            1,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path / "config" / "gateway.yaml")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "executions.db"


@pytest_asyncio.fixture
async def client(config_path: Path, db_path: Path, monkeypatch):
    """An HTTP client bound to a running gateway."""
    import httpx
    from asgi_lifespan import LifespanManager

    from reference.mcp_gateway.main import app

    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", timeout=30.0
        ) as http_client:
            yield http_client


def headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


async def wait_for_terminal(client, execution_id: str, api_key: str, timeout: float = 20.0):
    """Poll an execution until it reaches a terminal state."""
    deadline = asyncio.get_running_loop().time() + timeout
    payload: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(
            f"/executions/{execution_id}/status", headers=headers(api_key)
        )
        payload = response.json()
        if payload.get("state") in ("completed", "failed", "cancelled"):
            return payload
        await asyncio.sleep(0.05)
    return payload
