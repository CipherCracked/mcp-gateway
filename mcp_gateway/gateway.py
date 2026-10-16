"""Core gateway logic: MCP client management, routing, upstream process lifecycle."""

import asyncio
import json
import re
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .config import GatewayConfig
from .errors import (
    ToolNotFoundError,
    UpstreamFailedError,
    UpstreamUnavailableError,
)
from .persistence import ExecutionStore

SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def parse_tool_name(tool: str) -> tuple[str, str]:
    """Split namespaced tool name on first dot. Returns (server, tool).

    Raises ToolNotFoundError if the name is malformed or doesn't match
    the server/tool naming patterns.
    """
    idx = tool.find(".")
    if idx <= 0 or idx == len(tool) - 1:
        raise ToolNotFoundError(tool)
    server = tool[:idx]
    tool_name = tool[idx + 1 :]
    if not SERVER_NAME_RE.match(server):
        raise ToolNotFoundError(tool)
    if not TOOL_NAME_RE.match(tool_name):
        raise ToolNotFoundError(tool)
    return server, tool_name


class UpstreamClient:
    """Manages an MCP client session to a single upstream server."""

    def __init__(self, server_name: str, command: list[str]):
        self.server_name = server_name
        self.command = command
        self._session: Optional[ClientSession] = None
        self._context = None
        self._available_tools: dict[str, Any] = {}

    async def start(self):
        """Spawn the upstream server process and initialize the MCP session."""
        params = StdioServerParameters(command=self.command[0], args=self.command[1:])
        self._context = stdio_client(params)
        read_stream, write_stream = await self._context.__aenter__()
        self._session = ClientSession(read_stream, write_stream)
        await self._session.initialize()
        tools_response = await self._session.list_tools()
        self._available_tools = {t.name: t for t in tools_response.tools}

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._available_tools

    def get_tool_schema(self, tool_name: str) -> Optional[dict[str, Any]]:
        tool = self._available_tools.get(tool_name)
        if tool is None:
            return None
        return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}

    def is_available(self) -> bool:
        """Return True if the MCP session is active."""
        return self._session is not None

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a tool on the upstream server. Returns parsed result.

        Raises UpstreamUnavailableError if the session is not active.
        Raises UpstreamFailedError if the tool call itself fails.
        """
        if not self.is_available():
            raise UpstreamUnavailableError(self.server_name)

        try:
            result = await self._session.call_tool(tool_name, arguments)
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            # Distinguish connection/process errors from application errors
            msg = str(exc).lower()
            if any(k in msg for k in ("closed", "eof", "connection", "broken pipe", "timeout")):
                raise UpstreamUnavailableError(self.server_name) from exc
            raise UpstreamFailedError(str(exc)) from exc

        # Extract text content from MCP result
        if result.content:
            for content in result.content:
                text = getattr(content, "text", None)
                if text is not None:
                    try:
                        return json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        return text
        return {}

    async def close(self):
        """Close the MCP session and upstream process."""
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
        if self._context is not None:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                pass


class Gateway:
    """Core gateway: manages upstream MCP clients and routes tool calls."""

    def __init__(self, config: GatewayConfig, store: ExecutionStore):
        self.config = config
        self.store = store
        self._clients: dict[str, UpstreamClient] = {}
        self._started = False

    async def start(self):
        """Start all configured upstream MCP servers."""
        for server_name, server_cfg in self.config.servers.items():
            command = server_cfg.get("command")
            if not command:
                continue
            client = UpstreamClient(server_name, command)
            await client.start()
            self._clients[server_name] = client
        self._started = True

    async def stop(self):
        """Stop all upstream MCP servers."""
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()
        self._started = False

    def get_client(self, server_name: str) -> Optional[UpstreamClient]:
        return self._clients.get(server_name)

    def is_server_available(self, server_name: str) -> bool:
        client = self._clients.get(server_name)
        return client is not None and client.is_available()

    def resolve_tool(self, tool: str) -> tuple[str, str, UpstreamClient]:
        """Resolve a namespaced tool name to (server, tool_name, client).

        Raises ToolNotFoundError if the tool doesn't exist or the server is unavailable.
        """
        server, tool_name = parse_tool_name(tool)
        client = self._clients.get(server)
        if client is None or not client.is_available():
            raise ToolNotFoundError(tool)
        if not client.has_tool(tool_name):
            raise ToolNotFoundError(tool)
        return server, tool_name, client

    def get_tool_schema(self, tool: str) -> Optional[dict[str, Any]]:
        """Get the JSON Schema for a namespaced tool."""
        try:
            server, tool_name, client = self.resolve_tool(tool)
        except ToolNotFoundError:
            return None
        return client.get_tool_schema(tool_name)

    async def call_upstream(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> Any:
        """Call a tool on an upstream server.

        Raises UpstreamUnavailableError if the server is not available.
        Raises UpstreamFailedError if the tool call fails.
        """
        client = self._clients.get(server_name)
        if client is None or not client.is_available():
            raise UpstreamUnavailableError(server_name)
        return await client.call_tool(tool_name, arguments)
