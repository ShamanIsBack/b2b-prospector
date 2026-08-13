"""An asyncio token bucket.

Free-tier grounding quota is measured per minute as well as per month, so the
pipeline paces itself rather than discovering the limit through 429s. Time and
sleep are injected so the tests can run on a fake clock instead of really waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Limits how often an operation may run, smoothed over time.

    Tokens refill continuously rather than in per-minute steps, so a burst at the
    top of one minute cannot be followed immediately by another at the start of
    the next.
    """

    def __init__(
        self,
        rate_per_minute: float,
        *,
        capacity: float | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        """Create a bucket refilling at ``rate_per_minute`` tokens per minute.

        Args:
            rate_per_minute: Sustained rate. Must be positive.
            capacity: Maximum burst size. Defaults to one minute's worth.
            clock: Monotonic time source, injectable for tests.
            sleeper: Awaitable sleep, injectable for tests.

        Raises:
            ValueError: if ``rate_per_minute`` is not positive.
        """
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")

        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = capacity if capacity is not None else float(rate_per_minute)
        self._tokens = self._capacity
        self._clock = clock
        self._sleeper = sleeper
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Add the tokens accrued since the last update."""
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available, then consume them.

        Raises:
            ValueError: if ``tokens`` exceeds the bucket's capacity, which would
                otherwise block forever.
        """
        if tokens > self._capacity:
            raise ValueError(f"cannot acquire {tokens} tokens from a bucket of {self._capacity}")

        # The lock keeps concurrent workers from each seeing the same free token.
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await self._sleeper(deficit / self._rate_per_second)

    @property
    def available_tokens(self) -> float:
        """Return the tokens available right now, refilling first."""
        self._refill()
        return self._tokens
