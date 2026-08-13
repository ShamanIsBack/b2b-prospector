"""Tests for interaction parsing and the search providers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from b2b_prospector.demo import DEMO_INTERACTIONS, DEMO_SERPER_RESPONSES
from b2b_prospector.infra.cache import SqliteCache
from b2b_prospector.infra.ratelimit import TokenBucket
from b2b_prospector.models import SearchTarget
from b2b_prospector.providers._interaction import (
    executed_queries,
    model_notes,
    parse_interaction,
    searches_billed,
)
from b2b_prospector.providers.base import ProviderError, SearchProvider
from b2b_prospector.providers.fixture import FixtureProvider
from b2b_prospector.providers.gemini import GeminiGroundingProvider

from .conftest import FakeClock

AGENCY = SearchTarget(name="Dune & Palm Events", segment="mice")
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def make_payload(annotations: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Build a minimal interaction payload carrying ``annotations``."""
    payload: dict[str, Any] = {
        "status": "completed",
        "steps": [
            {
                "type": "google_search_call",
                "id": "c1",
                "arguments": {"queries": ['site:linkedin.com/in "Acme"']},
            },
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "Some prose.", "annotations": annotations}],
            },
        ],
    }
    payload.update(extra)
    return payload


def citation(url: str, title: str = "T") -> dict[str, Any]:
    return {"type": "url_citation", "url": url, "title": title}


def parse(payload: dict[str, Any]) -> Any:
    return parse_interaction(payload, target=AGENCY, query="q", provider="test", retrieved_at=NOW)


class TestParseInteraction:
    def test_url_citations_become_hits(self) -> None:
        result = parse(make_payload([citation("https://a.example", "A")]))
        assert [(h.url, h.title) for h in result.hits] == [("https://a.example", "A")]

    def test_hits_carry_target_query_and_provider(self) -> None:
        result = parse(make_payload([citation("https://a.example")]))
        hit = result.hits[0]
        assert (hit.target, hit.query, hit.provider) == (AGENCY.name, "q", "test")

    def test_non_url_annotations_are_ignored(self) -> None:
        """Place and file citations share the annotation list but carry no page."""
        payload = make_payload(
            [{"type": "place_citation", "title": "Dubai"}, citation("https://a.example")]
        )
        assert len(parse(payload).hits) == 1

    def test_citations_without_a_url_are_dropped(self) -> None:
        payload = make_payload([{"type": "url_citation", "title": "no url"}, citation("https://a")])
        assert len(parse(payload).hits) == 1

    def test_missing_title_becomes_empty_string_not_none(self) -> None:
        payload = make_payload([{"type": "url_citation", "url": "https://a.example"}])
        assert parse(payload).hits[0].title == ""

    def test_absent_steps_yield_no_hits_rather_than_raising(self) -> None:
        assert parse({"status": "completed"}).hits == []

    def test_failed_interaction_raises(self) -> None:
        payload = make_payload([], status="failed", errors=[{"message": "quota exhausted"}])
        with pytest.raises(ProviderError, match="quota exhausted"):
            parse(payload)

    def test_missing_status_is_tolerated(self) -> None:
        """Fixtures trimmed by hand should still parse."""
        payload = {"steps": [{"type": "model_output", "content": []}]}
        assert parse(payload).hits == []


class TestBillingAndNotes:
    def test_executed_queries_are_reported(self) -> None:
        assert executed_queries(make_payload([])) == ['site:linkedin.com/in "Acme"']

    def test_billing_prefers_the_apis_own_count(self) -> None:
        payload = make_payload(
            [], usage={"grounding_tool_count": [{"type": "google_search", "count": 3}]}
        )
        assert searches_billed(payload) == 3

    def test_billing_ignores_other_grounding_tools(self) -> None:
        payload = make_payload(
            [],
            usage={
                "grounding_tool_count": [
                    {"type": "google_maps", "count": 9},
                    {"type": "google_search", "count": 2},
                ]
            },
        )
        assert searches_billed(payload) == 2

    def test_billing_falls_back_to_counting_queries(self) -> None:
        assert searches_billed(make_payload([])) == 1

    def test_notes_prefer_output_text(self) -> None:
        assert model_notes(make_payload([], output_text="Summary.")) == "Summary."

    def test_notes_fall_back_to_model_output_blocks(self) -> None:
        assert model_notes(make_payload([])) == "Some prose."

    def test_notes_are_none_when_absent(self) -> None:
        assert model_notes({"status": "completed"}) is None


