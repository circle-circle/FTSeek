#!/usr/bin/env python3
"""SIM-only adaptive trailing drawdown helper."""


SIM_SOURCE_TYPE = "PLAYBOOK_SIM"


def _number(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def adaptive_trailing_drawdown_pct(
    peak_profit_pct,
    config=None,
    source_type=None,
):
    """Return the adaptive drawdown, or None when unavailable.

    The formula targets a percentage of gross MFE and clamps the drawdown:
        drawdown = clamp((1 - target_capture) * MFE, min, max)

    The source gate is deliberately hard-coded to PLAYBOOK_SIM. Live must keep
    the factor-specific static drawdown until a separate explicit decision.
    """
    config = config or {}
    if not bool(config.get("adaptive_trailing_enabled", False)):
        return None
    if str(source_type or "").strip().upper() != SIM_SOURCE_TYPE:
        return None

    target_capture_pct = _number(
        config.get("adaptive_trailing_target_capture_pct"), 60.0
    )
    min_drawdown_pct = _number(
        config.get("adaptive_trailing_min_drawdown_pct"), 0.4
    )
    max_drawdown_pct = _number(
        config.get("adaptive_trailing_max_drawdown_pct"), 1.2
    )
    if not (0 < target_capture_pct < 100):
        return None
    if min_drawdown_pct <= 0 or max_drawdown_pct < min_drawdown_pct:
        return None

    mfe_pct = max(0.0, _number(peak_profit_pct))
    drawdown_pct = (100.0 - target_capture_pct) / 100.0 * mfe_pct
    return round(
        min(max(drawdown_pct, min_drawdown_pct), max_drawdown_pct), 6
    )


def adaptive_trailing_snapshot(
    peak_profit_pct,
    config=None,
    source_type=None,
):
    drawdown_pct = adaptive_trailing_drawdown_pct(
        peak_profit_pct, config, source_type
    )
    if drawdown_pct is None:
        return None
    config = config or {}
    return {
        "mfe_pct": round(max(0.0, _number(peak_profit_pct)), 6),
        "drawdown_pct": drawdown_pct,
        "target_capture_pct": round(
            _number(config.get("adaptive_trailing_target_capture_pct"), 60.0), 6
        ) if config else 60.0,
    }
