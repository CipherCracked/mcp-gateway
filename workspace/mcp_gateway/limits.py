"""Rate limiting (token bucket) and per-client concurrency limits."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """A continuously refilling token bucket.

    The bucket starts full, holds ``capacity`` tokens and refills at
    ``capacity / 60`` tokens per second (one minute's worth of requests).
    """

    capacity: float
    refill_per_second: float
    tokens: float = field(init=False)
    updated_at: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now

    async def consume(self, amount: float = 1.0) -> bool:
        """Try to take ``amount`` tokens; ``False`` when the bucket is empty."""
        async with self._lock:
            self._refill(time.monotonic())
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False

    async def available(self) -> float:
        async with self._lock:
            self._refill(time.monotonic())
            return self.tokens


class RateLimiter:
    """Per-client token buckets."""

    def __init__(self, requests_per_minute: int) -> None:
        self.requests_per_minute = max(1, int(requests_per_minute))
        self._buckets: dict[str, TokenBucket] = {}

    def _bucket(self, client_id: str) -> TokenBucket:
        bucket = self._buckets.get(client_id)
        if bucket is None:
            capacity = float(self.requests_per_minute)
            bucket = TokenBucket(capacity=capacity, refill_per_second=capacity / 60.0)
            self._buckets[client_id] = bucket
        return bucket

    async def allow(self, client_id: str) -> bool:
        """Consume one token for ``client_id``."""
        # Dict access is atomic with respect to the event loop, so no lock is
        # needed to create a bucket safely.
        return await self._bucket(client_id).consume(1.0)

    async def reset(self, client_id: str | None = None) -> None:
        if client_id is None:
            self._buckets.clear()
        else:
            self._buckets.pop(client_id, None)


class ConcurrencyLimiter:
    """Per-client limit on simultaneously running executions.

    Each client owns a counter of in-flight executions.  Admission is a
    non-blocking check: when the limit is already reached the caller is
    rejected immediately instead of waiting.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._in_flight: dict[str, int] = {}

    def _count(self, client_id: str) -> int:
        return self._in_flight.get(client_id, 0)

    async def try_acquire(self, client_id: str) -> bool:
        """Non-blocking admission check.

        Returns ``True`` when a slot was taken, ``False`` when the client
        already runs the maximum number of executions.
        """
        if self._count(client_id) >= self.limit:
            return False
        # No await between the check and the increment, so the admission is
        # atomic with respect to other coroutines on this event loop.
        self._in_flight[client_id] = self._count(client_id) + 1
        return True

    async def release(self, client_id: str) -> None:
        """Return a slot to the client, never going below zero."""
        count = self._count(client_id)
        if count <= 0:
            return
        if count == 1:
            self._in_flight.pop(client_id, None)
        else:
            self._in_flight[client_id] = count - 1

    async def in_flight(self, client_id: str) -> int:
        return self._count(client_id)

    async def reset(self, client_id: str | None = None) -> None:
        if client_id is None:
            self._in_flight.clear()
        else:
            self._in_flight.pop(client_id, None)
