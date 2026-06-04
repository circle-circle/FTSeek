"""
Livermore 大盘联动过滤器 (Market Regime Signal Filter)

核心理念: "不要和市场争辩。如果大盘不对，个股再好的逻辑也不做。"

规则:
1. SPY < 50日均线 → 所有 LONG 信号强度 -30%
2. VIX > 30 → 停止所有新仓 (熔断级保护)
3. VIX < 12 → 市场过热预警, 不追高
4. SPY 连续 3 日下跌 → 防御模式, 只减不加
5. SPY > 50日均线 + 上涨趋势 → 信号强度 +10% (顺势而为)

作者: FTSeek Team
日期: 2026-04-16
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from enum import Enum

import pytz

logger = logging.getLogger("ftseek.market_regime_filter")

ET = pytz.timezone("America/New_York")


class RegimeMode(Enum):
    """大盘运行模式"""
    BULL = "bull"                 # 牛市: SPY>MA50 + 低 VIX
    BEAR = "bear"                 # 熊市: SPY<MA50 + 高 VIX
    CORRECTION = "correction"     # 回调: SPY 连跌3日
    OVERHEATED = "overheated"     # 过热: VIX<12 极端贪婪
    DEFENSIVE = "defensive"       # 防御: 高 VIX 或趋势不明
    NEUTRAL = "neutral"           # 中性: 正常震荡


class MarketRegimeFilter:
    """
    Livermore 大盘联动过滤器
    
    在信号进入执行层之前, 根据大盘状态动态调整信号强度或拦截。
    """
    
    # 配置参数
    VIX_FEAR_THRESHOLD = 30.0       # 恐惧阈值: 超过此值停止开新仓
    VIX_GREED_THRESHOLD = 12.0      # 贪婪阈值: 低于此值警告过热
    SPY_MA50_WEAKEN_PCT = 0.30      # SPY < MA50 时 LONG 信号削弱比例
    SPY_MA50_STRENGTHEN_PCT = 0.10  # SPY > MA50 + 上涨时信号增强比例
    CONSECUTIVE_DOWN_DAYS = 3       # 连续下跌天数触发防御模式
    
    def __init__(self):
        self._spy_history: list = []  # 记录最近 N 天 SPY 收盘价
        self._last_regime: Optional[RegimeMode] = None
        self._prev_vix: Optional[float] = None  # VIX 跳变检测
        logger.info(f"[大盘过滤] 初始化 | VIX恐惧={self.VIX_FEAR_THRESHOLD} | VIX贪婪={self.VIX_GREED_THRESHOLD}")
    
    def evaluate(self, spy_context: Dict[str, Any], vix: float, 
                 signal_direction: str, signal_strength: int) -> Tuple[int, str]:
        """
        评估并调整信号强度
        
        Args:
            spy_context: SPY 大盘上下文 (price, ma20, ma50, spx_change_percent, price_change_5d)
            vix: 当前 VIX 值
            signal_direction: 信号方向 (LONG/SHORT)
            signal_strength: 原始信号强度 (0-100)
        
        Returns:
            (调整后的信号强度, 原因说明)
        """
        regime = self._determine_regime(spy_context, vix)
        self._last_regime = regime
        
        # ── 规则 1: VIX > 30 → 熔断级保护 ──
        if vix > self.VIX_FEAR_THRESHOLD:
            logger.warning(f"[大盘过滤] VIX={vix:.1f} > {self.VIX_FEAR_THRESHOLD} → 熔断模式, 拒绝新仓")
            return 0, f"VIX恐惧 ({vix:.1f}) → 熔断"

        # ── 规则 1.5: VIX 跳变检测 (>20% 或 <20→>28 regime break) ──
        vix_jump_reason = self._detect_vix_jump(vix)
        if vix_jump_reason:
            adjusted = int(signal_strength * 0.70)
            logger.warning(f"[大盘过滤] {vix_jump_reason} → 信号强度削弱30% ({signal_strength}→{adjusted})")
            return adjusted, vix_jump_reason
        
        # ── 规则 2: VIX < 12 → 过热预警 ──
        if vix < self.VIX_GREED_THRESHOLD and signal_direction == "LONG":
            logger.warning(f"[大盘过滤] VIX={vix:.1f} < {self.VIX_GREED_THRESHOLD} → 市场过热, 削弱追高信号")
            adjusted = int(signal_strength * 0.6)  # 追高信号削弱 40%
            return adjusted, f"VIX过热 ({vix:.1f}) → 追高削弱40%"
        
        # ── 规则 3: SPY < 50日均线 → LONG 信号削弱 ──
        spy_price = spy_context.get("spy_price", 0)
        spy_ma50 = spy_context.get("spy_ma50", 0)
        
        if spy_price > 0 and spy_ma50 > 0 and spy_price < spy_ma50:
            if signal_direction == "LONG":
                weaken_amount = int(signal_strength * self.SPY_MA50_WEAKEN_PCT)
                adjusted = max(0, signal_strength - weaken_amount)
                logger.info(f"[大盘过滤] SPY ${spy_price:.0f} < MA50 ${spy_ma50:.0f} → LONG 信号削弱 {weaken_amount} ({signal_strength}→{adjusted})")
                return adjusted, f"SPY<MA50 → 削弱{int(self.SPY_MA50_WEAKEN_PCT*100)}%"
        
        # ── 规则 4: SPY 连续 3 日下跌 → 防御模式 ──
        consecutive_down = self._check_consecutive_down(spy_context)
        if consecutive_down >= self.CONSECUTIVE_DOWN_DAYS and signal_direction == "LONG":
            adjusted = int(signal_strength * 0.5)  # 削弱 50%
            logger.warning(f"[大盘过滤] SPY 连续 {consecutive_down} 日下跌 → 防御模式, LONG 信号削弱50%")
            return adjusted, f"SPY连跌{consecutive_down}日 → 防御削弱50%"
        
        # ── 规则 5: SPY > MA50 + 上涨趋势 → 顺势加强 ──
        if (spy_price > 0 and spy_ma50 > 0 and spy_price > spy_ma50 and 
            spy_context.get("spx_change_percent", 0) > 0.005 and
            signal_direction == "LONG"):
            strengthen_amount = int(signal_strength * self.SPY_MA50_STRENGTHEN_PCT)
            adjusted = min(100, signal_strength + strengthen_amount)
            logger.info(f"[大盘过滤] SPY ${spy_price:.0f} > MA50 ${spy_ma50:.0f} + 上涨 → LONG 信号加强 {strengthen_amount} ({signal_strength}→{adjusted})")
            return adjusted, f"顺势加强+{int(self.SPY_MA50_STRENGTHEN_PCT*100)}%"
        
        # 无调整
        return signal_strength, regime.value
    
    def _determine_regime(self, spy_context: Dict, vix: float) -> RegimeMode:
        """判断当前大盘运行模式"""
        spy_price = spy_context.get("spy_price", 0)
        spy_ma50 = spy_context.get("spy_ma50", 0)
        
        if vix > self.VIX_FEAR_THRESHOLD:
            return RegimeMode.DEFENSIVE
        if vix < self.VIX_GREED_THRESHOLD:
            return RegimeMode.OVERHEATED
        if spy_price > 0 and spy_ma50 > 0 and spy_price > spy_ma50 * 1.05 and vix < 20:
            return RegimeMode.BULL
        if spy_price > 0 and spy_ma50 > 0 and spy_price < spy_ma50 * 0.95:
            return RegimeMode.BEAR
        return RegimeMode.NEUTRAL
    
    def _check_consecutive_down(self, spy_context: Dict) -> int:
        """估算连续下跌天数 (基于 5 日涨跌幅近似)"""
        change_5d = spy_context.get("price_change_5d", 0)
        daily_change = spy_context.get("spx_change_percent", 0)

        # 如果 5 日跌幅超过 3% 且当日也在跌, 近似认为连续下跌
        if change_5d < -0.03 and daily_change < -0.005:
            return 3
        if change_5d < -0.015 and daily_change < -0.003:
            return 2
        return 0

    def _detect_vix_jump(self, vix: float) -> Optional[str]:
        """
        VIX 跳变检测

        触发条件:
          1. VIX 从前值跳升 > 20% → 恐慌突袭
          2. VIX 从 < 20 跨越至 > 28 → 平静→恐慌 regime break
        """
        if vix <= 0:
            return None

        prev = self._prev_vix
        self._prev_vix = vix

        if prev is None or prev <= 0:
            return None

        ratio = vix / prev

        if ratio > 1.20:
            return f"VIX 跳变 spike ({prev:.1f}→{vix:.1f}, +{(ratio-1)*100:.0f}%)"

        if prev < 20.0 and vix > 28.0:
            return f"VIX regime break ({prev:.1f}<20→{vix:.1f}>28)"

        return None
    
    def get_regime_summary(self, spy_context: Dict, vix: float) -> Dict[str, Any]:
        """获取大盘状态摘要 (供 LLM prompt 注入)"""
        regime = self._determine_regime(spy_context, vix)
        spy_price = spy_context.get("spy_price", 0)
        spy_ma50 = spy_context.get("spy_ma50", 0)
        change_5d = spy_context.get("price_change_5d", 0)
        
        regime_labels = {
            RegimeMode.BULL: "🟢 牛市格局",
            RegimeMode.BEAR: "🔴 熊市格局",
            RegimeMode.CORRECTION: "🟡 回调中",
            RegimeMode.OVERHEATED: "⚠️ 市场过热",
            RegimeMode.DEFENSIVE: "🛡️ 防御模式",
            RegimeMode.NEUTRAL: "⚪ 中性震荡",
        }
        
        return {
            "regime": regime.value,
            "regime_label": regime_labels.get(regime, ""),
            "vix": round(vix, 1),
            "vix_level": "极高" if vix > 30 else "高" if vix > 25 else "正常" if vix > 15 else "极低(过热)",
            "spy_vs_ma50": f"SPY ${spy_price:.0f} {'>' if spy_price > spy_ma50 else '<'} MA50 ${spy_ma50:.0f}" if spy_ma50 > 0 else "MA50 数据不足",
            "trend_5d": f"5日{'涨' if change_5d > 0 else '跌'} {abs(change_5d)*100:.1f}%",
        }


# 全局单例
_filter_instance: Optional[MarketRegimeFilter] = None

def get_market_regime_filter() -> MarketRegimeFilter:
    """获取全局大盘过滤器单例"""
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = MarketRegimeFilter()
    return _filter_instance