class TestFixtureProvider:
    def test_satisfies_the_provider_protocol(self) -> None:
        assert isinstance(FixtureProvider(), SearchProvider)

    async def test_replays_recorded_serper_results(self) -> None:
        result = await FixtureProvider().search("q", AGENCY)
        urls = [hit.url for hit in result.hits]
        assert "https://ae.linkedin.com/in/layla-haddad" in urls
        assert "https://gulfbusiness.example/top-10-event-agencies-dubai-2026" in urls

    async def test_serper_replay_carries_snippets_and_rank(self) -> None:
        """--demo must exercise the primary backend's richer shape, not a subset."""
        result = await FixtureProvider().search("q", AGENCY)
        first = result.hits[0]
        assert first.snippet is not None
        assert first.position == 1

    async def test_pagination_walks_recorded_pages_then_stops(self) -> None:
        provider = FixtureProvider()
        page1 = await provider.search("q", AGENCY, page=1)
        page2 = await provider.search("q", AGENCY, page=2)
        page3 = await provider.search("q", AGENCY, page=3)

        assert page1.has_more
        assert not page2.has_more
        assert {h.url for h in page1.hits}.isdisjoint({h.url for h in page2.hits})
        assert page3.hits == []

    async def test_gemini_shaped_recordings_are_replayed_too(self) -> None:
        """One fixture provider stands in for both backends."""
        provider = FixtureProvider(DEMO_INTERACTIONS)
        result = await provider.search("q", AGENCY)
        urls = [hit.url for hit in result.hits]
        assert "https://ae.linkedin.com/in/layla-haddad" in urls
        # Grounding carries no snippets, and the replay must not invent any.
        assert all(hit.snippet is None for hit in result.hits)

    async def test_gemini_shaped_recordings_have_no_second_page(self) -> None:
        provider = FixtureProvider(DEMO_INTERACTIONS)
        assert (await provider.search("q", AGENCY, page=2)).hits == []

    async def test_unknown_target_returns_nothing_by_default(self) -> None:
        result = await FixtureProvider().search("q", SearchTarget(name="Nobody Ltd"))
        assert result.hits == []

    async def test_unknown_target_raises_in_strict_mode(self) -> None:
        provider = FixtureProvider(strict=True)
        with pytest.raises(ProviderError, match="no recorded response"):
            await provider.search("q", SearchTarget(name="Nobody Ltd"))

    def test_documentation_keys_are_not_targets(self) -> None:
        assert not any(name.startswith("_") for name in FixtureProvider().recorded_targets)

    def test_missing_file_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(ProviderError, match="not found"):
            FixtureProvider(tmp_path / "absent.json")

    def test_invalid_json_is_reported_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ProviderError, match="not valid JSON"):
            FixtureProvider(path)

    def test_non_object_json_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ProviderError, match="must contain a JSON object"):
            FixtureProvider(path)

    def test_demo_file_covers_every_demo_target(self) -> None:
        """--demo must not silently return nothing for a listed target."""
        recorded = set(FixtureProvider().recorded_targets)
        assert recorded == {
            "Dune & Palm Events",
            "Falcon Bay Travel",
            "Majlis Concierge",
            "luxury concierge",
        }


