"""FastAPI application exposing the Secure MCP Gateway REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import authenticate
from .config import GatewayConfig, database_path, load_config
from .errors import (
    CONCURRENCY_EXCEEDED,
    FORBIDDEN_TOOL,
    INTERNAL,
    INVALID_ARGS,
    PAYLOAD_TOO_LARGE,
    RATE_LIMITED,
    TOOL_NOT_FOUND,
    GatewayError,
    ValidationError,
)
from .gateway import SecureMcpGateway
from .models import (
    ExecutionCancelResponse,
    ExecutionError,
    ExecutionResultResponse,
    ExecutionStatusResponse,
    ToolCallAccepted,
    ToolListResponse,
)
from .persistence import ExecutionStore
from .validation import validate_arguments

logging.basicConfig(
    level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_gateway")


def _error_response(error: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.to_payload())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop the gateway together with the application."""
    config: GatewayConfig = load_config()
    store = ExecutionStore(database_path())
    gateway = SecureMcpGateway(config, store)
    app.state.gateway = gateway
    app.state.config = config

    try:
        await gateway.start()
    except Exception:
        logger.exception("Gateway startup failed")
        raise

    _install_signal_handlers(app)
    logger.info("Secure MCP Gateway ready")
    try:
        yield
    finally:
        await gateway.stop()


def _install_signal_handlers(app: FastAPI) -> None:
    """Reload the configuration on SIGHUP."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - defensive
        return

    def _on_sighup() -> None:
        logger.info("Received SIGHUP, reloading configuration")
        gateway: SecureMcpGateway | None = getattr(app.state, "gateway", None)
        if gateway is None:  # pragma: no cover - defensive
            return
        asyncio.create_task(gateway.reload_config())

    try:
        if hasattr(signal, "SIGHUP"):  # SIGHUP is not available on Windows
            loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, RuntimeError):  # pragma: no cover - platform specific
        pass


app = FastAPI(
    title="Secure MCP Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------
@app.exception_handler(GatewayError)
async def gateway_error_handler(_request: Request, exc: GatewayError) -> JSONResponse:
    return _error_response(exc)


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled gateway error: %s", exc)
    return JSONResponse(
        status_code=500, content={"error": "Internal gateway error", "code": INTERNAL}
    )


_startup_lock = asyncio.Lock()


async def _ensure_started(app: FastAPI) -> SecureMcpGateway:
    """Start the gateway on demand.

    The normal path is the lifespan handler.  This fallback covers the case
    where the application is used without running the lifespan (for example
    a bare ``TestClient``), so the API never fails with an internal error
    just because the startup hook did not run.
    """
    gateway = getattr(app.state, "gateway", None)
    if gateway is not None:
        return gateway
    async with _startup_lock:
        gateway = getattr(app.state, "gateway", None)
        if gateway is not None:
            return gateway
        config: GatewayConfig = load_config()
        store = ExecutionStore(database_path())
        gateway = SecureMcpGateway(config, store)
        await gateway.start()
        app.state.gateway = gateway
        app.state.config = config
        logger.info("Secure MCP Gateway started on demand")
        return gateway


async def _gateway(request: Request) -> SecureMcpGateway:
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None:
        return gateway
    try:
        return await _ensure_started(request.app)
    except GatewayError:
        raise
    except Exception as exc:
        logger.exception("Could not start the gateway")
        raise GatewayError(INTERNAL, "Internal gateway error", status_code=500) from exc


async def _enforce_rate_limit(request: Request, client_id: str) -> None:
    """Consume one rate-limit token for the client."""
    gateway = await _gateway(request)
    if not await gateway.rate_limiter.allow(client_id):
        raise GatewayError(RATE_LIMITED, "Rate limit exceeded", status_code=429)


async def _read_body(request: Request, max_bytes: int) -> bytes:
    """Read the request body, enforcing the configured size limit."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise GatewayError(
                    PAYLOAD_TOO_LARGE, "Request payload too large", status_code=413
                )
        except ValueError:
            pass
    body = await request.body()
    if len(body) > max_bytes:
        raise GatewayError(
            PAYLOAD_TOO_LARGE, "Request payload too large", status_code=413
        )
    return body


@app.middleware("http")
async def enforce_gateway_timeout(request: Request, call_next):
    """Bound the overall time a request may take.

    The gateway timeout is the outer bound on any request; individual
    operations (such as the tool execution timeout) finish well before it.
    """
    gateway = getattr(request.app.state, "gateway", None)
    timeout = (
        gateway.config.gateway.gateway_timeout_seconds
        if gateway is not None
        else 35.0
    )
    try:
        return await asyncio.wait_for(call_next(request), timeout=timeout)
    except TimeoutError:
        logger.warning("Request %s %s exceeded the gateway timeout", request.method, request.url.path)
        return JSONResponse(
            status_code=504,
            content={"error": "Gateway timeout", "code": "INTERNAL"},
        )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@app.get("/tools/list", response_model=ToolListResponse)
async def list_tools(request: Request) -> ToolListResponse:
    """Return the tools the authenticated client is permitted to call."""
    client = await authenticate(request)
    await _enforce_rate_limit(request, client.client_id)
    gateway = await _gateway(request)
    return ToolListResponse(tools=gateway.permitted_tools(client.client_id))


