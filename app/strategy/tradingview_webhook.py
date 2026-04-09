"""
TradingView Webhook 接收器
接收 TradingView Alert Webhook 推送的交易信号，转化为 FTSeek 内部 Signal
支持自定义 JSON payload 格式
"""
import logging
import time
from typing import Optional
from .base import Signal, SignalAction

logger = logging.getLogger("ftseek.strategy.tradingview")
logger.propagate = False  # 防止日志重复

# TradingView ticker -> FTSeek code 映射
TICKER_MAP = {
    # 美股
    "AAPL": "US.AAPL", "TSLA": "US.TSLA", "NVDA": "US.NVDA",
    "MSFT": "US.MSFT", "AMZN": "US.AMZN", "GOOG": "US.GOOG",
    "META": "US.META", "AMD": "US.AMD", "NFLX": "US.NFLX",
    # 港股 (TradingView 用 HKEX: 前缀)
    "0700": "HK.00700", "9988": "HK.09988", "3690": "HK.03690",
    "1810": "HK.01810", "9618": "HK.09618",
}


def normalize_ticker(ticker: str) -> str:
    """将 TradingView ticker 标准化为 FTSeek code"""
    # 直接匹配
    if ticker in TICKER_MAP:
        return TICKER_MAP[ticker]
    # 尝试去掉交易所前缀: "NASDAQ:AAPL" -> "AAPL"
    if ":" in ticker:
        _, symbol = ticker.rsplit(":", 1)
        if symbol in TICKER_MAP:
            return TICKER_MAP[symbol]
    # 尝试 HKEX 格式
    if ticker.startswith("HKEX:"):
        num = ticker.replace("HKEX:", "").lstrip("0")
        for k, v in TICKER_MAP.items():
            if k.lstrip("0") == num:
                return v
    # 美股默认加 US. 前缀
    if ticker.isalpha() and ticker.isupper():
        return f"US.{ticker}"
    return ticker


def parse_tradingview_webhook(payload: dict) -> Optional[Signal]:
    """
    解析 TradingView Webhook payload 为 FTSeek Signal

    支持的 payload 格式:

    格式1 (推荐): 标准 JSON
    {
        "ticker": "AAPL",
        "action": "buy",           // buy | sell | alert
        "price": 245.50,
        "strategy": "SuperTrend",
        "message": "SuperTrend 翻多",
        "qty": 1
    }

    格式2: TradingView Pine Script alert
    {
        "strategy": "{{strategy.order.action}}",
        "ticker": "{{ticker}}",
        "price": {{close}},
        "time": "{{time}}"
    }

    格式3: 简单文本 (自动解析)
    {
        "text": "BUY AAPL @ 245.50"
    }
    """
    if not payload:
        return None

    try:
        # 格式1/2: 标准字段
        ticker = payload.get("ticker") or payload.get("symbol") or payload.get("stock", "")
        action_str = (payload.get("action") or payload.get("side")
                      or payload.get("strategy", "")).lower().strip()
        price = float(payload.get("price") or payload.get("close") or 0)
        message = payload.get("message") or payload.get("reason") or payload.get("text", "")
        strategy_name = payload.get("strategy_name") or payload.get("strategy", "tradingview")
        qty = int(payload.get("qty") or payload.get("quantity") or 0)

        # 格式3: 纯文本解析
        if not ticker and "text" in payload:
            text = payload["text"].strip().upper()
            parts = text.split()
            if len(parts) >= 2:
                if parts[0] in ("BUY", "SELL", "ALERT"):
                    action_str = parts[0].lower()
                    ticker = parts[1]
                    if "@" in text:
                        try:
                            price = float(text.split("@")[1].strip())
                        except (ValueError, IndexError):
                            pass

        if not ticker:
            logger.warning(f"TradingView webhook 缺少 ticker: {payload}")
            return None

        code = normalize_ticker(ticker)

        # 解析 action
        if action_str in ("buy", "long", "entry_long", "strategy.long"):
            action = SignalAction.BUY
        elif action_str in ("sell", "short", "exit_long", "close", "strategy.short", "strategy.close"):
            action = SignalAction.SELL
        else:
            action = SignalAction.ALERT

        # 自动设置数量
        if qty <= 0:
            from app import config
            qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        signal = Signal(
            code=code,
            name=ticker,
            action=action,
            price=price,
            quantity=qty,
            reason=message or f"TradingView: {action_str} {ticker} @ {price}",
            strategy=f"tv:{strategy_name}",
            extra={"source": "tradingview", "raw_payload": payload},
        )

        logger.info(f"TradingView 信号解析: {code} {action.value} @ {price} ({strategy_name})")
        return signal

    except Exception as e:
        logger.error(f"TradingView webhook 解析失败: {e}, payload={payload}")
        return None
