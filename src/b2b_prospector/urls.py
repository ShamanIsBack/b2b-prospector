"""LinkedIn profile URL recognition and canonicalisation.

Deduplication depends entirely on this module. The same person can be cited as
``https://ae.linkedin.com/in/jane-doe``, ``https://www.linkedin.com/in/jane-doe/``
and ``https://linkedin.com/in/jane-doe?trk=public_profile`` within a single run,
and all three must collapse to one prospect.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

# LinkedIn serves public profiles from country subdomains (ae., pl., uk.) and from
# the bare and www hosts. They are the same page.
_LINKEDIN_HOST_SUFFIX = "linkedin.com"

# A profile path looks like /in/<slug>, optionally followed by a locale segment
# (/in/jane-doe/de) or a subpage (/in/jane-doe/recent-activity).
_PROFILE_PREFIX = "/in/"


def is_linkedin_profile(url: str) -> bool:
    """Return whether ``url`` points at a public LinkedIn member profile.

    Company pages (``/company/``), posts (``/posts/``) and job listings are all
    rejected — only ``/in/`` member profiles carry a person's name in the title.
    """
    return canonicalise_profile_url(url) is not None


def canonicalise_profile_url(url: str) -> str | None:
    """Reduce a LinkedIn profile URL to a stable canonical form.

    Returns ``None`` if the URL is not a member profile, which is how callers
    filter out non-LinkedIn citations.

    The canonical form is always ``https://www.linkedin.com/in/<slug>``: the
    country subdomain is normalised away, query strings and fragments are
    dropped, and any trailing locale or subpage segment is removed.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None

    host = parts.hostname or ""
    if not (host == _LINKEDIN_HOST_SUFFIX or host.endswith("." + _LINKEDIN_HOST_SUFFIX)):
        return None

    path = unquote(parts.path)
    if not path.startswith(_PROFILE_PREFIX):
        return None

    slug = path[len(_PROFILE_PREFIX) :].strip("/").split("/")[0].strip()
    if not slug:
        return None

    return f"https://www.{_LINKEDIN_HOST_SUFFIX}/in/{slug}"


def dedupe_key(url: str) -> str | None:
    """Return a case-insensitive key identifying the person behind ``url``.

    LinkedIn treats profile slugs case-insensitively, so ``/in/Jane-Doe`` and
    ``/in/jane-doe`` are one person and must share a key.
    """
    canonical = canonicalise_profile_url(url)
    return canonical.lower() if canonical else None
