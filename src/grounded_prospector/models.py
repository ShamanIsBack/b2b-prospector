"""Domain models for a prospecting run.

The type split here encodes the project's central rule (see ``docs/DECISIONS.md``,
ADR-003): a :class:`SearchHit` is *evidence* — a URL and the page title a search
engine reported for it — while a :class:`Prospect` is an *interpretation* of that
evidence produced by deterministic parsing. Nothing in either type is ever taken
from the language model's prose.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Below this score a prospect is flagged for human review before any outreach.
CONFIDENCE_REVIEW_THRESHOLD = 0.60


class Agency(BaseModel):
    """A company we want to find decision-makers at."""

    model_config = ConfigDict(frozen=True)

    name: str
    segment: str | None = None
    domain: str | None = None


class TargetList(BaseModel):
    """A complete prospecting brief, loaded from ``agencies.yaml``."""

    location: str
    roles: list[str]
    agencies: list[Agency]


class SearchHit(BaseModel):
    """One source returned by a provider.

    A hit asserts only that a search engine associated ``title`` (and possibly
    ``snippet``) with ``url``. It makes no claim that a person exists or works
    anywhere.

    ``snippet`` and ``position`` are populated by backends that expose a real
    SERP; grounding-style backends leave them empty, which is why nothing
    downstream may depend on them being present.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    title: str
    agency: str
    query: str
    provider: str
    retrieved_at: datetime

    snippet: str | None = None
    position: int | None = None
    page: int = 1


class Prospect(BaseModel):
    """A parsed, deduplicated candidate contact.

    Every field except :attr:`llm_notes` is derived deterministically from a
    :class:`SearchHit`. :attr:`llm_notes` is the only model-authored text in the
    system and is explicitly excluded from the CRM columns.
    """

    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    company_from_title: str | None = None

    profile_url: str
    agency: str
    segment: str | None = None

    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool
    review_reasons: list[str] = Field(default_factory=list)

    # A hint, never a scoring signal. Matching on location is exactly how a
    # search for people *in* a city returns people *named* after it.
    location_hint: str | None = None

    raw_title: str
    snippet: str | None = None
    serp_position: int | None = None
    source_query: str
    provider: str
    retrieved_at: datetime
    llm_notes: str | None = None

    @property
    def full_name(self) -> str:
        """Return the name as a single string, empty if nothing was parsed."""
        return " ".join(part for part in (self.first_name, self.last_name) if part)


class RunReport(BaseModel):
    """Accounting for a single run: what was spent, found and skipped."""

    provider: str
    model: str | None
    started_at: datetime
    finished_at: datetime

    agencies_searched: int = 0
    queries_planned: int = 0
    queries_executed: int = 0
    grounded_searches_billed: int = 0

    cache_hits: int = 0
    cache_misses: int = 0

    hits_total: int = 0
    hits_linkedin: int = 0
    prospects: int = 0
    prospects_needing_review: int = 0

    estimated_cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """Return wall-clock duration of the run."""
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def cache_hit_rate(self) -> float:
        """Return the fraction of lookups served from cache, 0.0 if none were made."""
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total else 0.0
