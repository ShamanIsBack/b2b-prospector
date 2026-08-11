"""Domain models for a prospecting run.

The type split here encodes the project's central rule (see ``docs/DECISIONS.md``,
ADR-003): a :class:`SearchHit` is *evidence* — a URL and the page title a search
engine reported for it — while a :class:`Prospect` is an *interpretation* of that
evidence produced by deterministic parsing. Nothing in either type is ever taken
from the language model's prose.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# Below this score a prospect is flagged for human review before any outreach.
CONFIDENCE_REVIEW_THRESHOLD = 0.60


class TargetKind(StrEnum):
    """What the text in a target's ``name`` is meant to match.

    The query builder treats both the same way -- a quoted phrase AND-ed into the
    search expression. The difference is entirely one of *interpretation*, and it
    changes what a match means (see
    :func:`grounded_prospector.extract.score_prospect`):

    ``COMPANY``
        An employer. A match in the result title means "works here now", because
        a LinkedIn profile title names the current employer.
    ``PHRASE``
        A self-description such as ``"konsultant ślubny"``. A match means "this is
        how the person presents themselves" -- the more useful question when a
        market is a long tail of sole traders with no company worth naming.
    """

    COMPANY = "company"
    PHRASE = "phrase"


class SearchTarget(BaseModel):
    """One thing to search for: a company to staff-map, or a phrase to find.

    This was called ``Agency`` until phrase targets arrived; the alias below is
    kept so older imports go on working.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: TargetKind = TargetKind.COMPANY
    segment: str | None = None
    domain: str | None = None

    # Terms that disqualify a result. Added to the query as negative terms *and*
    # re-checked during scoring, because search engines honour negative terms
    # unreliably on phrases with few matches -- which is exactly the kind of
    # phrase this feature exists for. A tuple, not a list, so the model stays
    # hashable while frozen.
    #
    # Matched as a *substring* of the normalised title and snippet, which is
    # required rather than incidental: "pogrzeb" has to match the inflected
    # "pogrzebowej", and token equality would miss it. The cost of that choice is
    # that short terms match inside longer words -- "art" would match "smart" --
    # so prefer a distinctive stem.
    exclude: tuple[str, ...] = ()


# Retained so `from grounded_prospector.models import Agency` keeps working.
Agency = SearchTarget


class SearchBrief(BaseModel):
    """Everything that defines one search, loaded from ``search.yaml``.

    This is the single place a search is retargeted from. Anything describing
    *what* to look for or *how hard* to look belongs here; API keys and
    infrastructure live in the environment (see :mod:`grounded_prospector.config`).

    The ``country``/``language`` defaults are inherited from the project's
    original Dubai use case. They bias which results the search engine returns,
    so set them deliberately -- a Polish search left on ``ae`` will quietly
    return the wrong thing.
    """

    model_config = ConfigDict(populate_by_name=True)

    # --- what to search for ---
    location: str
    country: str = "ae"
    language: str = "en"
    roles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    # ``agencies:`` is the original spelling and still accepted; ``targets:`` is
    # the honest one now that a target need not be a company.
    targets: list[SearchTarget] = Field(
        validation_alias=AliasChoices("targets", "agencies"),
    )

    # Applied to every target, on top of each target's own ``exclude``.
    exclude: tuple[str, ...] = ()

    # --- how deep, and how strict ---
    max_pages: int = Field(default=3, ge=1)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


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
    # The target this hit was searched for -- a company name or a phrase.
    target: str
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

    # What was searched for, and how to read it. For ``COMPANY`` targets
    # ``target`` is an employer; for ``PHRASE`` targets it is the wording the
    # person's own headline was matched against, so it must not be read as one.
    target: str
    target_kind: TargetKind = TargetKind.COMPANY
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

    targets_searched: int = 0
    queries_planned: int = 0

    # Lookups attempted, including those served from cache. Drives the budget.
    queries_executed: int = 0

    # Searches the backend actually charged for. Cache hits are not billed, and
    # one Gemini prompt can trigger several billable searches -- so this is the
    # only number cost may be derived from.
    searches_billed: int = 0

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
