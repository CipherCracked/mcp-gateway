"""A tiny MCP server framework for the fixture servers.

Implements the subset of the MCP stdio transport the gateway relies on:
``initialize``, ``tools/list``, ``tools/call`` and ``notifications/cancelled``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mcp_gateway.servers")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ToolSpec:
    """A tool exposed by a fixture server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    output_schema: dict[str, Any] | None = None
    delay: float = 0.0
    fail: bool = False


@dataclass
class FixtureServer:
    """A minimal MCP server speaking JSON-RPC over stdio."""

    name: str
    version: str = "1.0.0"
    tools: list[ToolSpec] = field(default_factory=list)
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    def add_tool(self, tool: ToolSpec) -> None:
        self.tools.append(tool)

    def find(self, name: str) -> ToolSpec | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    async def run(self) -> None:
        """Serve requests until stdin is closed."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            await self._handle(message)

    async def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")

        if method is None:
            return

        if method == "notifications/initialized":
            return

        if method == "notifications/cancelled":
            params = message.get("params") or {}
            request_id = params.get("requestId")
            task = self._tasks.pop(str(request_id), None)
            if task is not None and not task.done():
                task.cancel()
            return

        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
            await self._respond(
                message_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            )
            return

        if method == "ping":
            await self._respond(message_id, {})
            return

        if method == "tools/list":
            tools = []
            for tool in self.tools:
                entry: dict[str, Any] = {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
                if tool.output_schema is not None:
                    entry["outputSchema"] = tool.output_schema
                tools.append(entry)
            await self._respond(message_id, {"tools": tools})
            return

        if method == "tools/call":
            params = message.get("params") or {}
            await self._call_tool(message_id, params)
            return

        if message_id is not None:
            await self._error(message_id, -32601, "Method not found")

    async def _call_tool(self, message_id: Any, params: dict[str, Any]) -> None:
        tool = self.find(str(params.get("name") or ""))
        if tool is None:
            await self._error(message_id, -32602, "Unknown tool")
            return

        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        key = str(message_id)
        task = asyncio.create_task(self._execute(key, tool, arguments, message_id))
        self._tasks[key] = task
        try:
            await task
        except asyncio.CancelledError:
            # A cancelled call produces no response: the gateway already
            # recorded the cancellation itself.
            return
        finally:
            self._tasks.pop(key, None)

    async def _execute(
        self, key: str, tool: ToolSpec, arguments: dict[str, Any], message_id: Any
    ) -> None:
        try:
            if tool.delay:
                await asyncio.sleep(tool.delay)
            if tool.fail:
                raise RuntimeError("fixture tool failure")
            result = await tool.handler(arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as a tool error
            logger.warning("Fixture tool %s failed: %s", tool.name, exc)
            await self._respond(
                message_id,
                {
                    "content": [{"type": "text", "text": "Tool execution failed"}],
                    "isError": True,
                },
            )
            return
        await self._respond(
            message_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "structuredContent": result,
                "isError": False,
            },
        )

    async def _respond(self, message_id: Any, result: Any) -> None:
        if message_id is None:
            return
        await self._write({"jsonrpc": "2.0", "id": message_id, "result": result})

    async def _error(self, message_id: Any, code: int, message: str) -> None:
        if message_id is None:
            return
        await self._write(
            {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
        )

    async def _write(self, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload, default=str) + "\n").encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def run_server(server: FixtureServer) -> None:
    """Run a fixture server until its stdin is closed."""
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:  # pragma: no cover - signal path
        pass
