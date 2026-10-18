"""Mutant MUT-NO-SANITIZE: Stack traces leaked in error responses.

This mutant's unhandled_exception_handler returns the full Python traceback
instead of a sanitized message, so SAN-001 (no stack traces leaked) fails.
"""

import traceback
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        details: Optional[dict] = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class AuthMissingError(GatewayError):
    def __init__(self):
        super().__init__(
            code="AUTH_MISSING",
            message="Missing X-API-Key header",
            status_code=401,
        )


class AuthInvalidError(GatewayError):
    def __init__(self):
        super().__init__(
            code="AUTH_INVALID",
            message="Invalid or empty API key",
            status_code=401,
        )


class ForbiddenToolError(GatewayError):
    def __init__(self, tool: str):
        super().__init__(
            code="FORBIDDEN_TOOL",
            message=f"Tool '{tool}' is not permitted for this client",
            status_code=403,
        )


class ForbiddenExecutionError(GatewayError):
    def __init__(self):
        super().__init__(
            code="FORBIDDEN_EXECUTION",
            message="You are not permitted to access this execution",
            status_code=403,
        )


class ToolNotFoundError(GatewayError):
    def __init__(self, tool: str):
        super().__init__(
            code="TOOL_NOT_FOUND",
            message=f"Tool '{tool}' not found",
            status_code=404,
        )


class InvalidArgsError(GatewayError):
    def __init__(self, details: dict):
        super().__init__(
            code="INVALID_ARGS",
            message="Argument validation failed",
            status_code=400,
            details=details,
        )


class PayloadTooLargeError(GatewayError):
    def __init__(self, max_bytes: int):
        super().__init__(
            code="PAYLOAD_TOO_LARGE",
            message=f"Request body exceeds {max_bytes} bytes",
            status_code=413,
        )


class RateLimitedError(GatewayError):
    def __init__(self):
        super().__init__(
            code="RATE_LIMITED",
            message="Rate limit exceeded",
            status_code=429,
        )


class ConcurrencyExceededError(GatewayError):
    def __init__(self):
        super().__init__(
            code="CONCURRENCY_EXCEEDED",
            message="Maximum concurrent executions reached for this client",
            status_code=429,
        )


class ExecutionNotFoundError(GatewayError):
    def __init__(self, execution_id: str):
        super().__init__(
            code="NOT_FOUND",
            message=f"Execution '{execution_id}' not found",
            status_code=404,
        )


class ResultUnavailableError(GatewayError):
    def __init__(self, state: str):
        super().__init__(
            code="RESULT_UNAVAILABLE",
            message=f"Result not available for execution in state '{state}'",
            status_code=409,
        )


class AlreadyTerminalError(GatewayError):
    def __init__(self, state: str):
        super().__init__(
            code="RESULT_UNAVAILABLE",
            message=f"Execution is already in terminal state '{state}'",
            status_code=409,
        )


class UpstreamUnavailableError(GatewayError):
    def __init__(self, server: str):
        super().__init__(
            code="UPSTREAM_UNAVAILABLE",
            message=f"Upstream server '{server}' is unavailable",
            status_code=502,
        )


class UpstreamFailedError(GatewayError):
    def __init__(self, message: str = "Upstream execution failed"):
        super().__init__(
            code="UPSTREAM_FAILED",
            message=message,
            status_code=500,
        )


class ExecutionTimeoutError(GatewayError):
    def __init__(self):
        super().__init__(
            code="EXECUTION_TIMEOUT",
            message="Execution timed out",
            status_code=500,
        )


class ExecutionInterruptedError(GatewayError):
    def __init__(self):
        super().__init__(
            code="EXECUTION_INTERRUPTED",
            message="Execution was interrupted by gateway restart",
            status_code=500,
        )


class InternalError(GatewayError):
    def __init__(self, message: str = "Internal server error"):
        super().__init__(
            code="INTERNAL",
            message="Internal server error",
            status_code=500,
        )


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    body: dict = {"error": exc.message, "code": exc.code}
    if exc.details is not None:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # MUTANT: leaks full traceback instead of sanitized message
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return JSONResponse(
        status_code=500,
        content={"error": tb, "code": "INTERNAL"},
    )
