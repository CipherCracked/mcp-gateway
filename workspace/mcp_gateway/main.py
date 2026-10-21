"""Main FastAPI application for the Secure MCP Gateway."""

import asyncio
import logging
import signal
from typing import Dict, Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

# Import from the package
from mcp_gateway.config import load_config
from mcp_gateway.gateway import Gateway
from mcp_gateway.errors import (
    GatewayError, AuthMissingError, AuthInvalidError, ForbiddenToolError,
    ToolNotFoundError, ForbiddenExecutionError, InvalidArgsError,
    PayloadTooLargeError, RateLimitedError, ConcurrencyExceededError,
    ExecutionNotFoundError, ExecutionTimeoutError, UpstreamUnavailableError,
    UpstreamFailedError, ResultUnavailableError, ExecutionCancelledError,
    GatewayInternalError
)
from mcp_gateway.models import CallRequest, CallResponse, StatusResponse, CancelResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Secure MCP Gateway",
    description="Gateway aggregating MCP servers with auth, authorization, and async execution",
    version="1.0.0",
)

# Global gateway instance
gateway_instance: Optional[Gateway] = None


# Define _authenticate BEFORE the route handlers
def _authenticate(api_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Authenticate the client using the X-API-Key header."""
    if api_key is None or api_key == "":
        raise AuthMissingError()
    
    if gateway_instance is None or api_key not in gateway_instance.clients:
        raise AuthInvalidError()
    
    return gateway_instance.clients[api_key]


@app.on_event("startup")
async def startup_event():
    """Initialize the gateway on startup."""
    global gateway_instance
    
    config = load_config("/app/config/gateway.yaml")
    gateway_instance = Gateway(config)
    await gateway_instance._initialize_upstream_servers()
    
    logger.info("Gateway started successfully")
    logger.info("Configured %d clients", len(gateway_instance.clients))
    logger.info("Available servers: %s", list(gateway_instance.server_tools.keys()))


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    global gateway_instance
    if gateway_instance:
        logger.info("Gateway shutting down")
        gateway_instance = None


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError):
    response: Dict[str, Any] = {
        "error": exc.message,
        "code": exc.code,
    }
    if exc.details:
        response["details"] = exc.details
    return JSONResponse(
        status_code=exc.http_status,
        content=response,
    )


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add timeout middleware."""
    try:
        response = await asyncio.wait_for(
            call_next(request),
            timeout=35.0
        )
        return response
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=500,
            content={"error": "Request timeout", "code": "INTERNAL"},
        )


