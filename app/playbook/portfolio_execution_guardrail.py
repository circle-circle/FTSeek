"""Execution-time portfolio guardrail shared by all instruction writers."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from app.playbook.pipeline import INVALIDATED_SKIP_REASONS


MAX_DIRECTION_SKEW = 0.80
MAX_SECTOR_PCT = 0.40
MIN_SECTOR_ROWS = 3


def _confidence(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _sort_key(row: Dict[str, Any]):
    return (_confidence(row), int(row.get("_order") or 0), int(row.get("id") or 0))


def _active_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    active = []
    for order, row in enumerate(rows or []):
        direction = str(row.get("direction") or "").upper()
        if direction not in ("BUY", "SELL_SHORT"):
            continue
        if str(row.get("skip_reason") or "") in INVALIDATED_SKIP_REASONS:
            continue
        item = dict(row)
        item["_order"] = order
        item["_cancellable"] = (
            not bool(row.get("executed"))
            and str(row.get("execution_state") or "PENDING") == "PENDING"
        )
        active.append(item)
    return active


def _fill_sectors(active: List[Dict[str, Any]]) -> None:
    missing = [str(row.get("symbol") or "") for row in active if not (row.get("sector") or "")]
    industry_map: Dict[str, str] = {}
    if missing:
        try:
            from app.playbook.pipeline import _get_vault_industry_map
            industry_map = _get_vault_industry_map()
        except Exception:
            industry_map = {}
    for row in active:
        row["_sector"] = str(
            row.get("sector") or industry_map.get(str(row.get("symbol") or ""), "") or "Unknown"
        )


def evaluate_portfolio_guardrail(
    rows: List[Dict[str, Any]],
    instruction_id: int,
    current: Optional[Dict[str, Any]] = None,
    *,
    max_direction_skew: float = MAX_DIRECTION_SKEW,
    max_sector_pct: float = MAX_SECTOR_PCT,
) -> Dict[str, Any]:
    """Return whether the current instruction survives the day portfolio gates."""
    snapshot = [dict(row) for row in rows or []]
    if current is not None and not any(int(row.get("id") or 0) == int(instruction_id) for row in snapshot):
        item = dict(current)
        item["id"] = instruction_id
        snapshot.append(item)

    active = _active_rows(snapshot)
    initial_counts = Counter(str(row.get("direction") or "").upper() for row in active)
    initial_active_count = len(active)
    if initial_active_count <= 1:
        return {
            "should_skip": False,
            "reason": "",
            "active_count": initial_active_count,
            "direction_counts": dict(initial_counts),
            "sector_counts": {},
            "skip_ids": [],
        }

    skipped: List[Dict[str, Any]] = []
    while len(active) > 1:
        total = len(active)
        buy_count = sum(1 for row in active if row.get("direction") == "BUY")
        short_count = total - buy_count
        majority_pct = max(buy_count, short_count) / total
        if majority_pct <= max_direction_skew:
            break
        majority_direction = "BUY" if buy_count > short_count else "SELL_SHORT"
        victims = [
            row for row in active
            if row.get("direction") == majority_direction and row["_cancellable"]
        ]
        if not victims:
            break
        victim = min(victims, key=_sort_key)
        victim["_portfolio_skip_reason"] = (
            f"组合方向偏斜 guardrail 拒绝执行 "
            f"(当日 {buy_count}B/{short_count}S={majority_pct:.0%} > {max_direction_skew:.0%})"
        )
        skipped.append(victim)
        active.remove(victim)

    sector_counts: Counter[str] = Counter()
    if len(active) >= MIN_SECTOR_ROWS:
        _fill_sectors(active)
        sector_counts = Counter(row["_sector"] for row in active)
        for sector in list(sector_counts):
            if not sector or sector == "Unknown":
                continue
            while len(active) > 1:
                total = len(active)
                current_sector_count = sum(
                    1 for row in active if row["_sector"] == sector
                )
                if current_sector_count / total <= max_sector_pct:
                    break
                victims = [
                    row for row in active
                    if row["_sector"] == sector and row["_cancellable"]
                ]
                if not victims:
                    break
                victim = min(victims, key=_sort_key)
                victim["_portfolio_skip_reason"] = (
                    f"组合行业集中度 guardrail 拒绝执行 "
                    f"({sector} {current_sector_count}/{total}={current_sector_count / total:.0%} "
                    f"> {max_sector_pct:.0%})"
                )
                skipped.append(victim)
                active.remove(victim)
                sector_counts = Counter(row["_sector"] for row in active)

    skip_ids = [int(row.get("id") or 0) for row in skipped]
    current_reason = ""
    if int(instruction_id) in skip_ids:
        current_reason = next(
            str(row.get("_portfolio_skip_reason") or "")
            for row in skipped
            if int(row.get("id") or 0) == int(instruction_id)
        )

    return {
        "should_skip": int(instruction_id) in skip_ids,
        "reason": current_reason,
        "active_count": initial_active_count,
        "direction_counts": dict(initial_counts),
        "sector_counts": dict(sector_counts),
        "skip_ids": skip_ids,
    }
