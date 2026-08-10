"""Shared test fixtures.

Everything here keeps the suite offline and instant: no network, no real clock,
no real sleeping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


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


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded API response from ``tests/fixtures``."""
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return payload


@pytest.fixture
def gemini_interaction() -> dict[str, Any]:
    """Return the recorded Gemini grounding response used across provider tests."""
    return load_fixture("gemini_interaction_raw.json")
