"""
OrderBook Analyzer — 买卖盘深度分析因子 (2026-05-20 新增)

从 Futu LV3 Push OrderBook 数据中提取实时深度因子：
  - bid_ask_spread:    买卖价差 (bps)
  - bid_ask_ratio:     买卖盘量比 (买盘总量 / 卖盘总量)
  - depth_imbalance:   深度不平衡度 (-1 到 +1)
  - order_flow_imbalance: 订单流失衡 (基于 BidNum/AskNum)
  - liquidity_score:   流动性评分

设计原则:
  1. 纯函数式，无状态，线程安全
  2. 入参 = OrderBook 快照 (从 PushOrderBookHandler 缓存获取)
  3. 出参 = 因子字典，可直接注入 consensus_engine / signal_pipeline
  4. 毫秒级返回，不阻塞回调线程

Futu OrderBook Push 字段:
  - BidPrice: 买一价
  - BidVol: 买一量
  - AskPrice: 卖一价
  - AskVol: 卖一量
  - BidNum: 买一档委托笔数
  - AskNum: 卖一档委托笔数
  - BidOrderNum: 买一档订单数
  - AskOrderNum: 卖一档订单数
"""
import logging
import time
from typing import Dict, Optional, Any

logger = logging.getLogger("ftseek.us")


