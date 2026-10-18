"""Mutant MUT-NO-CONCURRENCY: Concurrency limiter always allows.

This mutant's ConcurrencyLimiter.acquire() always returns True, so
LIM-002 (concurrency limit) fails.
"""

import asyncio
import time


class TokenBucket:
    """Token bucket rate limiter for a single client."""

    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._tokens = float(capacity)
        now = time.monotonic()
        self._last_refill = now
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_per_second,
            )
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class ConcurrencyLimiter:
    """Per-client asyncio semaphore (mutant: always allows)."""

    def __init__(self, limit: int):
        self._limit = limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str) -> bool:
        # MUTANT: always acquire, never reject
        async with self._lock:
            sem = self._semaphores.get(client_id)
            if sem is None:
                sem = asyncio.Semaphore(self._limit)
                self._semaphores[client_id] = sem
        await sem.acquire()
        return True

    def release(self, client_id: str) -> None:
        sem = self._semaphores.get(client_id)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass

    def reset(self) -> None:
        self._semaphores.clear()


class RateLimiter:
    """Per-client token bucket rate limiter."""

    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def consume(self, client_id: str) -> bool:
        async with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = TokenBucket(self._capacity, self._refill_per_second)
                self._buckets[client_id] = bucket
        return await bucket.consume()

    async def reset(self) -> None:
        async with self._lock:
            self._buckets.clear()
