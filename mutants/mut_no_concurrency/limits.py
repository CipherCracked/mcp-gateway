"""Mutant MUT-NO-CONCURRENCY: concurrency limiter always allows.

Overlay this file onto mcp_gateway/limits.py. ConcurrencyLimiter.acquire always
returns True, so CONCURRENCY_EXCEEDED is never raised. Expected to fail LIM-002.
"""

import asyncio


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._last_refill = 0.0
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        async with self._lock:
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class ConcurrencyLimiter:
    def __init__(self, limit: int):
        self._limit = limit
        self._semaphores = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str) -> bool:
        # MUTANT: always allow — concurrency limiting disabled.
        return True

    def release(self, client_id: str) -> None:
        pass
