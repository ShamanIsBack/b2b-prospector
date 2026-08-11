"""Tests for orchestration: pagination, budget, dedupe and scoring end to end."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grounded_prospector.models import SearchBrief, SearchHit, SearchTarget, TargetKind
from grounded_prospector.pipeline import (
    PipelineOptions,
    QueryBudget,
    exclusions_for,
    hits_to_prospects,
    plan_queries,
    run_pipeline,
)
from grounded_prospector.providers.base import Capabilities, ProviderError, SearchResult
from grounded_prospector.providers.fixture import FixtureProvider

NOW = datetime(2026, 8, 10, tzinfo=UTC)

BRIEF = SearchBrief(
    location="Dubai",
    roles=["MICE", "Director"],
    targets=[SearchTarget(name="Acme Events", segment="mice"), SearchTarget(name="Beta Travel")],
)


def hit(
    url: str, title: str, *, target: str = "Acme Events", snippet: str | None = None
) -> SearchHit:
    return SearchHit(
        url=url,
        title=title,
        target=target,
        query="q",
        provider="test",
        retrieved_at=NOW,
        snippet=snippet,
    )


class ScriptedProvider:
    """Returns a scripted page sequence per target and records what was asked."""

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

    async def search(self, query: str, target: SearchTarget, *, page: int = 1) -> SearchResult:  # noqa: ARG002
        self.requested.append((target.name, page))
        if self._fail_on == target.name:
            raise ProviderError(f"backend exploded for {target.name}")

        pages = self._pages.get(target.name, [])
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
    def test_one_query_per_target(self) -> None:
        assert len(plan_queries(BRIEF)) == 2

    def test_query_mentions_target_and_location(self) -> None:
        _, query = plan_queries(BRIEF)[0]
        assert '"Acme Events"' in query
        assert '"Dubai"' in query

    def test_roles_from_the_brief_reach_the_query(self) -> None:
        _, query = plan_queries(BRIEF)[0]
        assert '("MICE" OR "Director")' in query

    def test_keywords_from_the_brief_reach_the_query(self) -> None:
        """The wiring that was missing: the parameter existed but was never passed."""
        brief = BRIEF.model_copy(update={"keywords": ["luxury", "eco"]})
        _, query = plan_queries(brief)[0]
        assert '("luxury" OR "eco")' in query

    def test_no_empty_group_when_keywords_are_omitted(self) -> None:
        assert "()" not in plan_queries(BRIEF)[0][1]

    def test_a_retargeted_brief_produces_a_wholly_different_query(self) -> None:
        """Changing country, sector and seniority is a single-file edit."""
        warsaw = SearchBrief(
            location="Warsaw",
            country="pl",
            roles=["CTO"],
            keywords=["fintech"],
            targets=[SearchTarget(name="Booksy")],
        )
        _, query = plan_queries(warsaw)[0]
        assert '"Warsaw"' in query
        assert '("CTO")' in query
        assert '("fintech")' in query
        assert "Dubai" not in query


class TestHitsToProspects:
    def test_non_profile_urls_are_dropped(self) -> None:
        hits = [
            hit("https://www.linkedin.com/company/acme", "Acme Events | LinkedIn"),
            hit("https://news.example/article", "Top 10 Agencies"),
            hit("https://www.linkedin.com/in/jane-doe", "Jane Doe - MICE Manager - Acme Events"),
        ]
        assert len(hits_to_prospects(hits, BRIEF)) == 1

    def test_the_same_person_across_domains_collapses_to_one(self) -> None:
        title = "Jane Doe - MICE Manager - Acme Events | LinkedIn"
        hits = [
            hit("https://ae.linkedin.com/in/jane-doe", title),
            hit("https://www.linkedin.com/in/Jane-Doe/?trk=x", title),
        ]
        prospects = hits_to_prospects(hits, BRIEF)
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
        prospects = hits_to_prospects(hits, BRIEF)
        assert len(prospects) == 1
        assert prospects[0].confidence == 1.0
        assert prospects[0].headline == "MICE Manager"

    def test_segment_is_carried_over_from_the_target_list(self) -> None:
        hits = [hit("https://www.linkedin.com/in/j", "Jane Doe - MICE Manager - Acme Events")]
        assert hits_to_prospects(hits, BRIEF)[0].segment == "mice"

    def test_location_hint_comes_from_the_snippet(self) -> None:
        hits = [
            hit(
                "https://www.linkedin.com/in/j",
                "Jane Doe - MICE Manager - Acme Events",
                snippet="Dubai, United Arab Emirates · MICE Manager · Acme Events",
            )
        ]
        assert hits_to_prospects(hits, BRIEF)[0].location_hint == "Dubai, United Arab Emirates"

    def test_results_are_ordered_by_confidence(self) -> None:
        hits = [
            hit("https://www.linkedin.com/in/weak", "Someone Unknown | LinkedIn"),
            hit("https://www.linkedin.com/in/strong", "Jane Doe - MICE Manager - Acme Events"),
        ]
        prospects = hits_to_prospects(hits, BRIEF)
        assert prospects[0].confidence > prospects[1].confidence

    def test_empty_input_yields_no_prospects(self) -> None:
        assert hits_to_prospects([], BRIEF) == []


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
                            target="Beta Travel",
                        )
                    ]
                ],
            }
        )
        prospects, report = await run_pipeline(BRIEF, provider, PipelineOptions(max_pages=5))

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
        await run_pipeline(BRIEF, provider, PipelineOptions(max_pages=2))

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
        await run_pipeline(BRIEF, provider, PipelineOptions(max_pages=5))
        assert [p for n, p in provider.requested if n == "Acme Events"] == [1]

    async def test_an_empty_page_stops_pagination(self) -> None:
        provider = ScriptedProvider({"Acme Events": [], "Beta Travel": []})
        await run_pipeline(BRIEF, provider, PipelineOptions(max_pages=4))
        assert provider.requested.count(("Acme Events", 1)) == 1
        assert ("Acme Events", 2) not in provider.requested

    async def test_the_query_budget_is_a_hard_stop(self) -> None:
        pages = [
            [hit(f"https://www.linkedin.com/in/p{i}", f"P{i} X - Director - Acme Events")]
            for i in range(10)
        ]
        provider = ScriptedProvider({"Acme Events": pages, "Beta Travel": pages})
        _, report = await run_pipeline(
            BRIEF, provider, PipelineOptions(max_pages=10, max_queries=3, concurrency=1)
        )

        assert len(provider.requested) == 3
        assert report.queries_executed == 3
        assert any("budget" in message for message in report.errors)

    async def test_one_failing_target_does_not_abort_the_run(self) -> None:
        provider = ScriptedProvider(
            {
                "Acme Events": [
                    [hit("https://www.linkedin.com/in/a", "A Person - Director - Acme Events")]
                ]
            },
            fail_on="Beta Travel",
        )
        prospects, report = await run_pipeline(BRIEF, provider)

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
        _, report = await run_pipeline(BRIEF, provider)

        assert report.hits_total == 3
        assert report.hits_linkedin == 1
        assert report.prospects == 1

    async def test_demo_fixtures_produce_a_realistic_run(self) -> None:
        """The bundled demo must exercise dedupe, filtering and the review gate."""
        targets = SearchBrief(
            location="Dubai",
            roles=["MICE", "Incentive", "Events", "Concierge", "Outbound", "Director"],
            targets=[
                SearchTarget(name="Dune & Palm Events", segment="mice"),
                SearchTarget(name="Falcon Bay Travel", segment="boutique"),
                SearchTarget(name="Majlis Concierge", segment="concierge"),
            ],
        )
        prospects, report = await run_pipeline(targets, FixtureProvider(), PipelineOptions())

        assert report.hits_total > report.hits_linkedin, "noise should be present and discarded"
        assert report.prospects == len(prospects)
        assert any(p.needs_review for p in prospects), "the wrong-company case must be flagged"
        assert len({p.profile_url for p in prospects}) == len(prospects), "URLs must be unique"


@pytest.mark.parametrize("limit", [1, 2])
async def test_report_counts_targets_searched(limit: int) -> None:
    targets = BRIEF.model_copy(update={"targets": BRIEF.targets[:limit]})
    provider = ScriptedProvider({})
    _, report = await run_pipeline(targets, provider)
    assert report.targets_searched == limit


class TestExclusions:
    """Brief-level and target-level exclusions combine rather than compete."""

    def test_target_exclusions_add_to_brief_exclusions(self) -> None:
        brief = BRIEF.model_copy(
            update={
                "exclude": ("pogrzeb",),
                "targets": [SearchTarget(name="mistrz ceremonii", exclude=("wodzirej",))],
            }
        )
        assert exclusions_for(brief, brief.targets[0]) == ("pogrzeb", "wodzirej")

    def test_duplicates_are_dropped_but_order_is_kept(self) -> None:
        """The result is interpolated into a query a human reads in --dry-run."""
        brief = BRIEF.model_copy(
            update={
                "exclude": ("a", "b"),
                "targets": [SearchTarget(name="x", exclude=("b", "c"))],
            }
        )
        assert exclusions_for(brief, brief.targets[0]) == ("a", "b", "c")

    def test_a_brief_exclusion_reaches_the_query(self) -> None:
        brief = BRIEF.model_copy(update={"exclude": ("pogrzeb",)})
        assert all('-"pogrzeb"' in query for _, query in plan_queries(brief))


class TestTargetKindFlowsThrough:
    def test_a_phrase_target_marks_its_prospects(self) -> None:
        """Without this the CSV cannot tell the reader how to read `Target`."""
        brief = BRIEF.model_copy(
            update={"targets": [SearchTarget(name="konsultant ślubny", kind=TargetKind.PHRASE)]}
        )
        hits = [
            hit(
                "https://www.linkedin.com/in/anna-nowak",
                "Anna Nowak - Konsultant ślubny | LinkedIn",
                target="konsultant ślubny",
            )
        ]
        prospects = hits_to_prospects(hits, brief)
        assert [p.target_kind for p in prospects] == [TargetKind.PHRASE]

    def test_an_unknown_target_falls_back_to_company(self) -> None:
        """A hit whose target left the brief must not crash the run."""
        hits = [hit("https://www.linkedin.com/in/x-y", "X Y - Manager", target="Gone Ltd")]
        prospects = hits_to_prospects(hits, BRIEF)
        assert [p.target_kind for p in prospects] == [TargetKind.COMPANY]


class TestDedupePrefersActionableRows:
    """Regression: an exclusion veto must not delete a usable contact."""

    def test_a_vetoed_row_does_not_displace_a_clean_one(self) -> None:
        """The veto raises no flag on the score, so ranking on score alone lost people.

        The same person is found twice: once at their actual employer, once under
        a phrase that also matches funeral celebrants. The vetoed row scores
        higher, and before this was fixed it won and the person disappeared from
        the contactable list entirely.
        """
        brief = BRIEF.model_copy(
            update={
                "roles": ["Mistrz Ceremonii"],
                "targets": [
                    SearchTarget(name="Lawendowy Ślub"),
                    SearchTarget(
                        name="mistrz ceremonii",
                        kind=TargetKind.PHRASE,
                        exclude=("pogrzeb",),
                    ),
                ],
            }
        )
        url = "https://www.linkedin.com/in/jan-kowalski"
        clean = hit(
            url,
            "Jan Kowalski - Koordynator Wesel - Lawendowy Ślub | LinkedIn",
            target="Lawendowy Ślub",
            snippet="Szczecin · Lawendowy Ślub",
        )
        vetoed = hit(
            url,
            "Jan Kowalski - Mistrz Ceremonii Pogrzebowej | LinkedIn",
            target="mistrz ceremonii",
            snippet="Mistrz ceremonii pogrzebowej",
        )

        for order in ([clean, vetoed], [vetoed, clean]):
            (survivor,) = hits_to_prospects(order, brief)
            assert survivor.target == "Lawendowy Ślub"
            assert not survivor.needs_review

    def test_among_equally_actionable_rows_confidence_still_wins(self) -> None:
        """The original rule must survive: best evidence wins among usable rows."""
        url = "https://www.linkedin.com/in/jane-doe"
        weak = hit(url, "Jane Doe | LinkedIn", target="Acme Events")
        strong = hit(url, "Jane Doe - MICE Director - Acme Events | LinkedIn", target="Acme Events")

        (survivor,) = hits_to_prospects([weak, strong], BRIEF)
        assert survivor.headline == "MICE Director"
