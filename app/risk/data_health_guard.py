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
import time
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


def check_quote_health(
    quote: Optional[Dict[str, Any]],
    max_age_seconds: float = 60.0,
    max_spread_pct: float = 0.02,
) -> Tuple[bool, str]:
    """
    检查报价是否健康。

    Args:
        quote: 报价字典, 需要包含 bid, ask, price
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
