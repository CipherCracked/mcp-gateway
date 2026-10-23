"""Compatibility helpers around the official ``mcp`` SDK.

The gateway speaks the MCP wire protocol directly (see :mod:`.stdio`) so that
request ids — which ``notifications/cancelled`` needs — stay under its
control.  This module bridges the raw JSON-RPC payloads to the SDK's typed
models, tolerating the differences between SDK v1 and v2:

* ``Tool.inputSchema`` (v1) versus ``Tool.input_schema`` (v2),
* ``CallToolResult.structuredContent`` versus ``structured_content``,
* ``CallToolResult.isError`` versus ``is_error``.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - the SDK is a hard dependency in production
    from mcp import types as mcp_types
except Exception:  # pragma: no cover - the gateway still works without the SDK
    mcp_types = None  # type: ignore[assignment]


def sdk_available() -> bool:
    """Whether the official MCP SDK could be imported."""
    return mcp_types is not None


def _field(raw: Any, *names: str) -> Any:
    """Read the first present field of ``raw`` among ``names``."""
    if isinstance(raw, dict):
        for name in names:
            if name in raw:
                return raw[name]
        return None
    for name in names:
        value = getattr(raw, name, None)
        if value is not None:
            return value
    return None


def parse_tool(raw: Any) -> dict[str, Any] | None:
    """Normalise a ``tools/list`` entry into a plain dictionary.

    Returns ``None`` when the entry does not describe a usable tool.
    """
    name = _field(raw, "name")
    if not isinstance(name, str) or not name:
        return None
    schema = _field(raw, "inputSchema", "input_schema")
    if not isinstance(schema, dict):
        schema = {}
    description = _field(raw, "description")
    output_schema = _field(raw, "outputSchema", "output_schema")
    return {
        "name": name,
        "description": str(description or ""),
        "inputSchema": schema,
        "outputSchema": output_schema if isinstance(output_schema, dict) else None,
    }


def parse_tool_result(raw: Any) -> dict[str, Any]:
    """Normalise a ``tools/call`` result into a plain dictionary."""
    content = _field(raw, "content")
    if not isinstance(content, list):
        content = []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            blocks.append(block)
        elif block is not None:
            dumped = getattr(block, "model_dump", None)
            if callable(dumped):
                blocks.append(dumped(by_alias=True, exclude_none=True))
            else:  # pragma: no cover - unexpected block type
                blocks.append({"type": "text", "text": str(block)})
    structured = _field(raw, "structuredContent", "structured_content")
    is_error = bool(_field(raw, "isError", "is_error") or False)
    return {
        "content": blocks,
        "structuredContent": structured,
        "isError": is_error,
    }


__all__ = ["parse_tool", "parse_tool_result", "sdk_available"]
