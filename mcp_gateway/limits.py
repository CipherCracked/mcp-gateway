"""Token bucket rate limiter and per-client concurrency semaphores.

Rate limiting: token bucket per client_id.
  - Capacity = rate_limit_per_minute
  - Refill = rate_limit_per_minute / 60 tokens per second (continuous)
  - In-memory state (resets on restart — acceptable per spec)

Concurrency: asyncio.Semaphore per client_id.
  - Acquired on submit (non-blocking check → immediate 429 if full)
  - Released on completion/cancellation
"""

import asyncio
import time


class TokenBucket:
    """Token bucket rate limiter for a single client."""

    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        """Attempt to consume one token. Returns True if allowed, False if rate limited."""
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
    """Per-client asyncio semaphore for limiting concurrent executions."""

    def __init__(self, limit: int):
        self._limit = limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str) -> bool:
        """Non-blocking acquire. Returns True if acquired, False if at limit."""
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
        """Release one slot for a client."""
        sem = self._semaphores.get(client_id)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                # Semaphore already at max value; ignore
                pass


class RateLimiter:
    """Per-client token bucket rate limiter."""

    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def consume(self, client_id: str) -> bool:
        """Attempt to consume one token for a client. Returns True if allowed."""
        async with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = TokenBucket(self._capacity, self._refill_per_second)
                self._buckets[client_id] = bucket
        return await bucket.consume()
