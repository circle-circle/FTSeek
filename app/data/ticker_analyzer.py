"""
Ticker Analyzer — 逐笔成交量能分析因子 (2026-05-20 新增)

从 Futu LV3 Push Ticker 数据中提取实时量能因子：
  - buy_sell_ratio: 买卖力道比 (近 N 笔买入量 / 卖出量)
  - volume_surge:   成交量突增倍数 (近期 vs 基准)
  - large_order_ratio: 大单占比 (>=阈值 的成交占比)
  - vwap_deviation: VWAP 偏离度
  - ticker_velocity:  逐笔频率 (笔/分钟)

设计原则:
  1. 纯函数式，无状态，线程安全
  2. 入参 = 逐笔 ticker 列表 (从 PushTickerHandler 缓存获取)
  3. 出参 = 因子字典，可直接注入 consensus_engine / signal_pipeline
  4. 毫秒级返回，不阻塞回调线程
"""
import logging
import time
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.us")

# ── 配置常量 ──────────────────────────────────────────────────────────

# 大单阈值: 单笔成交量 >= 此值视为大单 (美股常见大单阈值)
DEFAULT_LARGE_ORDER_THRESHOLD = 500

# 时间窗口 (秒)
SHORT_WINDOW = 60      # 1 分钟短窗口
MEDIUM_WINDOW = 300    # 5 分钟中窗口
LONG_WINDOW = 900      # 15 分钟长窗口

# VWAP 偏离度阈值
VWAP_DEVIATION_BULL = 0.003   # 价格 > VWAP + 0.3% → 多头
VWAP_DEVIATION_BEAR = -0.003  # 价格 < VWAP - 0.3% → 空头


