# Secure MCP Gateway — Implementation Task

## Overview

Build a **Secure MCP Gateway** that aggregates two upstream MCP servers (Documents, Jobs) and exposes a custom REST API to clients with:

- **Authentication** via `X-API-Key` header
- **Fine-grained authorization** via per-client allowlists
- **Tool namespacing** (`{server}.{tool}`) for disambiguation
- **Async execution** with polling (submit → 202 + execution_id → poll status → get result)
- **Permission enforcement at every operation** (invocation, status, cancel, result)
- **Revocation semantics** (config reload blocks new calls, hides existing results)
- **Rate limiting** (token bucket), **concurrency limits** (semaphore), **timeouts**
- **Request size limits** (1 MB default)
- **Persistent execution state** in SQLite (survives gateway restart)
- **Error sanitization** (no stack traces, internal paths, or credentials leaked)
- **Two failure modes**: submission failure (502) vs execution failure (202 then failed)

The gateway communicates with upstream MCP servers via **stdio transport** (subprocess). Clients use **custom REST endpoints**, not the MCP protocol.

---

## Uploaded Files

```
/workspace/
├── task/
│   ├── instruction.md          # This file — the task specification
│   └── public/
│       └── gateway.yaml.example  # Example config (reference only)
└── ...
```

You are working in `/workspace/`. No source code exists yet — you must create the gateway package from scratch.

---

## Task Description

Implement a Secure MCP Gateway from scratch. The gateway:

1. **Spawns two upstream MCP servers** as child subprocesses via stdio transport: a Documents server and a Jobs server.
2. **Exposes a REST API** on HTTP that clients call to discover tools, invoke them asynchronously, poll execution status, cancel executions, and retrieve results.
3. **Enforces authentication and authorization** on every protected operation — not just at tool discovery time.
4. **Persists execution state** in SQLite so terminal states survive gateway restart.
5. **Sanitizes all error responses** so no stack traces, internal paths, or credentials leak to clients.

You must implement the gateway core, the two fixture MCP servers, configuration loading, auth, permission checks, the async execution engine, persistence, and error handling.

---

## Constraints

- **Language**: Python 3.11+
- **Framework**: FastAPI for the REST API, uvicorn as ASGI server
- **MCP SDK**: `mcp` package for upstream client-server communication via stdio
- **Persistence**: `aiosqlite` for async SQLite access
- **Config**: PyYAML for reading `/app/config/gateway.yaml`
- **No external services**: No Redis, no PostgreSQL, no OAuth server, no internet access. Everything runs in a single Docker container.
- **Single process**: The gateway runs as one process; concurrency is handled via asyncio (Tasks, Semaphores, `wait_for`).
- **Implement from scratch**: Do not copy an existing MCP gateway implementation. Write the gateway logic yourself.
- **Do not use fixture control ports**: The fixture servers expose test-only HTTP control endpoints; your gateway must not depend on or call them.

---

## Required Endpoints

All endpoints require the `X-API-Key` header unless otherwise noted.

### Tool Discovery

**GET /tools/list**

Returns the tools the authenticated client is permitted to call.

- **Response** (200): `{ "tools": ["documents.search_documents", "documents.get_document", "jobs.execute_job", "jobs.get_job_status"] }`
- The list is filtered by the client's allowlist — only permitted tools are returned.
- Requires authentication (401 if missing/invalid).

---

### Tool Invocation

**POST /tools/call**

Submit a tool call for async execution.

- **Request**:
  ```json
  {
    "tool": "documents.search_documents",
    "arguments": { "query": "example", "limit": 10 }
  }
  ```
- **Success Response** (202 Accepted):
  ```json
  { "execution_id": "uuid-v4", "state": "pending" }
  ```

