# -*- coding: utf-8 -*-
"""UE SHORT 借券 UNKNOWN 逐票补查 (2026-10-18).

背景: 每日借券快照只采集 `daily_active_pool.json` 的活跃池 (约 30-50 只),
UE 的榜外发现不在池内 ⇒ prompt 规则 10 把"无数据"标成 UNKNOWN/false ⇒
DA 源头过滤把 SHORT 当死单丢弃。Futu `get_margin_ratio` 本身支持逐票查询,
所以本模块在 UE 产出候选后补查一次, 只把 UNKNOWN 解析成 MATERIAL:

  shortable=True 且 pool>0  → borrow_feasible=True,  basis=MATERIAL
  shortable=False 或 pool<=0 → borrow_feasible=False, basis=MATERIAL
  无返回/异常/字段不足        → 保持 UNKNOWN, 但写 borrow_checked_at/source/status

边界:
  - 只处理 direction=SHORT; 已经是 MATERIAL 的不动; LONG/SKIP 不碰.
  - 单票调用, 不做大 batch — 实测一个无数据标的会让整批 get_margin_ratio 返回空.
  - 只补 short_check 数据, 不改 DA 过滤/仓位/止损/出场/执行门.
  - 失败 fail-open: 保持 UNKNOWN, 不阻断 UE 扫描.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

BorrowFetcher = Callable[[List[str]], Dict[str, Dict[str, Any]]]

# (ET 日期, 无前缀 symbol) -> get_short_pool 单票结果; 当日缓存, 防止同标的跨窗口重复查.
_CACHE: Dict[tuple[str, str], Dict[str, Any]] = {}


def _norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("US.")


def _quote_symbol(value: Any) -> str:
    return f"US.{_norm_symbol(value)}"


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def _default_fetch(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    from app.execution.dual_broker import get_dual_broker

    return get_dual_broker().get_short_pool(symbols)


def _apply_verified_row(
    item: Dict[str, Any],
    row: Optional[Dict[str, Any]],
    checked_at: str,
) -> str:
    """把单票查询结果写回 item['short_check']; 返回计数桶名."""
    short_check = item.get("short_check")
    if not isinstance(short_check, dict):
        short_check = {}
    short_check.setdefault("downside_catalyst_expiry_hours", 0)
    short_check.setdefault("squeeze_risk", 0)
    short_check["borrow_checked_at"] = checked_at
    short_check["borrow_source"] = "futu_get_margin_ratio_on_demand"
    item["short_check"] = short_check

    if not isinstance(row, dict):
        short_check["borrow_data_basis"] = "UNKNOWN"
        short_check["borrow_status"] = "no_data"
        return "no_data"

    shortable = row.get("shortable")
    pool = _as_float(row.get("pool"))
    fee_rate = _as_float(row.get("fee_rate"))
    if pool is not None:
        short_check["borrow_pool"] = pool
    if fee_rate is not None:
        short_check["borrow_fee_rate"] = fee_rate

    if shortable is True and pool is not None and pool > 0:
        short_check.update({
            "borrow_feasible": True,
            "borrow_data_basis": "MATERIAL",
            "borrow_status": "verified_shortable",
            "provided": True,
        })
        return "resolved_true"

    if shortable is False or (pool is not None and pool <= 0):
        short_check.update({
            "borrow_feasible": False,
            "borrow_data_basis": "MATERIAL",
            "borrow_status": "verified_not_shortable",
            "borrow_reject_reason": "not_shortable_or_zero_pool",
            "provided": True,
        })
        return "resolved_false"

    # shortable 缺失且 pool 缺失/异常, 或 permit 非 bool: 不能把未知当可借.
    short_check["borrow_data_basis"] = "UNKNOWN"
    short_check["borrow_status"] = "inconclusive"
    return "no_data"


def _mark_unknown(
    items: List[Dict[str, Any]],
    checked_at: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    for item in items:
        short_check = item.get("short_check")
        if not isinstance(short_check, dict):
            short_check = {}
        short_check.setdefault("downside_catalyst_expiry_hours", 0)
        short_check.setdefault("squeeze_risk", 0)
        short_check.update({
            "borrow_data_basis": "UNKNOWN",
            "borrow_checked_at": checked_at,
            "borrow_source": "futu_get_margin_ratio_on_demand",
            "borrow_status": status,
        })
        if error:
            short_check["borrow_error"] = str(error)[:200]
        item["short_check"] = short_check


def _apply_row_to_items(
    items: List[Dict[str, Any]],
    row: Optional[Dict[str, Any]],
    checked_at: str,
) -> str:
    bucket = ""
    for item in items:
        bucket = _apply_verified_row(item, row, checked_at)
    return bucket


def enrich_surfaced_short_borrow(
    surfaced: List[Dict[str, Any]],
    *,
    fetch: Optional[BorrowFetcher] = None,
    checked_at: Optional[str] = None,
    cache: Optional[Dict[tuple[str, str], Dict[str, Any]]] = None,
    max_symbols: int = 12,
    batch_size: int = 5,
    max_fallback: int = 5,
    pause_sec: float = 0.0,
) -> Dict[str, int]:
    """补查 surfaced SHORT 的借券 UNKNOWN, 只把未知解析成 MATERIAL.

    Returns 计数: checked/resolved_true/resolved_false/no_data/already_material/
    skipped_limit/error. 任何异常都不抛给调用方.

    调用策略: 先按 batch_size=5 小批查询 (Futu 单次 get_margin_ratio 算一次调用),
    某个小批空返回时再对缺失标的逐票兜底 (max_fallback 只). 实测一次大 batch
    会被无数据标的拖垮, 而连续逐票 10+ 次会被限流; 小批+有限兜底是两者折中.
    `pause_sec` 生产侧建议 1.0s, 单次扫描通常 3-6 只.
    """
    counts = {
        "checked": 0,
        "resolved_true": 0,
        "resolved_false": 0,
        "no_data": 0,
        "already_material": 0,
        "skipped_limit": 0,
        "error": 0,
    }
    if not surfaced:
        return counts

    fetcher = fetch or _default_fetch
    cache = _CACHE if cache is None else cache
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    day = checked_at[:10]

    items_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for item in surfaced:
        if not isinstance(item, dict):
            continue
        if str(item.get("direction") or "").upper() != "SHORT":
            continue
        symbol = _norm_symbol(item.get("symbol"))
        if not symbol:
            continue

        existing = item.get("short_check")
        if isinstance(existing, dict) and str(
            existing.get("borrow_data_basis") or ""
        ).upper() == "MATERIAL":
            counts["already_material"] += 1
            continue

        if len(items_by_symbol) >= max_symbols and symbol not in items_by_symbol:
            counts["skipped_limit"] += 1
            continue
        items_by_symbol.setdefault(symbol, []).append(item)

    if not items_by_symbol:
        return counts

    queried: set[str] = set()

    def _count_query(symbol: str) -> None:
        if symbol not in queried:
            queried.add(symbol)
            counts["checked"] += 1

    pending: List[str] = []
    for symbol, items in items_by_symbol.items():
        key = (day, symbol)
        if key in cache:
            _count_query(symbol)
            bucket = _apply_row_to_items(items, cache[key], checked_at)
            counts[bucket] += len(items)
        else:
            pending.append(symbol)

    missing: List[str] = []
    for start in range(0, len(pending), batch_size):
        chunk = pending[start:start + batch_size]
        for symbol in chunk:
            _count_query(symbol)
        try:
            response = fetcher([_quote_symbol(s) for s in chunk]) or {}
        except Exception as exc:
            counts["error"] += 1
            logger.warning(
                "[borrow_verify] 小批查询失败 %s (转逐票兜底): %s",
                chunk, exc, exc_info=True,
            )
            if len(chunk) == 1:
                _mark_unknown(items_by_symbol[chunk[0]], checked_at, "fetch_error", str(exc))
                counts["no_data"] += len(items_by_symbol[chunk[0]])
            else:
                missing.extend(chunk)
            if pause_sec > 0:
                time.sleep(pause_sec)
            continue

        for symbol in chunk:
            row = response.get(_quote_symbol(symbol)) or response.get(symbol)
            if isinstance(row, dict):
                cache[(day, symbol)] = row
                bucket = _apply_row_to_items(items_by_symbol[symbol], row, checked_at)
                counts[bucket] += len(items_by_symbol[symbol])
            else:
                missing.append(symbol)
        if pause_sec > 0:
            time.sleep(pause_sec)

    for index, symbol in enumerate(missing):
        items = items_by_symbol[symbol]
        if index >= max_fallback:
            _mark_unknown(items, checked_at, "no_data")
            counts["no_data"] += len(items)
            continue
        try:
            response = fetcher([_quote_symbol(symbol)]) or {}
            row = response.get(_quote_symbol(symbol)) or response.get(symbol)
        except Exception as exc:
            counts["error"] += 1
            _mark_unknown(items, checked_at, "fetch_error", str(exc))
            counts["no_data"] += len(items)
            logger.warning(
                "[borrow_verify] %s 逐票兜底失败 (保持 UNKNOWN): %s",
                symbol, exc, exc_info=True,
            )
            if pause_sec > 0:
                time.sleep(pause_sec)
            continue

        if isinstance(row, dict):
            cache[(day, symbol)] = row
            bucket = _apply_row_to_items(items, row, checked_at)
            counts[bucket] += len(items)
        else:
            _mark_unknown(items, checked_at, "no_data")
            counts["no_data"] += len(items)
        if pause_sec > 0:
            time.sleep(pause_sec)

    if any(counts[k] for k in (
        "checked", "already_material", "skipped_limit", "error"
    )):
        logger.info("[borrow_verify] %s", counts)
    return counts
