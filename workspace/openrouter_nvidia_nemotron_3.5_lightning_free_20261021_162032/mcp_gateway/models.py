"""Pydantic models for requests and responses."""

from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List


class APIKeyModel(BaseModel):
    """API key client configuration."""
    api_key: str = Field(..., description="The client's API key")
    allowlist: List[str] = Field(
        ..., description="List of tool names the client is permitted to call"
    )

    @validator("api_key")
    def api_key_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("API key must not be empty")
        return v.strip()


class ToolInfo(BaseModel):
    """Information about a registered tool."""
    name: str = Field(..., description="Full tool name in {server}.{tool} format")
    server: str = Field(..., description="Server name component")
    tool: str = Field(..., description="Tool name component")
    input_schema: Dict[str, Any] = Field(
        ..., description="JSON Schema for tool arguments"
    )


class ExecutionResult(BaseModel):
    """Result for an execution."""
    execution_id: str = Field(..., description="Unique execution ID")
    state: str = Field(..., description="Execution state")
    error: Optional[Dict[str, Any]] = Field(
        None, description="Error details if state is failed/cancelled"
    )
    result: Optional[Dict[str, Any]] = Field(
        None, description="Result data if state is completed"
    )


class CallRequest(BaseModel):
    """Request to call a tool."""
    tool: str = Field(..., description="Tool name in {server}.{tool} format")
    arguments: Dict[str, Any] = Field(
        ..., description="Tool arguments"
    )

    @validator("tool")
    def tool_must_have_dot(cls, v):
        if "." not in v:
            raise ValueError("Tool name must contain a dot (format: server.tool)")
        return v

    @validator("arguments")
    def arguments_must_be_dict(cls, v):
        if not isinstance(v, dict):
            raise ValueError("Arguments must be a JSON object")
        return v


class StatusResponse(BaseModel):
    """Response for execution status."""
    execution_id: str
    state: str
    error: Optional[Dict[str, Any]] = None


class CancelResponse(BaseModel):
    """Response for execution cancellation."""
    execution_id: str
    state: str


class CallResponse(BaseModel):
    """Response for tool call submission."""
    execution_id: str
    state: str = "pending"
