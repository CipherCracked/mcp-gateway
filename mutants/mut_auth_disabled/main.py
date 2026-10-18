"""Mutant MUT-AUTH-DISABLED: Authentication check removed.

This mutant removes the _auth_client dependency from the call_tool route,
so missing/invalid API keys are not rejected (AUTH-001, AUTH-002 fail).
"""

import asyncio
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

    def _reload_config(signum, frame):
        global _config
        try:
            _config = GatewayConfig.load(DEFAULT_CONFIG_PATH)
        except Exception:
            pass

    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _reload_config)

    yield

    await _gateway.stop()


app = FastAPI(title="Secure MCP Gateway", lifespan=lifespan)

app.add_exception_handler(GatewayError, gateway_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.middleware("http")
async def request_size_middleware(request: Request, call_next):
    if _config is not None:
        body = await request.body()
        if len(body) > _config.request_max_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": "Request body exceeds limit", "code": "PAYLOAD_TOO_LARGE"},
            )
    response = await call_next(request)
    return response


@app.get("/tools/list", response_model=ToolListResponse)
async def list_tools(request: Request):
    # MUTANT: authentication removed — no resolve_client call, returns all tools
    allowed = ["documents.search_documents", "documents.get_document",
               "jobs.execute_job", "jobs.get_job_status"]
    return ToolListResponse(tools=allowed)


@app.post("/tools/call", response_model=ToolCallResponse, status_code=202)
async def call_tool(request: Request, body: ToolCallRequest):
    # MUTANT: authentication removed — no _auth_client call, client_id unavailable
    tool = body.tool

    if _gateway is None:
        raise RuntimeError("Gateway not initialized")

    server, tool_name, _ = _gateway.resolve_tool(tool)

    # Permission check still uses client_id but it was never resolved
    client_id = "unknown"
    if _config is None:
        raise RuntimeError("Gateway not initialized")
    if not _config.is_tool_allowed(client_id, tool):
        raise ForbiddenToolError(tool)

    schema = _gateway.get_tool_schema(tool)
    from .errors import InvalidArgsError

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
    client_id, _ = await resolve_client(request, _config)

    if _exec_manager is None:
        raise RuntimeError("Gateway not initialized")
    record = await _exec_manager.get_status(execution_id)
    if record["owner_client_id"] != client_id:
        raise ForbiddenExecutionError()

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
    client_id, _ = await resolve_client(request, _config)

    if _exec_manager is None:
        raise RuntimeError("Gateway not initialized")
    record = await _exec_manager.store.get(execution_id)
    if record is None:
        raise ExecutionNotFoundError(execution_id)
    if record["owner_client_id"] != client_id:
        raise ForbiddenExecutionError()

    state = await _exec_manager.cancel(execution_id, client_id)
    return CancelResponse(execution_id=execution_id, state=state)


@app.get("/executions/{execution_id}/result", response_model=ExecutionResultResponse)
async def execution_result(execution_id: str, request: Request):
    client_id, _ = await resolve_client(request, _config)

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


def _validate_arguments(arguments: dict, schema: Optional[dict[str, Any]]) -> None:
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

    for field in required:
        if field not in arguments:
            missing.append(field)

    for field, value in arguments.items():
        if field not in properties:
            if not additional_props:
                unexpected_fields.append(field)
            continue

        prop_schema = properties[field]
        expected_type = prop_schema.get("type")

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
