"""Orchestration: target list in, scored prospects and a run report out.

Two responsibilities live here and nowhere else:

* **Pagination**, including knowing when to stop. Paging past the end of a result
  set costs real money and returns nothing, so the loop exits on the first of:
  an empty page, a page the backend said was the last, a backend that cannot
  paginate at all, the page limit, or the query budget.
* **The query budget**, enforced across concurrent workers. A runaway loop
  against a metered API is this tool's expensive failure mode, so the ceiling is
  checked before every request rather than tallied afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from grounded_prospector.extract import (
    extract_location_hint,
    parse_title,
    score_prospect,
    split_name,
)
from grounded_prospector.models import Agency, Prospect, RunReport, SearchHit, TargetList
from grounded_prospector.providers.base import ProviderError, SearchProvider
from grounded_prospector.query import build_xray_query
from grounded_prospector.urls import canonicalise_profile_url, dedupe_key


@dataclass
class PipelineOptions:
    """Knobs for one run."""

    max_pages: int = 3
    max_queries: int = 50
    concurrency: int = 3


@dataclass
class QueryBudget:
    """A shared, concurrency-safe cap on how many queries a run may issue."""

    limit: int
    spent: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def claim(self) -> bool:
        """Reserve one query, returning whether the budget allowed it."""
        async with self._lock:
            if self.spent >= self.limit:
                return False
            self.spent += 1
            return True

    @property
    def exhausted(self) -> bool:
        """Whether the budget has been fully spent."""
        return self.spent >= self.limit


async def _collect_agency(
    provider: SearchProvider,
    agency: Agency,
    query: str,
    options: PipelineOptions,
    budget: QueryBudget,
    errors: list[str],
) -> tuple[list[SearchHit], int]:
    """Page through one agency's results, returning its hits and billed searches."""
    hits: list[SearchHit] = []
    billed = 0

    for page in range(1, options.max_pages + 1):
        if not await budget.claim():
            errors.append(f"query budget of {budget.limit} reached before finishing {agency.name}")
            break

        try:
            result = await provider.search(query, agency, page=page)
        except ProviderError as error:
            errors.append(f"{agency.name} (page {page}): {error}")
            break

        hits.extend(result.hits)
        billed += result.searches_billed

        if not result.hits:
            break
        if not provider.capabilities.supports_pagination:
            break
        if result.has_more is False:
            break

    return hits, billed


def hits_to_prospects(hits: Sequence[SearchHit], targets: TargetList) -> list[Prospect]:
    """Turn raw search hits into scored, deduplicated prospects.

    Non-profile URLs are dropped here rather than at the provider, so the run
    report can report honestly on how much of what we paid for was noise.

    When the same person appears more than once -- under a country subdomain, on
    a later page, or from a different agency's query -- the highest-scoring
    record wins, since that is the one with the most corroborating evidence.
    """
    by_name = {agency.name: agency for agency in targets.agencies}
    best: dict[str, Prospect] = {}

    for hit in hits:
        profile_url = canonicalise_profile_url(hit.url)
        key = dedupe_key(hit.url)
        if profile_url is None or key is None:
            continue

        parsed = parse_title(hit.title)
        first_name, last_name = split_name(parsed.name)
        confidence = score_prospect(
            raw_title=hit.title,
            name=parsed.name,
            headline=parsed.headline,
            agency=hit.agency,
            roles=targets.roles,
            snippet=hit.snippet,
        )

        agency = by_name.get(hit.agency)
        prospect = Prospect(
            first_name=first_name,
            last_name=last_name,
            headline=parsed.headline,
            company_from_title=parsed.company,
            profile_url=profile_url,
            agency=hit.agency,
            segment=agency.segment if agency else None,
            confidence=confidence.score,
            needs_review=confidence.needs_review,
            review_reasons=confidence.reasons,
            location_hint=extract_location_hint(hit.snippet),
            raw_title=hit.title,
            snippet=hit.snippet,
            serp_position=hit.position,
            source_query=hit.query,
            provider=hit.provider,
            retrieved_at=hit.retrieved_at,
        )

        existing = best.get(key)
        if existing is None or prospect.confidence > existing.confidence:
            best[key] = prospect

    return sorted(best.values(), key=lambda p: (-p.confidence, p.agency, p.profile_url))


def plan_queries(targets: TargetList) -> list[tuple[Agency, str]]:
    """Build the query for every agency, without issuing anything."""
    return [
        (agency, build_xray_query(agency.name, targets.location, targets.roles))
        for agency in targets.agencies
    ]


async def run_pipeline(
    targets: TargetList,
    provider: SearchProvider,
    options: PipelineOptions | None = None,
) -> tuple[list[Prospect], RunReport]:
    """Search for every agency in ``targets`` and return scored prospects.

    Returns:
        The deduplicated prospects, and a report of what the run cost and found.
    """
    options = options or PipelineOptions()
    started_at = datetime.now(UTC)
    plan = plan_queries(targets)
    budget = QueryBudget(limit=options.max_queries)
    errors: list[str] = []
    semaphore = asyncio.Semaphore(options.concurrency)

    async def worker(agency: Agency, query: str) -> tuple[list[SearchHit], int]:
        async with semaphore:
            return await _collect_agency(provider, agency, query, options, budget, errors)

    collected = await asyncio.gather(*(worker(agency, query) for agency, query in plan))

    all_hits = [hit for hits, _ in collected for hit in hits]
    billed = sum(count for _, count in collected)
    prospects = hits_to_prospects(all_hits, targets)

    report = RunReport(
        provider=provider.name,
        model=None,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        agencies_searched=len(plan),
        queries_planned=len(plan),
        queries_executed=budget.spent,
        grounded_searches_billed=billed,
        hits_total=len(all_hits),
        hits_linkedin=sum(1 for hit in all_hits if canonicalise_profile_url(hit.url)),
        prospects=len(prospects),
        prospects_needing_review=sum(1 for p in prospects if p.needs_review),
        errors=errors,
    )
    return prospects, report
