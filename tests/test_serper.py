"""Tests for the Serper.dev provider, run against a mock transport."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from grounded_prospector.infra.cache import SqliteCache
from grounded_prospector.models import Agency
from grounded_prospector.providers.base import (
    ProviderAuthError,
    ProviderError,
    SearchProvider,
)
from grounded_prospector.providers.serper import MAX_RESULTS_PER_PAGE, SerperProvider

from .conftest import FakeClock

AGENCY = Agency(name="Dune & Palm Events", segment="mice")


def organic(count: int, *, start: int = 1) -> list[dict[str, Any]]:
    """Build ``count`` plausible organic entries."""
    return [
        {
            "position": index,
            "title": f"Person {index} - MICE Manager - Dune & Palm Events | LinkedIn",
            "link": f"https://www.linkedin.com/in/person-{index}",
            "snippet": f"Dubai, United Arab Emirates · MICE Manager · Entry {index}.",
        }
        for index in range(start, start + count)
    ]


class Recorder:
    """A mock transport that records requests and replays scripted responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(200, json={"organic": []})
        return self._responses.pop(0)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def make_provider(recorder: Recorder, **kwargs: Any) -> SerperProvider:
    return SerperProvider(
        api_key="test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        **kwargs,
    )


def ok(entries: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    payload: dict[str, Any] = {"organic": entries}
    if total is not None:
        payload["searchInformation"] = {"totalResults": total}
    return httpx.Response(200, json=payload)


class TestConstruction:
    def test_satisfies_the_provider_protocol(self) -> None:
        assert isinstance(make_provider(Recorder()), SearchProvider)

    def test_declares_pagination_and_snippet_support(self) -> None:
        provider = make_provider(Recorder())
        assert provider.capabilities.supports_pagination
        assert provider.capabilities.provides_snippets

    @pytest.mark.parametrize("key", ["", "   "])
    def test_missing_key_fails_with_actionable_guidance(self, key: str) -> None:
        with pytest.raises(ProviderAuthError, match=re.escape("serper.dev")):
            SerperProvider(api_key=key)


class TestRequestShape:
    async def test_posts_to_serper_with_the_api_key_header(self) -> None:
        recorder = Recorder(ok(organic(1)))
        await make_provider(recorder).search("my query", AGENCY)

        request = recorder.requests[0]
        assert str(request.url) == "https://google.serper.dev/search"
        assert request.method == "POST"
        assert request.headers["X-API-KEY"] == "test-key"

    async def test_body_carries_query_pagination_and_locale(self) -> None:
        recorder = Recorder(ok(organic(1)))
        provider = make_provider(recorder, results_per_page=25, country="pl", language="en")
        await provider.search("my query", AGENCY, page=3)

        assert recorder.bodies[0] == {
            "q": "my query",
            "num": 25,
            "page": 3,
            "gl": "pl",
            "hl": "en",
        }

    async def test_num_is_omitted_by_default(self) -> None:
        """Free Serper plans reject num > 10 on operator-heavy queries, and every
        X-ray query is operator-heavy -- so by default we let the API choose."""
        recorder = Recorder(ok(organic(1)))
        await make_provider(recorder).search("q", AGENCY)
        assert "num" not in recorder.bodies[0]

    async def test_page_size_is_capped_at_the_api_maximum(self) -> None:
        recorder = Recorder(ok(organic(1)))
        provider = make_provider(recorder, results_per_page=500)
        await provider.search("q", AGENCY)
        assert recorder.bodies[0]["num"] == MAX_RESULTS_PER_PAGE


class TestParsing:
    async def test_organic_entries_become_hits_with_snippet_and_rank(self) -> None:
        recorder = Recorder(ok(organic(2), total=17))
        result = await make_provider(recorder).search("q", AGENCY, page=2)

        assert [hit.url for hit in result.hits] == [
            "https://www.linkedin.com/in/person-1",
            "https://www.linkedin.com/in/person-2",
        ]
        first = result.hits[0]
        assert first.position == 1
        assert first.snippet is not None
        assert first.page == 2
        assert first.agency == AGENCY.name
        assert first.provider == "serper"
        assert result.total_results == 17

    async def test_a_full_page_implies_more_results(self) -> None:
        recorder = Recorder(ok(organic(10)))
        provider = make_provider(recorder, results_per_page=10)
        assert (await provider.search("q", AGENCY)).has_more

    async def test_a_short_page_means_the_result_set_is_exhausted(self) -> None:
        recorder = Recorder(ok(organic(3)))
        provider = make_provider(recorder, results_per_page=10)
        assert not (await provider.search("q", AGENCY)).has_more

    async def test_missing_organic_array_yields_no_hits(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"searchParameters": {}}))
        assert (await make_provider(recorder).search("q", AGENCY)).hits == []

    async def test_entries_without_a_link_are_skipped(self) -> None:
        recorder = Recorder(
            ok([{"title": "No link here"}, {"title": "T", "link": "https://a.example"}])
        )
        result = await make_provider(recorder).search("q", AGENCY)
        assert [hit.url for hit in result.hits] == ["https://a.example"]

    async def test_malformed_entries_do_not_break_the_page(self) -> None:
        recorder = Recorder(ok(["not an object", {"title": "T", "link": "https://a.example"}]))
        result = await make_provider(recorder).search("q", AGENCY)
        assert len(result.hits) == 1

    async def test_missing_title_becomes_empty_string(self) -> None:
        recorder = Recorder(ok([{"link": "https://a.example"}]))
        assert (await make_provider(recorder).search("q", AGENCY)).hits[0].title == ""

    async def test_non_json_body_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, content=b"<html>gateway</html>"))
        with pytest.raises(ProviderError, match="non-JSON"):
            await make_provider(recorder).search("q", AGENCY)

    async def test_json_array_body_is_rejected(self) -> None:
        recorder = Recorder(httpx.Response(200, json=[1, 2, 3]))
        with pytest.raises(ProviderError, match="not an object"):
            await make_provider(recorder).search("q", AGENCY)


