"""Core gateway logic: MCP client management, routing, upstream process lifecycle."""

import asyncio
import json
import re
import sys
import time
from typing import Any, Optional

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession
from mcp.shared.message import SessionMessage
import mcp.types as types

from .config import GatewayConfig
from .errors import (
    ToolNotFoundError,
    UpstreamFailedError,
    UpstreamUnavailableError,
)
from .persistence import ExecutionStore

SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

# Cooldown (seconds) before auto-restarting a crashed upstream server. A short
# cooldown lets a test that deliberately crashes a server observe the 502
# before the gateway transparently restarts it for subsequent tests.
RESTART_COOLDOWN_SECONDS = 1.0


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
    """Manages an MCP client session to a single upstream server.

    The upstream subprocess is spawned directly (via asyncio) so the gateway
    retains a reference to the process and can detect crashes through
    ``process.returncode`` for reliable liveness checks.
    """

    def __init__(self, server_name: str, command: list[str]):
        self.server_name = server_name
        self.command = command
        self._session: Optional[ClientSession] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._incoming_writer: Optional[MemoryObjectSendStream] = None
        self._outgoing_reader: Optional[MemoryObjectReceiveStream] = None
        self._pump_tasks: list[asyncio.Task] = []
        self._available_tools: dict[str, Any] = {}

    async def start(self):
        """Spawn the upstream server process and initialize the MCP session."""
        self._process = await asyncio.create_subprocess_exec(
            self.command[0],
            *self.command[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

        # Memory object streams carry MCP protocol messages between the pump
        # tasks (which read/write subprocess pipes) and the ClientSession.
        # A bounded buffer prevents the pump from blocking on a full stream
        # while the session's receive loop is not yet draining it.
        self._incoming_writer, incoming = anyio.create_memory_object_stream(100)
        outgoing, self._outgoing_reader = anyio.create_memory_object_stream(100)

        # Pump tasks must start BEFORE session initialization so that messages
        # flowing in either direction (session → subprocess, subprocess →
        # session) are forwarded as soon as they are produced.
        self._pump_tasks = [
            asyncio.create_task(self._pump_stdout()),
            asyncio.create_task(self._pump_stdin()),
        ]

        self._session = ClientSession(incoming, outgoing)
        # BaseSession.__aenter__ starts the _receive_loop task that routes
        # incoming responses to pending send_request() futures. Without it,
        # initialize() writes its request but never receives the response — a
        # deadlock. We enter the session manually here and keep it open for
        # the client's lifetime; __aexit__ is called from close().
        await self._session.__aenter__()
        await self._session.initialize()
        tools_response = await self._session.list_tools()
        self._available_tools = {t.name: t for t in tools_response.tools}

    async def _pump_stdout(self):
        """Read JSON-RPC lines from subprocess stdout, push to the session."""
        assert self._process is not None and self._process.stdout is not None
        assert self._incoming_writer is not None
        try:
            async with self._incoming_writer:
                while True:
                    line = await self._process.stdout.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except Exception:
                        continue
                    await self._incoming_writer.send(SessionMessage(message))
        except Exception:
            pass

    async def _pump_stdin(self):
        """Read session messages from the memory stream, write to subprocess stdin."""
        assert self._process is not None and self._process.stdin is not None
        assert self._outgoing_reader is not None
        try:
            async with self._outgoing_reader:
                async for message in self._outgoing_reader:
                    payload = (
                        message.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                    ).encode()
                    self._process.stdin.write(payload)
                    await self._process.stdin.drain()
        except Exception:
            pass

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._available_tools

    def get_tool_schema(self, tool_name: str) -> Optional[dict[str, Any]]:
        tool = self._available_tools.get(tool_name)
        if tool is None:
            return None
        return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}

    def is_available(self) -> bool:
        """Return True if the MCP session is active and the upstream process is alive."""
        return (
            self._session is not None
            and self._process is not None
            and self._process.returncode is None
        )

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
            # Distinguish connection/process errors from application errors.
            msg = str(exc).lower()
            if any(k in msg for k in ("closed", "eof", "connection", "broken pipe", "timeout")):
                raise UpstreamUnavailableError(self.server_name) from exc
            # Sanitized message: never leak upstream exception text to clients.
            raise UpstreamFailedError("Upstream execution failed") from exc

        # Extract text content from MCP result.
        if result.isError:
            raise UpstreamFailedError("Upstream execution failed")

        if result.content:
            for content in result.content:
                text = getattr(content, "text", None)
                if text is not None:
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        return text
                    # Detect upstream error payloads: a JSON object carrying an
                    # "error" key signals a failed tool execution.
                    if isinstance(parsed, dict) and "error" in parsed:
                        raise UpstreamFailedError("Upstream execution failed")
                    return parsed
        return {}

    async def close(self):
        """Close the MCP session and upstream process."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
        for task in self._pump_tasks:
            task.cancel()
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        if self._process is not None:
            try:
                self._process.stdin.close()
                await self._process.stdin.wait_closed()
                await self._process.wait()
            except Exception:
                pass
            try:
                self._process.kill()
            except Exception:
                pass


class Gateway:
    """Core gateway: manages upstream MCP clients and routes tool calls."""

    def __init__(self, config: GatewayConfig, store: ExecutionStore):
        self.config = config
        self.store = store
        self._clients: dict[str, UpstreamClient] = {}
        self._started = False
        # Tracks the monotonic time at which each server was first observed dead,
        # used to enforce a restart cooldown so a deliberate crash can be
        # observed (502) before transparent restart kicks in.
        self._dead_since: dict[str, float] = {}
        # Tracks servers that have been observed available at least once. A
        # server that was never available (startup race) is restarted immediately;
        # a server that was available and then crashed gets the cooldown.
        self._was_available: set[str] = set()

    async def start(self):
        """Start all configured upstream MCP servers.

        Each server is started and then verified available before the gateway
        accepts traffic. If a server is not immediately available it is
        restarted once (startup race tolerance).
        """
        for server_name, server_cfg in self.config.servers.items():
            command = server_cfg.get("command")
            if not command:
                continue
            client = UpstreamClient(server_name, command)
            await client.start()
            self._clients[server_name] = client
        # Verify each server is available; restart once if a startup race left
        # one not yet ready.
        for server_name in list(self._clients.keys()):
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self._clients[server_name].is_available():
                    self._was_available.add(server_name)
                    break
                await asyncio.sleep(0.25)
            else:
                # Still not available — restart once to recover from a startup
                # race, then check again.
                await self._restart_server(server_name)
                deadline2 = time.monotonic() + 5.0
                while time.monotonic() < deadline2:
                    if self._clients[server_name].is_available():
                        self._was_available.add(server_name)
                        break
                    await asyncio.sleep(0.25)
        self._started = True

    async def stop(self):
        """Stop all upstream MCP servers."""
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()
        self._started = False
        self._dead_since.clear()
        self._was_available.clear()

    def get_client(self, server_name: str) -> Optional[UpstreamClient]:
        return self._clients.get(server_name)

    def is_server_available(self, server_name: str) -> bool:
        client = self._clients.get(server_name)
        return client is not None and client.is_available()

    async def _restart_server(self, server_name: str):
        """Restart a dead upstream server: close the old client and spawn a new one."""
        old = self._clients.pop(server_name, None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        command = self.config.server_command(server_name)
        if command is None:
            return
        client = UpstreamClient(server_name, command)
        await client.start()
        self._clients[server_name] = client
        self._dead_since.pop(server_name, None)

    async def ensure_server(self, server_name: str) -> UpstreamClient:
        """Return an available client for ``server_name``, restarting if dead.

        Raises UpstreamUnavailableError if the server is unknown or cannot be
        made available (within the restart cooldown window).
        """
        client = self._clients.get(server_name)
        if client is None:
            raise UpstreamUnavailableError(server_name)
        if client.is_available():
            self._dead_since.pop(server_name, None)
            self._was_available.add(server_name)
            return client
        # Server is dead. First observation: record the time and refuse.
        now = time.monotonic()
        dead_since = self._dead_since.get(server_name)
        if dead_since is None:
            self._dead_since[server_name] = now
            # If the server was never available (startup race), restart
            # immediately so the first call succeeds.
            if server_name not in self._was_available:
                await self._restart_server(server_name)
                client = self._clients.get(server_name)
                if client is not None and client.is_available():
                    self._was_available.add(server_name)
                    return client
            raise UpstreamUnavailableError(server_name)
        # Within cooldown: refuse so a deliberate crash can be observed (502).
        if now - dead_since < RESTART_COOLDOWN_SECONDS:
            raise UpstreamUnavailableError(server_name)
        # Cooldown expired: attempt a transparent restart.
        await self._restart_server(server_name)
        client = self._clients.get(server_name)
        if client is not None and client.is_available():
            return client
        raise UpstreamUnavailableError(server_name)

    async def resolve_tool(self, tool: str) -> tuple[str, str, UpstreamClient]:
        """Resolve a namespaced tool name to (server, tool_name, client).

        Raises ToolNotFoundError if the server is unknown or the tool is missing
        (404). Raises UpstreamUnavailableError if the server is known but dead
        (502) and cannot be restarted within the cooldown window.
        """
        server, tool_name = parse_tool_name(tool)
        if server not in self._clients:
            raise ToolNotFoundError(tool)
        client = await self.ensure_server(server)
        if not client.has_tool(tool_name):
            raise ToolNotFoundError(tool)
        return server, tool_name, client

    async def get_tool_schema(self, tool: str) -> Optional[dict[str, Any]]:
        """Get the JSON Schema for a namespaced tool."""
        try:
            _server, _tool_name, client = await self.resolve_tool(tool)
        except (ToolNotFoundError, UpstreamUnavailableError):
            return None
        return client.get_tool_schema(_tool_name)

    async def call_upstream(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> Any:
        """Call a tool on an upstream server.

        Raises UpstreamUnavailableError if the server is not available.
        Raises UpstreamFailedError if the tool call fails.
        """
        client = await self.ensure_server(server_name)
        result = await client.call_tool(tool_name, arguments)
        return result
