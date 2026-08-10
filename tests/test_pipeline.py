"""Tests for orchestration: pagination, budget, dedupe and scoring end to end."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grounded_prospector.models import Agency, SearchHit, TargetList
from grounded_prospector.pipeline import (
    PipelineOptions,
    QueryBudget,
    hits_to_prospects,
    plan_queries,
    run_pipeline,
)
from grounded_prospector.providers.base import Capabilities, ProviderError, SearchResult
from grounded_prospector.providers.fixture import FixtureProvider

NOW = datetime(2026, 8, 10, tzinfo=UTC)

TARGETS = TargetList(
    location="Dubai",
    roles=["MICE", "Director"],
    agencies=[Agency(name="Acme Events", segment="mice"), Agency(name="Beta Travel")],
)


def hit(
    url: str, title: str, *, agency: str = "Acme Events", snippet: str | None = None
) -> SearchHit:
    return SearchHit(
        url=url,
        title=title,
        agency=agency,
        query="q",
        provider="test",
        retrieved_at=NOW,
        snippet=snippet,
    )


class ScriptedProvider:
    """Returns a scripted page sequence per agency and records what was asked."""

    name = "scripted"

    def __init__(
        self,
        pages: dict[str, list[list[SearchHit]]],
        *,
        supports_pagination: bool = True,
        fail_on: str | None = None,
    ) -> None:
        self.capabilities = Capabilities(
            supports_pagination=supports_pagination, provides_snippets=True
        )
        self._pages = pages
        self._fail_on = fail_on
        self.requested: list[tuple[str, int]] = []
        self.closed = False

    async def search(self, query: str, agency: Agency, *, page: int = 1) -> SearchResult:  # noqa: ARG002
        self.requested.append((agency.name, page))
        if self._fail_on == agency.name:
            raise ProviderError(f"backend exploded for {agency.name}")

        pages = self._pages.get(agency.name, [])
        hits = pages[page - 1] if 0 < page <= len(pages) else []
        return SearchResult(hits=hits, searches_billed=1, has_more=page < len(pages))

    async def aclose(self) -> None:
        self.closed = True


class TestQueryBudget:
    async def test_claims_up_to_the_limit(self) -> None:
        budget = QueryBudget(limit=2)
        assert await budget.claim()
        assert await budget.claim()
        assert not await budget.claim()
        assert budget.exhausted

    async def test_spent_never_exceeds_the_limit(self) -> None:
        budget = QueryBudget(limit=3)
        for _ in range(10):
            await budget.claim()
        assert budget.spent == 3


class TestPlanQueries:
    def test_one_query_per_agency(self) -> None:
        assert len(plan_queries(TARGETS)) == 2

    def test_query_mentions_agency_and_location(self) -> None:
        _, query = plan_queries(TARGETS)[0]
        assert '"Acme Events"' in query
        assert '"Dubai"' in query


class TestHitsToProspects:
    def test_non_profile_urls_are_dropped(self) -> None:
        hits = [
            hit("https://www.linkedin.com/company/acme", "Acme Events | LinkedIn"),
            hit("https://news.example/article", "Top 10 Agencies"),
            hit("https://www.linkedin.com/in/jane-doe", "Jane Doe - MICE Manager - Acme Events"),
        ]
        assert len(hits_to_prospects(hits, TARGETS)) == 1

    def test_the_same_person_across_domains_collapses_to_one(self) -> None:
        title = "Jane Doe - MICE Manager - Acme Events | LinkedIn"
        hits = [
            hit("https://ae.linkedin.com/in/jane-doe", title),
            hit("https://www.linkedin.com/in/Jane-Doe/?trk=x", title),
        ]
        prospects = hits_to_prospects(hits, TARGETS)
        assert len(prospects) == 1
        assert prospects[0].profile_url == "https://www.linkedin.com/in/jane-doe"

    def test_the_best_evidenced_duplicate_wins(self) -> None:
        """A later, richer result must not be discarded in favour of a poorer one."""
        hits = [
            hit("https://www.linkedin.com/in/jane-doe", "Jane Doe | LinkedIn"),
            hit(
                "https://ae.linkedin.com/in/jane-doe",
                "Jane Doe - MICE Manager - Acme Events | LinkedIn",
            ),
        ]
        prospects = hits_to_prospects(hits, TARGETS)
        assert len(prospects) == 1
        assert prospects[0].confidence == 1.0
        assert prospects[0].headline == "MICE Manager"

    def test_segment_is_carried_over_from_the_target_list(self) -> None:
        hits = [hit("https://www.linkedin.com/in/j", "Jane Doe - MICE Manager - Acme Events")]
        assert hits_to_prospects(hits, TARGETS)[0].segment == "mice"

    def test_location_hint_comes_from_the_snippet(self) -> None:
        hits = [
            hit(
                "https://www.linkedin.com/in/j",
                "Jane Doe - MICE Manager - Acme Events",
                snippet="Dubai, United Arab Emirates · MICE Manager · Acme Events",
            )
        ]
        assert hits_to_prospects(hits, TARGETS)[0].location_hint == "Dubai, United Arab Emirates"

    def test_results_are_ordered_by_confidence(self) -> None:
        hits = [
            hit("https://www.linkedin.com/in/weak", "Someone Unknown | LinkedIn"),
            hit("https://www.linkedin.com/in/strong", "Jane Doe - MICE Manager - Acme Events"),
        ]
        prospects = hits_to_prospects(hits, TARGETS)
        assert prospects[0].confidence > prospects[1].confidence

    def test_empty_input_yields_no_prospects(self) -> None:
        assert hits_to_prospects([], TARGETS) == []


class TestRunPipeline:
    async def test_walks_every_page_until_exhausted(self) -> None:
        provider = ScriptedProvider(
            {
                "Acme Events": [
                    [hit("https://www.linkedin.com/in/a", "A Person - Director - Acme Events")],
                    [hit("https://www.linkedin.com/in/b", "B Person - Director - Acme Events")],
                ],
                "Beta Travel": [
                    [
                        hit(
                            "https://www.linkedin.com/in/c",
                            "C Person - Director - Beta Travel",
                            agency="Beta Travel",
                        )
                    ]
                ],
            }
        )
        prospects, report = await run_pipeline(TARGETS, provider, PipelineOptions(max_pages=5))

        assert ("Acme Events", 2) in provider.requested
        assert ("Acme Events", 3) not in provider.requested, "must stop when has_more is False"
        assert len(prospects) == 3
        assert report.queries_executed == 3

    async def test_max_pages_caps_pagination(self) -> None:
        pages = [
            [hit(f"https://www.linkedin.com/in/p{i}", f"P{i} X - Director - Acme Events")]
            for i in range(5)
        ]
        provider = ScriptedProvider({"Acme Events": pages, "Beta Travel": []})
        await run_pipeline(TARGETS, provider, PipelineOptions(max_pages=2))

        acme_pages = [page for name, page in provider.requested if name == "Acme Events"]
        assert acme_pages == [1, 2]

    async def test_a_backend_without_pagination_is_only_asked_once(self) -> None:
        """Otherwise every page beyond the first is billed for nothing."""
        provider = ScriptedProvider(
            {
                "Acme Events": [
                    [hit("https://www.linkedin.com/in/a", "A P - Director - Acme Events")]
                ]
            },
            supports_pagination=False,
        )
        await run_pipeline(TARGETS, provider, PipelineOptions(max_pages=5))
        assert [p for n, p in provider.requested if n == "Acme Events"] == [1]

    async def test_an_empty_page_stops_pagination(self) -> None:
        provider = ScriptedProvider({"Acme Events": [], "Beta Travel": []})
        await run_pipeline(TARGETS, provider, PipelineOptions(max_pages=4))
        assert provider.requested.count(("Acme Events", 1)) == 1
        assert ("Acme Events", 2) not in provider.requested

    async def test_the_query_budget_is_a_hard_stop(self) -> None:
        pages = [
            [hit(f"https://www.linkedin.com/in/p{i}", f"P{i} X - Director - Acme Events")]
            for i in range(10)
        ]
        provider = ScriptedProvider({"Acme Events": pages, "Beta Travel": pages})
        _, report = await run_pipeline(
            TARGETS, provider, PipelineOptions(max_pages=10, max_queries=3, concurrency=1)
        )

        assert len(provider.requested) == 3
        assert report.queries_executed == 3
        assert any("budget" in message for message in report.errors)

    async def test_one_failing_agency_does_not_abort_the_run(self) -> None:
        provider = ScriptedProvider(
            {
                "Acme Events": [
                    [hit("https://www.linkedin.com/in/a", "A Person - Director - Acme Events")]
                ]
            },
            fail_on="Beta Travel",
        )
        prospects, report = await run_pipeline(TARGETS, provider)

        assert len(prospects) == 1
        assert any("Beta Travel" in message for message in report.errors)

    async def test_report_counts_discarded_noise(self) -> None:
        provider = ScriptedProvider(
            {
                "Acme Events": [
                    [
                        hit("https://www.linkedin.com/in/a", "A Person - Director - Acme Events"),
                        hit("https://www.linkedin.com/company/acme", "Acme Events | LinkedIn"),
                        hit("https://news.example/x", "Top 10"),
                    ]
                ],
                "Beta Travel": [],
            }
        )
        _, report = await run_pipeline(TARGETS, provider)

        assert report.hits_total == 3
        assert report.hits_linkedin == 1
        assert report.prospects == 1

    async def test_demo_fixtures_produce_a_realistic_run(self) -> None:
        """The bundled demo must exercise dedupe, filtering and the review gate."""
        targets = TargetList(
            location="Dubai",
            roles=["MICE", "Incentive", "Events", "Concierge", "Outbound", "Director"],
            agencies=[
                Agency(name="Dune & Palm Events", segment="mice"),
                Agency(name="Falcon Bay Travel", segment="boutique"),
                Agency(name="Majlis Concierge", segment="concierge"),
            ],
        )
        prospects, report = await run_pipeline(targets, FixtureProvider(), PipelineOptions())

        assert report.hits_total > report.hits_linkedin, "noise should be present and discarded"
        assert report.prospects == len(prospects)
        assert any(p.needs_review for p in prospects), "the wrong-company case must be flagged"
        assert len({p.profile_url for p in prospects}) == len(prospects), "URLs must be unique"


@pytest.mark.parametrize("limit", [1, 2])
async def test_report_counts_agencies_searched(limit: int) -> None:
    targets = TARGETS.model_copy(update={"agencies": TARGETS.agencies[:limit]})
    provider = ScriptedProvider({})
    _, report = await run_pipeline(targets, provider)
    assert report.agencies_searched == limit
