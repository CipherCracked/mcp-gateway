"""Custom exception classes and error handlers for the gateway."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from typing import Any, Dict, Optional


class GatewayError(Exception):
    """Base gateway error with HTTP status code and error code."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        super().__init__(self.message)


class AuthMissingError(GatewayError):
    """No X-API-Key header provided."""

    def __init__(self):
        super().__init__(
            code="AUTH_MISSING",
            message="Authentication missing: X-API-Key header required",
            http_status=401,
        )


class AuthInvalidError(GatewayError):
    """Invalid or empty API key."""

    def __init__(self):
        super().__init__(
            code="AUTH_INVALID",
            message="Authentication invalid: provided API key is not valid",
            http_status=401,
        )


class ForbiddenToolError(GatewayError):
    """Tool exists but not in client's allowlist."""

    def __init__(self, tool_name: str):
        super().__init__(
            code="FORBIDDEN_TOOL",
            message=f"Forbidden: tool '{tool_name}' is not available to this client",
            http_status=403,
        )


class ToolNotFoundError(GatewayError):
    """Tool name doesn't exist on any server."""

    def __init__(self, tool_name: str):
        super().__init__(
            code="TOOL_NOT_FOUND",
            message=f"Tool '{tool_name}' not found on any server",
            http_status=404,
        )


class ForbiddenExecutionError(GatewayError):
    """Client not owner of execution or permission revoked."""

    def __init__(self, execution_id: str = ""):
        msg = "Forbidden: you are not the owner of this execution or permission has been revoked"
        super().__init__(
            code="FORBIDDEN_EXECUTION",
            message=msg,
            http_status=403,
        )


class InvalidArgsError(GatewayError):
    """Argument validation failed."""

    def __init__(self, details: Dict[str, Any]):
        super().__init__(
            code="INVALID_ARGS",
            message="Invalid arguments provided",
            http_status=400,
            details=details,
        )


class PayloadTooLargeError(GatewayError):
    """Request body exceeds maximum size."""

    def __init__(self, max_bytes: int):
        super().__init__(
            code="PAYLOAD_TOO_LARGE",
            message=f"Request payload exceeds maximum size of {max_bytes} bytes",
            http_status=413,
        )


class RateLimitedError(GatewayError):
    """Token bucket rate limit exhausted."""

    def __init__(self):
        super().__init__(
            code="RATE_LIMITED",
            message="Rate limit exceeded: too many requests",
            http_status=429,
        )


class ConcurrencyExceededError(GatewayError):
    """Per-client concurrency limit exhausted."""

    def __init__(self):
        super().__init__(
            code="CONCURRENCY_EXCEEDED",
            message="Concurrency limit exceeded: too many simultaneous executions",
            http_status=429,
        )


class ExecutionNotFoundError(GatewayError):
    """Execution ID doesn't exist."""

    def __init__(self, execution_id: str):
        super().__init__(
            code="NOT_FOUND",
            message=f"Execution '{execution_id}' not found",
            http_status=404,
        )


class ExecutionTimeoutError(GatewayError):
    """Execution exceeded timeout."""

    def __init__(self):
        super().__init__(
            code="EXECUTION_TIMEOUT",
            message="Execution exceeded configured timeout",
            http_status=202,
        )


class UpstreamUnavailableError(GatewayError):
    """Upstream process is unavailable."""

    def __init__(self):
        super().__init__(
            code="UPSTREAM_UNAVAILABLE",
            message="Upstream server is unavailable",
            http_status=502,
        )


class UpstreamFailedError(GatewayError):
    """Upstream execution failed."""

    def __init__(self):
        super().__init__(
            code="UPSTREAM_FAILED",
            message="Upstream execution failed",
            http_status=202,
        )


class ResultUnavailableError(GatewayError):
    """Result requested for non-terminal execution state."""

    def __init__(self, state: str):
        super().__init__(
            code="RESULT_UNAVAILABLE",
            message=f"Result unavailable: execution is in '{state}' state",
            http_status=409,
        )


class ExecutionCancelledError(GatewayError):
    """Execution already terminal and cannot be cancelled."""

    def __init__(self, execution_id: str):
        super().__init__(
            code="CANCELLATION_FAILED",
            message=f"Execution '{execution_id}' is already in a terminal state",
            http_status=409,
        )


class GatewayInternalError(GatewayError):
    """Internal gateway error - sanitized for clients."""

    def __init__(self, original_error: str = ""):
        msg = "Internal gateway error"
        super().__init__(
            code="INTERNAL",
            message=msg,
            http_status=500,
            details={"original": original_error} if original_error else None,
        )


def register_gateway_errors(app: FastAPI) -> None:
    """Register error handlers for gateway-specific exceptions."""

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
