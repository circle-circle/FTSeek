"""FRED CSV observation parsing with explicit observation dates."""

import csv
import io
import logging
import math
from datetime import date
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def latest_valid_observation(csv_text: str) -> Tuple[Optional[str], Optional[float]]:
    """Return the latest valid (observation_date, value), skipping FRED '.' rows."""
    try:
        rows = list(csv.reader(io.StringIO(csv_text or "")))
    except (csv.Error, TypeError):
        return None, None
    for row in reversed(rows):
        if len(row) < 2:
            continue
        observation_date = row[0].strip()
        try:
            date.fromisoformat(observation_date)
            value = float(row[1].strip())
        except (TypeError, ValueError) as exc:
            logger.warning("FRED 坏观测行跳过 %s: %s", observation_date, exc, exc_info=True)
            continue
        if not math.isfinite(value):
            continue
        return observation_date, value
    return None, None
