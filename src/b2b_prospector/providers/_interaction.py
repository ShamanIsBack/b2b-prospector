"""Parsing of Gemini Interactions API payloads.

The parser works on the JSON payload rather than the SDK's response object, so a
live response, a cached response and a replayed fixture all travel the same code
path. A live response is converted with ``model_dump(mode="json")`` the moment it
arrives; nothing downstream knows or cares where the dict came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from b2b_prospector.models import SearchHit, SearchTarget
from b2b_prospector.providers.base import ProviderError, SearchResult

_STEP_SEARCH_CALL = "google_search_call"
_STEP_MODEL_OUTPUT = "model_output"
_ANNOTATION_URL_CITATION = "url_citation"
_TOOL_GOOGLE_SEARCH = "google_search"
_STATUS_COMPLETED = "completed"


def _as_list(value: object) -> list[Any]:
    """Coerce a possibly-absent JSON array into a list."""
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, Any]:
    """Coerce a possibly-absent JSON object into a dict."""
    return value if isinstance(value, dict) else {}


def executed_queries(payload: dict[str, Any]) -> list[str]:
    """Return the searches the model actually chose to run.

    This is rarely identical to the query we asked for: the model may rephrase,
    split or repeat it, and every one of those is separately billable.
    """
    queries: list[str] = []
    for step in _as_list(payload.get("steps")):
        step_dict = _as_dict(step)
        if step_dict.get("type") == _STEP_SEARCH_CALL:
            arguments = _as_dict(step_dict.get("arguments"))
            queries.extend(str(q) for q in _as_list(arguments.get("queries")))
    return queries


def searches_billed(payload: dict[str, Any]) -> int:
    """Return how many grounded searches this interaction was charged for.

    Prefers the API's own accounting, falling back to counting issued queries
    when usage data is absent.
    """
    usage = _as_dict(payload.get("usage"))
    counts = _as_list(usage.get("grounding_tool_count"))
    reported = sum(
        int(entry.get("count") or 0)
        for entry in (_as_dict(c) for c in counts)
        if entry.get("type") == _TOOL_GOOGLE_SEARCH
    )
    return reported or len(executed_queries(payload))


def model_notes(payload: dict[str, Any]) -> str | None:
    """Return the model's prose answer, for context only.

    This text is never parsed for facts. It is carried into a clearly labelled
    column so a human reviewer can see what the model claimed, and check it
    against the cited pages.
    """
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    fragments: list[str] = []
    for step in _as_list(payload.get("steps")):
        step_dict = _as_dict(step)
        if step_dict.get("type") != _STEP_MODEL_OUTPUT:
            continue
        for block in _as_list(step_dict.get("content")):
            text = _as_dict(block).get("text")
            if isinstance(text, str) and text.strip():
                fragments.append(text.strip())

    return "\n".join(fragments) or None


def parse_interaction(
    payload: dict[str, Any],
    *,
    target: SearchTarget,
    query: str,
    provider: str,
    retrieved_at: datetime,
    from_cache: bool = False,
) -> SearchResult:
    """Turn an Interactions API payload into cited search hits.

    Only ``url_citation`` annotations become hits. The model's prose is captured
    separately and never mined for names — see ``docs/DECISIONS.md`` ADR-003.

    Raises:
        ProviderError: if the interaction did not complete successfully.
    """
    status = payload.get("status")
    if status is not None and status != _STATUS_COMPLETED:
        errors = _as_list(payload.get("errors"))
        detail = "; ".join(str(_as_dict(e).get("message") or e) for e in errors) or status
        raise ProviderError(f"interaction did not complete ({status}): {detail}")

    hits: list[SearchHit] = []
    for step in _as_list(payload.get("steps")):
        step_dict = _as_dict(step)
        if step_dict.get("type") != _STEP_MODEL_OUTPUT:
            continue
        for block in _as_list(step_dict.get("content")):
            for annotation in _as_list(_as_dict(block).get("annotations")):
                citation = _as_dict(annotation)
                if citation.get("type") != _ANNOTATION_URL_CITATION:
                    continue
                url = citation.get("url")
                if not isinstance(url, str) or not url.strip():
                    continue
                title = citation.get("title")
                hits.append(
                    SearchHit(
                        url=url.strip(),
                        title=title.strip() if isinstance(title, str) else "",
                        target=target.name,
                        query=query,
                        provider=provider,
                        retrieved_at=retrieved_at,
                    )
                )

    return SearchResult(
        hits=hits,
        searches_billed=searches_billed(payload),
        notes=model_notes(payload),
        from_cache=from_cache,
    )
