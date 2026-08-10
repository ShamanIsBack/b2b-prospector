"""Deterministic extraction of people data from search-result titles.

This module is the reason the tool cannot invent people. The language model
supplies *citations*; every name, headline and company below is parsed from the
page title a search engine reported, using rules that are pure, synchronous and
unit-tested. No model output reaches these functions.

Public LinkedIn profile titles follow a stable shape::

    "Jane Doe - MICE Manager - Dune & Palm Events | LinkedIn"
    "Ahmed bin Rashid Al Maktoum - Head of Outbound | LinkedIn"
    "Jane Doe | LinkedIn"

The separator is a hyphen, en dash or em dash depending on how the profile was
rendered, and the trailing marker varies by locale.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from grounded_prospector.models import CONFIDENCE_REVIEW_THRESHOLD

# Separators between name / headline / company. Surrounding whitespace is required
# so that hyphenated names ("Anne-Marie") and companies ("Al-Futtaim") survive.
_SEPARATOR_RE = re.compile(r"\s[-–—]\s")

# Trailing segments that are site furniture rather than data. Matched case-folded.
_TRAILING_NOISE = frozenset(
    {
        "linkedin",
        "professional profile",
        "profil zawodowy",
        "perfil profesional",
        "profil professionnel",
    }
)

# Tokens dropped before matching a company name, because they carry no identity.
_AGENCY_STOPWORDS = frozenset({"the", "and", "of", "llc", "ltd", "fz", "fze", "dmcc", "l.l.c"})

# Scoring weights. They sum to 1.0 so the score reads as a percentage.
_WEIGHT_AGENCY_MATCH = 0.40
_WEIGHT_ROLE_MATCH = 0.25
_WEIGHT_WELL_FORMED = 0.20
_WEIGHT_PLAUSIBLE_NAME = 0.15

_MIN_NAME_TOKENS = 2
_MAX_NAME_TOKENS = 6

# "Name - Headline - Company": below this many parts there is no company segment.
_PARTS_INCLUDING_COMPANY = 3

# Longer leading segments are prose, not a place name.
_MAX_LOCATION_WORDS = 6


@dataclass(frozen=True)
class ParsedTitle:
    """The three data-bearing parts of a profile title, any of which may be absent."""

    name: str | None = None
    headline: str | None = None
    company: str | None = None


@dataclass(frozen=True)
class Confidence:
    """A score in ``[0, 1]`` plus the human-readable reasons it is not 1.0."""

    score: float
    needs_review: bool
    reasons: list[str] = field(default_factory=list)


def _strip_trailing_noise(segments: list[str]) -> list[str]:
    """Drop trailing site-furniture segments such as ``LinkedIn``."""
    while segments and segments[-1].strip().casefold() in _TRAILING_NOISE:
        segments.pop()
    return segments


def parse_title(title: str) -> ParsedTitle:
    """Split a search-result title into name, headline and company.

    Unrecognised shapes degrade gracefully: a title with no separators yields a
    name only, and an empty title yields an empty :class:`ParsedTitle` rather
    than raising.
    """
    # The pipe reliably fences off the site name; everything after it is furniture.
    segments = [seg.strip() for seg in title.split("|")]
    segments = _strip_trailing_noise(segments)
    if not segments:
        return ParsedTitle()

    # Anything before the first pipe holds the data; later segments are locale text.
    parts = [part.strip() for part in _SEPARATOR_RE.split(segments[0]) if part.strip()]
    parts = _strip_trailing_noise(parts)
    if not parts:
        return ParsedTitle()

    name = parts[0]
    headline = parts[1] if len(parts) > 1 else None
    # Titles with four or more parts put the company last; joining the middle
    # keeps a multi-part headline ("Head of MICE - EMEA - Acme") intact.
    company: str | None = None
    if len(parts) >= _PARTS_INCLUDING_COMPANY:
        headline = " - ".join(parts[1:-1])
        company = parts[-1]

    return ParsedTitle(name=name or None, headline=headline, company=company)


def split_name(name: str | None) -> tuple[str | None, str | None]:
    r"""Split a full name into first and remaining parts.

    Uses a plain token split rather than a capitalisation rule: Gulf and Arabic
    names routinely contain lowercase particles ("Ahmed bin Rashid Al Maktoum")
    that a ``[A-Z]\w+`` pattern would silently drop.
    """
    tokens = name.split() if name else []
    if not tokens:
        return None, None
    first, *rest = tokens
    return first, " ".join(rest) or None


def extract_location_hint(snippet: str | None) -> str | None:
    """Pull a location out of the leading segment of a search snippet.

    LinkedIn snippets conventionally open with the member's location followed by
    a middot: ``"Dubai, United Arab Emirates · MICE Manager · ..."``. The leading
    segment is also where connection counts and other non-locations appear, so
    anything numeric or obviously not a place is rejected rather than guessed at.

    The result is a *hint* for a human reviewer. It is never used for scoring:
    treating a location string as evidence is what turns a search for people in a
    city into a list of people named after it.
    """
    if not snippet:
        return None

    head = snippet.split("·")[0].strip()
    head = re.sub(r"^location:\s*", "", head, flags=re.IGNORECASE).strip(" .,-–—")
    if not head:
        return None

    # "500+ connections", "1.2K followers" and dates are not places.
    if any(char.isdigit() for char in head):
        return None
    if any(word in head.casefold() for word in ("connection", "follower", "experience")):
        return None
    if not (1 <= len(head.split()) <= _MAX_LOCATION_WORDS):
        return None

    return head


def _normalise(text: str) -> str:
    """Casefold and collapse to alphanumeric tokens for tolerant matching."""
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _agency_tokens(agency: str) -> list[str]:
    """Return the identity-bearing tokens of a company name."""
    return [tok for tok in _normalise(agency).split() if tok not in _AGENCY_STOPWORDS]


def is_plausible_person_name(name: str | None) -> bool:
    """Return whether ``name`` looks like a person rather than a page heading.

    Rejects digits, single tokens and implausibly long strings — the shapes that
    show up when a non-profile page slips through the URL filter.
    """
    if not name:
        return False
    if any(ch.isdigit() for ch in name):
        return False
    tokens = name.split()
    return _MIN_NAME_TOKENS <= len(tokens) <= _MAX_NAME_TOKENS


def score_prospect(
    *,
    raw_title: str,
    name: str | None,
    headline: str | None,
    agency: str,
    roles: Sequence[str],
    snippet: str | None = None,
) -> Confidence:
    """Score how likely a parsed result is to be the person we searched for.

    Four independent signals contribute. The dominant one is whether the target
    company actually appears, because the most common false positive is a real
    person at the wrong company: a search for staff *at* an agency readily
    returns people whose profile merely mentions it.

    The company is looked for in the title *and* the snippet, since a snippet
    legitimately names the employer when the title truncates it. Location is
    deliberately absent from the scoring — see :func:`extract_location_hint`.
    """
    reasons: list[str] = []
    score = 0.0

    haystack = _normalise(f"{raw_title} {snippet or ''}")
    tokens = _agency_tokens(agency)
    agency_matched = bool(tokens) and all(tok in haystack for tok in tokens)
    if agency_matched:
        score += _WEIGHT_AGENCY_MATCH
    else:
        reasons.append(f"target company {agency!r} not found in the result")

    role_haystack = _normalise(f"{headline or ''} {snippet or ''}")
    if role_haystack and any(_normalise(role) in role_haystack for role in roles):
        score += _WEIGHT_ROLE_MATCH
    else:
        reasons.append("no target role keyword in the headline or snippet")

    if name and headline:
        score += _WEIGHT_WELL_FORMED
    else:
        reasons.append("title did not split into both a name and a headline")

    if is_plausible_person_name(name):
        score += _WEIGHT_PLAUSIBLE_NAME
    else:
        reasons.append("parsed name does not look like a person's name")

    # Guard against float drift so the value always renders cleanly as a percentage.
    score = round(min(score, 1.0), 4)

    # A company mismatch is a gate, not a weight. Someone whose headline merely
    # mentions the target is not a lower-confidence version of the right person,
    # they are the wrong person -- so no combination of the other signals may
    # clear them for outreach.
    needs_review = score < CONFIDENCE_REVIEW_THRESHOLD or not agency_matched
    return Confidence(score=score, needs_review=needs_review, reasons=reasons)
