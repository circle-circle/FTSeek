"""Read-only drift observation between online effective SL and broker STOP.

The broker STOP is intentionally a static offline safety net.  Online exits may
tighten the effective SL (breakeven/trailing/early-tight), so the two prices can
differ.  This shadow records that difference and never calls ``modify_order``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "broker_stop_drift_shadow.v1"
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "broker_stop_drift.jsonl"
_LAST_EMITTED: dict[tuple[str, str], float] = {}
_LOCK = threading.Lock()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _token(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1].upper()


def _append(record: Mapping[str, Any], path: str | os.PathLike | None = None) -> str:
    target = Path(path or os.getenv("BROKER_STOP_DRIFT_SHADOW_PATH", "") or _DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with _LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return str(target)


def record_stop_drift_shadow(
    dual: Any,
    *,
    row: Mapping[str, Any],
    meta: Mapping[str, Any],
    effective_sl: float,
    path: str | os.PathLike | None = None,
    now_ts: float | None = None,
    min_interval_s: float = 300.0,
) -> dict | None:
    """Observe one LIVE position; failures never affect exit evaluation."""
    try:
        if str(row.get("source_type") or "").upper() != "PLAYBOOK_LIVE":
            return None
        stop_id = str(meta.get("sl_broker_order_id") or "").strip()
        online_sl = _number(effective_sl)
        if not stop_id or online_sl is None or online_sl <= 0:
            return None

        now = time.time() if now_ts is None else float(now_ts)
        key = (str(row.get("symbol") or ""), stop_id)
        with _LOCK:
            last = _LAST_EMITTED.get(key)
            if last is not None and now - last < float(min_interval_s):
                return None
            if len(_LAST_EMITTED) > 512:
                _LAST_EMITTED.clear()
            _LAST_EMITTED[key] = now

        symbol = str(row.get("symbol") or "")
        direction = _token(row.get("direction"))
        status = dual.query_order_status(stop_id, target_env="real")
        record = {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "record_only": True,
            "symbol": symbol,
            "direction": direction,
            "stop_order_id": stop_id,
            "online_effective_sl": round(online_sl, 6),
            "broker_stop_price": None,
            "broker_status": "",
            "drift_usd": None,
            "abs_drift_bps": None,
            "observation_status": "unavailable",
            "error": "",
        }
        broker_stop = meta.get("broker_stop")
        recorded_stop = (
            _number(broker_stop.get("stop_price"))
            if isinstance(broker_stop, Mapping) else None
        )
        record["recorded_stop_price"] = recorded_stop

        if not isinstance(status, Mapping) or not status.get("success"):
            record["error"] = str(
                status.get("error", "order status unavailable")
                if isinstance(status, Mapping) else status
            )
            _append(record, path=path)
            return record

        broker_price = _number(status.get("aux_price")) or _number(status.get("price"))
        record["broker_status"] = _token(status.get("order_status"))
        if broker_price is None or broker_price <= 0:
            record["error"] = "broker_stop_price_missing"
            _append(record, path=path)
            return record
        drift = broker_price - online_sl
        record.update(
            broker_stop_price=round(broker_price, 6),
            drift_usd=round(drift, 6),
            abs_drift_bps=round(abs(drift) / online_sl * 10000.0, 2),
            observation_status="observed",
        )
        _append(record, path=path)
        return record
    except Exception as error:
        logger.warning(
            "[stop-drift-shadow] observation failed %s (exit path unchanged): %s",
            row.get("symbol") if isinstance(row, Mapping) else "", error, exc_info=True,
        )
        return None
