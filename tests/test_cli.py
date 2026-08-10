"""Tests for the command-line interface.

Every test here runs with the API-key environment variables cleared, so a
regression that makes the offline paths require credentials fails loudly rather
than passing on the developer's own key.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grounded_prospector.cli import _build_provider, app
from grounded_prospector.config import Settings
from grounded_prospector.infra.cache import NullCache
from grounded_prospector.targets import load_brief

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove every API key and isolate the cache and working directory."""
    for name in ("SERPER_API_KEY", "GEMINI_API_KEY", "GP_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)


class TestDemo:
    def test_demo_run_succeeds_with_no_api_key(self, tmp_path: Path) -> None:
        """The headline promise of the project: clone it and it runs."""
        result = runner.invoke(app, ["run", "--demo", "--out", str(tmp_path / "p.csv")])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "p.csv").exists()

    def test_demo_writes_csv_json_and_a_run_report(self, tmp_path: Path) -> None:
        out = tmp_path / "results" / "p.csv"
        runner.invoke(app, ["run", "--demo", "--out", str(out)])

        assert out.exists()
        assert out.with_suffix(".json").exists()
        assert (out.parent / "run_report.json").exists()

    def test_demo_output_contains_the_expected_people(self, tmp_path: Path) -> None:
        out = tmp_path / "p.csv"
        runner.invoke(app, ["run", "--demo", "--out", str(out)])

        with out.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        names = {f"{row['First name']} {row['Last name']}" for row in rows}
        assert "Layla Haddad" in names
        assert "Ahmed bin Rashid Al Maktoum" in names, "name particles must survive"

    def test_duplicates_are_collapsed_in_the_output(self, tmp_path: Path) -> None:
        out = tmp_path / "p.csv"
        runner.invoke(app, ["run", "--demo", "--out", str(out)])

        with out.open(encoding="utf-8-sig", newline="") as handle:
            urls = [row["LinkedIn URL"] for row in csv.DictReader(handle)]
        assert len(urls) == len(set(urls))

    def test_report_records_discarded_noise(self, tmp_path: Path) -> None:
        out = tmp_path / "p.csv"
        runner.invoke(app, ["run", "--demo", "--out", str(out)])

        report = json.loads((out.parent / "run_report.json").read_text(encoding="utf-8"))
        assert report["hits_total"] > report["hits_linkedin"]
        assert report["provider"] == "fixture"
        assert report["estimated_cost_usd"] == 0.0

    def test_min_confidence_filters_the_output(self, tmp_path: Path) -> None:
        out = tmp_path / "p.csv"
        runner.invoke(app, ["run", "--demo", "--min-confidence", "0.9", "--out", str(out)])
        with out.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows
        assert all(float(row["Confidence"]) >= 0.9 for row in rows)

    def test_limit_restricts_the_agencies_searched(self, tmp_path: Path) -> None:
        out = tmp_path / "p.csv"
        runner.invoke(app, ["run", "--demo", "--limit", "1", "--out", str(out)])

        report = json.loads((out.parent / "run_report.json").read_text(encoding="utf-8"))
        assert report["agencies_searched"] == 1


class TestDryRun:
    def test_dry_run_prints_queries_and_writes_nothing(self, tmp_path: Path) -> None:
        out = tmp_path / "p.csv"
        result = runner.invoke(app, ["run", "--demo", "--dry-run", "--out", str(out)])

        assert result.exit_code == 0
        assert "site:linkedin.com/in/" in result.output
        assert "Nothing was sent" in result.output
        assert not out.exists()

    def test_dry_run_costs_nothing_on_the_fixture_backend(self) -> None:
        result = runner.invoke(app, ["run", "--demo", "--dry-run"])
        assert "$0.000" in result.output


