"""Loading and validating the target list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from grounded_prospector.models import TargetList


class TargetListError(ValueError):
    """The target list could not be read or did not have the expected shape."""


def load_targets(path: Path) -> TargetList:
    """Read a target list from a YAML file.

    Raises:
        TargetListError: if the file is missing, malformed, or does not match the
            expected schema. The underlying validation message is preserved,
            because "roles: expected list, got str" is far more useful than
            "invalid file".
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TargetListError(f"target list not found: {path}") from error
    except yaml.YAMLError as error:
        raise TargetListError(f"target list is not valid YAML: {path}\n{error}") from error

    if not isinstance(raw, dict):
        raise TargetListError(f"target list must be a YAML mapping: {path}")

    try:
        targets = TargetList.model_validate(raw)
    except ValidationError as error:
        raise TargetListError(f"target list is invalid: {path}\n{error}") from error

    if not targets.agencies:
        raise TargetListError(f"target list contains no agencies: {path}")

    return targets
