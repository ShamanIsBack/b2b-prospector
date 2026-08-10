"""Tests for retry classification and backoff."""

from __future__ import annotations

import pytest

from grounded_prospector.infra.retry import (
    RetryError,
    full_jitter,
    is_transient,
    retry_after_seconds,
    retry_async,
)

from .conftest import FakeClock


class FakeApiError(Exception):
    """Stands in for the SDK's error type, which also exposes ``code``."""

    def __init__(self, code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.code = code


def no_jitter(delay: float) -> float:
    """Identity jitter, so backoff delays are exactly predictable in tests."""
    return delay


class TestClassification:
    @pytest.mark.parametrize("code", [408, 409, 429, 500, 502, 503, 504])
    def test_transient_status_codes_are_retried(self, code: int) -> None:
        assert is_transient(FakeApiError(code))

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retried(self, code: int) -> None:
        """Retrying a 403 wastes quota and can look like abuse."""
        assert not is_transient(FakeApiError(code))

    @pytest.mark.parametrize("error", [TimeoutError(), ConnectionError()])
    def test_network_failures_are_retried(self, error: Exception) -> None:
        assert is_transient(error)

    def test_status_code_is_recovered_from_the_message_when_absent(self) -> None:
        assert is_transient(RuntimeError("server replied 503 Service Unavailable"))

    def test_unknown_errors_are_treated_as_permanent(self) -> None:
        assert not is_transient(ValueError("something structural"))


class TestRetryAfter:
    def test_attribute_is_preferred(self) -> None:
        error = FakeApiError(429)
        error.retry_after = 7.5  # type: ignore[attr-defined]
        assert retry_after_seconds(error) == 7.5

    def test_value_is_parsed_from_the_message(self) -> None:
        assert retry_after_seconds(FakeApiError(429, "rate limited, retry-after: 12")) == 12.0

    def test_absent_value_is_none(self) -> None:
        assert retry_after_seconds(FakeApiError(500)) is None


class TestRetryAsync:
    async def test_success_on_first_attempt_does_not_sleep(self, clock: FakeClock) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await retry_async(operation, sleeper=clock.sleep) == "ok"
        assert calls == 1
        assert clock.slept == []

    async def test_retries_until_success(self, clock: FakeClock) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise FakeApiError(503)
            return "ok"

        result = await retry_async(operation, sleeper=clock.sleep, jitter=no_jitter)
        assert result == "ok"
        assert calls == 3

    async def test_backoff_doubles(self, clock: FakeClock) -> None:
        async def operation() -> str:
            raise FakeApiError(503)

        with pytest.raises(RetryError):
            await retry_async(
                operation, max_retries=3, base_delay=1.0, sleeper=clock.sleep, jitter=no_jitter
            )
        assert clock.slept == [1.0, 2.0, 4.0]

    async def test_backoff_is_capped(self, clock: FakeClock) -> None:
        async def operation() -> str:
            raise FakeApiError(503)

        with pytest.raises(RetryError):
            await retry_async(
                operation,
                max_retries=4,
                base_delay=10.0,
                max_delay=20.0,
                sleeper=clock.sleep,
                jitter=no_jitter,
            )
        assert clock.slept == [10.0, 20.0, 20.0, 20.0]

    async def test_server_requested_delay_overrides_backoff(self, clock: FakeClock) -> None:
        """Undercutting a Retry-After is how a rate limit becomes a ban."""

        async def operation() -> str:
            raise FakeApiError(429, "slow down, retry-after: 30")

        with pytest.raises(RetryError):
            await retry_async(
                operation, max_retries=1, base_delay=1.0, sleeper=clock.sleep, jitter=no_jitter
            )
        assert clock.slept == [30.0]

    async def test_permanent_error_is_raised_unwrapped_and_immediately(
        self, clock: FakeClock
    ) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            raise FakeApiError(400, "bad request")

        with pytest.raises(FakeApiError):
            await retry_async(operation, sleeper=clock.sleep)
        assert calls == 1
        assert clock.slept == []

    async def test_zero_retries_attempts_once(self, clock: FakeClock) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            raise FakeApiError(503)

        with pytest.raises(RetryError):
            await retry_async(operation, max_retries=0, sleeper=clock.sleep)
        assert calls == 1

    async def test_on_retry_callback_receives_attempt_delay_and_error(
        self, clock: FakeClock
    ) -> None:
        seen: list[tuple[int, float]] = []

        async def operation() -> str:
            raise FakeApiError(503)

        with pytest.raises(RetryError):
            await retry_async(
                operation,
                max_retries=2,
                base_delay=1.0,
                sleeper=clock.sleep,
                jitter=no_jitter,
                on_retry=lambda attempt, delay, _error: seen.append((attempt, delay)),
            )
        assert seen == [(1, 1.0), (2, 2.0)]


def test_full_jitter_stays_within_bounds() -> None:
    assert all(0.0 <= full_jitter(5.0) <= 5.0 for _ in range(100))
