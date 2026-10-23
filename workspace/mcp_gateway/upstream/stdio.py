"""A minimal, spec-compliant JSON-RPC 2.0 client over MCP stdio transport.

The gateway speaks the MCP wire protocol directly over a child process' stdin
and stdout.  Doing so keeps full control over JSON-RPC request ids, which the
``notifications/cancelled`` message requires, and makes crash detection and
timeouts explicit.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcp_compat import parse_tool, parse_tool_result

logger = logging.getLogger("mcp_gateway.upstream")

#: Protocol versions tried during the handshake, newest first.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

CLIENT_INFO = {"name": "secure-mcp-gateway", "version": "1.0.0"}


class UpstreamUnavailable(Exception):
    """The upstream process is not running or died while a request was in flight."""


class UpstreamTimeout(Exception):
    """The upstream did not answer within the allotted time."""


class UpstreamProtocolError(Exception):
    """The upstream returned a JSON-RPC error for a request."""


@dataclass
class ToolDefinition:
    """A tool advertised by an upstream MCP server."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"


@dataclass
class ToolCallResult:
    """The outcome of an upstream ``tools/call``."""

    is_error: bool = False
    structured: Any = None
    content: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> Any:
        """Return the client-facing result payload for a successful call."""
        if self.structured is not None:
            return self.structured
        texts: list[str] = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    try:
                        return json.loads(text)
                    except (TypeError, ValueError):
                        texts.append(text)
        if len(texts) == 1:
            return texts[0]
        if texts:
            return {"content": texts}
        return {}


