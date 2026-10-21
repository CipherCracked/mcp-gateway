"""Rate limiting and concurrency management for the gateway."""

import asyncio
import time
from typing import Dict, Optional


class TokenBucket:
    """Token bucket rate limiter per client."""

    def __init__(self, capacity: int, refill_rate_per_second: float):
        self.capacity = capacity
        self.refill_rate = refill_rate_per_second
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Try to acquire a token. Returns True if successful, False if rate limited."""
        async with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(
            float(self.capacity),
            self.tokens + elapsed * self.refill_rate
        )
        self.last_refill = now


class ConcurrencyLimiter:
    """Asyncio semaphore-based concurrency limiter per client."""

    def __init__(self, limit: int):
        self.limit = limit
        self.semaphore = asyncio.Semaphore(limit)

    async def acquire_nonblocking(self) -> bool:
        """Non-blocking acquire. Returns True if slot available, False if at limit."""
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=0)
            return True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Release back if we somehow got it
            if not self.semaphore.locked() or self.semaphore._value < 0:
                self.semaphore.release()
            return False

    def release(self):
        """Release a concurrency slot."""
        self.semaphore.release()


class RateLimiter:
    """Token bucket rate limiter per client."""

    def __init__(self, capacity: int, refill_rate_per_second: float):
        self.bucket = TokenBucket(capacity, refill_rate_per_second)

    async def check_and_acquire(self) -> bool:
        """Check rate limit and acquire token. Returns True if allowed, False if rate limited."""
        return await self.bucket.acquire()

    def release(self):
        """Release a token (for manual refill control)."""
        self.bucket._refill()
        self.bucket.tokens = min(
            float(self.bucket.capacity),
            self.bucket.tokens + 1.0
        )


class ConcurrencyManager:
    """Per-client concurrency manager using asyncio semaphores."""

    def __init__(self, default_limit: int):
        self.default_limit = default_limit
        self.clients: Dict[str, ConcurrencyLimiter] = {}
        self._lock = asyncio.Lock()

    async def get_limiter(self, client_id: str) -> ConcurrencyLimiter:
        async with self._lock:
            if client_id not in self.clients:
                self.clients[client_id] = ConcurrencyLimiter(self.default_limit)
            return self.clients[client_id]

    async def try_acquire(self, client_id: str) -> bool:
        """Try to acquire concurrency slot for client. Non-blocking."""
        limiter = await self.get_limiter(client_id)
        return await limiter.acquire_nonblocking()

    def release(self, client_id: str):
        """Release concurrency slot for client."""
        if client_id in self.clients:
            self.clients[client_id].release()
