"""Tests for search query construction."""

from __future__ import annotations

from grounded_prospector.models import TargetKind
from grounded_prospector.query import build_prompt, build_xray_query

ROLES = ["MICE", "Director"]


def test_query_restricts_to_member_profiles() -> None:
    query = build_xray_query("Dune & Palm Events", "Dubai", ROLES)
    assert query.startswith("site:linkedin.com/in/ ")


def test_directory_pages_are_excluded_at_the_query() -> None:
    """LinkedIn's own member directories rank well and contain nobody useful."""
    query = build_xray_query("Acme", "Dubai", ROLES)
    assert '-intitle:"profiles"' in query
    assert '-inurl:"dir/"' in query


def test_optional_keywords_form_their_own_or_group() -> None:
    query = build_xray_query("Acme", "Dubai", ROLES, keywords=["luxury", "eco"])
    assert '("luxury" OR "eco")' in query


def test_query_quotes_target_and_location_as_phrases() -> None:
    query = build_xray_query("Dune & Palm Events", "Dubai", ROLES)
    assert '"Dune & Palm Events"' in query
    assert '"Dubai"' in query


def test_roles_are_or_ed_inside_a_single_group() -> None:
    query = build_xray_query("Acme", "Dubai", ROLES)
    assert '("MICE" OR "Director")' in query


def test_single_role_needs_no_or() -> None:
    assert '("MICE")' in build_xray_query("Acme", "Dubai", ["MICE"])


def test_optional_parts_are_omitted_cleanly() -> None:
    assert build_xray_query("Acme", "", []) == (
        'site:linkedin.com/in/ "Acme" -intitle:"profiles" -inurl:"dir/"'
    )


def test_prompt_embeds_the_exact_query_and_target() -> None:
    query = build_xray_query("Acme", "Dubai", ROLES)
    prompt = build_prompt(query, "Acme")
    assert query in prompt
    assert "Acme" in prompt


def test_prompt_forbids_uncited_people() -> None:
    """The anti-invention instruction is load-bearing and must not be dropped."""
    prompt = build_prompt("q", "Acme")
    assert "do not describe people you cannot cite" in prompt.casefold()


def test_exclusions_become_negative_terms() -> None:
    query = build_xray_query("mistrz ceremonii", "", ROLES, (), ("pogrzeb", "krematorium"))
    assert '-"pogrzeb"' in query
    assert '-"krematorium"' in query


def test_no_exclusions_leaves_the_query_untouched() -> None:
    """Every existing brief must produce a byte-identical query."""
    assert build_xray_query("Acme", "Dubai", ROLES, ()) == build_xray_query(
        "Acme", "Dubai", ROLES, (), ()
    )


def test_a_phrase_target_is_quoted_exactly_like_a_company() -> None:
    """The whole technique rests on this: the builder never inspects the text."""
    assert '"konsultant slubny"' in build_xray_query("konsultant slubny", "", ())


def test_the_prompt_goal_matches_the_target_kind() -> None:
    """ "decision-makers at 'konsultant ślubny'" describes an employer that does not exist."""
    company = build_prompt("q", "Acme", TargetKind.COMPANY)
    phrase = build_prompt("q", "konsultant ślubny", TargetKind.PHRASE)

    assert "decision-makers at Acme" in company
    assert "decision-makers at" not in phrase
    assert "describe themselves as" in phrase
