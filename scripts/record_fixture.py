"""Record one real Gemini grounding response, and print its shape.

This is a tool for working *on* :mod:`grounded_prospector.providers._interaction`,
not a part of the test suite. Nothing in the repository reads the file it writes:
the provider tests build their payloads inline, and ``--demo`` replays the
fabricated recordings in ``src/grounded_prospector/demo/``. Reach for it when a
change to the parser needs checking against what the API actually returns, rather
than against the documentation's description of it.

The summary it prints matters more than the file. It reports which searches the
model actually ran -- it rewrites the query, which is one of the reasons grounding
is the secondary backend (``docs/DECISIONS.md`` ADR-006) -- and how many
``url_citation`` annotations came back, since those are the entire usable yield of
a grounded response.

Its output is left untracked on purpose. A real recording is real people's names
and employers; the repository ships fabricated profiles only.

Usage::

    python scripts/record_fixture.py "Dune & Palm Events"

Requires ``GEMINI_API_KEY``. Costs one grounded prompt, and the model may issue
several billable searches for it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from google import genai

from grounded_prospector.config import Settings
from grounded_prospector.query import SYSTEM_INSTRUCTION, build_prompt, build_xray_query

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
DEFAULT_AGENCY = "Dune & Palm Events"
LOCATION = "Dubai"
ROLES = ("MICE", "Incentive", "Events", "Director", "Head of")


def main() -> int:
    """Record a fixture and print a summary of its shape."""
    agency = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_AGENCY

    settings = Settings()
    client = genai.Client(api_key=settings.require_gemini_key())

    query = build_xray_query(agency, LOCATION, list(ROLES))
    print(f"Agency : {agency}")
    print(f"Query  : {query}")
    print(f"Model  : {settings.gemini_model}\n")

    interaction = client.interactions.create(
        model=settings.gemini_model,
        input=build_prompt(query, agency),
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[{"type": "google_search"}],
    )

    payload: dict[str, Any] = interaction.model_dump(mode="json", exclude_none=True)

    FIXTURE_PATH.mkdir(parents=True, exist_ok=True)
    raw_path = FIXTURE_PATH / "gemini_interaction_raw.json"
    raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Summarise what came back, so a mismatch is obvious immediately --------
    print(f"status : {payload.get('status')}")
    steps = payload.get("steps") or []
    print(f"steps  : {[s.get('type') for s in steps]}")

    searches: list[str] = []
    citations: list[tuple[str, str]] = []
    for step in steps:
        if step.get("type") == "google_search_call":
            searches.extend((step.get("arguments") or {}).get("queries") or [])
        if step.get("type") == "model_output":
            for block in step.get("content") or []:
                for ann in block.get("annotations") or []:
                    if ann.get("type") == "url_citation":
                        citations.append((ann.get("url", ""), ann.get("title", "")))

    print(f"\nsearches actually run ({len(searches)}):")
    for search in searches:
        print(f"  - {search}")

    print(f"\nurl_citations ({len(citations)}):")
    for url, title in citations:
        marker = "LI " if "linkedin.com/in/" in url else "   "
        print(f"  {marker}{title}\n      {url}")

    usage = payload.get("usage") or {}
    print(f"\nusage.grounding_tool_count : {usage.get('grounding_tool_count')}")
    print(f"usage.total_tokens         : {usage.get('total_tokens')}")
    print(f"\nWrote {raw_path}")

    if not citations:
        print("\nWARNING: no citations returned. The parser needs a fixture with some.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