**Validation Rules** (against the tool's JSON Schema from MCP `tools/list`):
- Missing required fields → 400 with `details.missing`
- Type mismatches → 400 with `details.type_mismatch`
- Unexpected fields (`additionalProperties: false`) → 400 with `details.unexpected_fields`
- Enum mismatches → 400 with `details.enum_mismatch`

**Namespacing**: Tool name format is `{server}.{tool}` (e.g., `documents.search_documents`).
- Split on **first dot only**: `server = name[:idx]`, `tool = name[idx+1:]`
- Server names: `[a-z][a-z0-9-]*`
- Tool names: `[a-zA-Z][a-zA-Z0-9_]*`
- The gateway routes to the correct upstream server based on the resolved server name.

**Error Responses**:

| Code | HTTP | Scenario |
|------|------|----------|
| `AUTH_MISSING` | 401 | No `X-API-Key` header |
| `AUTH_INVALID` | 401 | Invalid/empty API key |
| `FORBIDDEN_TOOL` | 403 | Tool exists but not in client's allowlist |
| `TOOL_NOT_FOUND` | 404 | Tool name doesn't exist on any server |
| `INVALID_ARGS` | 400 | Argument validation failed (`details` populated) |
| `PAYLOAD_TOO_LARGE` | 413 | Request body exceeds `request_max_bytes` |
| `RATE_LIMITED` | 429 | Token bucket exhausted |
| `CONCURRENCY_EXCEEDED` | 429 | Per-client semaphore exhausted |
| `UPSTREAM_UNAVAILABLE` | 502 | Upstream process dead/crashed before submission |
| `INTERNAL` | 500 | Gateway internal error (sanitized) |

---

### Execution Status Polling

**GET /executions/{id}/status**

Poll the status of an execution.

- **Success** (200):
  ```json
  { "execution_id": "uuid", "state": "pending|running|completed|failed|cancelled", "error?": { "code": "ERROR_CODE", "message": "human_readable" } }
  ```
- Returns metadata only for `pending`/`running` states.
- For `failed`/`cancelled` states, includes an `error` object with sanitized error code and message.
- Requires ownership — 403 if the caller is not the execution owner or if the tool permission was revoked.
- 404 if the execution ID is unknown.

---

### Execution Cancellation

**POST /executions/{id}/cancel**

Cancel a pending or running execution.

- **Success** (200): `{ "execution_id": "uuid", "state": "cancelled" }`
- Sends MCP `notifications/cancelled` to the upstream server if supported.
- Requires ownership + current permission for the tool — 403 if not owner or permission revoked.
- 404 if execution ID unknown.
- 409 if already terminal.

---

### Execution Result Retrieval

**GET /executions/{id}/result**

Retrieve the result of a completed or failed execution.

- **Success** (200) when state == `completed`:
  ```json
  { "execution_id": "uuid", "state": "completed", "result": { ... } }
  ```
- **Success** (200) when state == `failed`:
  ```json
  { "execution_id": "uuid", "state": "failed", "error": { "code": "ERROR_CODE", "message": "human_readable" } }
  ```
- Available only when state is `completed` or `failed`.
- Returns 409 for `pending`/`running`/`cancelled` states.
- Requires ownership + current permission for the tool — 403 if not owner or permission revoked.
- 404 if execution ID unknown.

---

## Execution State Machine

```
pending → running → completed
              ↓
              → failed (UPSTREAM_FAILED | UPSTREAM_UNAVAILABLE | EXECUTION_TIMEOUT | EXECUTION_INTERRUPTED)
              ↓
              → cancelled (client-requested)
```

**Terminal states**: `completed`, `failed`, `cancelled` — no transitions out.

**Distinct terminal states** (not interchangeable):
- Client cancellation → `cancelled`
- Execution timeout → `failed` with `EXECUTION_TIMEOUT`
- Upstream crash → `failed` with `UPSTREAM_UNAVAILABLE`
- Upstream error → `failed` with `UPSTREAM_FAILED`
- Gateway restart (non-terminal execution) → `failed` with `EXECUTION_INTERRUPTED`

---

## Two Failure Modes

| Failure Mode | When | HTTP Response |
|--------------|------|---------------|
| **Submission failure** | Gateway knows upstream is unavailable *before* accepting the job (process dead, not started) | **502** `{"error": "upstream_unavailable", "code": "UPSTREAM_UNAVAILABLE"}` |
| **Execution failure** | Job accepted (202), upstream fails *during* execution | **202** on submit; status → `failed` with sanitized error |

---

## Error Sanitization

**Never expose in client responses**:
- Stack traces
- Internal file paths
- Credentials (API keys, tokens)
- Database schemas
- Upstream server implementation details
- Fixture control port information

**All errors** follow this format:
```json
{ "error": "human_readable_message", "code": "MACHINE_CODE", "details?": {} }
```
*`details` is only present for `INVALID_ARGS` validation errors.*

**Error Code Catalog** (exhaustive):

| Code | HTTP Status | Scenario |
|------|-------------|----------|
| `AUTH_MISSING` | 401 | No `X-API-Key` header provided |
| `AUTH_INVALID` | 401 | Invalid/empty API key |
| `FORBIDDEN_TOOL` | 403 | Tool exists but not in client's allowlist |
| `TOOL_NOT_FOUND` | 404 | Tool name doesn't exist on any server |
| `FORBIDDEN_EXECUTION` | 403 | Client not owner of execution / permission revoked |
| `NOT_FOUND` | 404 | Execution ID doesn't exist |
| `INVALID_ARGS` | 400 | Argument validation failed (`details` populated) |
| `PAYLOAD_TOO_LARGE` | 413 | Request body exceeds `request_max_bytes` |
| `RATE_LIMITED` | 429 | Token bucket exhausted |
| `CONCURRENCY_EXCEEDED` | 429 | Per-client semaphore exhausted (admission check, no wait) |
| `EXECUTION_TIMEOUT` | 202→failed | Execution exceeded `execution_timeout_seconds` |
| `UPSTREAM_UNAVAILABLE` | 502 / 202→failed | Upstream process dead/crashed |
| `UPSTREAM_FAILED` | 202→failed | Upstream returned error (sanitized) |
| `EXECUTION_INTERRUPTED` | 202→failed | Gateway restarted while execution non-terminal |
| `RESULT_UNAVAILABLE` | 409 | Result requested for pending/running/cancelled execution |
| `INTERNAL` | 500 | Gateway internal error (sanitized) |

Upstream exceptions are caught, logged internally (full detail), and returned sanitized:
```json
{ "error": "upstream_error", "code": "UPSTREAM_FAILED" }
```

---

## Permission Enforcement

Authorization is checked at **every** protected operation, not just at discovery time.

| Operation | Auth Required? | Permission Checked? |
|-----------|----------------|---------------------|
| `GET /tools/list` | **Yes** | **Yes** (filtered discovery — returns only permitted tools) |
| `POST /tools/call` | **Yes** | Yes (tool in allowlist) |
| `GET /executions/{id}/status` | **Yes** | Yes (owner + tool currently permitted) |
| `POST /executions/{id}/cancel` | **Yes** | Yes (owner + tool currently permitted) |
| `GET /executions/{id}/result` | **Yes** | Yes (owner + tool currently permitted) |

A client that was permitted when it submitted an execution may lose access to the result if its permission is revoked before it polls.

---

## Revocation Semantics

**Trigger**: Config reload via **SIGHUP** — not gateway restart.

| Scenario | Behavior |
|----------|----------|
| New tool invocations | Immediate 403 if permission revoked |
| In-flight executions (`pending`/`running`) | Allowed to complete upstream |
| Results of in-flight executions | Inaccessible to revoked client (403 on poll/result) |
| Other clients | Unaffected |
| Stale client cache | Ignored (stateless per-request check) |

---

## Limits (Configurable via `/app/config/gateway.yaml`)

| Limit | Default | Enforcement |
|-------|---------|-------------|
| Request body size | 1 MB (1048576 bytes) | 413 `PAYLOAD_TOO_LARGE` |
| Rate limit | 60 req/min (token bucket) | 429 `RATE_LIMITED` |
| Concurrency | 10 simultaneous executions/client | 429 `CONCURRENCY_EXCEEDED` |
| Tool execution timeout | 30 seconds | Marks `failed` with `EXECUTION_TIMEOUT` |
| Gateway timeout | 35 seconds | Overall request timeout |

**Rate Limiting**: Token bucket per client.
- Capacity = `rate_limit_per_minute`
- Refill rate = `rate_limit_per_minute / 60` tokens per second (continuous)
- In-memory state (resets on restart — acceptable)

**Concurrency**: Asyncio Semaphore per client_id.
- Acquire on submit (non-blocking check → immediate 429 if full)
- Release on completion/cancellation

---

## Persistence & Restart Recovery

**Storage**: SQLite at `/app/data/executions.db` (single `executions` table with JSON columns).

**On Gateway Restart**:
- Terminal states (`completed`, `failed`, `cancelled`) → recovered faithfully from SQLite
- Non-terminal states (`pending`, `running`) → marked `failed` with `EXECUTION_INTERRUPTED`
- Authorization checks still apply after restart (owner + current permission)

---

## Upstream MCP Servers (Fixtures)

The gateway launches MCP servers as child subprocesses via stdio. All components run in a single Docker container.

### Documents Server (`python -m mcp_gateway.servers.documents`)
- **MCP Transport**: stdio (spawned by gateway)
- **Control Port**: `localhost:9001` (test-only — not part of the gateway API)
- Tools:
  - `search_documents(query: string, limit?: integer) → {documents: [{id, title, snippet}]}`
  - `get_document(id: string) → {id, title, content}`

### Jobs Server (`python -m mcp_gateway.servers.jobs`)
- **MCP Transport**: stdio (spawned by gateway)
- **Control Port**: `localhost:9002` (test-only — not part of the gateway API)
- Tools:
  - `execute_job(payload: object, priority?: "low"|"normal"|"high") → {job_id: string}`
  - `get_job_status(job_id: string) → {job_id, state, result?, error?}`

Test-only control endpoints (`/_test/fail_next`, `/_test/delay`, `/_test/crash`) exist for external test harnesses. Your gateway must not depend on or call these endpoints.

---

## Configuration File

**Path**: `/app/config/gateway.yaml` — read by the gateway at runtime.

```yaml
gateway:
  host: "0.0.0.0"
  port: 8000
  request_max_bytes: 1048576
  rate_limit_per_minute: 60
  concurrency_limit: 10
  execution_timeout_seconds: 30
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["python", "-m", "mcp_gateway.servers.documents"]
  jobs:
    command: ["python", "-m", "mcp_gateway.servers.jobs"]

clients:
  client_a:
    api_key: "key-a-123"
    allowlist: ["documents.search_documents", "documents.get_document"]
  client_b:
    api_key: "key-b-456"
    allowlist: ["jobs.execute_job", "jobs.get_job_status"]
  client_admin:
    api_key: "key-admin-789"
    allowlist: ["documents.search_documents", "documents.get_document", "jobs.execute_job", "jobs.get_job_status"]
```

---

## Project Structure

```
mcp_gateway/
├── __init__.py
├── main.py                 # FastAPI app, lifespan, routes
├── config.py               # YAML config loading, validation
├── auth.py                 # API key validation, client resolution
├── gateway.py              # Core gateway logic (routing, upstream clients)
├── execution.py            # ExecutionManager (state machine, persistence)
├── persistence.py          # SQLite operations (aiosqlite)
├── errors.py               # Custom exception classes + handlers
├── limits.py               # Token bucket, semaphore management
├── servers/
│   ├── __init__.py
│   ├── documents.py        # Documents MCP server (stdio + control port)
│   └── jobs.py             # Jobs MCP server (stdio + control port)
└── models.py               # Pydantic models for requests/responses
```

---

## Test Command

Run the test suite inside the Docker container:

```
pytest <test_dir> -v
```

---

## Git Commit

Make a local git commit for each agent step for all code changes. If there is no change in the current step, do not make a commit.
