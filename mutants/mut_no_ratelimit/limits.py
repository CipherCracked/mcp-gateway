"""Mutant MUT-NO-RATELIMIT: rate limiter always allows.

Overlay this file onto mcp_gateway/limits.py. RateLimiter.consume always
returns True, so RATE_LIMITED is never raised. Expected to fail LIM-001.
"""

import asyncio
import time


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second


class ConcurrencyLimiter:
    def __init__(self, limit: int):
        self._limit = limit
        self._semaphores = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str) -> bool:
        async with self._lock:
            sem = self._semaphores.get(client_id)
            if sem is None:
                sem = asyncio.Semaphore(self._limit)
                self._semaphores[client_id] = sem
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self, client_id: str) -> None:
        sem = self._semaphores.get(client_id)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass


class RateLimiter:
    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._buckets = {}
        self._lock = asyncio.Lock()

    async def consume(self, client_id: str) -> bool:
        # MUTANT: always allow — rate limiting disabled.
        return True
