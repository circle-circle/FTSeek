"""Bucket lineage contract for Playbook trades.

This module is deliberately pure: it computes an audit record from an
instruction row and its executed trade.  It does not read the database, send
orders, or modify stop/trail decisions.

Units:
    atr_pct_*                 fraction of price (0.035 == 3.5%)
    sl_pct_from_fill          percent of fill price (5.098 == 5.098%)
    bucket_sl_*_pct           percent of price
    sl_cap_breach_bps         basis points; positive means the cap was breached
    invalidation_pct_*        percent of anchor price; negative means below
    invalidation_atr_multiple ATR multiples; signed relative to anchor

Consumer:
    scripts/enrich_bucket_lineage.py writes the object under
    ``trade_records.metadata_json.bucket_lineage``; audits read the same key.
    The effective bucket is the bucket actually used by re-price when present,
    otherwise the generation bucket.  Generated/repriced buckets are retained
    for lineage and must not be used as the performance bucket.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Mapping, Optional


BUCKET_LINEAGE_SCHEMA = "bucket_lineage_v1"


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _positive_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _direction(value: Any) -> str:
    direction = str(value or "").upper()
    if direction in ("SHORT", "SELL_SHORT", "SELL"):
        return "SHORT"
    if direction in ("LONG", "BUY"):
        return "LONG"
    return direction


def select_vol_bucket(
    atr_pct: Any, vol_buckets: Mapping[str, Any]
) -> Optional[str]:
    """Select the configured bucket for an ATR fraction without side effects.

    与生产 `instruction_generator._compute_volatility_based_sl_tp` 的选择口径逐字对齐:
    按 `atr_pct_max` 升序, 取第一个 `atr < bound`; **缺 bound 的桶按 999.0 兜底**
    (生产用 `.get("atr_pct_max", 999.0)`, 即缺键桶是 catch-all)。早期版本这里跳过缺键桶,
    两套实现会在"桶缺边界"时给出不同归桶 —— 已由
    `tests/test_bucket_selector_parity.py` 钉住一致性。
    """
    atr = _positive_float(atr_pct)
    if atr is None or not isinstance(vol_buckets, Mapping):
        return None
    ordered = []
    for name, config in vol_buckets.items():
        if not isinstance(config, Mapping):
            continue
        upper = _positive_float(config.get("atr_pct_max", 999.0))
        if upper is None:
            continue
        ordered.append((upper, str(name)))
    for upper, name in sorted(ordered):
        if atr < upper:
            return name
    return None


def bucket_cap_pct(
    bucket: Optional[str], direction: str, vol_buckets: Mapping[str, Any]
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (ceil, short_widen, applicable_cap) for the effective bucket."""
    if not bucket or not isinstance(vol_buckets, Mapping):
        return None, None, None
    config = vol_buckets.get(bucket)
    if not isinstance(config, Mapping):
        return None, None, None
    ceil_pct = _positive_float(config.get("sl_ceil_pct"))
    if ceil_pct is None:
        return None, None, None
    widen_pct = 0.0
    if _direction(direction) == "SHORT":
        widen_pct = _positive_float(config.get("short_widen_pct")) or 0.0
    return round(ceil_pct, 6), round(widen_pct, 6), round(ceil_pct + widen_pct, 6)


def _alignment(actual: Optional[str], expected: Optional[str]) -> str:
    if not actual or not expected:
        return "unknown"
    return "match" if actual == expected else "mismatch"


def _round_optional(value: Optional[float], digits: int) -> Optional[float]:
    return round(value, digits) if value is not None else None


