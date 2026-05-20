"""
Yahoo Finance 数据提供商

免费美股行情数据源 (15 分钟延迟)
适用于中长线波段交易策略
"""
import yfinance as yf
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class YahooFinanceProvider:
    """Yahoo Finance 数据提供商"""
    
    def __init__(self, cache_ttl_seconds: int = 900):  # 15 分钟缓存
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_time: Dict[str, datetime] = {}
    
    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时报价
        
        Args:
            symbol: 股票代码 (US.AAPL 格式)
        
        Returns:
            报价字典或 None
        """
        try:
            # 检查缓存
            if self._is_cache_valid(symbol):
                return self.cache[symbol]
            
            # 转换代码格式 (US.AAPL → AAPL)
            ticker_symbol = symbol.replace("US.", "")
            ticker = yf.Ticker(ticker_symbol)
            
            # 获取实时数据
            data = ticker.fast_info
            
            quote = {
                "symbol": symbol,
                "price": float(data.last_price),
                "volume": int(data.last_volume) if data.last_volume else 0,
                "bid": float(data.bid) if data.bid else 0,
                "ask": float(data.ask) if data.ask else 0,
                "open": float(data.day_open) if data.day_open else 0,
                "high": float(data.day_high) if data.day_high else 0,
                "low": float(data.day_low) if data.day_low else 0,
                "previous_close": float(data.previous_close) if data.previous_close else 0,
                "timestamp": datetime.now().isoformat(),
            }
            
            # 更新缓存
            self.cache[symbol] = quote
            self.cache_time[symbol] = datetime.now()
            
            logger.info(f"获取报价：{symbol} ${quote['price']}")
            return quote
            
        except Exception as e:
            logger.error(f"获取报价失败 {symbol}: {e}")
            return None
    
    def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取完整市场数据 (用于信号评分)
        
        Args:
            symbol: 股票代码
        
        Returns:
            市场数据字典
        """
        try:
            # 检查缓存
            if self._is_cache_valid(symbol, min_age_seconds=300):  # 5 分钟
                return self.cache.get(f"{symbol}_market")
            
            ticker_symbol = symbol.replace("US.", "")
            ticker = yf.Ticker(ticker_symbol)
            
            # 获取历史数据计算指标
            hist = ticker.history(period="60d")
            
            if len(hist) < 50:
                logger.warning(f"历史数据不足 {symbol}: {len(hist)} 天")
                return None
            
            # 计算技术指标
            current_price = float(hist["Close"].iloc[-1])
            volume = int(hist["Volume"].iloc[-1])
            
            # 移动平均
            ma20 = float(hist["Close"].rolling(window=20).mean().iloc[-1])
            ma50 = float(hist["Close"].rolling(window=50).mean().iloc[-1])
            
            # 成交量均值
            avg_volume = int(hist["Volume"].mean())
            
            # 计算指标
            volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
            momentum = (current_price - ma20) / ma20 if ma20 > 0 else 0
            
            market_data = {
                "symbol": symbol,
                "price": current_price,
                "volume": volume,
                "volume_ratio": volume_ratio,
                "momentum": momentum,
                "trend": "UP" if current_price > ma50 else "DOWN",
                "ma20": ma20,
                "ma50": ma50,
                "price_vs_ma20": (current_price - ma20) / ma20,
                "price_vs_ma50": (current_price - ma50) / ma50,
                "timestamp": datetime.now().isoformat(),
            }
            
            # 更新缓存
            self.cache[f"{symbol}_market"] = market_data
            self.cache_time[f"{symbol}_market"] = datetime.now()
            
            logger.info(f"获取市场数据：{symbol} 趋势={market_data['trend']} 动量={momentum:.2%}")
            return market_data
            
        except Exception as e:
            logger.error(f"获取市场数据失败 {symbol}: {e}")
            return None
    
    def get_batch_quotes(self, symbols: list) -> Dict[str, Dict[str, Any]]:
        """批量获取报价"""
        results = {}
        
        for symbol in symbols:
            quote = self.get_quote(symbol)
            if quote:
                results[symbol] = quote
        
        return results
    
    def _is_cache_valid(self, key: str, min_age_seconds: int = 0) -> bool:
        """检查缓存是否有效"""
        if key not in self.cache:
            return False
        
        cache_time = self.cache_time.get(key)
        if not cache_time:
            return False
        
        age = datetime.now() - cache_time
        
        # 最小年龄检查 (避免频繁刷新)
        if age.total_seconds() < min_age_seconds:
            return True
        
        # 最大年龄检查 (超时失效)
        return age.total_seconds() < self.cache_ttl.total_seconds()
    
    def get_vix(self) -> float:
        """
        获取 VIX 恐慌指数
        
        注意：当前网络环境下 yfinance 无法访问 ^VIX，直接返回默认值。
        Returns:
            20.0（市场历史均值）
        """
        return 20.0

    def get_spy_context(self) -> Dict[str, Any]:
        """
        获取大盘方向上下文（SPY + QQQ）
        
        Returns:
            spx_change_percent / qqq_change_percent / spy_ma20 / spy_ma50 / spy_price
        """
        try:
            cache_key = "__spy_context__"
            if self._is_cache_valid(cache_key, min_age_seconds=900):
                return self.cache.get(cache_key, {})

            spy = yf.Ticker("SPY")
            qqq = yf.Ticker("QQQ")

            spy_hist = spy.history(period="60d")
            qqq_hist = qqq.history(period="5d")

            if len(spy_hist) < 20:
                return {}

            spy_price = float(spy_hist["Close"].iloc[-1])
            spy_prev = float(spy_hist["Close"].iloc[-2])
            spy_ma20 = float(spy_hist["Close"].rolling(20).mean().iloc[-1])
            spy_ma50 = float(spy_hist["Close"].rolling(min(50, len(spy_hist))).mean().iloc[-1])

            qqq_price = float(qqq_hist["Close"].iloc[-1]) if len(qqq_hist) > 1 else 0
            qqq_prev = float(qqq_hist["Close"].iloc[-2]) if len(qqq_hist) > 1 else qqq_price

            context = {
                "spy_price": spy_price,
                "spy_ma20": spy_ma20,
                "spy_ma50": spy_ma50,
                "spx_change_percent": (spy_price - spy_prev) / spy_prev if spy_prev > 0 else 0,
                "qqq_change_percent": (qqq_price - qqq_prev) / qqq_prev if qqq_prev > 0 else 0,
                "price_change_5d": (spy_price - float(spy_hist["Close"].iloc[-5])) / float(spy_hist["Close"].iloc[-5]) if len(spy_hist) >= 5 else 0,
            }
            self.cache[cache_key] = context
            self.cache_time[cache_key] = datetime.now()
            logger.info(f"大盘方向 SPY={spy_price:.2f} MA20={spy_ma20:.2f} 涨跌={context['spx_change_percent']:.2%}")
            return context
        except Exception as e:
            logger.warning(f"获取大盘上下文失败: {e}")
            return {}

    def compute_atr(self, symbol: str, period: int = 14) -> Dict[str, float]:
        """
        计算 ATR（平均真实波幅）
        
        Returns:
            {"atr": float, "avg_atr": float, "volatility": float, "avg_volatility": float}
        """
        try:
            cache_key = f"{symbol}_atr"
            if self._is_cache_valid(cache_key, min_age_seconds=1800):
                return self.cache.get(cache_key, {})

            ticker_symbol = symbol.replace("US.", "")
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period="60d")

            if len(hist) < period + 5:
                return {"atr": 0, "avg_atr": 0, "volatility": 0, "avg_volatility": 0}

            high = hist["High"]
            low = hist["Low"]
            close = hist["Close"]
            prev_close = close.shift(1)

            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)

            atr_series = tr.rolling(window=period).mean()
            current_atr = float(atr_series.iloc[-1])
            avg_atr = float(atr_series.mean())

            # 波动率 = ATR / 收盘价
            current_price = float(close.iloc[-1])
            volatility = current_atr / current_price if current_price > 0 else 0
            avg_price = float(close.mean())
            avg_volatility = avg_atr / avg_price if avg_price > 0 else 0

            result = {
                "atr": current_atr,
                "avg_atr": avg_atr,
                "volatility": volatility,
                "avg_volatility": avg_volatility,
            }
            self.cache[cache_key] = result
            self.cache_time[cache_key] = datetime.now()
            return result
        except Exception as e:
            logger.warning(f"计算 ATR 失败 {symbol}: {e}")
            return {"atr": 0, "avg_atr": 0, "volatility": 0, "avg_volatility": 0}

    def get_full_market_context(self, symbol: str) -> Dict[str, Any]:
        """
        获取完整市场上下文（用于 SignalScorer）
        
        整合：技术指标 + ATR + VIX + 大盘方向
        
        Returns:
            满足 SignalScorer 所有因子需要的上下文字典
        """
        # 基础行情数据
        market_data = self.get_market_data(symbol) or {}

        # ATR / 波动率
        atr_data = self.compute_atr(symbol)

        # VIX
        vix = self.get_vix()

        # 大盘方向
        spy_ctx = self.get_spy_context()

        # 判断趋势方向
        trend_direction = market_data.get("trend", "NEUTRAL")
        if trend_direction == "UP":
            trend_direction = "UP"
        elif trend_direction == "DOWN":
            trend_direction = "DOWN"
        else:
            trend_direction = "NEUTRAL"

        # 市场状态（简单判断）
        from app.core.models import MarketRegime
        if vix > 25 or atr_data.get("volatility", 0) > atr_data.get("avg_volatility", 0) * 1.5:
            regime = MarketRegime.HIGH_VOL.value
        elif trend_direction == "UP":
            regime = MarketRegime.TREND_UP.value
        elif trend_direction == "DOWN":
            regime = MarketRegime.TREND_DOWN.value
        elif vix < 15:
            regime = MarketRegime.LOW_VOL.value
        else:
            regime = MarketRegime.RANGE.value

        context = {
            # 基础数据
            "symbol": symbol,
            "price": market_data.get("price", 0),
            "volume": market_data.get("volume", 0),
            "volume_ratio": market_data.get("volume_ratio", 1.0),
            "momentum": market_data.get("momentum", 0),
            "ma20": market_data.get("ma20", 0),
            "ma50": market_data.get("ma50", 0),
            # 趋势
            "trend_direction": trend_direction,
            # 波动率
            "volatility": atr_data.get("volatility", 0),
            "avg_volatility": atr_data.get("avg_volatility", 0),
            "atr": atr_data.get("atr", 0),
            "avg_atr": atr_data.get("avg_atr", 0),
            # VIX + 大盘
            "vix": vix,
            "spy_price": spy_ctx.get("spy_price", 0),
            "spy_ma20": spy_ctx.get("spy_ma20", 0),
            "spy_ma50": spy_ctx.get("spy_ma50", 0),
            "spx_change_percent": spy_ctx.get("spx_change_percent", 0),
            "qqq_change_percent": spy_ctx.get("qqq_change_percent", 0),
            "price_change_5d": spy_ctx.get("price_change_5d", 0),
            # 市场状态
            "market_regime": regime,
            # 占位（由 news_provider 注入）
            "news_sentiment": 0.0,
            # 无重大事件默认值
            "major_event_hours": 999,
            "liquidity_ratio": 1.0,
            "spread_ratio": 0.001,
        }
        return context

    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()
        self.cache_time.clear()
        logger.info("缓存已清空")


# 测试脚本
if __name__ == "__main__":
    import json
    
    provider = YahooFinanceProvider()
    
    # 测试单只股票
    print("测试单只股票:")
    data = provider.get_market_data("US.AAPL")
    if data:
        print(json.dumps(data, indent=2))
    
    # 测试批量获取
    print("\n测试批量获取:")
    symbols = ["US.AAPL", "US.MSFT", "US.NVDA"]
    quotes = provider.get_batch_quotes(symbols)
    for symbol, quote in quotes.items():
        print(f"{symbol}: ${quote['price']}")
