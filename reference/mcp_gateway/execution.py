"""ExecutionManager: async execution engine with state machine, persistence, limits.

States: pending → running → completed | failed | cancelled
Terminal states: completed, failed, cancelled (no transitions out).
"""

import asyncio
import time
import uuid
from typing import Any, Optional

from .config import GatewayConfig
from .errors import (
    AlreadyTerminalError,
    ConcurrencyExceededError,
    ExecutionInterruptedError,
    ExecutionNotFoundError,
    ExecutionTimeoutError,
    ForbiddenExecutionError,
    RateLimitedError,
    ResultUnavailableError,
    UpstreamFailedError,
    UpstreamUnavailableError,
)
from .gateway import Gateway, parse_tool_name
from .limits import ConcurrencyLimiter, RateLimiter
from .persistence import ExecutionStore


class ExecutionManager:
    """Each execution is an asyncio.Task. Per-client semaphores enforce concurrency
    limits. asyncio.wait_for enforces per-execution timeouts.
    """

    def __init__(
        self,
        config: GatewayConfig,
        store: ExecutionStore,
        gateway: Gateway,
        rate_limiter: RateLimiter,
        concurrency_limiter: ConcurrencyLimiter,
    ):
        self.config = config
        self.store = store
        self.gateway = gateway
        self.rate_limiter = rate_limiter
        self.concurrency_limiter = concurrency_limiter
        self._tasks: dict[str, asyncio.Task] = {}
        self._recovered = False

    async def recover(self):
        """Recover execution state from SQLite on restart.

        Terminal states are recovered faithfully. Non-terminal states
        (pending, running) are marked failed with EXECUTION_INTERRUPTED.
        """
        records = await self.store.list_all()
        for record in records:
            state = record["state"]
            if state in ("pending", "running"):
                await self.store.update_state(
                    record["execution_id"],
                    "failed",
                    error_code="EXECUTION_INTERRUPTED",
                    error_message="Execution was interrupted by gateway restart",
                )
        self._recovered = True

    def _new_execution_id(self) -> str:
        return f"exec-{uuid.uuid4().hex}"

    async def submit(
        self,
        client_id: str,
        tool: str,
        server: str,
        arguments: dict,
    ) -> tuple[str, str]:
        """Submit a new execution. Returns (execution_id, state).

        Raises RateLimitedError if the client's token bucket is exhausted.
        Raises ConcurrencyExceededError if the client's semaphore is full.
        Raises UpstreamUnavailableError if the upstream server is unavailable.
        """
        # Rate limit check
        if not await self.rate_limiter.consume(client_id):
            raise RateLimitedError()

        # Concurrency check (non-blocking admission)
        if not await self.concurrency_limiter.acquire(client_id):
            raise ConcurrencyExceededError()

        # Check upstream availability before accepting the job
        try:
            await self.gateway.ensure_server(server)
        except UpstreamUnavailableError:
            self.concurrency_limiter.release(client_id)
            raise

        execution_id = self._new_execution_id()
        now = time.monotonic()

        await self.store.create(
            execution_id=execution_id,
            client_id=client_id,
            tool=tool,
            server=server,
            arguments=arguments,
            owner_client_id=client_id,
            created_at=now,
        )

        # Start the execution task
        task = asyncio.create_task(
            self._run_execution(execution_id, client_id, tool, server, arguments)
        )
        self._tasks[execution_id] = task

        return execution_id, "pending"

    async def _run_execution(
        self,
        execution_id: str,
        client_id: str,
        tool: str,
        server: str,
        arguments: dict,
    ):
        """Run a single execution end-to-end in a background task."""
        try:
            await self.store.update_state(
                execution_id, "running", updated_at=time.monotonic()
            )

            # Execute with timeout — call_upstream expects the bare tool name,
            # so strip the server namespace prefix.
            _, bare_tool_name = parse_tool_name(tool)
            try:
                result = await asyncio.wait_for(
                    self.gateway.call_upstream(server, bare_tool_name, arguments),
                    timeout=self.config.execution_timeout_seconds,
                )
            except asyncio.TimeoutError:
                await self.store.update_state(
                    execution_id,
                    "failed",
                    error_code="EXECUTION_TIMEOUT",
                    error_message="Execution timed out",
                    updated_at=time.monotonic(),
                )
                return

            await self.store.update_state(
                execution_id,
                "completed",
                result=result,
                updated_at=time.monotonic(),
            )
        except UpstreamUnavailableError as exc:
            await self.store.update_state(
                execution_id,
                "failed",
                error_code="UPSTREAM_UNAVAILABLE",
                error_message=str(exc.message),
                updated_at=time.monotonic(),
            )
        except UpstreamFailedError as exc:
            await self.store.update_state(
                execution_id,
                "failed",
                error_code="UPSTREAM_FAILED",
                error_message=str(exc.message),
                updated_at=time.monotonic(),
            )
        except asyncio.CancelledError:
            # Check if client explicitly cancelled (state already set to "cancelled")
            record = await self.store.get(execution_id)
            if record and record["state"] == "cancelled":
                # Client-initiated cancellation already recorded; don't overwrite
                pass
            else:
                # Gateway shutdown or other interruption - mark as interrupted
                await self.store.update_state(
                    execution_id,
                    "failed",
                    error_code="EXECUTION_INTERRUPTED",
                    error_message="Execution was interrupted by gateway restart",
                    updated_at=time.monotonic(),
                )
        except Exception as exc:  # noqa: BLE001 - unexpected gateway error
            await self.store.update_state(
                execution_id,
                "failed",
                error_code="INTERNAL",
                error_message="Internal server error",
                updated_at=time.monotonic(),
            )
        finally:
            self.concurrency_limiter.release(client_id)
            self._tasks.pop(execution_id, None)

    async def get_status(self, execution_id: str) -> dict[str, Any]:
        """Get execution status record. Raises ExecutionNotFoundError if unknown."""
        record = await self.store.get(execution_id)
        if record is None:
            raise ExecutionNotFoundError(execution_id)
        return record

    async def cancel(self, execution_id: str, client_id: str) -> str:
        """Cancel a pending or running execution. Returns new state.

        Raises ExecutionNotFoundError if unknown.
        Raises ForbiddenExecutionError if not the owner.
        Raises AlreadyTerminalError if already terminal.
        """
        record = await self.store.get(execution_id)
        if record is None:
            raise ExecutionNotFoundError(execution_id)

        if record["owner_client_id"] != client_id:
            raise ForbiddenExecutionError()

        state = record["state"]
        if state in ("completed", "failed", "cancelled"):
            raise AlreadyTerminalError(state)

        task = self._tasks.get(execution_id)
        if task is not None:
            task.cancel()

        await self.store.update_state(
            execution_id,
            "cancelled",
            error_code="RESULT_UNAVAILABLE",
            error_message="Execution cancelled by client",
            updated_at=time.monotonic(),
        )
        return "cancelled"

    async def get_result(self, execution_id: str, client_id: str) -> dict[str, Any]:
        """Get execution result record.

        Raises ExecutionNotFoundError if unknown.
        Raises ForbiddenExecutionError if not the owner.
        Raises ResultUnavailableError if not in completed/failed state.
        """
        record = await self.store.get(execution_id)
        if record is None:
            raise ExecutionNotFoundError(execution_id)

        if record["owner_client_id"] != client_id:
            raise ForbiddenExecutionError()

        state = record["state"]
        if state not in ("completed", "failed"):
            raise ResultUnavailableError(state)

        return record
