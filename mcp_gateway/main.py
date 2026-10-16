"""FastAPI application: lifespan, routes, exception handlers.

Exposes the Secure MCP Gateway REST API:

    GET    /tools/list          - Tool discovery (filtered by allowlist)
    POST   /tools/call          - Async tool invocation (202 + execution_id)
    GET    /executions/{id}/status - Poll execution status
    POST   /executions/{id}/cancel - Cancel a pending/running execution
    GET    /executions/{id}/result - Retrieve execution result
"""

import json
import signal
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import resolve_client
from .config import DEFAULT_CONFIG_PATH, GatewayConfig
from .errors import (
    AlreadyTerminalError,
    ConcurrencyExceededError,
    ExecutionNotFoundError,
    ForbiddenExecutionError,
    ForbiddenToolError,
    GatewayError,
    gateway_error_handler,
    RateLimitedError,
    ResultUnavailableError,
    ToolNotFoundError,
    unhandled_exception_handler,
    UpstreamFailedError,
    UpstreamUnavailableError,
)
from .execution import ExecutionManager
from .gateway import Gateway
from .limits import ConcurrencyLimiter, RateLimiter
from .models import (
    CancelResponse,
    ErrorResponse,
    ExecutionResultResponse,
    ExecutionStatusResponse,
    ToolCallRequest,
    ToolCallResponse,
    ToolListResponse,
)
from .persistence import ExecutionStore

# Global instances (set up in lifespan)
_config: Optional[GatewayConfig] = None
_store: Optional[ExecutionStore] = None
_gateway: Optional[Gateway] = None
_rate_limiter: Optional[RateLimiter] = None
_concurrency_limiter: Optional[ConcurrencyLimiter] = None
_exec_manager: Optional[ExecutionManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _store, _gateway, _rate_limiter, _concurrency_limiter, _exec_manager

    _config = GatewayConfig.load(DEFAULT_CONFIG_PATH)
    _store = ExecutionStore()
    await _store.init_schema()

    _gateway = Gateway(_config, _store)
    await _gateway.start()

    rate_cap = _config.rate_limit_per_minute
    rate_refill = rate_cap / 60.0
    _rate_limiter = RateLimiter(rate_cap, rate_refill)
    _concurrency_limiter = ConcurrencyLimiter(_config.concurrency_limit)

    _exec_manager = ExecutionManager(
        _config, _store, _gateway, _rate_limiter, _concurrency_limiter
    )
    await _exec_manager.recover()

    # Reload configuration on SIGHUP (revocation without restart).
    def _reload_config(signum, frame):
        global _config
        try:
            _config = GatewayConfig.load(DEFAULT_CONFIG_PATH)
        except Exception:
            pass  # Keep existing config if reload fails

    signal.signal(signal.SIGHUP, _reload_config)

    yield

    await _gateway.stop()


app = FastAPI(title="Secure MCP Gateway", lifespan=lifespan)

app.add_exception_handler(GatewayError, gateway_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.middleware("http")
async def request_size_middleware(request: Request, call_next):
    """Reject request bodies larger than request_max_bytes with 413."""
    if _config is not None:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _config.request_max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "Request body exceeds limit", "code": "PAYLOAD_TOO_LARGE"},
                    )
            except ValueError:
                pass
    return await call_next(request)


async def _auth_client(request: Request) -> tuple[str, dict]:
    """Async auth dependency — resolves client_id and client config."""
    if _config is None:
        raise RuntimeError("Gateway not initialized")
    return await resolve_client(request, _config)


def _check_tool_permission(client_id: str, tool: str):
    """Raise ForbiddenToolError if the client is not allowed to call the tool."""
    if _config is None:
        raise RuntimeError("Gateway not initialized")
    if not _config.is_tool_allowed(client_id, tool):
        raise ForbiddenToolError(tool)


def _check_execution_permission(record: dict, client_id: str):
    """Raise ForbiddenExecutionError if client is not the owner or tool is revoked."""
    if record["owner_client_id"] != client_id:
        raise ForbiddenExecutionError()
    if _config is None:
        raise RuntimeError("Gateway not initialized")
    if not _config.is_tool_allowed(client_id, record["tool"]):
        raise ForbiddenExecutionError()


