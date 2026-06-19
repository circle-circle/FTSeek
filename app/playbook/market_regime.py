"""
Market Regime Gate - 市场环境门禁

基于 SPY 趋势 + VIX 波动率判断市场环境，决定今天的操作基调。

Regime 判定：
- RISK_ON: 市场趋势向上，VIX 低，可以正常操作
- CAUTIOUS: 市场不确定，需要缩小仓位
- RISK_OFF: 市场风险偏高，防守为主
- NO_TRADE: 极端环境，全部 SKIP

设计原则：
- 简单的规则判断，不是 LLM 预测
- 使用现有 MarketDataRouter 和 AlphaVantageProvider
- 每个 regime 影响后续 Screener 的 top_n 和仓位倍率
"""
import logging
from typing import Dict, Any, Optional
from enum import Enum
from dataclasses import dataclass

from app.data.market_data_router import MarketDataRouter
from app.data.alphavantage_provider import AlphaVantageProvider

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """市场环境枚举"""
    RISK_ON = "risk_on"
    CAUTIOUS = "cautious"
    RISK_OFF = "risk_off"
    NO_TRADE = "no_trade"


@dataclass
class RegimeContext:
    """市场环境上下文"""
    regime: MarketRegime
    vix: float
    spy_price: float
    spy_ma20: float
    spy_ma50: float
    spy_ma200: float
    spy_change_pct: float
    score: int  # -10 到 +10 的评分
    
    # 影响后续流程的参数
    top_n: int  # Screener 筛选数量
    position_multiplier: float  # 仓位倍率
    min_strength: int  # 最低信号强度要求
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime.value,
            "vix": self.vix,
            "spy_price": self.spy_price,
            "spy_ma20": self.spy_ma20,
            "spy_ma50": self.spy_ma50,
            "spy_ma200": self.spy_ma200,
            "spy_change_pct": self.spy_change_pct,
            "score": self.score,
            "top_n": self.top_n,
            "position_multiplier": self.position_multiplier,
            "min_strength": self.min_strength,
        }


