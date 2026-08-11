"""Command-line interface."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from grounded_prospector import __version__
from grounded_prospector.config import (
    GEMINI_USD_PER_1K_GROUNDED_SEARCHES,
    SERPER_USD_PER_1K_QUERIES,
    Settings,
)
from grounded_prospector.demo import DEMO_BRIEF
from grounded_prospector.export import write_csv, write_json, write_report
from grounded_prospector.infra.cache import Cache, NullCache, SqliteCache
from grounded_prospector.infra.logging import setup_logging
from grounded_prospector.infra.ratelimit import TokenBucket
from grounded_prospector.models import Prospect, RunReport, SearchBrief
from grounded_prospector.pipeline import (
    PipelineOptions,
    plan_queries,
    run_pipeline,
)
from grounded_prospector.providers.base import ProviderError, SearchProvider
from grounded_prospector.providers.fixture import FixtureProvider
from grounded_prospector.providers.gemini import GeminiGroundingProvider
from grounded_prospector.providers.serper import SerperProvider
from grounded_prospector.targets import (
    DEFAULT_BRIEF_PATH,
    SearchBriefError,
    lint_brief,
    load_brief,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find B2B decision-makers via public search results, without scraping.",
)
console = Console()

PROVIDERS = ("serper", "gemini", "fixture")


def _build_provider(
    name: str, settings: Settings, cache: Cache, brief: SearchBrief
) -> SearchProvider:
    """Construct the requested backend.

    Country and language come from the brief, not the environment: they describe
    what you are searching for, not how the tool is deployed.

    Raises:
        typer.BadParameter: if the provider name is not recognised.
    """
    bucket = TokenBucket(settings.rate_limit_per_minute)

    if name == "fixture":
        return FixtureProvider()
    if name == "serper":
        return SerperProvider(
            api_key=settings.require_serper_key(),
            cache=cache,
            bucket=bucket,
            results_per_page=settings.results_per_page,
            country=brief.country,
            language=brief.language,
            max_retries=settings.max_retries,
            timeout_seconds=settings.request_timeout_seconds,
        )
    if name == "gemini":
        return GeminiGroundingProvider(
            api_key=settings.require_gemini_key(),
            model=settings.gemini_model,
            cache=cache,
            bucket=bucket,
            max_retries=settings.max_retries,
            timeout_seconds=settings.request_timeout_seconds,
        )
    raise typer.BadParameter(f"unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}")


def _usd_per_1k(provider: str) -> float:
    """Return the list price per 1,000 queries for a backend.

    Shared by the estimator and the dry-run planner so the two can never quote
    different numbers for the same run.
    """
    return {
        "serper": SERPER_USD_PER_1K_QUERIES,
        "gemini": GEMINI_USD_PER_1K_GROUNDED_SEARCHES,
        "fixture": 0.0,
    }.get(provider, 0.0)


def _estimate_cost(report: RunReport) -> float:
    """Estimate the spend for a run, in USD.

    Derived from what was actually billed, never from what was attempted: a run
    served entirely from cache costs nothing and must report nothing.
    """
    return report.searches_billed * _usd_per_1k(report.provider) / 1000


def _summary_table(report: RunReport, output: Path | None) -> Table:
    """Build the end-of-run summary."""
    table = Table(title="Run summary", show_header=False, title_style="bold")
    table.add_column("", style="dim")
    table.add_column("")

    table.add_row("Provider", report.provider)
    table.add_row("Targets searched", str(report.targets_searched))
    table.add_row(
        "Queries",
        f"{report.queries_executed} attempted, {report.searches_billed} billed",
    )
    table.add_row(
        "Cache hits",
        f"{report.cache_hits} of {report.cache_hits + report.cache_misses} "
        f"({report.cache_hit_rate:.0%})",
    )
    table.add_row("Results returned", str(report.hits_total))
    table.add_row(
        "LinkedIn profiles",
        f"{report.hits_linkedin} ({report.hits_total - report.hits_linkedin} discarded)",
    )
    table.add_row("Prospects after dedupe", str(report.prospects))
    table.add_row(
        "Needing human review",
        f"[yellow]{report.prospects_needing_review}[/yellow]"
        if report.prospects_needing_review
        else "0",
    )
    table.add_row("Estimated cost", f"${report.estimated_cost_usd:.3f}")
    table.add_row("Duration", f"{report.duration_seconds:.1f}s")
    if output is not None:
        table.add_row("Written to", str(output))
    return table


def _preview_table(prospects: list[Prospect], limit: int = 10) -> Table:
    """Build a short preview of the highest-confidence prospects."""
    table = Table(title=f"Top {min(limit, len(prospects))} prospects", title_style="bold")
    table.add_column("Name")
    table.add_column("Headline", max_width=40)
    table.add_column("Target")
    table.add_column("Conf", justify="right")
    table.add_column("Review", justify="center")

    for prospect in prospects[:limit]:
        table.add_row(
            prospect.full_name or "[dim]-[/dim]",
            prospect.headline or "[dim]-[/dim]",
            prospect.target,
            f"{prospect.confidence:.2f}",
            "[yellow]yes[/yellow]" if prospect.needs_review else "[green]no[/green]",
        )
    return table


def _load(search: Path | None, demo: bool) -> SearchBrief:
    """Resolve and load the search brief.

    Raises:
        typer.BadParameter: if the brief cannot be read or validated.
    """
    path = DEMO_BRIEF if demo and search is None else (search or DEFAULT_BRIEF_PATH)
    try:
        brief = load_brief(path)
    except SearchBriefError as error:
        # A fresh clone has no search.yaml, and this is the first thing such a
        # user hits -- so point at the two ways forward rather than just failing.
        hint = (
            "\n\nEither copy search.example.yaml to search.yaml and edit it, "
            "or run with --demo to try the tool against bundled offline data."
            if search is None and not demo
            else ""
        )
        raise typer.BadParameter(f"{error}{hint}") from error

    for warning in lint_brief(brief):
        # Printed, never raised: these describe a perfectly valid brief that is
        # probably not what the operator meant, and they may have good reason.
        console.print(f"[yellow]warning:[/yellow] {warning}\n")
    return brief


@app.command()
def run(
    search: Annotated[
        Path | None,
        typer.Option("--search", "-s", help="Search brief YAML. Defaults to ./search.yaml."),
    ] = None,
    out: Annotated[Path, typer.Option("--out", "-o", help="Where to write the CSV.")] = Path(
        "out/prospects.csv"
    ),
    provider: Annotated[
        str, typer.Option("--provider", "-p", help=f"Backend: {', '.join(PROVIDERS)}.")
    ] = "serper",
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Run offline against bundled fixtures. Needs no API key."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the planned queries and cost, call nothing.")
    ] = False,
    pages: Annotated[
        int | None, typer.Option("--pages", help="Maximum result pages per target.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Only search the first N targets.")
    ] = None,
    max_queries: Annotated[
        int | None, typer.Option("--max-queries", help="Hard ceiling on queries for this run.")
    ] = None,
    min_confidence: Annotated[
        float | None,
        typer.Option("--min-confidence", help="Drop prospects scoring below this."),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore and do not write the response cache.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Search for decision-makers and write them to CSV."""
    log = setup_logging(verbose=verbose)
    settings = Settings()

    if demo:
        provider = "fixture"

    brief = _load(search, demo)
    if limit is not None:
        brief = brief.model_copy(update={"targets": brief.targets[:limit]})

    # Precedence throughout: an explicit CLI flag beats the brief, which beats
    # the built-in default. A `None` here means the flag was not given at all.
    options = PipelineOptions(
        max_pages=pages if pages is not None else brief.max_pages,
        max_queries=max_queries if max_queries is not None else settings.max_queries,
        concurrency=settings.concurrency,
    )
    threshold = min_confidence if min_confidence is not None else brief.min_confidence

    if dry_run:
        _show_plan(brief, provider, options)
        return

    cache: Cache = (
        NullCache()
        if no_cache or provider == "fixture"
        else SqliteCache(
            settings.cache_dir / "responses.sqlite",
            ttl_seconds=settings.cache_ttl_hours * 3600,
        )
    )

    try:
        backend = _build_provider(provider, settings, cache, brief)
    except RuntimeError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from error

    prospects, report = asyncio.run(_execute(brief, backend, options, log))

    report.cache_hits = cache.hits
    report.cache_misses = cache.misses
    report.model = settings.gemini_model if provider == "gemini" else None
    report.estimated_cost_usd = _estimate_cost(report)

    if threshold > 0:
        prospects = [p for p in prospects if p.confidence >= threshold]
        report.prospects = len(prospects)

    csv_path = write_csv(prospects, out)
    write_json(prospects, out.with_suffix(".json"))
    write_report(report, out.parent / "run_report.json")

    if prospects:
        console.print(_preview_table(prospects))
    console.print(_summary_table(report, csv_path))

    for message in report.errors:
        console.print(f"[yellow]warning:[/yellow] {message}")


