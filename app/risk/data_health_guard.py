"""
数据健康度 guard (Data Health Guard)

2026-06-25: 为 OKR 新增。在交易前检查价格数据是否健康:
  1. bid/ask 必须存在且 > 0
  2. 价格时间戳必须新鲜 (默认 < 60 秒)
  3. bid-ask spread 必须合理 (默认 < 2%)

使用:
  from app.risk.data_health_guard import check_quote_health
  ok, reason = check_quote_health(quote, max_age_seconds=60, max_spread_pct=0.02)
  if not ok:
      # 拒绝交易
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


def check_quote_health(
    quote: Optional[Dict[str, Any]],
    max_age_seconds: float = 60.0,
    max_spread_pct: float = 0.02,
    *,
    require_exchange_quote: bool = False,
) -> Tuple[bool, str]:
    """
    检查报价是否健康。

    Args:
        quote: 报价字典, 需要包含 bid, ask, price.
            timestamp 必须为 Unix float (time.time() 兼容), 不接受 ISO 字符串.
            契约来源: futu_lv3_provider / finnhub_provider 均产出 float timestamp.
        max_age_seconds: 最大允许延迟 (秒)
        max_spread_pct: 最大允许 bid-ask spread

    Returns:
        (is_healthy, reason)
    """
    if not quote:
        return False, "quote is None"

    bid = float(quote.get("bid", 0) or 0)
    ask = float(quote.get("ask", 0) or 0)
    price = float(quote.get("price", 0) or quote.get("last", 0) or 0)

    if require_exchange_quote:
        if not all(math.isfinite(value) and value > 0 for value in (bid, ask, price)):
            return False, "实盘报价必须为有限正数"
        if quote.get("book_is_proxy") or bid >= ask:
            return False, "实盘必须使用有效双边盘口，拒绝代理、锁定或交叉盘口"
        source = str(quote.get("data_source") or quote.get("source") or "").lower()
        if source in DEGRADED_QUOTE_SOURCES:
            return False, f"降级数据源 ({source})"
        from app.playbook.candidate_eligibility import _source_time
        _, exchange_age = _source_time(quote, datetime.now(timezone.utc))
        if exchange_age is None or not math.isfinite(exchange_age) or exchange_age < 0:
            return False, "交易所报价时间缺失、无效或在未来"
        if exchange_age > max_age_seconds:
            return False, f"交易所报价过期 (age={exchange_age:.0f}s > {max_age_seconds:.0f}s)"

    if bid <= 0 or ask <= 0:
        return False, f"bid/ask 缺失 (bid={bid}, ask={ask})"

    if bid > ask:
        return False, f"bid > ask (bid={bid}, ask={ask})"

    spread_pct = (ask - bid) / ((ask + bid) / 2.0)
    if spread_pct > max_spread_pct:
        return False, f"spread 过大 ({spread_pct:.2%} > {max_spread_pct:.2%})"

    # 时间戳检查
    ts = quote.get("timestamp")
    if ts:
        try:
            age = time.time() - float(ts)
            if age > max_age_seconds:
                return False, f"quote 过期 (age={age:.0f}s > {max_age_seconds:.0f}s)"
        except Exception:
            pass

    # 价格与 mid 偏差检查 (last 不应偏离 mid 超过 spread)
    if price > 0:
        mid = (bid + ask) / 2.0
        if abs(price - mid) / mid > max_spread_pct:
            return False, f"last_price {price} 偏离 mid {mid} 过大"

    return True, "ok"


# B4 (2026-07-20): 已知降级数据源 — 均为延迟/无盘口报价, 不可作为开新仓的价格依据.
#   finnhub /quote 15min 延迟; twelvedata/alphavantage timestamp 为抓取时间 (非成交时间).
DEGRADED_QUOTE_SOURCES = {"finnhub", "finnhub_fallback", "twelvedata", "alphavantage"}


def check_quote_freshness(
    quote: Optional[Dict[str, Any]],
    max_age_seconds: float = 90.0,
    degraded_sources: Optional[set] = None,
) -> Tuple[bool, str]:
    """开新仓专用数据新鲜度检查 (B4, 2026-07-20)。

    与 check_quote_health 互补: health 门依赖 bid/ask 齐备, 而降级源报价
    (finnhub /quote 等) 恰好无盘口 → 整体绕过 health 门。本检查不依赖盘口:
      1. data_source/source 命中降级源 → 不新鲜 (延迟数据不可开仓)
      2. timestamp 年龄 > max_age_seconds → 不新鲜 (兼容 Unix float 与 ISO 字符串)
    仅用于拦开新仓; 平仓路径不应调用本函数 (平仓宁可用旧价也要能出场)。

    Returns:
        (is_fresh, reason)
    """
    if not quote:
        return False, "quote is None"

    src = str(quote.get("data_source") or quote.get("source") or "").lower()
    if degraded_sources is None:
        degraded_sources = DEGRADED_QUOTE_SOURCES
    if src in degraded_sources:
        return False, f"降级数据源 ({src})"

    ts = quote.get("timestamp")
    if ts:
        age = None
        try:
            age = time.time() - float(ts)
        except (TypeError, ValueError):
            try:
                from datetime import datetime as _dt
                age = time.time() - _dt.fromisoformat(str(ts)).timestamp()
            except Exception:
                age = None
        if age is not None and age > max_age_seconds:
            return False, f"quote 过期 (age={age:.0f}s > {max_age_seconds:.0f}s, source={src or 'unknown'})"

    return True, "ok"
