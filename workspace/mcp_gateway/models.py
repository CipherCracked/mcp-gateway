"""Pydantic request and response models for the gateway REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """Body of ``POST /tools/call``."""

    tool: str = Field(..., description="Namespaced tool name, e.g. documents.search_documents")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolListResponse(BaseModel):
    tools: list[str]


class ToolCallAccepted(BaseModel):
    execution_id: str
    state: str


class ExecutionError(BaseModel):
    code: str
    message: str

    @classmethod
    def from_payload(cls, payload: dict[str, str] | None) -> ExecutionError | None:
        """Build an error model from a stored error payload."""
        if not payload:
            return None
        return cls(code=payload.get("code", "INTERNAL"), message=payload.get("message", "Execution failed"))


class ExecutionStatusResponse(BaseModel):
    execution_id: str
    state: str
    error: ExecutionError | None = None


class ExecutionResultResponse(BaseModel):
    execution_id: str
    state: str
    result: Any = None
    error: ExecutionError | None = None


class ExecutionCancelResponse(BaseModel):
    execution_id: str
    state: str


class ErrorResponse(BaseModel):
    error: str
    code: str
    details: dict[str, Any] | None = None