async def _execute(
    brief: SearchBrief,
    backend: SearchProvider,
    options: PipelineOptions,
    log: logging.Logger,
) -> tuple[list[Prospect], RunReport]:
    """Run the pipeline and always close the backend afterwards."""
    try:
        return await run_pipeline(brief, backend, options)
    except ProviderError as error:
        log.exception("search failed")
        raise typer.Exit(code=1) from error
    finally:
        await backend.aclose()


def _show_plan(brief: SearchBrief, provider: str, options: PipelineOptions) -> None:
    """Print what a run would do, without doing it."""
    plan = plan_queries(brief)
    worst_case = min(len(plan) * options.max_pages, options.max_queries)

    table = Table(title="Planned queries", title_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Target")
    table.add_column("Query", overflow="fold")
    for index, (target, query) in enumerate(plan, start=1):
        table.add_row(str(index), target.name, query)
    console.print(table)

    rate = _usd_per_1k(provider)
    console.print(
        f"\n[bold]{len(plan)}[/bold] targets x up to [bold]{options.max_pages}[/bold] pages "
        f"= at most [bold]{worst_case}[/bold] queries "
        f"(budget {options.max_queries}), "
        f"about [bold]${worst_case * rate / 1000:.3f}[/bold] on {provider} at list price.\n"
        "[dim]Nothing was sent. Drop --dry-run to execute.[/dim]"
    )


@app.command()
def providers() -> None:
    """Show the available backends and what each can do."""
    table = Table(title="Search backends", title_style="bold")
    table.add_column("Name")
    table.add_column("Pagination", justify="center")
    table.add_column("Snippets", justify="center")
    table.add_column("Key required")
    table.add_column("Notes", overflow="fold")

    rows = [
        (
            "serper",
            SerperProvider.capabilities,
            "SERPER_API_KEY",
            "Default. Google organic results as JSON, real pagination, ~$1/1k after 2,500 free.",
        ),
        (
            "gemini",
            GeminiGroundingProvider.capabilities,
            "GEMINI_API_KEY",
            "Grounding with Google Search. No pagination: returns only what the model cites. "
            "See docs/DECISIONS.md ADR-006.",
        ),
        ("fixture", FixtureProvider.capabilities, "none", "Offline replay used by --demo."),
    ]
    for name, caps, key, note in rows:
        table.add_row(
            name,
            "[green]yes[/green]" if caps.supports_pagination else "[red]no[/red]",
            "[green]yes[/green]" if caps.provides_snippets else "[red]no[/red]",
            key,
            note,
        )
    console.print(table)


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"grounded-prospector {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