def analyze_tickers(
    tickers: List[Dict[str, Any]],
    current_price: Optional[float] = None,
    large_order_threshold: int = DEFAULT_LARGE_ORDER_THRESHOLD,
) -> Optional[Dict[str, Any]]:
    """
    从逐笔数据中提取量能因子

    Args:
        tickers: 逐笔数据列表, 每条:
            {
                "price": float,
                "volume": float,
                "turnover": float,
                "direction": "BUY"|"SELL"|"NEUTRAL",
                "time": str,
                "sequence": int,
            }
        current_price: 当前最新价 (用于 VWAP 偏离度计算)
        large_order_threshold: 大单阈值 (笔)

    Returns:
        因子字典 or None (数据不足)
    """
    if not tickers or len(tickers) < 10:
        return None

    now = time.time()

    # ── 按时间窗口分割 ────────────────────────────────────────────
    # 注: ticker["time"] 格式为 "2026-05-20 09:30:01.123" 等
    # 但 Push 数据没有 Unix 时间戳，我们用 sequence 单调递增 + 最近 N 笔近似

    short_tickers = tickers[-50:]    # 最近 ~50 笔 ≈ 1 分钟
    medium_tickers = tickers[-250:]  # 最近 ~250 笔 ≈ 5 分钟
    all_tickers = tickers            # 全量 (最多 500 笔)

    # ── 1. 买卖力道比 ─────────────────────────────────────────────
    buy_vol_short = sum(t["volume"] for t in short_tickers if t["direction"] == "BUY")
    sell_vol_short = sum(t["volume"] for t in short_tickers if t["direction"] == "SELL")
    buy_vol_medium = sum(t["volume"] for t in medium_tickers if t["direction"] == "BUY")
    sell_vol_medium = sum(t["volume"] for t in medium_tickers if t["direction"] == "SELL")

    buy_sell_ratio_short = buy_vol_short / sell_vol_short if sell_vol_short > 0 else 2.0
    buy_sell_ratio_medium = buy_vol_medium / sell_vol_medium if sell_vol_medium > 0 else 2.0

    # ── 2. 成交量突增 (短期 vs 中期) ──────────────────────────────
    avg_vol_short = sum(t["volume"] for t in short_tickers) / len(short_tickers) if short_tickers else 0
    avg_vol_medium = sum(t["volume"] for t in medium_tickers) / len(medium_tickers) if medium_tickers else 0
    volume_surge = avg_vol_short / avg_vol_medium if avg_vol_medium > 0 else 1.0

    # ── 3. 大单占比 ───────────────────────────────────────────────
    large_orders = [t for t in all_tickers if t["volume"] >= large_order_threshold]
    large_order_ratio = len(large_orders) / len(all_tickers) if all_tickers else 0
    large_order_volume = sum(t["volume"] for t in large_orders)
    total_volume = sum(t["volume"] for t in all_tickers)
    large_order_volume_ratio = large_order_volume / total_volume if total_volume > 0 else 0

    # ── 4. VWAP 偏离度 ────────────────────────────────────────────
    total_turnover = sum(t["turnover"] for t in medium_tickers)
    vwap = total_turnover / (sum(t["volume"] for t in medium_tickers) or 1)
    if current_price and vwap > 0:
        vwap_deviation = (current_price - vwap) / vwap
    else:
        vwap_deviation = 0.0

    # ── 5. 逐笔频率 (笔/分钟) ─────────────────────────────────────
    # 用序列号差值 / 时间跨度近似，或直接按数量估算
    ticker_velocity = len(short_tickers)  # 最近 ~1 分钟的笔数

    # ── 6. 综合评分 ───────────────────────────────────────────────
    # 0~100 分，50 为中性
    score = 50.0

    # 买卖力道: ratio > 1.2 加分, < 0.8 减分
    if buy_sell_ratio_short > 1.5:
        score += 15
    elif buy_sell_ratio_short > 1.2:
        score += 8
    elif buy_sell_ratio_short < 0.67:
        score -= 15
    elif buy_sell_ratio_short < 0.83:
        score -= 8

    # 量能突增: surge > 1.5 加分 (放量)
    if volume_surge > 2.0:
        score += 10
    elif volume_surge > 1.5:
        score += 5

    # 大单占比: 大单多 → 机构参与 → 加分
    if large_order_volume_ratio > 0.3:
        score += 10
    elif large_order_volume_ratio > 0.15:
        score += 5

    # VWAP 偏离: 多头偏离加分, 空头偏离减分
    if vwap_deviation > VWAP_DEVIATION_BULL:
        score += 8
    elif vwap_deviation < VWAP_DEVIATION_BEAR:
        score -= 8

    # 频率: 高频交易活跃 → 流动性好
    if ticker_velocity > 30:
        score += 5

    # Clamp to [0, 100]
    score = max(0, min(100, score))

    # ── 7. 信号标签 ───────────────────────────────────────────────
    if score >= 70:
        signal_label = "STRONG_BUY"
    elif score >= 55:
        signal_label = "BUY"
    elif score >= 45:
        signal_label = "NEUTRAL"
    elif score >= 30:
        signal_label = "SELL"
    else:
        signal_label = "STRONG_SELL"

    return {
        "buy_sell_ratio_short": round(buy_sell_ratio_short, 3),
        "buy_sell_ratio_medium": round(buy_sell_ratio_medium, 3),
        "volume_surge": round(volume_surge, 3),
        "large_order_ratio": round(large_order_ratio, 4),
        "large_order_volume_ratio": round(large_order_volume_ratio, 4),
        "vwap": round(vwap, 4),
        "vwap_deviation": round(vwap_deviation, 5),
        "ticker_velocity": ticker_velocity,
        "total_volume": round(total_volume, 2),
        "buy_vol_short": round(buy_vol_short, 2),
        "sell_vol_short": round(sell_vol_short, 2),
        "buy_vol_medium": round(buy_vol_medium, 2),
        "sell_vol_medium": round(sell_vol_medium, 2),
        "score": round(score, 1),
        "signal_label": signal_label,
        "ticker_count": len(all_tickers),
        "timestamp": now,
    }


def get_ticker_factor_for_symbol(
    provider,  # FutuLV3Provider instance
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """
    从 FutuLV3Provider 获取某只股票的量能因子

    Args:
        provider: FutuLV3Provider 实例
        symbol: 股票代码 (US.AAPL 格式)

    Returns:
        因子字典 or None
    """
    try:
        tickers = provider.get_push_tickers(symbol)
        if not tickers:
            return None

        # 获取最新价
        quote = provider.get_push_quote(symbol)
        current_price = quote.get("price") if quote else None

        return analyze_tickers(tickers, current_price=current_price)
    except Exception as e:
        logger.debug(f"[TickerAnalyzer] {symbol} 异常: {e}")
        return None
