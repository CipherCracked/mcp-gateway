"""Gateway error types and the sanitized error envelope.

Every error surfaced to a client follows::

    {"error": "human_readable_message", "code": "MACHINE_CODE", "details"?: {}}

``details`` is only ever populated for ``INVALID_ARGS`` validation failures.
Internal details (stack traces, file paths, credentials) are logged but never
returned to clients.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mcp_gateway.errors")

# --- Error codes -----------------------------------------------------------
AUTH_MISSING = "AUTH_MISSING"
AUTH_INVALID = "AUTH_INVALID"
FORBIDDEN_TOOL = "FORBIDDEN_TOOL"
TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
FORBIDDEN_EXECUTION = "FORBIDDEN_EXECUTION"
NOT_FOUND = "NOT_FOUND"
INVALID_ARGS = "INVALID_ARGS"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
RATE_LIMITED = "RATE_LIMITED"
CONCURRENCY_EXCEEDED = "CONCURRENCY_EXCEEDED"
EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
UPSTREAM_FAILED = "UPSTREAM_FAILED"
EXECUTION_INTERRUPTED = "EXECUTION_INTERRUPTED"
RESULT_UNAVAILABLE = "RESULT_UNAVAILABLE"
INTERNAL = "INTERNAL"

#: Default HTTP status for each error code.
STATUS_BY_CODE: dict[str, int] = {
    AUTH_MISSING: 401,
    AUTH_INVALID: 401,
    FORBIDDEN_TOOL: 403,
    TOOL_NOT_FOUND: 404,
    FORBIDDEN_EXECUTION: 403,
    NOT_FOUND: 404,
    INVALID_ARGS: 400,
    PAYLOAD_TOO_LARGE: 413,
    RATE_LIMITED: 429,
    CONCURRENCY_EXCEEDED: 429,
    EXECUTION_TIMEOUT: 500,
    UPSTREAM_UNAVAILABLE: 502,
    UPSTREAM_FAILED: 500,
    EXECUTION_INTERRUPTED: 500,
    RESULT_UNAVAILABLE: 409,
    INTERNAL: 500,
}

#: Sanitized human-readable messages. These never contain upstream internals.
MESSAGE_BY_CODE: dict[str, str] = {
    AUTH_MISSING: "Missing API key",
    AUTH_INVALID: "Invalid API key",
    FORBIDDEN_TOOL: "Tool is not permitted for this client",
    TOOL_NOT_FOUND: "Tool not found",
    FORBIDDEN_EXECUTION: "Execution is not accessible for this client",
    NOT_FOUND: "Execution not found",
    INVALID_ARGS: "Invalid tool arguments",
    PAYLOAD_TOO_LARGE: "Request payload too large",
    RATE_LIMITED: "Rate limit exceeded",
    CONCURRENCY_EXCEEDED: "Too many concurrent executions",
    EXECUTION_TIMEOUT: "Execution timed out",
    UPSTREAM_UNAVAILABLE: "Upstream server unavailable",
    UPSTREAM_FAILED: "Upstream execution failed",
    EXECUTION_INTERRUPTED: "Execution interrupted",
    RESULT_UNAVAILABLE: "Result is not available",
    INTERNAL: "Internal gateway error",
}


class GatewayError(Exception):
    """Base class for errors that map onto a client-visible error envelope."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or MESSAGE_BY_CODE.get(code, "Gateway error")
        self.status_code = status_code or STATUS_BY_CODE.get(code, 500)
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class AuthError(GatewayError):
    """Missing or invalid credentials."""


class ForbiddenError(GatewayError):
    """The caller is not allowed to perform the operation."""


class NotFoundError(GatewayError):
    """The requested resource does not exist."""


class ValidationError(GatewayError):
    """Tool argument validation failed."""

    def __init__(self, details: dict[str, Any], message: str | None = None) -> None:
        super().__init__(INVALID_ARGS, message, status_code=400, details=details)


class LimitError(GatewayError):
    """A rate, concurrency or payload limit was exceeded."""


class UpstreamError(GatewayError):
    """The upstream MCP server is unavailable or failed."""


def internal_error(exc: BaseException, context: str = "") -> GatewayError:
    """Log an internal failure and return a sanitized error for the client."""
    logger.exception("Internal gateway error%s: %s", f" in {context}" if context else "", exc)
    return GatewayError(INTERNAL)
