"""The contract every search backend implements.

A provider's job is narrow on purpose: given a query and a page number, return
the sources a search engine reported for it. Providers do not parse names, score
relevance or deduplicate — that work is deterministic and lives in
:mod:`b2b_prospector.extract`, so it stays identical no matter which backend
produced the results.

Backends differ in what they can do, and the pipeline must not pretend otherwise.
A SERP API exposes real pagination, ranking and snippets; a grounding-style
backend returns only the handful of sources a model chose to cite, with no way to
ask for more. Those differences are declared through :attr:`SearchProvider.capabilities`
rather than discovered through surprising behaviour at page two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from b2b_prospector.models import SearchHit, SearchTarget


class ProviderError(RuntimeError):
    """A provider could not complete a search."""


class ProviderAuthError(ProviderError):
    """Credentials were missing, malformed or rejected.

    Separated from the general case because it is never worth retrying and the
    fix is always the same: supply a valid key.
    """


@dataclass(frozen=True)
class Capabilities:
    """What a backend can and cannot do.

    Attributes:
        supports_pagination: Whether asking for page 2 returns different results.
            When ``False`` the pipeline stops after the first page instead of
            re-fetching identical results and paying for them.
        provides_snippets: Whether hits carry result snippets, which supply the
            location hint and widen company matching.
        results_per_page_max: Largest page size the backend accepts, or ``None``
            if the caller cannot influence it.
    """

    supports_pagination: bool
    provides_snippets: bool
    results_per_page_max: int | None = None


@dataclass
class SearchResult:
    """What a provider returns for one query on one page.

    Attributes:
        hits: Every source returned, including non-LinkedIn ones. Filtering
            happens downstream so the run report can show how much noise was
            discarded rather than hiding it.
        searches_billed: Searches the backend actually charged for. One request
            can trigger several, so this is counted, not assumed.
        notes: The backend's own prose, if any. Carried for a human reviewer and
            never parsed for facts.
        from_cache: Whether this result was replayed rather than fetched.
        total_results: Backend's estimate of the total match count, if reported.
        has_more: Whether the backend indicated further pages exist. ``None``
            means it did not say, and the pipeline falls back to inspecting how
            full the page was.
    """

    hits: list[SearchHit]
    searches_billed: int = 0
    notes: str | None = None
    from_cache: bool = False
    total_results: int | None = None
    has_more: bool | None = None


@runtime_checkable
class SearchProvider(Protocol):
    """A backend that can answer an X-ray query with search results."""

    name: str
    capabilities: Capabilities

    async def search(self, query: str, target: SearchTarget, *, page: int = 1) -> SearchResult:
        """Run ``query`` and return the sources found for it.

        Args:
            query: The literal search expression to run.
            target: The target the query was built for, echoed onto each hit.
            page: 1-based page number. Backends without pagination support must
                return an empty result for any page beyond the first rather than
                silently repeating page one.

        Returns:
            The sources found, plus billing metadata.

        Raises:
            ProviderError: if the backend failed permanently.
        """
        ...

    async def aclose(self) -> None:
        """Release any held resources."""
        ...