class MarketRegimeGate:
    """市场环境门禁"""
    
    def __init__(
        self,
        market_data_router: Optional[MarketDataRouter] = None,
        alphavantage_provider: Optional[AlphaVantageProvider] = None,
    ):
        self.router = market_data_router
        self.av = alphavantage_provider
        
        # 如果没有传入，尝试创建默认实例
        if not self.router:
            try:
                self.router = MarketDataRouter()
            except Exception as e:
                logger.warning(f"Failed to create MarketDataRouter: {e}")
        
        if not self.av:
            try:
                self.av = AlphaVantageProvider()
            except Exception as e:
                logger.warning(f"Failed to create AlphaVantageProvider: {e}")
    
    def assess(self) -> RegimeContext:
        """
        评估当前市场环境
        
        评分逻辑：
        - SPY > MA20: +2 (短期趋势向上)
        - SPY < MA20: -2 (短期趋势向下)
        - MA20 > MA50: +2 (中期趋势向上)
        - MA20 < MA50: -2 (中期趋势向下)
        - SPY > MA200: +1 (长期趋势向上)
        - SPY < MA200: -1 (长期趋势向下)
        - VIX < 15: +3 (低波动，市场平静)
        - VIX 15-20: +1 (正常波动)
        - VIX 20-30: -2 (高波动，市场紧张)
        - VIX > 30: -5 (极高波动，市场恐慌)
        
        Regime 映射：
        - score >= 5: RISK_ON (top_n=10, multiplier=1.0)
        - score 0-4: CAUTIOUS (top_n=5, multiplier=0.5)
        - score -5 to -1: RISK_OFF (top_n=3, multiplier=0.3)
        - score < -5: NO_TRADE (top_n=0, multiplier=0.0)
        """
        score = 0
        
        # 获取 SPY 数据
        spy_price = 0.0
        spy_ma20 = 0.0
        spy_ma50 = 0.0
        spy_ma200 = 0.0
        spy_change_pct = 0.0
        
        # 优先使用 MarketDataRouter
        if self.router:
            try:
                spy_ctx = self.router.get_spy_context()
                if spy_ctx:
                    spy_price = spy_ctx.get("spy_price", 0.0)
                    spy_ma200 = spy_ctx.get("spy_ma200", 0.0)
                    spy_change_pct = spy_ctx.get("spx_change_percent", 0.0)
                    logger.info(f"[Regime] SPY from Router: price={spy_price:.2f}, MA200={spy_ma200:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get SPY from Router: {e}")
        
        # 补充 MA20/MA50 数据 (Router 可能没有)
        if self.av and (spy_ma20 == 0 or spy_ma50 == 0):
            try:
                av_spy_ctx = self.av.get_spy_context()
                if av_spy_ctx:
                    if spy_price == 0:
                        spy_price = av_spy_ctx.get("spy_price", 0.0)
                    spy_ma20 = av_spy_ctx.get("spy_ma20", 0.0)
                    spy_ma50 = av_spy_ctx.get("spy_ma50", 0.0)
                    if spy_change_pct == 0:
                        spy_change_pct = av_spy_ctx.get("spx_change_percent", 0.0)
                    logger.info(f"[Regime] SPY from AV: MA20={spy_ma20:.2f}, MA50={spy_ma50:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get SPY from AlphaVantage: {e}")
        
        # 获取 VIX
        vix = 20.0  # 默认值
        vix_from_router = False
        if self.router:
            try:
                vix = self.router.get_vix()
                vix_from_router = True
                logger.info(f"[Regime] VIX from Router: {vix:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get VIX from Router: {e}")
        if not vix_from_router and self.av:
            try:
                vix = self.av.get_vix()
                logger.info(f"[Regime] VIX from AlphaVantage: {vix:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get VIX from AlphaVantage: {e}")
        
        # 评分计算
        # 1. 短期趋势 (SPY vs MA20)
        if spy_price > 0 and spy_ma20 > 0:
            if spy_price > spy_ma20:
                score += 2
                logger.debug(f"[Regime] SPY > MA20: +2")
            else:
                score -= 2
                logger.debug(f"[Regime] SPY < MA20: -2")
        
        # 2. 中期趋势 (MA20 vs MA50)
        if spy_ma20 > 0 and spy_ma50 > 0:
            if spy_ma20 > spy_ma50:
                score += 2
                logger.debug(f"[Regime] MA20 > MA50: +2")
            else:
                score -= 2
                logger.debug(f"[Regime] MA20 < MA50: -2")
        
        # 3. 长期趋势 (SPY vs MA200)
        if spy_price > 0 and spy_ma200 > 0:
            if spy_price > spy_ma200:
                score += 1
                logger.debug(f"[Regime] SPY > MA200: +1")
            else:
                score -= 1
                logger.debug(f"[Regime] SPY < MA200: -1")
        
        # 4. 波动率 (VIX)
        if vix < 15:
            score += 3
            logger.debug(f"[Regime] VIX < 15: +3")
        elif vix <= 20:
            score += 1
            logger.debug(f"[Regime] VIX 15-20: +1")
        elif vix < 30:
            score -= 2
            logger.debug(f"[Regime] VIX 20-30: -2")
        else:
            score -= 5
            logger.debug(f"[Regime] VIX > 30: -5")
        
        # Regime 映射
        if score >= 5:
            regime = MarketRegime.RISK_ON
            top_n = 10
            position_multiplier = 1.0
            min_strength = 50
        elif score >= 0:
            regime = MarketRegime.CAUTIOUS
            top_n = 5
            position_multiplier = 0.5
            min_strength = 60
        elif score >= -5:
            regime = MarketRegime.RISK_OFF
            top_n = 3
            position_multiplier = 0.3
            min_strength = 70
        else:
            regime = MarketRegime.NO_TRADE
            top_n = 0
            position_multiplier = 0.0
            min_strength = 999
        
        logger.info(
            f"[Regime] Assessment: {regime.value} (score={score}) | "
            f"SPY={spy_price:.2f} MA20={spy_ma20:.2f} MA50={spy_ma50:.2f} MA200={spy_ma200:.2f} | "
            f"VIX={vix:.2f} | top_n={top_n} multiplier={position_multiplier}"
        )
        
        return RegimeContext(
            regime=regime,
            vix=vix,
            spy_price=spy_price,
            spy_ma20=spy_ma20,
            spy_ma50=spy_ma50,
            spy_ma200=spy_ma200,
            spy_change_pct=spy_change_pct,
            score=score,
            top_n=top_n,
            position_multiplier=position_multiplier,
            min_strength=min_strength,
        )