@app.post("/tools/call", status_code=202, response_model=ToolCallAccepted)
async def call_tool(request: Request) -> ToolCallAccepted:
    """Submit a tool call for asynchronous execution."""
    client = await authenticate(request)
    gateway = await _gateway(request)

    # The payload size limit is a hard request-level constraint and is
    # checked before anything that consumes a rate-limit token.
    body = await _read_body(request, gateway.config.gateway.request_max_bytes)

    await _enforce_rate_limit(request, client.client_id)

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        raise GatewayError(
            INVALID_ARGS,
            "Invalid tool arguments",
            status_code=400,
            details={"type_mismatch": ["arguments"]},
        ) from None
    if not isinstance(payload, dict):
        raise GatewayError(
            INVALID_ARGS,
            "Invalid tool arguments",
            status_code=400,
            details={"type_mismatch": ["arguments"]},
        )

    tool_name = payload.get("tool")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise GatewayError(TOOL_NOT_FOUND, "Tool not found", status_code=404)

    arguments = payload.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise GatewayError(
            INVALID_ARGS,
            "Invalid tool arguments",
            status_code=400,
            details={"type_mismatch": ["arguments"]},
        )

    # The tool must exist before the allowlist is consulted, so that a
    # non-existent tool is reported as not found rather than forbidden.
    definition = gateway.resolve_tool(tool_name)

    if not gateway.is_permitted(client.client_id, tool_name):
        raise GatewayError(
            FORBIDDEN_TOOL, "Tool is not permitted for this client", status_code=403
        )

    errors = validate_arguments(definition.input_schema, arguments)
    if errors:
        raise ValidationError(errors)

    # A dead upstream is a submission failure (502), not an execution
    # failure: the job was never accepted.
    gateway.assert_upstream_available(definition.server)

    # Admission control: at most `concurrency_limit` executions per client.
    if not await gateway.concurrency_limiter.try_acquire(client.client_id):
        raise GatewayError(
            CONCURRENCY_EXCEEDED, "Too many concurrent executions", status_code=429
        )

    try:
        execution_id = await gateway.execution_manager.submit(
            client_id=client.client_id,
            server_name=definition.server,
            tool_name=definition.name,
            qualified_name=tool_name,
            arguments=arguments,
        )
    except Exception:
        await gateway.concurrency_limiter.release(client.client_id)
        raise
    return ToolCallAccepted(execution_id=execution_id, state="pending")


@app.get("/executions", response_model=list[ExecutionStatusResponse])
async def list_executions(request: Request) -> list[ExecutionStatusResponse]:
    """List the executions owned by the authenticated client."""
    client = await authenticate(request)
    await _enforce_rate_limit(request, client.client_id)
    gateway = await _gateway(request)
    records = await gateway.store.list_for_client(client.client_id)
    responses = []
    for record in records:
        item = ExecutionStatusResponse(
            execution_id=record.execution_id, state=record.state
        )
        if record.state in ("failed", "cancelled"):
            item.error = ExecutionError.from_payload(record.error_payload())
        responses.append(item)
    return responses


@app.get(
    "/executions/{execution_id}/status",
    response_model=ExecutionStatusResponse,
    response_model_exclude_none=True,
)
async def execution_status(request: Request, execution_id: str) -> ExecutionStatusResponse:
    """Poll the status of an execution."""
    client = await authenticate(request)
    await _enforce_rate_limit(request, client.client_id)
    gateway = await _gateway(request)
    record = await gateway.authorize_execution(client.client_id, execution_id)
    response = ExecutionStatusResponse(execution_id=record.execution_id, state=record.state)
    if record.state in ("failed", "cancelled"):
        response.error = ExecutionError.from_payload(record.error_payload())
    return response


@app.post(
    "/executions/{execution_id}/cancel",
    response_model=ExecutionCancelResponse,
    response_model_exclude_none=True,
)
async def cancel_execution(request: Request, execution_id: str) -> ExecutionCancelResponse:
    """Cancel a pending or running execution."""
    client = await authenticate(request)
    await _enforce_rate_limit(request, client.client_id)
    gateway = await _gateway(request)
    record = await gateway.cancel_execution(client.client_id, execution_id)
    return ExecutionCancelResponse(execution_id=record.execution_id, state=record.state)


@app.get(
    "/executions/{execution_id}/result",
    response_model=ExecutionResultResponse,
    response_model_exclude_none=True,
)
async def execution_result(request: Request, execution_id: str) -> ExecutionResultResponse:
    """Retrieve the result of a completed or failed execution."""
    client = await authenticate(request)
    await _enforce_rate_limit(request, client.client_id)
    gateway = await _gateway(request)
    record = await gateway.get_result(client.client_id, execution_id)
    payload = ExecutionResultResponse(
        execution_id=record.execution_id, state=record.state
    )
    if record.state == "completed":
        payload.result = record.result if record.result is not None else {}
    else:
        payload.error = ExecutionError.from_payload(record.error_payload())
    return payload


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """A lightweight health probe (unauthenticated)."""
    gateway = getattr(request.app.state, "gateway", None)
    upstreams: dict[str, bool] = {}
    if gateway is not None:
        upstreams = {name: client.alive for name, client in gateway.upstreams.items()}
    return {"status": "ok", "upstreams": upstreams}


@app.post("/_test/reset_limiters")
async def _test_reset_limiters(request: Request):
    """Test-only endpoint: reset rate limiter and concurrency limiter state.

    The gateway runs as a subprocess, so the conftest fixture cannot directly
    access its Python objects. We reset via the gateway's test-only HTTP
    endpoint (POST /_test/reset_limiters) so each test starts with a fresh
    token bucket and empty semaphores.
    """
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None:
        await gateway.rate_limiter.reset()
        await gateway.concurrency_limiter.reset()
    return {"ok": True}


@app.post("/_test/raise_error")
async def _test_raise_error():
    """Test-only endpoint: raises an unhandled exception to exercise the
    catch-all exception handler. Used to verify stack traces are not leaked."""
    raise ValueError("test unhandled exception")