def analyze_orderbook(
    orderbook: Dict[str, Any],
    current_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    从买卖盘快照中提取深度因子

    Args:
        orderbook: OrderBook 快照字典:
            {
                "BidPrice": float,  # 买一价
                "BidVol": float,    # 买一量
                "AskPrice": float,  # 卖一价
                "AskVol": float,    # 卖一量
                "BidNum": float,    # 买一档委托笔数
                "AskNum": float,    # 卖一档委托笔数
                "BidOrderNum": float,
                "AskOrderNum": float,
                "timestamp": float,
            }
        current_price: 当前最新价 (用于相对价差计算)

    Returns:
        因子字典 or None (数据无效)
    """
    if not orderbook:
        return None

    bid_price = orderbook.get("BidPrice", 0)
    bid_vol = orderbook.get("BidVol", 0)
    ask_price = orderbook.get("AskPrice", 0)
    ask_vol = orderbook.get("AskVol", 0)
    bid_num = orderbook.get("BidNum", 0)
    ask_num = orderbook.get("AskNum", 0)
    bid_order_num = orderbook.get("BidOrderNum", 0)
    ask_order_num = orderbook.get("AskOrderNum", 0)

    # 数据有效性检查
    if bid_price <= 0 or ask_price <= 0:
        return None
    if ask_price < bid_price:
        # 异常情况: 卖价 < 买价 (数据错误)
        return None

    # ── 1. 买卖价差 (bps) ──────────────────────────────────────────
    mid_price = (bid_price + ask_price) / 2
    spread = ask_price - bid_price
    spread_bps = (spread / mid_price) * 10000  # 基点

    # ── 2. 买卖盘量比 ──────────────────────────────────────────────
    bid_ask_ratio = bid_vol / ask_vol if ask_vol > 0 else 5.0  # 无卖盘时默认 5.0

    # ── 3. 深度不平衡度 (-1 到 +1) ─────────────────────────────────
    # +1 = 买盘远强于卖盘, -1 = 卖盘远强于买盘
    total_vol = bid_vol + ask_vol
    depth_imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0

    # ── 4. 订单流失衡 ─────────────────────────────────────────────
    # 基于委托笔数 (BidNum/AskNum) — 反映市场参与者数量
    total_num = bid_num + ask_num
    order_flow_imbalance = (bid_num - ask_num) / total_num if total_num > 0 else 0

    # ── 5. 平均订单大小 ───────────────────────────────────────────
    avg_bid_size = bid_vol / bid_num if bid_num > 0 else bid_vol
    avg_ask_size = ask_vol / ask_num if ask_num > 0 else ask_vol
    avg_order_size_ratio = avg_bid_size / avg_ask_size if avg_ask_size > 0 else 2.0

    # ── 6. 流动性评分 (0~100) ──────────────────────────────────────
    # 价差越小 + 挂单量越大 + 订单数越多 = 流动性越好
    liquidity_score = 50.0

    # 价差: < 5bps 优秀, > 20bps 差
    if spread_bps < 3:
        liquidity_score += 20
    elif spread_bps < 5:
        liquidity_score += 10
    elif spread_bps < 10:
        liquidity_score += 5
    elif spread_bps > 30:
        liquidity_score -= 15
    elif spread_bps > 20:
        liquidity_score -= 10

    # 挂单量: 量大 = 流动性好
    if total_vol > 10000:
        liquidity_score += 15
    elif total_vol > 5000:
        liquidity_score += 8
    elif total_vol < 500:
        liquidity_score -= 15

    # 订单数: 多 = 参与度高
    if total_num > 50:
        liquidity_score += 10
    elif total_num > 20:
        liquidity_score += 5
    elif total_num < 5:
        liquidity_score -= 10

    liquidity_score = max(0, min(100, liquidity_score))

    # ── 7. 综合方向评分 (0~100, 50=中性) ──────────────────────────
    direction_score = 50.0

    # 深度不平衡: 买盘强 → 加分
    if depth_imbalance > 0.5:
        direction_score += 15
    elif depth_imbalance > 0.2:
        direction_score += 8
    elif depth_imbalance < -0.5:
        direction_score -= 15
    elif depth_imbalance < -0.2:
        direction_score -= 8

    # 订单流失衡: 买方订单多 → 加分
    if order_flow_imbalance > 0.3:
        direction_score += 10
    elif order_flow_imbalance > 0.1:
        direction_score += 5
    elif order_flow_imbalance < -0.3:
        direction_score -= 10
    elif order_flow_imbalance < -0.1:
        direction_score -= 5

    # 平均订单大小: 买方大单 → 加分
    if avg_order_size_ratio > 2.0:
        direction_score += 8
    elif avg_order_size_ratio > 1.5:
        direction_score += 4
    elif avg_order_size_ratio < 0.5:
        direction_score -= 8

    direction_score = max(0, min(100, direction_score))

    # ── 8. 信号标签 ───────────────────────────────────────────────
    if direction_score >= 70:
        signal_label = "STRONG_BUY"
    elif direction_score >= 55:
        signal_label = "BUY"
    elif direction_score >= 45:
        signal_label = "NEUTRAL"
    elif direction_score >= 30:
        signal_label = "SELL"
    else:
        signal_label = "STRONG_SELL"

    return {
        "bid_price": bid_price,
        "ask_price": ask_price,
        "mid_price": round(mid_price, 4),
        "spread": round(spread, 4),
        "spread_bps": round(spread_bps, 2),
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "bid_ask_ratio": round(bid_ask_ratio, 3),
        "depth_imbalance": round(depth_imbalance, 4),
        "bid_num": bid_num,
        "ask_num": ask_num,
        "order_flow_imbalance": round(order_flow_imbalance, 4),
        "avg_bid_size": round(avg_bid_size, 2),
        "avg_ask_size": round(avg_ask_size, 2),
        "avg_order_size_ratio": round(avg_order_size_ratio, 3),
        "liquidity_score": round(liquidity_score, 1),
        "direction_score": round(direction_score, 1),
        "signal_label": signal_label,
        "timestamp": orderbook.get("timestamp", time.time()),
    }


def get_orderbook_factor_for_symbol(
    provider,  # FutuLV3Provider instance
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """
    从 FutuLV3Provider 获取某只股票的深度因子

    Args:
        provider: FutuLV3Provider 实例
        symbol: 股票代码 (US.AAPL 格式)

    Returns:
        因子字典 or None
    """
    try:
        orderbook = provider.get_push_orderbook(symbol)
        if not orderbook:
            return None

        # 获取最新价
        quote = provider.get_push_quote(symbol)
        current_price = quote.get("price") if quote else None

        return analyze_orderbook(orderbook, current_price=current_price)
    except Exception as e:
        logger.debug(f"[OrderBookAnalyzer] {symbol} 异常: {e}")
        return None
