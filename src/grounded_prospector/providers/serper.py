"""Serper.dev provider — the primary backend.

Serper returns Google's organic results as JSON: title, link, snippet and rank,
with real pagination. That combination is what a prospecting tool needs and what
a grounding-style backend structurally cannot offer — see ``docs/DECISIONS.md``
ADR-006 for the comparison that put this one first.

Nothing here interprets results. Titles and snippets are passed through verbatim
for :mod:`grounded_prospector.extract` to parse deterministically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from grounded_prospector.infra.cache import Cache, NullCache, make_cache_key
from grounded_prospector.infra.ratelimit import TokenBucket
from grounded_prospector.infra.retry import Sleeper, retry_async
from grounded_prospector.models import Agency, SearchHit
from grounded_prospector.providers.base import (
    Capabilities,
    ProviderAuthError,
    ProviderError,
    SearchResult,
)

SERPER_SEARCH_URL = "https://google.serper.dev/search"

# Serper accepts up to 100 results per request. Asking for the maximum is the
# cheapest way to paginate: one billed query covers what would otherwise be ten.
MAX_RESULTS_PER_PAGE = 100
DEFAULT_RESULTS_PER_PAGE = 100

_AUTH_STATUS = frozenset({401, 403})


class SerperProvider:
    """Queries Google through Serper.dev and returns organic results."""

    name = "serper"
    capabilities = Capabilities(
        supports_pagination=True,
        provides_snippets=True,
        results_per_page_max=MAX_RESULTS_PER_PAGE,
    )

    def __init__(
        self,
        *,
        api_key: str,
        cache: Cache | None = None,
        bucket: TokenBucket | None = None,
        results_per_page: int = DEFAULT_RESULTS_PER_PAGE,
        country: str = "ae",
        language: str = "en",
        max_retries: int = 4,
        timeout_seconds: float = 30.0,
        on_retry: Callable[[int, float, BaseException], None] | None = None,
        sleeper: Sleeper = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure the provider.

        Args:
            api_key: Serper API key.
            cache: Response cache. Defaults to no caching.
            bucket: Rate limiter. Defaults to unthrottled.
            results_per_page: Results requested per query, capped at 100.
            country: Serper ``gl`` parameter, biasing results by country.
            language: Serper ``hl`` parameter.
            max_retries: Extra attempts after a transient failure.
            timeout_seconds: Per-request timeout.
            on_retry: Called before each retry, for logging.
            sleeper: Awaitable sleep used for backoff, injectable for tests.
            client: Pre-built HTTP client, injected by tests.

        Raises:
            ProviderAuthError: if no API key was supplied.
        """
        if not api_key or not api_key.strip():
            raise ProviderAuthError(
                "No SERPER_API_KEY found. Copy .env.example to .env and paste a key "
                "from https://serper.dev (2,500 free queries, no card), or run with "
                "--demo to use the bundled offline fixtures instead."
            )

        self._api_key = api_key
        self._cache = cache if cache is not None else NullCache()
        self._bucket = bucket
        self._results_per_page = min(results_per_page, MAX_RESULTS_PER_PAGE)
        self._country = country
        self._language = language
        self._max_retries = max_retries
        self._on_retry = on_retry
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _body(self, query: str, page: int) -> dict[str, Any]:
        """Build the request body for one page of one query."""
        return {
            "q": query,
            "num": self._results_per_page,
            "page": page,
            "gl": self._country,
            "hl": self._language,
        }

    async def _fetch(self, query: str, page: int) -> dict[str, Any]:
        """POST one query and return the decoded JSON body."""
        response = await self._client.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            json=self._body(query, page),
        )

        # Auth failures are raised as a distinct, non-retryable type: hammering
        # a rejected key wastes time and looks like an attack.
        if response.status_code in _AUTH_STATUS:
            raise ProviderAuthError(
                f"Serper rejected the API key (HTTP {response.status_code}). "
                "Check SERPER_API_KEY in your .env."
            )

        response.raise_for_status()

        try:
            payload: dict[str, Any] = response.json()
        except json.JSONDecodeError as error:
            raise ProviderError(f"Serper returned a non-JSON body: {error}") from error

        if not isinstance(payload, dict):
            raise ProviderError("Serper returned a JSON body that was not an object")
        return payload

    def _to_hits(
        self, payload: dict[str, Any], *, agency: Agency, query: str, page: int
    ) -> list[SearchHit]:
        """Map Serper's ``organic`` array onto search hits."""
        organic = payload.get("organic")
        if not isinstance(organic, list):
            return []

        retrieved_at = datetime.now(UTC)
        hits: list[SearchHit] = []
        for entry in organic:
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
                    agency=agency.name,
                    query=query,
                    provider=self.name,
                    retrieved_at=retrieved_at,
                )
            )
        return hits

    def _result(
        self, payload: dict[str, Any], *, agency: Agency, query: str, page: int, cached: bool
    ) -> SearchResult:
        """Assemble a :class:`SearchResult` from a decoded Serper payload."""
        hits = self._to_hits(payload, agency=agency, query=query, page=page)
        search_info = payload.get("searchInformation")
        total = None
        if isinstance(search_info, dict):
            candidate = search_info.get("totalResults")
            if isinstance(candidate, (int, str)):
                try:
                    total = int(candidate)
                except ValueError:
                    total = None

        return SearchResult(
            hits=hits,
            # Serper bills per request regardless of how many results come back,
            # and a cache hit costs nothing.
            searches_billed=0 if cached else 1,
            from_cache=cached,
            total_results=total,
            # A short page means the result set is exhausted; a full one implies
            # more. Serper does not report this directly.
            has_more=len(hits) >= self._results_per_page,
        )

    async def search(self, query: str, agency: Agency, *, page: int = 1) -> SearchResult:
        """Fetch one page of organic results for ``query``.

        Raises:
            ProviderAuthError: if the API key was rejected.
            ProviderError: if the response could not be understood.
        """
        cache_key = make_cache_key(
            self.name, str(self._results_per_page), self._country, query, str(page)
        )

        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._result(
                json.loads(cached), agency=agency, query=query, page=page, cached=True
            )

        if self._bucket is not None:
            await self._bucket.acquire()

        payload = await retry_async(
            lambda: self._fetch(query, page),
            max_retries=self._max_retries,
            sleeper=self._sleeper,
            on_retry=self._on_retry,
        )

        result = self._result(payload, agency=agency, query=query, page=page, cached=False)
        self._cache.put(cache_key, json.dumps(payload, ensure_ascii=False))
        return result

    async def aclose(self) -> None:
        """Close the HTTP client if this provider created it, and the cache."""
        if self._owns_client:
            await self._client.aclose()
        self._cache.close()
