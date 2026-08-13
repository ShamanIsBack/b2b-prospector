"""Tests for the token bucket, run entirely on a fake clock."""

from __future__ import annotations

import pytest

from b2b_prospector.infra.ratelimit import TokenBucket

from .conftest import FakeClock


def make_bucket(clock: FakeClock, rate_per_minute: float = 60.0, capacity: float = 2.0):
    return TokenBucket(rate_per_minute, capacity=capacity, clock=clock, sleeper=clock.sleep)


async def test_burst_up_to_capacity_does_not_sleep(clock: FakeClock) -> None:
    bucket = make_bucket(clock)
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == []


async def test_exceeding_capacity_waits_for_refill(clock: FakeClock) -> None:
    bucket = make_bucket(clock)  # 60/min == 1 token per second
    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == [pytest.approx(1.0)]


async def test_tokens_refill_over_elapsed_time(clock: FakeClock) -> None:
    bucket = make_bucket(clock)
    await bucket.acquire(2)
    assert bucket.available_tokens == pytest.approx(0.0)

    clock.advance(1.5)
    assert bucket.available_tokens == pytest.approx(1.5)


async def test_refill_is_capped_at_capacity(clock: FakeClock) -> None:
    bucket = make_bucket(clock)
    clock.advance(3600)
    assert bucket.available_tokens == pytest.approx(2.0)


async def test_requesting_more_than_capacity_fails_fast(clock: FakeClock) -> None:
    """Blocking forever would be the alternative, so this must raise."""
    bucket = make_bucket(clock)
    with pytest.raises(ValueError, match="cannot acquire"):
        await bucket.acquire(5)


def test_non_positive_rate_is_rejected(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        TokenBucket(0, clock=clock, sleeper=clock.sleep)


async def test_default_capacity_is_one_minute_of_tokens(clock: FakeClock) -> None:
    bucket = TokenBucket(15, clock=clock, sleeper=clock.sleep)
    assert bucket.available_tokens == pytest.approx(15.0)
