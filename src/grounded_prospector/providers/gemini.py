"""Gemini API provider using Grounding with Google Search — secondary backend.

The model is given a search expression and asked to cite what it finds. Its prose
answer is deliberately unused; only ``url_citation`` annotations become hits, so
no name here can be invented (``docs/DECISIONS.md`` ADR-003).

**This is not the default backend.** Grounding exposes no raw result list — the
API surfaces only the sources the model chose to cite, with no page or offset
parameter — so it cannot page through a result set, and the model rewrites the
query between runs. ADR-006 records the comparison that made Serper primary.
It is kept because it is a genuinely different way to reach the same index, and
because a provider abstraction with one implementation proves nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google import genai

from grounded_prospector.infra.cache import Cache, NullCache, make_cache_key
from grounded_prospector.infra.ratelimit import TokenBucket
from grounded_prospector.infra.retry import Sleeper, retry_async
from grounded_prospector.models import SearchTarget
from grounded_prospector.providers._interaction import parse_interaction
from grounded_prospector.providers.base import Capabilities, ProviderError, SearchResult
from grounded_prospector.query import SYSTEM_INSTRUCTION, build_prompt

GOOGLE_SEARCH_TOOL: list[dict[str, str]] = [{"type": "google_search"}]


class GeminiGroundingProvider:
    """Runs X-ray queries through the Gemini Interactions API."""

    name = "gemini"
    capabilities = Capabilities(
        supports_pagination=False,
        provides_snippets=False,
        results_per_page_max=None,
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        cache: Cache | None = None,
        bucket: TokenBucket | None = None,
        max_retries: int = 4,
        timeout_seconds: float = 120.0,
        on_retry: Callable[[int, float, BaseException], None] | None = None,
        sleeper: Sleeper = asyncio.sleep,
        client: Any = None,  # noqa: ANN401 -- the SDK client is not statically typed
    ) -> None:
        """Configure the provider.

        Args:
            api_key: Gemini API key.
            model: Model id, e.g. ``gemini-3.6-flash``.
            cache: Response cache. Defaults to no caching.
            bucket: Rate limiter. Defaults to unthrottled.
            max_retries: Extra attempts after a transient failure.
            timeout_seconds: Per-request timeout.
            on_retry: Called before each retry, for logging.
            sleeper: Awaitable sleep used for backoff, injectable for tests.
            client: Pre-built SDK client, injected by tests.
        """
        self._model = model
        self._cache = cache if cache is not None else NullCache()
        self._bucket = bucket
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds
        self._on_retry = on_retry
        self._sleeper = sleeper
        self._client = client if client is not None else genai.Client(api_key=api_key)

    async def _create_interaction(self, query: str, target: SearchTarget) -> dict[str, Any]:
        """Call the API once and return the response as a plain dict."""
        response = await self._client.aio.interactions.create(
            model=self._model,
            input=build_prompt(query, target.name, target.kind),
            system_instruction=SYSTEM_INSTRUCTION,
            tools=GOOGLE_SEARCH_TOOL,
            timeout=self._timeout_seconds,
        )

        # create() is typed as returning either an Interaction or a stream. We
        # never request streaming, so anything without model_dump is a surprise
        # worth failing loudly on rather than silently parsing as empty.
        dump = getattr(response, "model_dump", None)
        if dump is None:
            raise ProviderError(
                f"unexpected response type from the Gemini API: {type(response).__name__}"
            )
        payload: dict[str, Any] = dump(mode="json", exclude_none=True)
        return payload

    async def search(self, query: str, target: SearchTarget, *, page: int = 1) -> SearchResult:
        """Run ``query`` through Google Search grounding and return cited sources.

        Grounding has no pagination, so any page beyond the first returns nothing
        rather than silently re-fetching and re-billing page one.

        Raises:
            ProviderError: if the interaction failed or returned an unusable shape.
        """
        if page > 1:
            return SearchResult(hits=[], searches_billed=0, has_more=False)

        cache_key = make_cache_key(self.name, self._model, query)

        cached = self._cache.get(cache_key)
        if cached is not None:
            return parse_interaction(
                json.loads(cached),
                target=target,
                query=query,
                provider=self.name,
                retrieved_at=datetime.now(UTC),
                from_cache=True,
            )

        if self._bucket is not None:
            await self._bucket.acquire()

        payload = await retry_async(
            lambda: self._create_interaction(query, target),
            max_retries=self._max_retries,
            sleeper=self._sleeper,
            on_retry=self._on_retry,
        )

        # Only successful interactions are cached; a failed one would otherwise
        # be replayed for the whole TTL.
        result = parse_interaction(
            payload,
            target=target,
            query=query,
            provider=self.name,
            retrieved_at=datetime.now(UTC),
        )
        self._cache.put(cache_key, json.dumps(payload, ensure_ascii=False))
        return result

    async def aclose(self) -> None:
        """Close the cache; the SDK client manages its own transport."""
        self._cache.close()
