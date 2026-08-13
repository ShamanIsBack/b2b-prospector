"""On-disk response cache.

Re-running a prospecting run is normal: you widen the role list, fix a typo in an
target name, or just want the CSV again. Against a metered API each of those
re-runs would otherwise cost real quota, so identical queries are served from
SQLite until their entry expires.

The cache key covers every request part that changes the answer -- provider,
model, locale, query, page -- so switching any of them correctly misses rather
than returning another configuration's answer.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Protocol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_responses_expires_at ON responses (expires_at);
"""


def make_cache_key(*parts: str) -> str:
    """Hash the identifying parts of a request into a stable cache key.

    Parts are separated by a null byte so that ``("ab", "c")`` and ``("a", "bc")``
    cannot collide.
    """
    digest = hashlib.sha256("\0".join(parts).encode("utf-8"))
    return digest.hexdigest()


class Cache(Protocol):
    """The cache surface the pipeline depends on."""

    hits: int
    misses: int

    def get(self, key: str) -> str | None:
        """Return the cached payload for ``key``, or ``None``."""
        ...

    def put(self, key: str, payload: str) -> None:
        """Store ``payload`` under ``key``."""
        ...

    def close(self) -> None:
        """Release any underlying resources."""
        ...


class NullCache:
    """A cache that stores nothing, used by ``--no-cache``."""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> str | None:  # noqa: ARG002 -- Cache protocol
        """Always miss."""
        self.misses += 1
        return None

    def put(self, key: str, payload: str) -> None:
        """Discard the payload."""

    def close(self) -> None:
        """Nothing to release."""


class SqliteCache:
    """A TTL cache backed by a single SQLite file."""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Open (creating if needed) the cache database at ``path``."""
        self.hits = 0
        self.misses = 0
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()

        path.parent.mkdir(parents=True, exist_ok=True)
        # The pipeline is async with a thread-pool underneath, so the connection
        # is shared across threads and guarded by an explicit lock.
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        # Expired rows are unreadable but not free: without this sweep they
        # accumulate for as long as the file exists.
        self.purge_expired()

    def get(self, key: str) -> str | None:
        """Return a live cached payload, or ``None`` if missing or expired."""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload, expires_at FROM responses WHERE key = ?", (key,)
            ).fetchone()

            if row is None or row[1] <= self._clock():
                self.misses += 1
                return None

            self.hits += 1
            payload: str = row[0]
            return payload

    def put(self, key: str, payload: str) -> None:
        """Store ``payload``, replacing any existing entry for ``key``."""
        now = self._clock()
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO responses (key, payload, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (key, payload, now, now + self._ttl_seconds),
            )
            self._connection.commit()

    def purge_expired(self) -> int:
        """Delete expired rows and return how many were removed."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM responses WHERE expires_at <= ?", (self._clock(),)
            )
            self._connection.commit()
            return cursor.rowcount

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SqliteCache:
        """Enter a context manager that closes the connection on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection."""
        self.close()
