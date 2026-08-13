"""A provider that replays recorded responses instead of calling an API.

This exists for three reasons, in order of importance:

1. The whole test suite runs offline and deterministically.
2. ``--demo`` lets anyone clone the repository and see a complete, realistic run
   without signing up for anything.
3. Response parsing can be exercised against edge cases that are awkward to
   provoke on demand — a company page in the results, a duplicate profile under
   a country subdomain, a profile with no headline.

The replayed file may hold either backend's shape. Serper payloads carry an
``organic`` array; Gemini interactions carry ``steps``. Dispatching on that keeps
one fixture provider able to stand in for both.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from b2b_prospector.demo import DEMO_SERPER_RESPONSES
from b2b_prospector.models import SearchHit, SearchTarget
from b2b_prospector.providers._interaction import parse_interaction
from b2b_prospector.providers.base import (
    Capabilities,
    ProviderError,
    SearchResult,
)

# A stable timestamp keeps golden-file comparisons meaningful across runs.
DEMO_TIMESTAMP = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


class FixtureProvider:
    """Serves recorded payloads keyed by target name, and by page within target."""

    name = "fixture"
    capabilities = Capabilities(
        supports_pagination=True,
        provides_snippets=True,
        results_per_page_max=None,
    )

    def __init__(
        self,
        path: Path = DEMO_SERPER_RESPONSES,
        *,
        strict: bool = False,
        retrieved_at: datetime = DEMO_TIMESTAMP,
    ) -> None:
        """Load recorded payloads from ``path``.

        The file maps an target name either to a single payload, or to an object
        keyed by page number for multi-page recordings.

        Args:
            path: JSON file of recorded responses.
            strict: Raise on an target with no recording, rather than returning
                no results. Useful in tests, wrong for a demo run.
            retrieved_at: Timestamp stamped onto every replayed hit.

        Raises:
            ProviderError: if the file is missing or is not a JSON object.
        """
        self._strict = strict
        self._retrieved_at = retrieved_at

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ProviderError(f"fixture file not found: {path}") from error
        except json.JSONDecodeError as error:
            raise ProviderError(f"fixture file is not valid JSON: {path}") from error

        if not isinstance(raw, dict):
            raise ProviderError(f"fixture file must contain a JSON object: {path}")

        # Keys beginning with an underscore are documentation, not targets.
        self._payloads: dict[str, Any] = {
            key: value for key, value in raw.items() if not key.startswith("_")
        }

    @property
    def recorded_targets(self) -> list[str]:
        """Return the target names this provider can answer for."""
        return sorted(self._payloads)

    def _payload_for(self, target: SearchTarget, page: int) -> dict[str, Any] | None:
        """Return the recording for one target and page, if there is one."""
        recorded = self._payloads.get(target.name)
        if not isinstance(recorded, dict):
            return None

        # A recording is either a bare payload or a mapping of page -> payload.
        if "organic" in recorded or "steps" in recorded:
            return recorded if page == 1 else None

        by_page = recorded.get(str(page))
        return by_page if isinstance(by_page, dict) else None

    def _parse_serper(
        self, payload: dict[str, Any], *, target: SearchTarget, query: str, page: int
    ) -> SearchResult:
        """Turn a recorded Serper payload into hits."""
        hits: list[SearchHit] = []
        for entry in payload.get("organic") or []:
            if not isinstance(entry, dict):
                continue
            link = entry.get("link")
            if not isinstance(link, str) or not link.strip():
                continue
            title = entry.get("title")
            snippet = entry.get("snippet")
            position = entry.get("position")
            hits.append(
                SearchHit(
                    url=link.strip(),
                    title=title.strip() if isinstance(title, str) else "",
                    snippet=snippet.strip() if isinstance(snippet, str) else None,
                    position=position if isinstance(position, int) else None,
                    page=page,
                    target=target.name,
                    query=query,
                    provider=self.name,
                    retrieved_at=self._retrieved_at,
                )
            )
        return SearchResult(hits=hits, searches_billed=0)

    async def search(self, query: str, target: SearchTarget, *, page: int = 1) -> SearchResult:
        """Replay the recording for ``target`` and ``page``.

        Raises:
            ProviderError: in strict mode, if no recording exists for page 1.
        """
        payload = self._payload_for(target, page)
        if payload is None:
            if self._strict and page == 1:
                raise ProviderError(
                    f"no recorded response for {target.name!r}; "
                    f"recorded: {', '.join(self.recorded_targets) or 'none'}"
                )
            return SearchResult(hits=[], searches_billed=0, has_more=False)

        if "organic" in payload:
            result = self._parse_serper(payload, target=target, query=query, page=page)
            # Truthful pagination without inventing a field Serper does not send:
            # there is more only if the recording actually holds a next page.
            result.has_more = self._payload_for(target, page + 1) is not None
            return result

        return parse_interaction(
            payload,
            target=target,
            query=query,
            provider=self.name,
            retrieved_at=self._retrieved_at,
        )

    async def aclose(self) -> None:
        """No resources to release."""