@app.get("/tools/list")
async def list_tools(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """List available tools for the authenticated client."""
    try:
        client_config = _authenticate(x_api_key)
    except AuthMissingError:
        raise
    except AuthInvalidError:
        raise
    
    permitted_tools = []
    for tool_name in client_config["allowlist"]:
        found = False
        for server_name, tools in gateway_instance.server_tools.items():
            if any(t["name"] == tool_name for t in tools):
                found = True
                break
        if found:
            permitted_tools.append(tool_name)
    
    return {"tools": permitted_tools}


@app.post("/tools/call")
async def call_tool(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    body: Dict[str, Any] = ...,
):
    """Submit a tool call for async execution."""
    try:
        client_config = _authenticate(x_api_key)
    except AuthMissingError:
        raise
    except AuthInvalidError:
        raise
    
    # Check payload size
    body_bytes = body if isinstance(body, bytes) else str(body).encode("utf-8")
    max_bytes = gateway_instance.gw_config.get("request_max_bytes", 1048576)
    if len(body_bytes) > max_bytes:
        raise PayloadTooLargeError(max_bytes)
    
    # Parse the call request
    try:
        call_request = CallRequest(**body)
    except Exception:
        raise GatewayInternalError("Invalid request format")
    
    tool_name = call_request.tool
    arguments = call_request.arguments
    
    # Submit execution
    client_id = x_api_key
    execution_id, status_code, response_data = await gateway_instance.submit_execution(
        client_id=client_id,
        tool_name=tool_name,
        arguments=arguments,
        client_config=client_config,
    )
    
    return JSONResponse(
        status_code=status_code,
        content=response_data,
    )


@app.get("/executions/{execution_id}/status")
async def execution_status(
    execution_id: str,
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Poll the status of an execution."""
    try:
        client_config = _authenticate(x_api_key)
    except AuthMissingError:
        raise
    except AuthInvalidError:
        raise
    
    execution = await gateway_instance.get_execution(execution_id)
    if execution is None:
        raise ExecutionNotFoundError(execution_id)
    
    # Check ownership
    if execution["client_id"] != x_api_key:
        raise ForbiddenExecutionError()
    
    # Check current permission for the tool
    if not gateway_instance._is_tool_allowed(client_config, execution["tool_name"]):
        raise ForbiddenExecutionError()
    
    state = execution["state"]
    error = execution.get("error")
    result = execution.get("result")
    
    response: Dict[str, Any] = {
        "execution_id": execution_id,
        "state": state,
    }
    
    if state in ("failed", "cancelled") and error is not None:
        response["error"] = error
    
    if state == "completed" and result is not None:
        response["result"] = result
    
    return response


@app.post("/executions/{execution_id}/cancel")
async def cancel_execution(
    execution_id: str,
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Cancel a pending or running execution."""
    try:
        client_config = _authenticate(x_api_key)
    except AuthMissingError:
        raise
    except AuthInvalidError:
        raise
    
    execution = await gateway_instance.get_execution(execution_id)
    if execution is None:
        raise ExecutionNotFoundError(execution_id)
    
    # Check ownership
    if execution["client_id"] != x_api_key:
        raise ForbiddenExecutionError()
    
    # Check current permission for the tool
    if not gateway_instance._is_tool_allowed(client_config, execution["tool_name"]):
        raise ForbiddenExecutionError()
    
    # Check if already terminal
    if execution["state"] in ("completed", "failed", "cancelled"):
        raise ExecutionCancelledError(execution_id)
    
    # Cancel the execution
    response_data, status_code = await gateway_instance.cancel_execution(
        execution_id=execution_id,
        client_id=x_api_key,
        tool_name=execution["tool_name"],
        client_config=client_config,
    )
    
    return JSONResponse(
        status_code=status_code,
        content=response_data,
    )


@app.get("/executions/{execution_id}/result")
async def execution_result(
    execution_id: str,
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """Retrieve the result of a completed or failed execution."""
    try:
        client_config = _authenticate(x_api_key)
    except AuthMissingError:
        raise
    except AuthInvalidError:
        raise
    
    execution = await gateway_instance.get_execution(execution_id)
    if execution is None:
        raise ExecutionNotFoundError(execution_id)
    
    # Check ownership
    if execution["client_id"] != x_api_key:
        raise ForbiddenExecutionError()
    
    # Check current permission for the tool
    if not gateway_instance._is_tool_allowed(client_config, execution["tool_name"]):
        raise ForbiddenExecutionError()
    
    state = execution["state"]
    
    # Check if result is available
    if state in ("pending", "running", "cancelled"):
        raise ResultUnavailableError(state)
    
    response: Dict[str, Any] = {
        "execution_id": execution_id,
        "state": state,
    }
    
    if state == "failed" and execution.get("error") is not None:
        response["error"] = execution["error"]
    
    if state == "completed" and execution.get("result") is not None:
        response["result"] = execution["result"]
    
    return response


async def handle_sighup(signum, frame):
    """Handle SIGHUP for config reload."""
    global gateway_instance
    if gateway_instance:
        logger.info("SIGHUP received, refreshing gateway permissions")
        try:
            config = load_config("/app/config/gateway.yaml")
            await gateway_instance.refresh_permissions(config)
        except Exception as e:
            logger.error("Error during SIGHUP refresh: %s", e)


def main():
    """Main entry point for the gateway."""
    import uvicorn
    
    config = load_config("/app/config/gateway.yaml")
    signal.signal(signal.SIGHUP, handle_sighup)
    
    host = config["gateway"]["host"]
    port = config["gateway"]["port"]
    
    logger.info("Starting gateway on %s:%s", host, port)
    logger.info("Configuration loaded from /app/config/gateway.yaml")
    
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