class StdioMcpClient:
    """Runs one MCP server as a child process and talks JSON-RPC to it."""

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        self.command = list(command)
        self.env = env or {}
        self.cwd = cwd

        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._calls: dict[str, int] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._alive = False
        self._failure: str | None = None
        self._tools: list[ToolDefinition] = []
        self._protocol_version: str | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._alive and self._process is not None and self._process.returncode is None

    @property
    def process_alive(self) -> bool:
        """Whether the child process is running, regardless of the session state."""
        return self._process is not None and self._process.returncode is None

    @property
    def tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    @property
    def failure_reason(self) -> str | None:
        return self._failure

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    def _environment(self) -> dict[str, str]:
        """Build the child environment.

        The directory holding the ``reference`` package is added to
        ``PYTHONPATH`` so that ``python -m reference.mcp_gateway.servers.*``
        works no matter which working directory the gateway runs in.
        """
        environ = dict(os.environ)
        environ.update(self.env)
        environ.setdefault("PYTHONUNBUFFERED", "1")
        package_root = str(Path(__file__).resolve().parents[3])
        existing = environ.get("PYTHONPATH", "")
        parts = [part for part in existing.split(os.pathsep) if part]
        if package_root not in parts:
            parts.insert(0, package_root)
        environ["PYTHONPATH"] = os.pathsep.join(parts)
        return environ

    async def start(self) -> None:
        """Spawn the child process and perform the MCP handshake."""
        environ = self._environment()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environ,
                cwd=self.cwd,
            )
        except (OSError, ValueError) as exc:
            self._alive = False
            self._failure = f"failed to spawn upstream: {exc}"
            logger.error("Could not spawn upstream %s: %s", self.name, exc)
            raise UpstreamUnavailable(f"could not spawn upstream {self.name}: {exc}") from exc

        self._alive = True
        self._failure = None
        # Ensure the child never outlives the gateway, even if the process is
        # torn down without a chance to await the shutdown.
        atexit.register(self._terminate_sync)
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        last_error: Exception | None = None
        for version in PROTOCOL_VERSIONS:
            try:
                await self._handshake(version)
                return
            except Exception as exc:  # noqa: BLE001 - retry with another version
                last_error = exc
                logger.warning(
                    "Handshake with upstream %s using protocol %s failed: %s",
                    self.name,
                    version,
                    exc,
                )
                # A rejected handshake may leave the session unusable, so the
                # child is restarted before the next protocol version is tried.
                await self._restart()

        self._failure = f"handshake failed: {last_error}"
        logger.error("Handshake with upstream %s failed: %s", self.name, last_error)
        await self.stop()
        raise UpstreamUnavailable(
            f"upstream {self.name} rejected the handshake: {last_error}"
        )

    async def _restart(self) -> None:
        """Replace the child process with a fresh one."""
        process = self._process
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._process = None
        environ = self._environment()
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environ,
            cwd=self.cwd,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _handshake(self, version: str) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            },
            timeout=20.0,
        )
        await self.notify("notifications/initialized", {})
        self._protocol_version = version

    async def stop(self) -> None:
        """Terminate the child process and release pending requests."""
        self._alive = False
        try:
            await self._stop()
        except RuntimeError:  # pragma: no cover - the loop is already closed
            self._terminate_sync()

    def _terminate_sync(self) -> None:
        """Terminate the child process without an event loop."""
        process = self._process
        if process is None:
            return
        try:
            if process.returncode is None:
                process.kill()
        except (ProcessLookupError, OSError):  # pragma: no cover
            pass
        # Close the transport so that asyncio does not warn about a
        # half-open pipe when the child is garbage collected.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
        self._process = None

    async def _stop(self) -> None:
        """Terminate the child process and release pending requests."""
        process = self._process
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(UpstreamUnavailable("upstream stopped"))
        if process is not None and process.returncode is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            try:
                process.terminate()
            except (ProcessLookupError, OSError):  # pragma: no cover
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError, OSError):  # pragma: no cover
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
        self._process = None

    # -- transport ---------------------------------------------------------
    def _fail_pending(self, exc: BaseException) -> None:
        """Fail every in-flight request with ``exc``.

        The exception is set (not cancelled) so that callers awaiting the
        request observe an ``UpstreamUnavailable`` rather than a cancellation.
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        self._calls.clear()

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        stream = process.stdout
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    logger.warning("Upstream %s emitted a non-JSON line", self.name)
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reader for upstream %s failed: %s", self.name, exc)
        finally:
            self._alive = False
            self._failure = "upstream process exited"
            self._fail_pending(UpstreamUnavailable("upstream process exited"))

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                logger.debug(
                    "upstream[%s] stderr: %s",
                    self.name,
                    line.decode("utf-8", "replace").rstrip(),
                )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001 pragma: no cover
            pass

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            request_id = message.get("id")
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if "id" in message and "method" in message:
            # Server-initiated request: answer "method not found" so the peer
            # is not left waiting.
            asyncio.create_task(
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
            )
            return
        # Notifications are ignored by the gateway.

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise UpstreamUnavailable("upstream is not running")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._alive = False
                self._failure = "upstream closed its input stream"
                raise UpstreamUnavailable("upstream closed its input stream") from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        call_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and return the ``result`` payload."""
        if not self.alive:
            raise UpstreamUnavailable(self._failure or "upstream is not running")
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        if call_key is not None:
            self._calls[call_key] = request_id
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
        except Exception:
            self._pending.pop(request_id, None)
            if call_key is not None:
                self._calls.pop(call_key, None)
            raise
        try:
            message = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            if call_key is not None:
                self._calls.pop(call_key, None)
            await self._cancel_request(request_id, "timeout")
            raise UpstreamTimeout(f"upstream {self.name} timed out") from exc
        finally:
            if call_key is not None and self._calls.get(call_key) == request_id:
                self._calls.pop(call_key, None)
        if "error" in message and message["error"] is not None:
            error = message["error"]
            if isinstance(error, dict):
                raise UpstreamProtocolError(str(error.get("message", "upstream error")))
            raise UpstreamProtocolError("upstream error")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def _cancel_request(self, request_id: int, reason: str) -> None:
        try:
            await self.notify(
                "notifications/cancelled",
                {"requestId": request_id, "reason": reason},
            )
        except Exception:  # noqa: BLE001 - best effort
            logger.debug("Could not deliver cancellation for upstream %s", self.name)

    async def cancel_call(self, call_key: str, reason: str = "cancelled by client") -> bool:
        """Send ``notifications/cancelled`` for a tracked in-flight call."""
        request_id = self._calls.get(call_key)
        if request_id is None:
            return False
        await self._cancel_request(request_id, reason)
        return True

    # -- MCP helpers -------------------------------------------------------
    async def list_tools(self, *, timeout: float = 20.0) -> list[ToolDefinition]:
        result = await self.request("tools/list", {}, timeout=timeout)
        tools: list[ToolDefinition] = []
        for raw in result.get("tools") or []:
            parsed = parse_tool(raw)
            if parsed is None:
                continue
            tools.append(
                ToolDefinition(
                    server=self.name,
                    name=parsed["name"],
                    description=parsed["description"],
                    input_schema=parsed["inputSchema"],
                    output_schema=parsed["outputSchema"],
                )
            )
        # A tool whose name already contains a dot would be ambiguous once
        # namespaced; the upstream is authoritative, so it is kept as-is.
        self._tools = tools
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 30.0,
        call_key: str | None = None,
    ) -> ToolCallResult:
        result = await self.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout,
            call_key=call_key,
        )
        parsed = parse_tool_result(result)
        return ToolCallResult(
            is_error=parsed["isError"],
            structured=parsed["structuredContent"],
            content=parsed["content"],
            raw=result,
        )
