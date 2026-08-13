"""Retry with exponential backoff and full jitter.

Two details matter more than the backoff curve itself:

* Only transient failures are retried. Retrying a 400 wastes quota and hides a
  bug; retrying a 401 does the same and can trip abuse detection.
* ``Retry-After`` wins when the server sends it. Guessing a shorter delay than
  the server asked for is how a rate-limit becomes a ban.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

T = TypeVar("T")

Sleeper = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]

# Status codes worth a second attempt: timeouts, rate limits and server faults.
RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})

_STATUS_IN_TEXT = re.compile(r"\b(4\d{2}|5\d{2})\b")
_RETRY_AFTER_IN_TEXT = re.compile(r"retry[- ]after[\"':\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


class RetryError(RuntimeError):
    """Raised when every attempt failed, chaining the final underlying error."""


def _status_code_of(error: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception.

    Clients disagree about where the status lives: the Gemini SDK exposes
    ``code``, httpx nests it under ``response.status_code``, others use
    ``status_code`` directly. Falling back to the message text keeps a transport
    swap from silently disabling retries altogether.
    """
    for attribute in ("code", "status_code", "status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value

    response_status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(response_status, int):
        return response_status

    match = _STATUS_IN_TEXT.search(str(error))
    return int(match.group(1)) if match else None


def retry_after_seconds(error: BaseException) -> float | None:
    """Return the server-requested delay, if the error carries one."""
    for attribute in ("retry_after", "retry_delay"):
        value = getattr(error, attribute, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)

    match = _RETRY_AFTER_IN_TEXT.search(str(error))
    return float(match.group(1)) if match else None


def is_transient(error: BaseException) -> bool:
    """Return whether ``error`` is worth retrying."""
    if isinstance(error, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True

    # httpx timeouts and connection failures do not inherit from the builtins
    # above, so they need naming explicitly or every network blip looks fatal.
    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
        return True

    status = _status_code_of(error)
    if status is not None:
        return status in RETRYABLE_STATUS_CODES

    # An unrecognised error with no status is assumed permanent: retrying blind
    # against a metered API is the more expensive mistake.
    return False


def full_jitter(delay: float) -> float:
    """Return a random delay in ``[0, delay]``.

    Full jitter spreads retries from concurrent workers instead of synchronising
    them into a second thundering herd.
    """
    return random.uniform(0.0, delay)  # noqa: S311 -- backoff timing, not security


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    is_retryable: Callable[[BaseException], bool] = is_transient,
    sleeper: Sleeper = asyncio.sleep,
    jitter: Jitter = full_jitter,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """Call ``operation`` until it succeeds or the retry budget is exhausted.

    Args:
        operation: Zero-argument coroutine function to call.
        max_retries: Additional attempts after the first. ``0`` disables retrying.
        base_delay: Backoff base, doubled each attempt before jitter.
        max_delay: Ceiling applied before jitter.
        is_retryable: Predicate deciding whether an error deserves another try.
        sleeper: Awaitable sleep, injectable for tests.
        jitter: Maps a computed delay to an actual delay.
        on_retry: Called with ``(attempt, delay, error)`` before each retry.

    Returns:
        Whatever ``operation`` returns.

    Raises:
        RetryError: if every attempt failed with a retryable error.
        BaseException: the original error, unwrapped, if it was not retryable.
    """
    last_error: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            return await operation()
        except Exception as error:
            if not is_retryable(error):
                raise
            last_error = error
            if attempt == max_retries:
                break

            requested = retry_after_seconds(error)
            delay = (
                requested
                if requested is not None
                else jitter(min(base_delay * (2**attempt), max_delay))
            )
            if on_retry is not None:
                on_retry(attempt + 1, delay, error)
            await sleeper(delay)

    raise RetryError(f"giving up after {max_retries + 1} attempts: {last_error}") from last_error
