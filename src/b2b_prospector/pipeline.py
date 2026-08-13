"""Orchestration: a search brief in, scored prospects and a run report out.

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

from b2b_prospector.extract import (
    extract_location_hint,
    parse_title,
    score_prospect,
    split_name,
)
from b2b_prospector.models import (
    Prospect,
    RunReport,
    SearchBrief,
    SearchHit,
    SearchTarget,
    TargetKind,
)
from b2b_prospector.providers.base import (
    ProviderAuthError,
    ProviderError,
    SearchProvider,
)
from b2b_prospector.query import build_xray_query
from b2b_prospector.urls import canonicalise_profile_url, dedupe_key


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


async def _collect_target(
    provider: SearchProvider,
    target: SearchTarget,
    query: str,
    options: PipelineOptions,
    budget: QueryBudget,
    errors: list[str],
) -> tuple[list[SearchHit], int]:
    """Page through one target's results, returning its hits and billed searches."""
    hits: list[SearchHit] = []
    billed = 0

    for page in range(1, options.max_pages + 1):
        if not await budget.claim():
            errors.append(f"query budget of {budget.limit} reached before finishing {target.name}")
            break

        try:
            result = await provider.search(query, target, page=page)
        except ProviderError as error:
            if isinstance(error, ProviderAuthError):
                # A rejected key fails identically for every target, so carrying
                # on would spend the rest of the run rediscovering it -- and a
                # run where nothing could be searched must not exit as a success
                # with an empty CSV. Aborting the whole run is the honest shape.
                raise
            errors.append(f"{target.name} (page {page}): {error}")
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


def _dedupe_rank(prospect: Prospect) -> tuple[int, float]:
    """Rank two records of the same person: actionable first, then best evidence.

    Confidence alone is not enough, because an exclusion veto deliberately does
    *not* lower the score (the score describes the evidence, the flag describes
    our judgement of it). Ranking on score alone therefore let a vetoed row at
    1.00 displace a clean row at 0.85 for the same person, deleting a usable
    contact: someone found both at their employer and under a phrase that
    happened to match a funeral celebrant vanished from the contactable list.
    """
    return (0 if prospect.needs_review else 1, prospect.confidence)


def hits_to_prospects(hits: Sequence[SearchHit], brief: SearchBrief) -> list[Prospect]:
    """Turn raw search hits into scored, deduplicated prospects.

    Non-profile URLs are dropped here rather than at the provider, so the run
    report can report honestly on how much of what we paid for was noise.

    When the same person appears more than once -- under a country subdomain, on
    a later page, or from a different target's query -- the record a human can
    act on wins, and among equals the highest-scoring one; see
    :func:`_dedupe_rank`.
    """
    by_name = {target.name: target for target in brief.targets}
    best: dict[str, Prospect] = {}

    for hit in hits:
        profile_url = canonicalise_profile_url(hit.url)
        key = dedupe_key(hit.url)
        if profile_url is None or key is None:
            continue

        parsed = parse_title(hit.title)
        first_name, last_name = split_name(parsed.name)

        # A hit knows only the target's *text*. How to read that text -- employer
        # or self-description -- lives on the brief, so it is looked up here
        # rather than carried through the provider layer.
        target = by_name.get(hit.target)
        kind = target.kind if target else TargetKind.COMPANY

        confidence = score_prospect(
            raw_title=hit.title,
            name=parsed.name,
            headline=parsed.headline,
            target=hit.target,
            roles=brief.roles,
            snippet=hit.snippet,
            kind=kind,
            exclude=exclusions_for(brief, target),
        )

        prospect = Prospect(
            first_name=first_name,
            last_name=last_name,
            headline=parsed.headline,
            company_from_title=parsed.company,
            profile_url=profile_url,
            target=hit.target,
            target_kind=kind,
            segment=target.segment if target else None,
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
        if existing is None or _dedupe_rank(prospect) > _dedupe_rank(existing):
            best[key] = prospect

    return sorted(best.values(), key=lambda p: (-p.confidence, p.target, p.profile_url))


def exclusions_for(brief: SearchBrief, target: SearchTarget | None) -> tuple[str, ...]:
    """Return the exclusion terms applying to one target.

    Brief-level terms apply everywhere; a target's own terms are added to them
    rather than replacing them, so a global rule cannot be lost by giving one
    target a specific one. Order is preserved and duplicates dropped, because the
    result is interpolated into a query a human will read.
    """
    combined = (*brief.exclude, *(target.exclude if target else ()))
    return tuple(dict.fromkeys(combined))


def plan_queries(brief: SearchBrief) -> list[tuple[SearchTarget, str]]:
    """Build the query for every target, without issuing anything."""
    return [
        (
            target,
            build_xray_query(
                target.name,
                brief.location,
                brief.roles,
                brief.keywords,
                exclusions_for(brief, target),
            ),
        )
        for target in brief.targets
    ]


async def run_pipeline(
    brief: SearchBrief,
    provider: SearchProvider,
    options: PipelineOptions | None = None,
) -> tuple[list[Prospect], RunReport]:
    """Search for every target in ``brief`` and return scored prospects.

    Returns:
        The deduplicated prospects, and a report of what the run cost and found.
    """
    options = options or PipelineOptions()
    started_at = datetime.now(UTC)
    plan = plan_queries(brief)
    budget = QueryBudget(limit=options.max_queries)
    errors: list[str] = []
    semaphore = asyncio.Semaphore(options.concurrency)

    async def worker(target: SearchTarget, query: str) -> tuple[list[SearchHit], int]:
        async with semaphore:
            return await _collect_target(provider, target, query, options, budget, errors)

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(worker(target, query)) for target, query in plan]
    except* ProviderAuthError as auth_failures:
        # One rejected key means every worker fails the same way. The TaskGroup
        # cancels the siblings (a worker already past the semaphore may slip one
        # request through, which fails identically and unbilled), so one error
        # surfaces instead of one warning per target.
        raise auth_failures.exceptions[0] from None

    collected = [task.result() for task in tasks]

    all_hits = [hit for hits, _ in collected for hit in hits]
    billed = sum(count for _, count in collected)
    prospects = hits_to_prospects(all_hits, brief)

    report = RunReport(
        provider=provider.name,
        model=None,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        targets_searched=len(plan),
        queries_planned=len(plan),
        queries_executed=budget.spent,
        searches_billed=billed,
        hits_total=len(all_hits),
        hits_linkedin=sum(1 for hit in all_hits if canonicalise_profile_url(hit.url)),
        prospects=len(prospects),
        prospects_needing_review=sum(1 for p in prospects if p.needs_review),
        errors=errors,
    )
    return prospects, report
