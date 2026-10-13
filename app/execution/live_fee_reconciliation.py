"""Reconcile LIVE round-trip PnL with Futu's actual order fees."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _estimated_total(fee_breakdown: Any) -> float | None:
    if not isinstance(fee_breakdown, dict):
        return None
    try:
        value = fee_breakdown.get("total")
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def fee_discrepancy_is_material(estimated: float | None, actual: float) -> bool:
    if estimated is None or estimated <= 0:
        return False
    return abs(actual - estimated) > max(0.05, estimated * 0.02)


def reconcile_live_order_fees(broker: Any, conn: Any, trade_id: int) -> Dict[str, Any]:
    """Persist broker fees for one closed LIVE playbook round trip.

    This is post-close bookkeeping only. A failed query never reopens the trade
    and never blocks the exit pipeline. The original fee estimate is preserved
    inside metadata while the authoritative net PnL columns are upgraded to
    `fees_source=broker`.
    """
    if conn.in_transaction:
        return {"status": "failed", "reason": "connection has a pending transaction"}

    row = conn.execute(
        "SELECT source_type, trade_mode, symbol, futu_order_id, "
        "close_futu_order_id, realized_pnl, fees_est, fees_source, metadata_json "
        ", entry_price, quantity "
        "FROM trade_records WHERE id=?",
        (trade_id,),
    ).fetchone()
    if row is None:
        return {"status": "skipped", "reason": "trade row not found"}
    if str(row[0] or "") != "PLAYBOOK_LIVE" or str(row[1] or "") != "live":
        return {"status": "skipped", "reason": "not a live playbook trade"}
    if str(row[7] or "") in ("broker", "reconciled"):
        return {"status": "skipped", "reason": "fees already broker-authoritative"}
    entry_order_id = str(row[3] or "")
    exit_order_id = str(row[4] or "")
    if not entry_order_id or not exit_order_id:
        return {"status": "failed", "reason": "entry or exit order id missing"}

    result = broker.query_order_fees(
        [entry_order_id, exit_order_id], target_env="real"
    )
    if not result.get("success"):
        return {"status": "failed", "reason": str(result.get("error") or "broker fee query failed")}
    fees = result.get("fees") or {}
    if entry_order_id not in fees or exit_order_id not in fees:
        return {
            "status": "failed",
            "reason": "broker fee query did not return both entry and exit orders",
        }

    entry_fee = round(float(fees[entry_order_id]), 2)
    exit_fee = round(float(fees[exit_order_id]), 2)
    actual_total = round(entry_fee + exit_fee, 2)
    recorded_at = _utc_now_iso()

    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT fees_source, metadata_json, realized_pnl FROM trade_records WHERE id=?",
            (trade_id,),
        ).fetchone()
        if current is None:
            conn.rollback()
            return {"status": "failed", "reason": "trade row disappeared"}
        if str(current[0] or "") in ("broker", "reconciled"):
            conn.rollback()
            return {"status": "skipped", "reason": "fees already broker-authoritative"}

        metadata = {}
        if current[1]:
            metadata = json.loads(current[1])
            if not isinstance(metadata, dict):
                raise ValueError("trade metadata is not an object")
        existing_breakdown = metadata.get("fee_breakdown")
        estimated_total = _estimated_total(existing_breakdown)
        broker_breakdown = {
            "entry_order_id": entry_order_id,
            "exit_order_id": exit_order_id,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "total": actual_total,
            "source": "broker",
            "recorded_at": recorded_at,
        }
        if isinstance(existing_breakdown, dict):
            metadata["fee_breakdown"] = {
                **existing_breakdown,
                "broker": broker_breakdown,
                "estimated_total": estimated_total,
                "broker_total": actual_total,
                "difference": round(actual_total - float(estimated_total), 2)
                if estimated_total is not None else None,
                "source": "broker",
                "recorded_at": recorded_at,
            }
        else:
            metadata["fee_breakdown"] = broker_breakdown

        gross_pnl = round(float(current[2] or 0), 2)
        instruction_id = metadata.get("playbook_instruction_id")
        net_pnl = round(gross_pnl - actual_total, 2)
        notional = float(row[9] or 0) * float(row[10] or 0)
        net_pnl_pct = round(net_pnl / notional, 4) if notional > 0 else None

        updated = conn.execute(
            "UPDATE trade_records SET fees_est=?, pnl_net=?, fees_source='broker', "
            "net_pnl_pct=?, "
            "metadata_json=? WHERE id=? AND fees_source NOT IN ('broker','reconciled')",
            (
                actual_total,
                round(gross_pnl - actual_total, 2),
                net_pnl_pct,
                json.dumps(metadata, ensure_ascii=False, default=str),
                trade_id,
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return {"status": "skipped", "reason": "fees source changed concurrently"}
        if instruction_id is not None:
            conn.execute(
                "UPDATE playbook_instructions SET pnl=?, pnl_pct=?, "
                "outcome=CASE WHEN outcome='void' THEN 'void' ELSE ? END "
                "WHERE id=?",
                (
                    net_pnl,
                    net_pnl_pct,
                    "win" if net_pnl > 0 else "loss",
                    instruction_id,
                ),
            )
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        finally:
            raise

    return {
        "status": "ok",
        "symbol": row[2],
        "fees_total": actual_total,
        "estimated_total": estimated_total,
        "difference": round(actual_total - float(estimated_total), 2)
        if estimated_total is not None else None,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
    }