def _validate_arguments(arguments: dict, schema: Optional[dict[str, Any]]) -> None:
    """Validate arguments against a tool's JSON Schema.

    Raises InvalidArgsError with details on failure.
    """
    from .errors import InvalidArgsError

    if schema is None:
        return

    missing: list[str] = []
    type_mismatch: list[dict[str, Any]] = []
    unexpected_fields: list[str] = []
    enum_mismatch: list[dict[str, Any]] = []

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_props = schema.get("additionalProperties", True)

    # Check required fields
    for field in required:
        if field not in arguments:
            missing.append(field)

    # Check types, enums, and unexpected fields for provided fields
    for field, value in arguments.items():
        if field not in properties:
            if not additional_props:
                unexpected_fields.append(field)
            continue

        prop_schema = properties[field]
        expected_type = prop_schema.get("type")

        # Type check
        if expected_type == "string" and not isinstance(value, str):
            type_mismatch.append({field: expected_type})
        elif expected_type == "integer" and not isinstance(value, int):
            type_mismatch.append({field: expected_type})
        elif expected_type == "number" and not isinstance(value, (int, float)):
            type_mismatch.append({field: expected_type})
        elif expected_type == "boolean" and not isinstance(value, bool):
            type_mismatch.append({field: expected_type})
        elif expected_type == "object" and not isinstance(value, dict):
            type_mismatch.append({field: expected_type})
        elif expected_type == "array" and not isinstance(value, list):
            type_mismatch.append({field: expected_type})

        # Enum check
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            enum_mismatch.append({field: prop_schema["enum"]})

    details: dict[str, Any] = {}
    if missing:
        details["missing"] = missing
    if type_mismatch:
        details["type_mismatch"] = type_mismatch
    if unexpected_fields:
        details["unexpected_fields"] = unexpected_fields
    if enum_mismatch:
        details["enum_mismatch"] = enum_mismatch

    if details:
        raise InvalidArgsError(details)


@app.get("/tools/list", response_model=ToolListResponse)
async def list_tools(request: Request):
    """Return the tools the authenticated client is permitted to call."""
    client_id, client_cfg = await _auth_client(request)
    allowed = list(client_cfg.get("allowlist", []))
    return ToolListResponse(tools=allowed)


@app.post("/tools/call", response_model=ToolCallResponse, status_code=202)
async def call_tool(request: Request, body: ToolCallRequest):
    """Submit a tool call for async execution."""
    client_id, _ = await _auth_client(request)
    tool = body.tool

    # Permission check
    _check_tool_permission(client_id, tool)

    # Resolve tool (existence + server availability)
    if _gateway is None:
        raise RuntimeError("Gateway not initialized")
    try:
        server, tool_name, _ = _gateway.resolve_tool(tool)
    except ToolNotFoundError:
        raise

    # Argument validation against tool schema
    schema = _gateway.get_tool_schema(tool)
    _validate_arguments(body.arguments, schema)

    execution_id, state = await _exec_manager.submit(
        client_id=client_id,
        tool=tool,
        server=server,
        arguments=body.arguments,
    )
    return ToolCallResponse(execution_id=execution_id, state=state)


@app.get("/executions/{execution_id}/status", response_model=ExecutionStatusResponse)
async def execution_status(execution_id: str, request: Request):
    """Poll the status of an execution."""
    client_id, _ = await _auth_client(request)

    if _exec_manager is None:
        raise RuntimeError("Gateway not initialized")
    record = await _exec_manager.get_status(execution_id)
    _check_execution_permission(record, client_id)

    response = ExecutionStatusResponse(
        execution_id=execution_id,
        state=record["state"],
    )
    if record["state"] in ("failed", "cancelled"):
        response.error = {
            "code": record.get("error_code", "INTERNAL"),
            "message": record.get("error_message", "Unknown error"),
        }
    return response


@app.post("/executions/{execution_id}/cancel", response_model=CancelResponse)
async def cancel_execution(execution_id: str, request: Request):
    """Cancel a pending or running execution."""
    client_id, _ = await _auth_client(request)

    if _exec_manager is None:
        raise RuntimeError("Gateway not initialized")
    record = await _exec_manager.store.get(execution_id)
    if record is None:
        raise ExecutionNotFoundError(execution_id)
    _check_execution_permission(record, client_id)

    state = await _exec_manager.cancel(execution_id, client_id)
    return CancelResponse(execution_id=execution_id, state=state)


@app.get("/executions/{execution_id}/result", response_model=ExecutionResultResponse)
async def execution_result(execution_id: str, request: Request):
    """Retrieve the result of a completed or failed execution."""
    client_id, _ = await _auth_client(request)

    if _exec_manager is None:
        raise RuntimeError("Gateway not initialized")
    record = await _exec_manager.get_result(execution_id, client_id)

    response = ExecutionResultResponse(
        execution_id=execution_id,
        state=record["state"],
    )
    if record["state"] == "completed":
        response.result = (
            json.loads(record["result"]) if record["result"] else None
        )
    elif record["state"] == "failed":
        response.error = {
            "code": record.get("error_code", "INTERNAL"),
            "message": record.get("error_message", "Unknown error"),
        }
    return response