class FakeResponse:
    """Stands in for an SDK Interaction."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return self._payload


class FakeInteractions:
    """Records calls and returns a canned payload, optionally failing first."""

    def __init__(self, payload: dict[str, Any], errors: list[Exception] | None = None) -> None:
        self.payload = payload
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return FakeResponse(self.payload)


def fake_client(interactions: FakeInteractions) -> Any:
    return SimpleNamespace(aio=SimpleNamespace(interactions=interactions))


class TransientError(Exception):
    def __init__(self) -> None:
        super().__init__("temporarily unavailable")
        self.code = 503


class TestGeminiProvider:
    def make(self, interactions: FakeInteractions, **kwargs: Any) -> GeminiGroundingProvider:
        return GeminiGroundingProvider(
            api_key="unused",
            model="gemini-3.6-flash",
            client=fake_client(interactions),
            **kwargs,
        )

    def test_satisfies_the_provider_protocol(self) -> None:
        assert isinstance(self.make(FakeInteractions({})), SearchProvider)

    async def test_sends_the_search_tool_and_system_instruction(self) -> None:
        interactions = FakeInteractions(make_payload([citation("https://a.example")]))
        await self.make(interactions).search("my query", AGENCY)

        call = interactions.calls[0]
        assert call["tools"] == [{"type": "google_search"}]
        assert "my query" in call["input"]
        assert "search dispatcher" in call["system_instruction"]
        assert call["model"] == "gemini-3.6-flash"

    async def test_parses_citations_from_a_live_response(self) -> None:
        interactions = FakeInteractions(make_payload([citation("https://a.example", "A")]))
        result = await self.make(interactions).search("q", AGENCY)
        assert [h.url for h in result.hits] == ["https://a.example"]
        assert not result.from_cache

    async def test_a_second_identical_query_is_served_from_cache(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        interactions = FakeInteractions(make_payload([citation("https://a.example")]))
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=3600, clock=clock)
        provider = self.make(interactions, cache=cache)

        first = await provider.search("q", AGENCY)
        second = await provider.search("q", AGENCY)

        assert len(interactions.calls) == 1, "the second search should not hit the network"
        assert not first.from_cache
        assert second.from_cache
        assert [h.url for h in second.hits] == [h.url for h in first.hits]
        cache.close()

    async def test_a_different_model_misses_the_cache(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        """Otherwise switching model would replay the previous model's answer."""
        payload = make_payload([citation("https://a.example")])
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=3600, clock=clock)

        first = FakeInteractions(payload)
        await self.make(first, cache=cache).search("q", AGENCY)

        second = FakeInteractions(payload)
        provider = GeminiGroundingProvider(
            api_key="unused",
            model="gemini-3.5-flash",
            client=fake_client(second),
            cache=cache,
        )
        await provider.search("q", AGENCY)

        assert len(second.calls) == 1
        cache.close()

    async def test_failed_interactions_are_not_cached(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        """A cached failure would poison every run until the TTL expired."""
        payload = make_payload([], status="failed", errors=[{"message": "boom"}])
        interactions = FakeInteractions(payload)
        cache = SqliteCache(tmp_path / "c.sqlite", ttl_seconds=3600, clock=clock)
        provider = self.make(interactions, cache=cache)

        with pytest.raises(ProviderError):
            await provider.search("q", AGENCY)

        with pytest.raises(ProviderError):
            await provider.search("q", AGENCY)
        assert len(interactions.calls) == 2
        cache.close()

    async def test_transient_failures_are_retried(self, clock: FakeClock) -> None:
        interactions = FakeInteractions(
            make_payload([citation("https://a.example")]), errors=[TransientError()]
        )
        provider = self.make(interactions, max_retries=2, sleeper=clock.sleep)
        result = await provider.search("q", AGENCY)
        assert len(interactions.calls) == 2
        assert len(result.hits) == 1
        assert clock.slept, "backoff should have been applied between attempts"

    async def test_rate_limiter_is_consulted(self, clock: FakeClock) -> None:
        interactions = FakeInteractions(make_payload([citation("https://a.example")]))
        bucket = TokenBucket(60, capacity=1, clock=clock, sleeper=clock.sleep)
        provider = self.make(interactions, bucket=bucket)

        await provider.search("q1", AGENCY)
        await provider.search("q2", AGENCY)

        assert clock.slept == [pytest.approx(1.0)]

    async def test_unexpected_response_type_fails_loudly(self) -> None:
        class Streamish:
            pass

        interactions = FakeInteractions({})

        async def create(**_kwargs: Any) -> Any:
            return Streamish()

        interactions.create = create  # type: ignore[method-assign]
        with pytest.raises(ProviderError, match="unexpected response type"):
            await self.make(interactions).search("q", AGENCY)


@pytest.mark.parametrize("path", [DEMO_INTERACTIONS, DEMO_SERPER_RESPONSES])
def test_demo_fixture_files_are_valid_json(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)


@pytest.mark.parametrize("path", [DEMO_INTERACTIONS, DEMO_SERPER_RESPONSES])
def test_demo_fixtures_contain_no_real_linkedin_profiles(path: Path) -> None:
    """Guards the privacy decision: shipped demo data must be fabricated.

    The fictional slugs are listed explicitly so that pasting a real recording
    into a demo file fails the build instead of publishing someone's data.
    """
    allowed = {
        "layla-haddad",
        "ahmed-bin-rashid-al-maktoum",
        "priya-nair",
        "tomasz-wierzbicki",
        "marco-ferretti",
        "sara-okonkwo",
        "yusuf-demir",
        "elena-rossi",
        # Phrase-target demo: a genuine self-description, a recruiter caught by
        # an exclusion term, and someone the phrase matches only in the snippet.
        "layla-haddad-concierge",
        "tom-bexley",
        "nadia-fahim",
    }
    text = path.read_text(encoding="utf-8")
    slugs = set(re.findall(r"linkedin\.com/in/([A-Za-z0-9\-]+)", text))
    assert {slug.lower() for slug in slugs} <= allowed
