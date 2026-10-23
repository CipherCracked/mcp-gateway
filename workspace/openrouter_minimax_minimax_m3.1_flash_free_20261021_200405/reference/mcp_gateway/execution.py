"""The execution manager: async execution lifecycle and state machine.

State machine::

    pending -> running -> completed
                    \\-> failed    (UPSTREAM_FAILED | UPSTREAM_UNAVAILABLE |
                    \\                EXECUTION_TIMEOUT | EXECUTION_INTERRUPTED)
                    \\-> cancelled (client requested)

Terminal states never transition again.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig
from .errors import (
    EXECUTION_INTERRUPTED,
    EXECUTION_TIMEOUT,
    UPSTREAM_FAILED,
    UPSTREAM_UNAVAILABLE,
    GatewayError,
)
from .persistence import (
    CANCELLED,
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    TERMINAL_STATES,
    ExecutionRecord,
    ExecutionStore,
)
from .upstream.stdio import (
    StdioMcpClient,
    UpstreamProtocolError,
    UpstreamTimeout,
    UpstreamUnavailable,
)

logger = logging.getLogger("mcp_gateway.execution")

#: Error code reported for an execution cancelled by the client.
CANCELLED_CODE = "CANCELLED"


@dataclass
class ExecutionOutcome:
    """The result of a finished execution."""

    state: str
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None


class ExecutionManager:
    """Owns the execution lifecycle: submission, running, and finalisation."""

    def __init__(
        self,
        config: GatewayConfig,
        store: ExecutionStore,
        upstreams: dict[str, StdioMcpClient],
        on_finish: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.upstreams = upstreams
        #: Called with the client id when an execution reaches a terminal state.
        self.on_finish = on_finish
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._finished: set[str] = set()
        self._lock = asyncio.Lock()

    # -- helpers -----------------------------------------------------------
    def _now(self) -> float:
        return time.time()

    def _client_for(self, client_id: str):
        return self.config.clients.get(client_id)

    def is_permitted(self, client_id: str, tool_name: str) -> bool:
        """Whether ``client_id`` may currently call ``tool_name``."""
        client = self._client_for(client_id)
        if client is None:
            return False
        return tool_name in client.allowlist

    async def recover_interrupted(self) -> int:
        """Mark executions left non-terminal by a previous run as interrupted."""
        count = await self.store.mark_interrupted(self._now())
        if count:
            logger.warning("Marked %d interrupted executions as failed", count)
        return count

    async def get(self, execution_id: str) -> ExecutionRecord | None:
        return await self.store.get(execution_id)

    async def _release_slot(self, execution_id: str, client_id: str) -> None:
        """Release the client's concurrency slot exactly once."""
        async with self._lock:
            if execution_id in self._finished:
                return
            self._finished.add(execution_id)
        if self.on_finish is not None:
            try:
                result = self.on_finish(client_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Could not release the concurrency slot")

    # -- submission --------------------------------------------------------
    async def submit(
        self,
        *,
        client_id: str,
        server_name: str,
        tool_name: str,
        qualified_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Create a pending execution and schedule its asynchronous run."""
        execution_id = str(uuid.uuid4())
        now = self._now()
        record = ExecutionRecord(
            execution_id=execution_id,
            client_id=client_id,
            tool_name=qualified_name,
            server_name=server_name,
            state=PENDING,
            arguments=arguments,
            created_at=now,
            updated_at=now,
        )
        await self.store.create(record)
        task = asyncio.create_task(
            self._run(execution_id, client_id, server_name, tool_name, arguments)
        )
        async with self._lock:
            self._tasks[execution_id] = task
        return execution_id

    async def _run(
        self,
        execution_id: str,
        client_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """Execute the tool upstream and finalise the execution record."""
        upstream = self.upstreams.get(server_name)
        timeout = self.config.gateway.execution_timeout_seconds
        try:
            if upstream is None or not upstream.alive:
                await self._fail(
                    execution_id, UPSTREAM_UNAVAILABLE, "Upstream server unavailable"
                )
                return

            await self._set_state(execution_id, RUNNING)
            if execution_id in self._cancelled:
                return

            result = await upstream.call_tool(
                tool_name, arguments, timeout=timeout, call_key=execution_id
            )
            if execution_id in self._cancelled:
                return
            if result.is_error:
                await self._fail(execution_id, UPSTREAM_FAILED, "Upstream execution failed")
                return
            await self._succeed(execution_id, result.payload())
        except asyncio.CancelledError:
            # The transition is written by whoever cancelled the task: the
            # client (cancelled) or the shutdown routine (interrupted).
            raise
        except UpstreamTimeout:
            await self._fail(execution_id, EXECUTION_TIMEOUT, "Execution timed out")
        except UpstreamUnavailable:
            await self._fail(
                execution_id, UPSTREAM_UNAVAILABLE, "Upstream server unavailable"
            )
        except UpstreamProtocolError:
            await self._fail(execution_id, UPSTREAM_FAILED, "Upstream execution failed")
        except GatewayError as exc:
            await self._fail(execution_id, exc.code, exc.message)
        except Exception:
            logger.exception("Unexpected failure while running execution %s", execution_id)
            await self._fail(execution_id, UPSTREAM_FAILED, "Upstream execution failed")
        finally:
            async with self._lock:
                self._tasks.pop(execution_id, None)
            await self._release_slot(execution_id, client_id)

    # -- state transitions -------------------------------------------------
    async def _set_state(self, execution_id: str, state: str) -> None:
        await self.store.update(execution_id, state=state, updated_at=self._now())

    async def _succeed(self, execution_id: str, result: Any) -> None:
        await self.store.update(
            execution_id,
            state=COMPLETED,
            result=result if result is not None else {},
            updated_at=self._now(),
        )

    async def _fail(self, execution_id: str, code: str, message: str) -> None:
        await self.store.update(
            execution_id,
            state=FAILED,
            error_code=code,
            error_message=message,
            updated_at=self._now(),
        )

    async def _finalise_cancelled(self, execution_id: str) -> None:
        record = await self.store.get(execution_id)
        if record is None or record.state in TERMINAL_STATES:
            return
        await self.store.update(
            execution_id,
            state=CANCELLED,
            error_code=CANCELLED_CODE,
            error_message="Execution cancelled by client request",
            updated_at=self._now(),
        )

    async def _finalise_interrupted(self, execution_id: str) -> None:
        record = await self.store.get(execution_id)
        if record is None or record.state in TERMINAL_STATES:
            return
        await self.store.update(
            execution_id,
            state=FAILED,
            error_code=EXECUTION_INTERRUPTED,
            error_message="Execution interrupted by gateway restart",
            updated_at=self._now(),
        )

    # -- cancellation ------------------------------------------------------
    async def cancel(self, execution_id: str) -> ExecutionOutcome:
        """Cancel a pending or running execution."""
        record = await self.store.get(execution_id)
        if record is None:
            raise GatewayError("NOT_FOUND", "Execution not found", status_code=404)
        if record.state in TERMINAL_STATES:
            raise GatewayError(
                "RESULT_UNAVAILABLE",
                "Execution is already in a terminal state",
                status_code=409,
            )

        async with self._lock:
            self._cancelled.add(execution_id)
            task = self._tasks.get(execution_id)

        # Ask the upstream to stop working on this call.
        upstream = self.upstreams.get(record.server_name)
        if upstream is not None:
            try:
                await upstream.cancel_call(execution_id, "cancelled by client")
            except Exception:
                logger.debug("Could not notify upstream of cancellation", exc_info=True)

        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error while cancelling execution %s", execution_id)

        await self._finalise_cancelled(execution_id)
        final = await self.store.get(execution_id)
        if final is None:  # pragma: no cover - defensive
            return ExecutionOutcome(state=CANCELLED)
        return ExecutionOutcome(
            state=final.state,
            result=final.result,
            error_code=final.error_code,
            error_message=final.error_message,
        )

    async def shutdown(self) -> None:
        """Interrupt every in-flight execution (used on gateway shutdown)."""
        async with self._lock:
            tasks = list(self._tasks.items())
        for execution_id, task in tasks:
            if not task.done():
                task.cancel()
        for execution_id, task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error while stopping execution %s", execution_id)
            await self._finalise_interrupted(execution_id)


__all__ = ["CANCELLED_CODE", "ExecutionManager", "ExecutionOutcome"]
