"""Pydantic models for request/response contracts.

All models follow the observable API contract defined in task/instruction.md.
Error responses never expose stack traces, internal paths, or credentials.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Tool Discovery ──────────────────────────────────────────────────────────

class ToolListResponse(BaseModel):
    """Response for GET /tools/list."""

    tools: list[str] = Field(..., description="Namespaced tool names the client may call")


# ── Tool Invocation ─────────────────────────────────────────────────────────

class ToolCallRequest(BaseModel):
    """Request for POST /tools/call."""

    tool: str = Field(..., description="Namespaced tool name, e.g. documents.search_documents")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")


class ToolCallResponse(BaseModel):
    """Success response for POST /tools/call (202 Accepted)."""

    execution_id: str = Field(..., description="Unique execution identifier")
    state: str = Field(..., description="Initial execution state (pending)")


# ── Validation error details ────────────────────────────────────────────────

class ValidationDetail(BaseModel):
    """Details for INVALID_ARGS errors."""

    missing: Optional[list[str]] = None
    type_mismatch: Optional[list[dict[str, Any]]] = None
    unexpected_fields: Optional[list[str]] = None
    enum_mismatch: Optional[list[dict[str, Any]]] = None


# ── Execution Status ────────────────────────────────────────────────────────

class ExecutionError(BaseModel):
    """Sanitized error object included in failed/cancelled execution responses."""

    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")


class ExecutionStatusResponse(BaseModel):
    """Response for GET /executions/{id}/status."""

    execution_id: str
    state: str = Field(..., description="pending | running | completed | failed | cancelled")
    error: Optional[ExecutionError] = None


# ── Execution Result ────────────────────────────────────────────────────────

class ExecutionResultResponse(BaseModel):
    """Response for GET /executions/{id}/result."""

    execution_id: str
    state: str = Field(..., description="completed | failed")
    result: Optional[Any] = None
    error: Optional[ExecutionError] = None


# ── Cancellation ────────────────────────────────────────────────────────────

class CancelResponse(BaseModel):
    """Response for POST /executions/{id}/cancel."""

    execution_id: str
    state: str = Field(..., description="cancelled")


# ── Generic Error ───────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """Standardized error response for all error status codes."""

    error: str = Field(..., description="Human-readable error message")
    code: str = Field(..., description="Machine-readable error code")
    details: Optional[ValidationDetail] = None
