"""Shared test fixtures.

Everything here keeps the suite offline and instant: no network, no real clock,
no real sleeping.

Provider responses are *not* loaded from here. Each provider test builds the
payload shape it is exercising inline -- see ``make_payload`` in
``test_providers.py`` and the ``httpx.MockTransport`` handlers in
``test_serper.py`` -- so a test states the response it depends on next to the
assertion about it.
"""

from __future__ import annotations

import pytest


class FakeClock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward without recording a sleep."""
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        """Record a sleep and advance the clock instead of waiting."""
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    """Return a fresh fake clock."""
    return FakeClock()
