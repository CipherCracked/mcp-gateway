"""Core gateway logic: upstream supervision, routing and authorization."""

from __future__ import annotations

import asyncio
import logging
import time

from .config import GatewayConfig, split_tool_name
from .errors import (
    FORBIDDEN_EXECUTION,
    NOT_FOUND,
    RESULT_UNAVAILABLE,
    TOOL_NOT_FOUND,
    GatewayError,
)
from .execution import ExecutionManager
from .limits import ConcurrencyLimiter, RateLimiter
from .persistence import TERMINAL_STATES, ExecutionRecord, ExecutionStore
from .upstream.stdio import StdioMcpClient, ToolDefinition

logger = logging.getLogger("mcp_gateway.gateway")

#: States for which a result may be retrieved.
RESULT_STATES = ("completed", "failed")


class SecureMcpGateway:
    """Aggregates upstream MCP servers and enforces the security policy."""

    def __init__(self, config: GatewayConfig, store: ExecutionStore) -> None:
        self.config = config
        self.store = store
        self.upstreams: dict[str, StdioMcpClient] = {}
        self.execution_manager = ExecutionManager(
            config, store, self.upstreams, on_finish=self._release_slot
        )
        self.rate_limiter = RateLimiter(config.gateway.rate_limit_per_minute)
        self.concurrency_limiter = ConcurrencyLimiter(config.gateway.concurrency_limit)
        self._tool_index: dict[str, ToolDefinition] = {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Spawn the upstream servers, load their tools and recover state."""
        await self.store.connect()
        await self.execution_manager.recover_interrupted()

        for name, server_config in self.config.servers.items():
            client = StdioMcpClient(
                name,
                server_config.command,
                env=server_config.env,
                cwd=server_config.cwd,
            )
            self.upstreams[name] = client
            try:
                await client.start()
            except Exception as exc:  # noqa: BLE001 - keep the gateway alive
                logger.error("Upstream %s failed to start: %s", name, exc)
                continue
            try:
                await client.list_tools()
                logger.info("Upstream %s ready with %d tools", name, len(client.tools))
            except Exception as exc:  # noqa: BLE001
                logger.error("Could not list tools of upstream %s: %s", name, exc)

        self._rebuild_tool_index()

        # A slow upstream may still be enumerating its tools; give it a
        # bounded amount of time to become ready so that the first request
        # does not fail.
        await self._wait_for_upstreams(timeout=10.0)

    async def stop(self) -> None:
        await self.execution_manager.shutdown()
        for client in self.upstreams.values():
            await client.stop()
        await self.store.close()

    def _rebuild_tool_index(self) -> None:
        index: dict[str, ToolDefinition] = {}
        for client in self.upstreams.values():
            for tool in client.tools:
                index[tool.qualified_name] = tool
        self._tool_index = index

    async def _release_slot(self, client_id: str) -> None:
        """Release a client's concurrency slot when an execution finishes."""
        await self.concurrency_limiter.release(client_id)

    # -- configuration reload ---------------------------------------------
    async def reload_config(self) -> None:
        """Reload the configuration file (SIGHUP) and refresh tool listings."""
        from .config import load_config

        new_config = load_config(self.config.path)
        self.config = new_config
        self.execution_manager.config = new_config
        self.rate_limiter = RateLimiter(new_config.gateway.rate_limit_per_minute)
        # The concurrency limiter keeps track of in-flight executions, so it
        # is only replaced when the limit itself changed.
        if new_config.gateway.concurrency_limit != self.concurrency_limiter.limit:
            self.concurrency_limiter = ConcurrencyLimiter(
                new_config.gateway.concurrency_limit
            )

        # Refresh tool definitions from the upstreams.
        for client in self.upstreams.values():
            if client.alive:
                try:
                    await client.list_tools()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not refresh tools of %s: %s", client.name, exc)
        self._rebuild_tool_index()
        logger.info("Configuration reloaded")

    # -- tools -------------------------------------------------------------
    @property
    def tools(self) -> dict[str, ToolDefinition]:
        return self._tool_index

    def resolve_tool(self, tool_name: str) -> ToolDefinition:
        """Look up a namespaced tool.

        The name is split on the first dot into ``{server}.{tool}`` and the
        server must be one the gateway knows about.  Raises
        ``TOOL_NOT_FOUND`` when the name is malformed or unknown.
        """
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise GatewayError(TOOL_NOT_FOUND, "Tool not found", status_code=404)
        definition = self._tool_index.get(tool_name)
        if definition is None:
            raise GatewayError(TOOL_NOT_FOUND, "Tool not found", status_code=404)
        return definition

    def server_of(self, tool_name: str) -> str | None:
        """Return the server a namespaced tool name refers to, if known."""
        parts = split_tool_name(tool_name)
        if parts is None:
            return None
        return parts[0]

    def permitted_tools(self, client_id: str) -> list[str]:
        """The tools a client is currently allowed to call.

        The order of the client's allowlist is preserved so that discovery
        is stable across calls.
        """
        client = self.config.clients.get(client_id)
        if client is None:
            return []
        return [name for name in client.allowlist if name in self._tool_index]

    def is_permitted(self, client_id: str, tool_name: str) -> bool:
        client = self.config.clients.get(client_id)
        if client is None:
            return False
        return tool_name in client.allowlist

    # -- executions --------------------------------------------------------
    async def authorize_execution(
        self, client_id: str, execution_id: str
    ) -> ExecutionRecord:
        """Load an execution and enforce ownership plus current permission."""
        record = await self.store.get(execution_id)
        if record is None:
            raise GatewayError(NOT_FOUND, "Execution not found", status_code=404)
        if record.client_id != client_id:
            raise GatewayError(
                FORBIDDEN_EXECUTION,
                "Execution is not accessible for this client",
                status_code=403,
            )
        if not self.is_permitted(client_id, record.tool_name):
            raise GatewayError(
                FORBIDDEN_EXECUTION,
                "Execution is not accessible for this client",
                status_code=403,
            )
        return record

    async def cancel_execution(
        self, client_id: str, execution_id: str
    ) -> ExecutionRecord:
        record = await self.authorize_execution(client_id, execution_id)
        if record.state in TERMINAL_STATES:
            raise GatewayError(
                RESULT_UNAVAILABLE,
                "Execution is already in a terminal state",
                status_code=409,
            )
        await self.execution_manager.cancel(execution_id)
        updated = await self.store.get(execution_id)
        return updated or record

    async def get_result(self, client_id: str, execution_id: str) -> ExecutionRecord:
        record = await self.authorize_execution(client_id, execution_id)
        if record.state not in RESULT_STATES:
            raise GatewayError(
                RESULT_UNAVAILABLE,
                "Result is not available for this execution",
                status_code=409,
            )
        return record

    # -- upstream liveness -------------------------------------------------
    def upstream_alive(self, server_name: str) -> bool:
        client = self.upstreams.get(server_name)
        return bool(client and client.alive)

    async def _wait_for_upstreams(self, timeout: float) -> None:
        """Wait until every upstream is ready, or the timeout expires."""
        if not self.upstreams:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        # Only upstreams that started successfully are waited for; a server
        # that failed to start stays unavailable and is reported as such.
        pending = {
            name
            for name, client in self.upstreams.items()
            if client.process_alive
        }
        while pending and time.monotonic() < deadline:
            for name in list(pending):
                client = self.upstreams.get(name)
                if client is None or not client.alive:
                    pending.discard(name)
                    continue
                try:
                    if not client.tools:
                        await client.list_tools()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not list tools of %s: %s", name, exc)
                pending.discard(name)
            if pending:
                await asyncio.sleep(0.1)
        if pending:
            logger.warning("Upstreams not ready after %.1fs: %s", timeout, sorted(pending))

    def assert_upstream_available(self, server_name: str) -> None:
        """Raise ``UPSTREAM_UNAVAILABLE`` (502) when the upstream is dead."""
        if not self.upstream_alive(server_name):
            raise GatewayError(
                "UPSTREAM_UNAVAILABLE", "Upstream server unavailable", status_code=502
            )
