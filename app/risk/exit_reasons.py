"""Canonical trade exit reason helpers shared by writers and reports."""
from __future__ import annotations

from typing import Optional


_LEGACY_EXIT_REASON_ALIASES = {
    "tighten": "EARLY_TIGHT_SL",
    "breakeven_lock": "SL_BREAKEVEN",
    "lock_in": "SL_TRAIL_LOCK",
}


def normalize_exit_reason(reason: Optional[str]) -> str:
    """Map known legacy lowercase labels to the production exit taxonomy."""
    value = str(reason or "").strip()
    return _LEGACY_EXIT_REASON_ALIASES.get(value.lower(), value)