class TestErrorHandling:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_key_raises_auth_error_without_retrying(
        self, status: int, clock: FakeClock
    ) -> None:
        recorder = Recorder(*[httpx.Response(status, json={"message": "nope"})] * 5)
        provider = make_provider(recorder, max_retries=3, sleeper=clock.sleep)

        with pytest.raises(ProviderAuthError, match="rejected the API key"):
            await provider.search("q", AGENCY)
        assert len(recorder.requests) == 1, "an invalid key must not be retried"

    async def test_rate_limiting_is_retried(self, clock: FakeClock) -> None:
        recorder = Recorder(httpx.Response(429), ok(organic(1)))
        provider = make_provider(recorder, max_retries=3, sleeper=clock.sleep)

        result = await provider.search("q", AGENCY)
        assert len(recorder.requests) == 2
        assert len(result.hits) == 1

    async def test_server_errors_are_retried(self, clock: FakeClock) -> None:
        recorder = Recorder(httpx.Response(503), httpx.Response(502), ok(organic(1)))
        provider = make_provider(recorder, max_retries=3, sleeper=clock.sleep)

        assert len((await provider.search("q", AGENCY)).hits) == 1
        assert len(recorder.requests) == 3

    async def test_client_errors_are_not_retried(self, clock: FakeClock) -> None:
        recorder = Recorder(*[httpx.Response(400, json={"message": "bad query"})] * 5)
        provider = make_provider(recorder, max_retries=3, sleeper=clock.sleep)

        with pytest.raises(ProviderError):
            await provider.search("q", AGENCY)
        assert len(recorder.requests) == 1

    async def test_the_servers_own_explanation_is_surfaced(self, clock: FakeClock) -> None:
        """A 400 body says what to change; discarding it would waste the reply."""
        recorder = Recorder(httpx.Response(400, json={"message": "bad query"}))
        provider = make_provider(recorder, sleeper=clock.sleep)

        with pytest.raises(ProviderError, match="bad query"):
            await provider.search("q", AGENCY)

    async def test_free_account_rejection_explains_the_page_size(self, clock: FakeClock) -> None:
        """Serper rejects num > 10 on free plans for operator-heavy queries."""
        recorder = Recorder(
            httpx.Response(400, json={"message": "Query pattern not allowed for free accounts."})
        )
        provider = make_provider(recorder, results_per_page=100, sleeper=clock.sleep)

        with pytest.raises(ProviderError, match="GP_RESULTS_PER_PAGE"):
            await provider.search("q", AGENCY)

    async def test_non_json_error_body_still_produces_a_message(self, clock: FakeClock) -> None:
        recorder = Recorder(httpx.Response(400, content=b"upstream exploded"))
        provider = make_provider(recorder, sleeper=clock.sleep)

        with pytest.raises(ProviderError, match="upstream exploded"):
            await provider.search("q", AGENCY)


class TestCaching:
    async def test_repeating_a_query_costs_nothing(self, tmp_path: Path, clock: FakeClock) -> None:
        recorder = Recorder(ok(organic(2)))
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=3600, clock=clock)
        provider = make_provider(recorder, cache=cache)

        first = await provider.search("q", AGENCY)
        second = await provider.search("q", AGENCY)

        assert len(recorder.requests) == 1
        assert first.searches_billed == 1
        assert second.searches_billed == 0
        assert second.from_cache
        assert [h.url for h in second.hits] == [h.url for h in first.hits]
        cache.close()

    async def test_each_page_is_cached_separately(self, tmp_path: Path, clock: FakeClock) -> None:
        """Sharing a key across pages would replay page one forever."""
        recorder = Recorder(ok(organic(2)), ok(organic(2, start=3)))
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=3600, clock=clock)
        provider = make_provider(recorder, cache=cache)

        page1 = await provider.search("q", AGENCY, page=1)
        page2 = await provider.search("q", AGENCY, page=2)

        assert len(recorder.requests) == 2
        assert [h.url for h in page1.hits] != [h.url for h in page2.hits]
        cache.close()

    async def test_changing_page_size_misses_the_cache(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        recorder = Recorder(ok(organic(2)), ok(organic(2)))
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=3600, clock=clock)

        await make_provider(recorder, cache=cache, results_per_page=10).search("q", AGENCY)
        await make_provider(recorder, cache=cache, results_per_page=20).search("q", AGENCY)

        assert len(recorder.requests) == 2
        cache.close()


class TestLifecycle:
    async def test_aclose_leaves_an_injected_client_alone(self) -> None:
        """The caller owns a client it supplied, so the provider must not close it."""
        client = httpx.AsyncClient(transport=httpx.MockTransport(Recorder()))
        provider = SerperProvider(api_key="k", client=client)
        await provider.aclose()
        assert not client.is_closed
        await client.aclose()
