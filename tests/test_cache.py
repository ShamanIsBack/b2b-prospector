"""Tests for the on-disk response cache."""

from __future__ import annotations

from pathlib import Path

from b2b_prospector.infra.cache import NullCache, SqliteCache, make_cache_key

from .conftest import FakeClock

HOUR = 3600.0


def make_cache(tmp_path: Path, clock: FakeClock, ttl_seconds: float = HOUR) -> SqliteCache:
    return SqliteCache(tmp_path / "cache.sqlite", ttl_seconds=ttl_seconds, clock=clock)


class TestCacheKey:
    def test_key_is_stable(self) -> None:
        assert make_cache_key("gemini", "m", "q") == make_cache_key("gemini", "m", "q")

    def test_different_parts_give_different_keys(self) -> None:
        assert make_cache_key("gemini", "m", "q") != make_cache_key("serper", "m", "q")

    def test_boundaries_cannot_be_shifted_to_collide(self) -> None:
        """('ab','c') and ('a','bc') must not hash alike."""
        assert make_cache_key("ab", "c") != make_cache_key("a", "bc")


class TestSqliteCache:
    def test_roundtrip(self, tmp_path: Path, clock: FakeClock) -> None:
        cache = make_cache(tmp_path, clock)
        cache.put("k", '{"a": 1}')
        assert cache.get("k") == '{"a": 1}'
        assert cache.hits == 1
        assert cache.misses == 0

    def test_missing_key_counts_as_a_miss(self, tmp_path: Path, clock: FakeClock) -> None:
        cache = make_cache(tmp_path, clock)
        assert cache.get("absent") is None
        assert cache.misses == 1

    def test_entry_expires_after_ttl(self, tmp_path: Path, clock: FakeClock) -> None:
        cache = make_cache(tmp_path, clock, ttl_seconds=HOUR)
        cache.put("k", "v")

        clock.advance(HOUR - 1)
        assert cache.get("k") == "v"

        clock.advance(2)
        assert cache.get("k") is None

    def test_put_replaces_an_existing_entry(self, tmp_path: Path, clock: FakeClock) -> None:
        cache = make_cache(tmp_path, clock)
        cache.put("k", "old")
        cache.put("k", "new")
        assert cache.get("k") == "new"

    def test_cache_survives_reopening(self, tmp_path: Path, clock: FakeClock) -> None:
        """The point of an on-disk cache is that a later run pays nothing."""
        path = tmp_path / "cache.sqlite"
        first = SqliteCache(path, ttl_seconds=HOUR, clock=clock)
        first.put("k", "v")
        first.close()

        second = SqliteCache(path, ttl_seconds=HOUR, clock=clock)
        assert second.get("k") == "v"
        second.close()

    def test_purge_expired_removes_only_stale_rows(self, tmp_path: Path, clock: FakeClock) -> None:
        cache = make_cache(tmp_path, clock, ttl_seconds=HOUR)
        cache.put("old", "v")
        clock.advance(HOUR + 1)
        cache.put("fresh", "v")

        assert cache.purge_expired() == 1
        assert cache.get("fresh") == "v"

    def test_parent_directory_is_created(self, tmp_path: Path, clock: FakeClock) -> None:
        cache = SqliteCache(tmp_path / "a" / "b" / "c.sqlite", ttl_seconds=HOUR, clock=clock)
        cache.put("k", "v")
        assert cache.get("k") == "v"
        cache.close()

    def test_context_manager_closes(self, tmp_path: Path, clock: FakeClock) -> None:
        with make_cache(tmp_path, clock) as cache:
            cache.put("k", "v")
            assert cache.get("k") == "v"


class TestNullCache:
    def test_never_returns_anything(self) -> None:
        cache = NullCache()
        cache.put("k", "v")
        assert cache.get("k") is None
        assert cache.hits == 0
        assert cache.misses == 1

    def test_close_is_safe(self) -> None:
        NullCache().close()
