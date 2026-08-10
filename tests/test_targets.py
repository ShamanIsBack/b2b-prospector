"""Tests for target-list loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from grounded_prospector.demo import DEMO_AGENCIES
from grounded_prospector.targets import TargetListError, load_targets

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


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "agencies.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_a_valid_list(tmp_path: Path) -> None:
    targets = load_targets(write(tmp_path, VALID))
    assert targets.location == "Dubai"
    assert [agency.name for agency in targets.agencies] == ["Acme Events", "Beta Travel"]
    assert targets.agencies[0].segment == "mice"
    assert targets.agencies[1].segment is None


def test_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(TargetListError, match="not found"):
        load_targets(tmp_path / "absent.yaml")


def test_malformed_yaml_is_reported(tmp_path: Path) -> None:
    with pytest.raises(TargetListError, match="not valid YAML"):
        load_targets(write(tmp_path, "location: [unclosed"))


def test_a_top_level_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TargetListError, match="must be a YAML mapping"):
        load_targets(write(tmp_path, "- just\n- a list\n"))


def test_schema_violations_keep_the_underlying_detail(tmp_path: Path) -> None:
    """ "roles: expected list" is far more useful than "invalid file"."""
    content = "location: Dubai\nroles: MICE\nagencies:\n  - name: Acme\n"
    with pytest.raises(TargetListError, match="roles"):
        load_targets(write(tmp_path, content))


def test_an_empty_agency_list_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(TargetListError, match="no agencies"):
        load_targets(write(tmp_path, "location: Dubai\nroles: []\nagencies: []\n"))


def test_the_bundled_demo_list_is_valid() -> None:
    """--demo must never fail on its own shipped data."""
    targets = load_targets(DEMO_AGENCIES)
    assert len(targets.agencies) == 3
    assert targets.roles


def test_the_example_list_shipped_in_the_repo_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "agencies.example.yaml"
    targets = load_targets(path)
    assert len(targets.agencies) == 14