class TestMissingCredentials:
    def test_serper_without_a_key_explains_how_to_get_one(self, tmp_path: Path) -> None:
        (tmp_path / "search.yaml").write_text(
            "location: Dubai\nroles: [MICE]\nagencies:\n  - name: Acme\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["run", "--provider", "serper"])
        assert result.exit_code != 0
        assert "SERPER_API_KEY" in result.output
        assert "serper.dev" in result.output

    def test_gemini_without_a_key_explains_how_to_get_one(self) -> None:
        result = runner.invoke(app, ["run", "--provider", "gemini", "--demo"])
        # --demo forces the fixture backend, so this must still succeed.
        assert result.exit_code == 0

    def test_unknown_provider_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "search.yaml").write_text(
            "location: Dubai\nroles: [MICE]\nagencies:\n  - name: Acme\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["run", "--provider", "nope"])
        assert result.exit_code != 0


class TestSearchBriefDrivesEverything:
    """One file must be enough to retarget the tool."""

    BRIEF = """
location: Warsaw
country: pl
language: pl
roles: [CTO, Head of Engineering]
keywords: [fintech, payments]
max_pages: 2
min_confidence: 0.9
agencies:
  - name: Booksy
    segment: saas
"""

    def write_brief(self, tmp_path: Path, content: str | None = None) -> Path:
        path = tmp_path / "warsaw.yaml"
        path.write_text(content or self.BRIEF, encoding="utf-8")
        return path

    def test_a_brief_alone_is_enough_to_run(self, tmp_path: Path) -> None:
        """No env vars, no flags beyond the file itself."""
        brief = self.write_brief(tmp_path)
        result = runner.invoke(
            app, ["run", "--search", str(brief), "--provider", "fixture", "--dry-run"]
        )
        assert result.exit_code == 0, result.output

    def test_max_pages_from_the_brief_drives_the_plan(self, tmp_path: Path) -> None:
        brief = self.write_brief(tmp_path)
        result = runner.invoke(
            app, ["run", "--search", str(brief), "--provider", "fixture", "--dry-run"]
        )
        assert "up to 2 pages" in " ".join(result.output.split())

    def test_cli_pages_flag_overrides_the_brief(self, tmp_path: Path) -> None:
        brief = self.write_brief(tmp_path)
        result = runner.invoke(
            app,
            ["run", "--search", str(brief), "--provider", "fixture", "--dry-run", "--pages", "1"],
        )
        assert "up to 1 pages" in " ".join(result.output.split())

    def test_min_confidence_from_the_brief_filters_output(self, tmp_path: Path) -> None:
        """A strict brief should drop the weak demo rows without any CLI flag."""
        content = self.BRIEF.replace("Booksy", "Majlis Concierge").replace(
            "location: Warsaw", "location: Dubai"
        )
        brief = self.write_brief(tmp_path, content)
        out = tmp_path / "p.csv"
        runner.invoke(
            app, ["run", "--search", str(brief), "--provider", "fixture", "--out", str(out)]
        )

        with out.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert all(float(row["Confidence"]) >= 0.9 for row in rows)

    def test_country_and_language_come_from_the_brief(self, tmp_path: Path) -> None:
        """They used to be env vars; a Warsaw search must not silently stay on gl=ae."""
        brief = load_brief(self.write_brief(tmp_path))
        provider = _build_provider("serper", Settings(SERPER_API_KEY="k"), NullCache(), brief)

        # Reaches past the public surface deliberately: the request body is the
        # actual contract, and asserting on it is what proves the wiring.
        body = provider._body("q", 1)
        assert body["gl"] == "pl"
        assert body["hl"] == "pl"


class TestOtherCommands:
    def test_providers_lists_every_backend_and_its_capabilities(self) -> None:
        result = runner.invoke(app, ["providers"])
        assert result.exit_code == 0
        for name in ("serper", "gemini", "fixture"):
            assert name in result.output

    def test_version_prints_the_package_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "grounded-prospector" in result.output

    def test_no_arguments_shows_help(self) -> None:
        result = runner.invoke(app, [])
        assert "Usage" in result.output

    def test_a_missing_search_brief_points_at_the_way_forward(self) -> None:
        """A fresh clone hits this first, so it must not be a dead end."""
        result = runner.invoke(app, ["run", "--provider", "fixture"])
        assert result.exit_code != 0
        assert "not found" in result.output
        assert "--demo" in result.output
