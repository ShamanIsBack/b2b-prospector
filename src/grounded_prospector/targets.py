"""Loading and validating the search brief."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from grounded_prospector.models import SearchBrief

DEFAULT_BRIEF_PATH = Path("search.yaml")

# The file was called this before search.yaml absorbed country, language,
# keywords, depth and strictness. Recognised only to give a useful error.
LEGACY_BRIEF_PATH = Path("agencies.yaml")


class SearchBriefError(ValueError):
    """The search brief could not be read or did not have the expected shape."""


def _missing_file_message(path: Path) -> str:
    """Explain a missing brief, including how to migrate an older one."""
    if path == DEFAULT_BRIEF_PATH and LEGACY_BRIEF_PATH.exists():
        return (
            f"{DEFAULT_BRIEF_PATH} not found, but {LEGACY_BRIEF_PATH} exists.\n\n"
            f"The file was renamed when it grew beyond a list of agencies: it now also "
            f"holds country, language, keywords, max_pages and min_confidence, so one "
            f"file fully describes a search.\n\n"
            f"Rename it (`mv {LEGACY_BRIEF_PATH} {DEFAULT_BRIEF_PATH}`) and add the new "
            f"fields -- see search.example.yaml for a commented template. Existing "
            f"fields are unchanged and the new ones all have defaults."
        )
    return f"search brief not found: {path}"


def load_brief(path: Path = DEFAULT_BRIEF_PATH) -> SearchBrief:
    """Read a search brief from a YAML file.

    Raises:
        SearchBriefError: if the file is missing, malformed, or does not match the
            expected schema. The underlying validation message is preserved,
            because "roles: expected list, got str" is far more useful than
            "invalid file".
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SearchBriefError(_missing_file_message(path)) from error
    except yaml.YAMLError as error:
        raise SearchBriefError(f"search brief is not valid YAML: {path}\n{error}") from error

    if not isinstance(raw, dict):
        raise SearchBriefError(f"search brief must be a YAML mapping: {path}")

    try:
        brief = SearchBrief.model_validate(raw)
    except ValidationError as error:
        raise SearchBriefError(f"search brief is invalid: {path}\n{error}") from error

    if not brief.agencies:
        raise SearchBriefError(f"search brief contains no agencies: {path}")

    return brief
