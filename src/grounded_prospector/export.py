"""Writing results to disk.

The CSV is the deliverable a sales team actually opens, so its column order is
part of the contract: evidence first, then the empty columns they fill in. The
trailing CRM columns are intentionally blank — this tool finds people, it does
not find contact details, and pretending otherwise would invite someone to email
an address nobody verified.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path

from grounded_prospector.models import Prospect, RunReport

# Filled in by this tool.
EVIDENCE_COLUMNS = [
    "First name",
    "Last name",
    "Headline",
    "Company from title",
    "Target agency",
    "Segment",
    "LinkedIn URL",
    "Location hint",
    "Confidence",
    "Needs review",
    "Review reasons",
    "SERP position",
    "Snippet",
    "Raw title",
    "Source query",
    "Provider",
    "Retrieved at",
]

# Left empty on purpose: enrichment and qualification are separate steps.
CRM_COLUMNS = [
    "Email",
    "Phone",
    "Business profile",
    "Client segment",
    "Potential rating",
    "Notes",
]

CSV_COLUMNS = [*EVIDENCE_COLUMNS, *CRM_COLUMNS]


def _row(prospect: Prospect) -> dict[str, str]:
    """Flatten a prospect into CSV cells."""
    values = {
        "First name": prospect.first_name or "",
        "Last name": prospect.last_name or "",
        "Headline": prospect.headline or "",
        "Company from title": prospect.company_from_title or "",
        "Target agency": prospect.agency,
        "Segment": prospect.segment or "",
        "LinkedIn URL": prospect.profile_url,
        "Location hint": prospect.location_hint or "",
        "Confidence": f"{prospect.confidence:.2f}",
        "Needs review": "yes" if prospect.needs_review else "no",
        "Review reasons": "; ".join(prospect.review_reasons),
        "SERP position": str(prospect.serp_position) if prospect.serp_position else "",
        "Snippet": prospect.snippet or "",
        "Raw title": prospect.raw_title,
        "Source query": prospect.source_query,
        "Provider": prospect.provider,
        "Retrieved at": prospect.retrieved_at.isoformat(),
    }
    return {**values, **dict.fromkeys(CRM_COLUMNS, "")}


def write_csv(prospects: Sequence[Prospect], path: Path) -> Path:
    """Write prospects to ``path`` as CSV.

    Encoded as UTF-8 with a BOM because the primary consumer opens it in Excel,
    which otherwise mangles every non-ASCII name in the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(_row(prospect) for prospect in prospects)
    return path


def write_json(prospects: Sequence[Prospect], path: Path) -> Path:
    """Write prospects to ``path`` as JSON, preserving full types."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [prospect.model_dump(mode="json") for prospect in prospects]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_report(report: RunReport, path: Path) -> Path:
    """Write the run report to ``path`` as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    payload["duration_seconds"] = round(report.duration_seconds, 3)
    payload["cache_hit_rate"] = round(report.cache_hit_rate, 4)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
