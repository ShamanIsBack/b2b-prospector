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
from grounded_prospector.models import SearchHit, SearchTarget
from grounded_prospector.providers.base import (
    Capabilities,
    ProviderAuthError,
    ProviderError,
    SearchResult,
)

SERPER_SEARCH_URL = "https://google.serper.dev/search"

# Serper accepts up to 100 results per request on paid plans. On free accounts a
# `num` above 10 combined with an operator-heavy query -- which every X-ray query
# is -- is rejected outright with "Query pattern not allowed for free accounts".
# Verified live on 2026-08-10: num=10 succeeds, num=20 and num=100 do not, while
# `page` works on every plan.
#
# So `num` is omitted by default. Google then returns its own page size (~10),
# which works on every plan and, in testing, actually returned *more* results
# than passing num=10 explicitly.
MAX_RESULTS_PER_PAGE = 100
FREE_TIER_MAX_RESULTS_PER_PAGE = 10

# Used only to judge whether a page looked full, when we did not set a size.
ASSUMED_PAGE_SIZE = 10

_AUTH_STATUS = frozenset({401, 403})
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


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
        results_per_page: int | None = None,
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
            results_per_page: Results requested per query. Leave as ``None`` to
                let the API choose, which is what free accounts require; paid
                plans may set up to 100.
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
        self._results_per_page = (
            min(results_per_page, MAX_RESULTS_PER_PAGE) if results_per_page else None
        )
        self._country = country
        self._language = language
        self._max_retries = max_retries
        self._on_retry = on_retry
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _body(self, query: str, page: int) -> dict[str, Any]:
        """Build the request body for one page of one query.

        ``num`` is sent only when explicitly configured -- see the note at the
        top of this module about free-account query patterns.
        """
        body: dict[str, Any] = {
            "q": query,
            "page": page,
            "gl": self._country,
            "hl": self._language,
        }
        if self._results_per_page is not None:
            body["num"] = self._results_per_page
        return body

    @staticmethod
    def _server_message(response: httpx.Response) -> str:
        """Extract the API's own explanation of a failure.

        Serper puts a genuinely useful sentence in the body of its 4xx replies,
        so discarding it in favour of a bare status code would throw away the
        only part of the response that says what to change.
        """
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return response.text.strip()[:200] or "no detail provided"
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return response.text.strip()[:200] or "no detail provided"

    def _hint_for(self, message: str) -> str:
        """Turn a known server message into actionable advice."""
        if "free account" in message.casefold() and self._results_per_page is not None:
            return (
                f"\nThis usually means num={self._results_per_page} is too high for a free "
                f"Serper plan when the query uses search operators. Free accounts allow at "
                f"most {FREE_TIER_MAX_RESULTS_PER_PAGE}; unset GP_RESULTS_PER_PAGE to let "
                f"the API choose, and use --pages to fetch more."
            )
        return ""

    async def _fetch(self, query: str, page: int) -> dict[str, Any]:
        """POST one query and return the decoded JSON body.

        Raises:
            ProviderAuthError: if the key was rejected.
            ProviderError: on any other permanent failure.
        """
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

        # Transient failures go back to the retry layer untouched.
        if response.status_code in _RETRYABLE_STATUS:
            response.raise_for_status()

        if response.is_error:
            message = self._server_message(response)
            raise ProviderError(
                f"Serper rejected the request (HTTP {response.status_code}): "
                f"{message}{self._hint_for(message)}"
            )

        try:
            payload: dict[str, Any] = response.json()
        except json.JSONDecodeError as error:
            raise ProviderError(f"Serper returned a non-JSON body: {error}") from error

        if not isinstance(payload, dict):
            raise ProviderError("Serper returned a JSON body that was not an object")
        return payload

    def _to_hits(
        self, payload: dict[str, Any], *, target: SearchTarget, query: str, page: int
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
                    target=target.name,
                    query=query,
                    provider=self.name,
                    retrieved_at=retrieved_at,
                )
            )
        return hits

    def _result(
        self, payload: dict[str, Any], *, target: SearchTarget, query: str, page: int, cached: bool
    ) -> SearchResult:
        """Assemble a :class:`SearchResult` from a decoded Serper payload."""
        hits = self._to_hits(payload, target=target, query=query, page=page)
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
            # more. Serper does not report this directly, and when we let the API
            # pick the page size we have to assume what "full" means.
            has_more=len(hits) >= (self._results_per_page or ASSUMED_PAGE_SIZE),
        )

    async def search(self, query: str, target: SearchTarget, *, page: int = 1) -> SearchResult:
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
                json.loads(cached), target=target, query=query, page=page, cached=True
            )

        if self._bucket is not None:
            await self._bucket.acquire()

        payload = await retry_async(
            lambda: self._fetch(query, page),
            max_retries=self._max_retries,
            sleeper=self._sleeper,
            on_retry=self._on_retry,
        )

        result = self._result(payload, target=target, query=query, page=page, cached=False)
        self._cache.put(cache_key, json.dumps(payload, ensure_ascii=False))
        return result

    async def aclose(self) -> None:
        """Close the HTTP client if this provider created it, and the cache."""
        if self._owns_client:
            await self._client.aclose()
        self._cache.close()
