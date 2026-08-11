"""Tests for CSV, JSON and run-report output."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from grounded_prospector.export import CRM_COLUMNS, CSV_COLUMNS, write_csv, write_json, write_report
from grounded_prospector.models import Prospect, RunReport, TargetKind

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def prospect(**overrides: object) -> Prospect:
    defaults: dict[str, object] = {
        "first_name": "Jana",
        "last_name": "Kowalska",
        "headline": "MICE Manager",
        "profile_url": "https://www.linkedin.com/in/jana-kowalska",
        "target": "Acme Events",
        "segment": "mice",
        "confidence": 0.85,
        "needs_review": False,
        "review_reasons": [],
        "raw_title": "Jana Kowalska - MICE Manager - Acme Events | LinkedIn",
        "source_query": 'site:linkedin.com/in/ "Acme Events"',
        "provider": "serper",
        "retrieved_at": NOW,
    }
    return Prospect(**{**defaults, **overrides})  # type: ignore[arg-type]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class TestWriteCsv:
    def test_writes_a_row_per_prospect(self, tmp_path: Path) -> None:
        path = write_csv([prospect(), prospect(first_name="Ola")], tmp_path / "out.csv")
        assert len(read_csv(path)) == 2

    def test_header_matches_the_declared_contract(self, tmp_path: Path) -> None:
        path = write_csv([prospect()], tmp_path / "out.csv")
        assert list(read_csv(path)[0].keys()) == CSV_COLUMNS

    def test_crm_columns_are_left_empty_for_a_human(self, tmp_path: Path) -> None:
        """This tool finds people; it must not imply it found contact details."""
        row = read_csv(write_csv([prospect()], tmp_path / "out.csv"))[0]
        assert all(row[column] == "" for column in CRM_COLUMNS)

    def test_file_is_utf8_with_bom_so_excel_reads_names_correctly(self, tmp_path: Path) -> None:
        path = write_csv([prospect(first_name="Zoë", last_name="Żółć")], tmp_path / "out.csv")
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert read_csv(path)[0]["Last name"] == "Żółć"

    def test_review_reasons_are_joined_into_one_cell(self, tmp_path: Path) -> None:
        path = write_csv(
            [prospect(needs_review=True, review_reasons=["reason one", "reason two"])],
            tmp_path / "out.csv",
        )
        row = read_csv(path)[0]
        assert row["Needs review"] == "yes"
        assert row["Review reasons"] == "reason one; reason two"

    def test_missing_optional_fields_become_empty_cells_not_none(self, tmp_path: Path) -> None:
        path = write_csv(
            [prospect(headline=None, segment=None, location_hint=None)], tmp_path / "out.csv"
        )
        row = read_csv(path)[0]
        assert row["Headline"] == ""
        assert row["Location hint"] == ""

    def test_no_prospects_still_writes_a_usable_header(self, tmp_path: Path) -> None:
        path = write_csv([], tmp_path / "out.csv")
        with path.open(encoding="utf-8-sig") as handle:
            assert next(csv.reader(handle)) == CSV_COLUMNS

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        path = write_csv([prospect()], tmp_path / "deep" / "nested" / "out.csv")
        assert path.exists()


class TestWriteJson:
    def test_roundtrips_every_field(self, tmp_path: Path) -> None:
        path = write_json([prospect(location_hint="Dubai")], tmp_path / "out.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload[0]["location_hint"] == "Dubai"
        assert payload[0]["profile_url"].endswith("jana-kowalska")

    def test_non_ascii_is_written_literally(self, tmp_path: Path) -> None:
        path = write_json([prospect(last_name="Żółć")], tmp_path / "out.json")
        assert "Żółć" in path.read_text(encoding="utf-8")


class TestWriteReport:
    def test_includes_derived_metrics(self, tmp_path: Path) -> None:
        report = RunReport(
            provider="serper",
            model=None,
            started_at=NOW,
            finished_at=datetime(2026, 8, 10, 12, 0, 30, tzinfo=UTC),
            cache_hits=3,
            cache_misses=1,
        )
        path = write_report(report, tmp_path / "run_report.json")
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["duration_seconds"] == 30.0
        assert payload["cache_hit_rate"] == 0.75

    def test_cache_hit_rate_is_zero_when_nothing_was_looked_up(self, tmp_path: Path) -> None:
        report = RunReport(provider="fixture", model=None, started_at=NOW, finished_at=NOW)
        payload = json.loads(write_report(report, tmp_path / "r.json").read_text(encoding="utf-8"))
        assert payload["cache_hit_rate"] == 0.0


def test_match_type_tells_the_reader_how_to_read_the_target_column(tmp_path: Path) -> None:
    """Without this column a phrase row reads as a company that does not exist."""
    path = write_csv(
        [
            prospect(target="Acme Events"),
            prospect(target="konsultant ślubny", target_kind=TargetKind.PHRASE),
        ],
        tmp_path / "out.csv",
    )
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [row["Match type"] for row in rows] == ["company", "phrase"]
    assert [row["Target"] for row in rows] == ["Acme Events", "konsultant ślubny"]