def build_bucket_lineage(
    *,
    instruction: Mapping[str, Any],
    trade: Mapping[str, Any],
    vol_buckets: Mapping[str, Any],
    bucket_schema_version: Optional[str],
    invalidation_text_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the deterministic bucket-lineage record for one executed trade."""
    trade_meta = parse_json_object(trade.get("metadata_json"))
    inst_meta = parse_json_object(instruction.get("metadata_json"))
    attribution = trade_meta.get("attribution")
    if not isinstance(attribution, dict):
        attribution = {}
    reprice = inst_meta.get("reprice_audit")
    if not isinstance(reprice, dict):
        reprice = {}

    direction = _direction(trade.get("direction") or instruction.get("direction"))
    fill_price = _positive_float(trade.get("entry_price"))
    stop_loss = _positive_float(trade_meta.get("stop_loss"))
    if stop_loss is None:
        stop_loss = _positive_float(instruction.get("stop_loss"))

    bucket_generated = str(
        instruction.get("vol_bucket") or attribution.get("vol_bucket") or ""
    ).strip() or None
    atr_generated = _positive_float(instruction.get("atr_pct"))
    if atr_generated is None:
        atr_generated = _positive_float(attribution.get("atr_pct"))

    bucket_repriced = str(reprice.get("vol_bucket") or "").strip() or None
    atr_repriced = _positive_float(reprice.get("atr_pct"))
    reprice_to = _positive_float(reprice.get("reprice_to"))

    bucket_effective = bucket_repriced or bucket_generated
    bucket_effective_source = "reprice" if bucket_repriced else (
        "generated" if bucket_generated else None
    )
    if reprice_to is not None:
        sl_anchor_price = reprice_to
        sl_anchor_type = "reprice"
    else:
        sl_anchor_price = _positive_float(instruction.get("entry_price"))
        sl_anchor_type = "instruction" if sl_anchor_price is not None else None

    sl_pct_from_fill = None
    if fill_price is not None and stop_loss is not None:
        sl_pct_from_fill = abs(fill_price - stop_loss) / fill_price * 100.0

    ceil_pct, widen_pct, cap_pct = bucket_cap_pct(
        bucket_effective, direction, vol_buckets
    )
    breach_bps = None
    if sl_pct_from_fill is not None and cap_pct is not None:
        breach_bps = (sl_pct_from_fill - cap_pct) * 100.0

    invalidation_pct = None
    invalidation_atr_multiple = None
    text_price = _positive_float(invalidation_text_price)
    effective_atr = atr_repriced or atr_generated
    if text_price is not None and sl_anchor_price is not None:
        invalidation_pct = (text_price - sl_anchor_price) / sl_anchor_price * 100.0
        if effective_atr is not None:
            invalidation_atr_multiple = invalidation_pct / (effective_atr * 100.0)

    generated_expected = select_vol_bucket(atr_generated, vol_buckets)
    repriced_expected = select_vol_bucket(atr_repriced, vol_buckets)
    missing_fields = []
    if fill_price is None:
        missing_fields.append("fill_price")
    if stop_loss is None:
        missing_fields.append("stop_loss")
    if bucket_generated is None:
        missing_fields.append("bucket_generated")
    if bucket_effective is None:
        missing_fields.append("bucket_effective")
    if effective_atr is None:
        missing_fields.append("atr_pct_effective")
    if sl_anchor_price is None:
        missing_fields.append("sl_anchor_price")

    core: Dict[str, Any] = {
        "lineage_schema": BUCKET_LINEAGE_SCHEMA,
        "bucket_schema_version": bucket_schema_version,
        "bucket_generated": bucket_generated,
        "bucket_generated_expected": generated_expected,
        "bucket_generated_alignment": _alignment(bucket_generated, generated_expected),
        "bucket_repriced": bucket_repriced,
        "bucket_repriced_expected": repriced_expected,
        "bucket_repriced_alignment": _alignment(bucket_repriced, repriced_expected),
        "bucket_effective": bucket_effective,
        "bucket_effective_source": bucket_effective_source,
        "atr_pct_generated": _round_optional(atr_generated, 6),
        "atr_pct_repriced": _round_optional(atr_repriced, 6),
        "sl_anchor_price": _round_optional(sl_anchor_price, 4),
        "sl_anchor_type": sl_anchor_type,
        "fill_price": _round_optional(fill_price, 4),
        "stop_loss": _round_optional(stop_loss, 4),
        "sl_pct_from_fill": _round_optional(sl_pct_from_fill, 3),
        "bucket_sl_ceil_pct": ceil_pct,
        "bucket_sl_widen_pct": widen_pct,
        "bucket_sl_cap_pct": cap_pct,
        "sl_cap_breach_bps": _round_optional(breach_bps, 1),
        "invalidation_text_price": _round_optional(text_price, 4),
        "invalidation_pct_from_anchor": _round_optional(invalidation_pct, 3),
        "invalidation_atr_multiple": _round_optional(invalidation_atr_multiple, 4),
        "missing_fields": missing_fields,
        "input_complete": not missing_fields,
    }
    digest = hashlib.sha256(
        json.dumps(core, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    core["source_hash"] = digest
    return core


def lineage_needs_update(
    existing: Any, candidate: Mapping[str, Any]
) -> bool:
    """True when the stored lineage is absent or its deterministic input changed."""
    if not isinstance(existing, dict):
        return True
    return (
        existing.get("lineage_schema") != candidate.get("lineage_schema")
        or existing.get("source_hash") != candidate.get("source_hash")
    )
