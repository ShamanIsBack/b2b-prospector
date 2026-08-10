"""Tests for LinkedIn URL recognition and canonicalisation."""

from __future__ import annotations

import pytest

from grounded_prospector.urls import canonicalise_profile_url, dedupe_key, is_linkedin_profile

CANONICAL = "https://www.linkedin.com/in/jane-doe"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/jane-doe",
        "https://linkedin.com/in/jane-doe",
        "https://ae.linkedin.com/in/jane-doe",
        "https://pl.linkedin.com/in/jane-doe",
        "http://uk.linkedin.com/in/jane-doe",
        "https://www.linkedin.com/in/jane-doe/",
        "https://www.linkedin.com/in/jane-doe?trk=public_profile_browsemap",
        "https://www.linkedin.com/in/jane-doe#experience",
        "https://ae.linkedin.com/in/jane-doe/de",
        "https://www.linkedin.com/in/jane-doe/recent-activity/all/",
    ],
)
def test_all_profile_url_variants_collapse_to_one_canonical_form(url: str) -> None:
    assert canonicalise_profile_url(url) == CANONICAL


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/the-circle-events",
        "https://www.linkedin.com/posts/jane-doe_activity-123",
        "https://www.linkedin.com/jobs/view/123456",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/in///",
        "https://notlinkedin.com/in/jane-doe",
        "https://evil-linkedin.com.attacker.net/in/jane-doe",
        "ftp://www.linkedin.com/in/jane-doe",
        "https://example.com/",
        "not a url at all",
        "",
    ],
)
def test_non_profile_urls_are_rejected(url: str) -> None:
    assert canonicalise_profile_url(url) is None
    assert not is_linkedin_profile(url)


def test_lookalike_domain_is_not_treated_as_linkedin() -> None:
    """A host merely *containing* linkedin.com must not pass the suffix check."""
    assert canonicalise_profile_url("https://linkedin.com.phish.io/in/jane-doe") is None


def test_dedupe_key_is_case_insensitive() -> None:
    assert dedupe_key("https://www.linkedin.com/in/Jane-Doe") == dedupe_key(
        "https://ae.linkedin.com/in/jane-doe/"
    )


def test_dedupe_key_is_none_for_non_profiles() -> None:
    assert dedupe_key("https://www.linkedin.com/company/acme") is None


def test_percent_encoded_slugs_are_decoded() -> None:
    assert canonicalise_profile_url("https://www.linkedin.com/in/jos%C3%A9-garcia") == (
        "https://www.linkedin.com/in/josé-garcia"
    )


def test_surrounding_whitespace_is_tolerated() -> None:
    assert canonicalise_profile_url("  https://www.linkedin.com/in/jane-doe  ") == CANONICAL
