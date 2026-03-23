from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from prompt_engineering.config import PACKAGE_ROOT

logger = logging.getLogger(__name__)

_DATA_DIR = PACKAGE_ROOT / "data"


def load_dataset(path: str | None = None) -> str:
    """Read the CSV dataset and return its full text for prompt injection."""
    csv_path = Path(path) if path else _DATA_DIR / "dataset.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Dataset not found: {csv_path}")
    return csv_path.read_text(encoding="utf-8")


def load_golden(path: str | None = None) -> dict[str, Any]:
    """Load ``golden_data.json``: ``row_errors`` (v3-shaped, rows 121–300), metadata."""
    json_path = Path(path) if path else _DATA_DIR / "golden_data.json"
    if not json_path.exists():
        raise FileNotFoundError(
            f"Golden data not found: {json_path}. "
            "Expected `golden_data.json` with `row_errors` array."
        )
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if "row_errors" not in data:
        raise ValueError(
            f"Golden file must contain 'row_errors' array: {json_path}"
        )
    rows = data["row_errors"]
    if not isinstance(rows, list):
        raise ValueError(f"'row_errors' must be a list: {json_path}")
    n = len(rows)
    declared = data.get("golden_error_row_count")
    if declared is not None and int(declared) != n:
        logger.warning(
            "golden_error_row_count (%s) != len(row_errors) (%s) in %s — using len(row_errors).",
            declared,
            n,
            json_path,
        )
    return data
