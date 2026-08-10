"""Tests for search-brief loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from grounded_prospector.demo import DEMO_BRIEF
from grounded_prospector.targets import SearchBriefError, load_brief

VALID = """
location: Dubai
roles:
  - MICE
  - Director
agencies:
  - name: Acme Events
    segment: mice
  - name: Beta Travel
"""


def write(tmp_path: Path, content: str, name: str = "search.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_a_valid_brief(tmp_path: Path) -> None:
    brief = load_brief(write(tmp_path, VALID))
    assert brief.location == "Dubai"
    assert [agency.name for agency in brief.agencies] == ["Acme Events", "Beta Travel"]
    assert brief.agencies[0].segment == "mice"
    assert brief.agencies[1].segment is None


def test_omitted_fields_fall_back_to_current_behaviour(tmp_path: Path) -> None:
    """Adding fields to the brief must not change how an older one behaves."""
    brief = load_brief(write(tmp_path, VALID))
    assert brief.country == "ae"
    assert brief.language == "en"
    assert brief.keywords == []
    assert brief.max_pages == 3
    assert brief.min_confidence == 0.0


def test_every_search_setting_can_be_declared_in_one_file(tmp_path: Path) -> None:
    """The point of the file: retargeting touches nothing else."""
    content = """
location: Warsaw
country: pl
language: pl
roles: [CTO, Head of Engineering]
keywords: [fintech, payments]
max_pages: 1
min_confidence: 0.7
agencies:
  - name: Booksy
    segment: saas
"""
    brief = load_brief(write(tmp_path, content))
    assert brief.location == "Warsaw"
    assert brief.country == "pl"
    assert brief.language == "pl"
    assert brief.roles == ["CTO", "Head of Engineering"]
    assert brief.keywords == ["fintech", "payments"]
    assert brief.max_pages == 1
    assert brief.min_confidence == 0.7


def test_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(SearchBriefError, match="not found"):
        load_brief(tmp_path / "absent.yaml")


def test_a_legacy_agencies_file_gets_migration_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anyone upgrading has agencies.yaml and would otherwise just see 'not found'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agencies.yaml").write_text(VALID, encoding="utf-8")

    with pytest.raises(SearchBriefError) as caught:
        load_brief()

    message = str(caught.value)
    assert "agencies.yaml" in message
    assert "search.yaml" in message
    assert "search.example.yaml" in message


def test_no_migration_hint_when_there_is_nothing_to_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SearchBriefError, match="not found"):
        load_brief()


def test_malformed_yaml_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SearchBriefError, match="not valid YAML"):
        load_brief(write(tmp_path, "location: [unclosed"))


def test_a_top_level_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SearchBriefError, match="must be a YAML mapping"):
        load_brief(write(tmp_path, "- just\n- a list\n"))


def test_schema_violations_keep_the_underlying_detail(tmp_path: Path) -> None:
    """ "roles: expected list" is far more useful than "invalid file"."""
    content = "location: Dubai\nroles: MICE\nagencies:\n  - name: Acme\n"
    with pytest.raises(SearchBriefError, match="roles"):
        load_brief(write(tmp_path, content))


def test_out_of_range_confidence_is_rejected(tmp_path: Path) -> None:
    content = "location: Dubai\nmin_confidence: 2.0\nagencies:\n  - name: Acme\n"
    with pytest.raises(SearchBriefError, match="min_confidence"):
        load_brief(write(tmp_path, content))


def test_an_empty_agency_list_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SearchBriefError, match="no agencies"):
        load_brief(write(tmp_path, "location: Dubai\nroles: []\nagencies: []\n"))


def test_the_bundled_demo_brief_is_valid() -> None:
    """--demo must never fail on its own shipped data."""
    brief = load_brief(DEMO_BRIEF)
    assert len(brief.agencies) == 3
    assert brief.roles


def test_the_example_brief_shipped_in_the_repo_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "search.example.yaml"
    brief = load_brief(path)
    assert len(brief.agencies) == 14
    assert brief.country == "ae"
