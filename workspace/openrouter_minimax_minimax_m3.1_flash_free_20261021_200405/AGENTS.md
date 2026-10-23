# Secure MCP Gateway — Repository Notes

## What this repository is

A FastAPI gateway that aggregates two upstream MCP servers (Documents, Jobs) and
exposes a custom REST API. Clients authenticate with `X-API-Key` and are
authorized per tool via allowlists. Tool calls execute asynchronously:
submit → `202` + `execution_id` → poll status → fetch result.

## Layout

```
reference/mcp_gateway/
├── main.py         FastAPI app, lifespan, routes, error handlers
├── config.py       YAML config loading/validation, client + server config
├── auth.py         API key extraction and client resolution
├── gateway.py      Core: routing, tool index, upstream lifecycle, reload
├── execution.py    ExecutionManager: the state machine
├── persistence.py  SQLite (aiosqlite) execution store
├── limits.py       Token bucket (rate limit) + per-client concurrency
├── validation.py   JSON Schema validation of tool arguments
├── errors.py       Error codes, status map, sanitized envelope
├── models.py       Pydantic request/response models
├── upstream/
│   ├── stdio.py    JSON-RPC 2.0 client over MCP stdio transport
│   └── mcp_compat.py  Bridges raw payloads to the official `mcp` SDK types
└── servers/        Fixture MCP servers (documents, jobs, testing)
```

## Key decisions

- **The gateway speaks raw JSON-RPC over stdio** (`upstream/stdio.py`) rather
  than the SDK's `stdio_client`. Reason: `notifications/cancelled` needs the
  JSON-RPC request id, which the SDK does not expose. The SDK *is* still used —
  `upstream/mcp_compat.py` normalises tool/result payloads through
  `mcp.types`, tolerating the v1 (`inputSchema`) / v2 (`input_schema`) split.
- **The client is verified against both SDK v1 and v2 servers.** A v1 server
  is available at `testdata/sdk_server_v1.py` (needs `mcp==1.30.0`).
- **Errors are sanitised centrally** in `errors.py`. `details` is only ever
  populated for `INVALID_ARGS`, and only with the four documented categories
  (`missing`, `type_mismatch`, `unexpected_fields`, `enum_mismatch`).
- **Ordering of checks in `POST /tools/call`** (each produces a distinct code):
  auth → payload size → rate limit → JSON parse → tool exists (404) →
  allowlist (403) → argument validation (400) → upstream alive (502) →
  concurrency (429) → submit (202).
- **Revocation is a SIGHUP reload**, not a restart. `reload_config()` swaps the
  config in place; the concurrency limiter is only replaced when the limit
  itself changed, so in-flight slots are not lost.
- **Allowlist order is preserved** so `GET /tools/list` is stable and matches
  the order documented in the task.
- **The gateway starts on demand** as well as via the lifespan, so a bare
  `TestClient(app)` (no lifespan) still works.

## Environment

- Python 3.12, FastAPI, uvicorn, `mcp`, aiosqlite, PyYAML, httpx,
  pytest + pytest-asyncio (asyncio mode `auto` via `pytest.ini`).
- Config is read from `/app/config/gateway.yaml`; override with
  `GATEWAY_CONFIG_PATH`. The DB path comes from `GATEWAY_DB_PATH`
  (default `/app/data/executions.db`).
- Upstream subprocesses get `PYTHONPATH` extended with the repository root so
  `python -m reference.mcp_gateway.servers.*` works from any cwd.

## Running the tests

```bash
PYTHONPATH=/workspace python3 -m pytest tests/ -q
```

`PYTHONPATH` is required because the package lives under `reference/`.

## Fixture servers

- `servers/documents.py` — `search_documents`, `get_document`
- `servers/jobs.py` — `execute_job`, `get_job_status`
- `servers/testing.py` — **test only**; `echo`/`fail` with a configurable delay.
  It is driven by `GATEWAY_TEST_TOOL_DELAY`, `GATEWAY_TEST_TOOL_FAIL` and
  `GATEWAY_TEST_CRASH_ON_CALL`. Use it instead of adding tools to
  `documents`/`jobs`, because exact tool-list assertions depend on those.

## Gotchas

- `GatewayError` is raised with string codes in some places and imported
  constants in others; keep them in sync with `errors.STATUS_BY_CODE`.
- The concurrency limiter is a plain counter, not an `asyncio.Semaphore`;
  admission must stay non-blocking (immediate 429, never a wait).
- `execution.py` releases the concurrency slot exactly once per execution,
  guarded by `_finished`.
