"""
双重上市套利策略 (Dual-Listed Arbitrage Strategy)

适用标的:
  - US.BABA ↔ HK.09988 (阿里巴巴)
  - US.JD   ↔ HK.09618 (京东)
  - US.BIDU ↔ HK.09888 (百度)
  - US.LI   ↔ HK.02015 (理想)
  - US.XPEV ↔ HK.09868 (小鹏)

逻辑:
  1. 美股收盘后计算 ADR 与港股的理论价差
  2. 港股开盘时如果实际价格偏离理论价格超过阈值，生成套利信号
  3. 非实时策略，仅在港股开盘时执行一次

公式:
  理论港股价格 = 美股收盘价 × 换股比例 × 汇率
  价差 % = (实际港股价格 - 理论港股价格) / 理论港股价格

阈值:
  - 触发套利: |价差| > 2.0% (可配置)
  - 方向: 价差 > 0 → 卖港股买美股 (次日制裁)
          价差 < 0 → 买港股卖美股 (当日可执行)

更新时间: 2026-04-13
"""

import os
import logging
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger("ftseek.arb_dual_listed")


@dataclass
class ArbPairConfig:
    """套利配对配置"""
    us_code: str
    hk_code: str
    ratio: float  # 换股比例 (通常 1:1 或 1:8)
    threshold_pct: float  # 触发阈值 (%)
    description: str


@dataclass
class ArbSignal:
    """套利信号"""
    us_code: str
    hk_code: str
    us_price: float
    hk_price: float
    theoretical_hk_price: float
    spread_pct: float
    direction: str  # "BUY_HK" or "SELL_HK"
    confidence: float
    reason: str
    timestamp: float


# 默认套利配对配置
DEFAULT_ARB_PAIRS = [
    ArbPairConfig("US.BABA", "HK.09988", 1.0, 2.0, "阿里巴巴"),
    ArbPairConfig("US.JD", "HK.09618", 1.0, 2.0, "京东"),
    ArbPairConfig("US.BIDU", "HK.09888", 1.0, 2.0, "百度"),
    ArbPairConfig("US.LI", "HK.02015", 1.0, 2.5, "理想"),
    ArbPairConfig("US.XPEV", "HK.09868", 1.0, 2.5, "小鹏"),
]


class DualListedArbitrage:
    """双重上市套利策略"""

    def __init__(self, pairs: List[ArbPairConfig] = None):
        self.pairs = pairs or DEFAULT_ARB_PAIRS
        self._usd_hkd_rate = 7.8  # 联系汇率，可从 API 获取

    def set_exchange_rate(self, rate: float):
        """设置 USD/HKD 汇率"""
        self._usd_hkd_rate = rate

    def calculate_theoretical_price(self, us_price: float, ratio: float) -> float:
        """
        计算理论港股价格

        Args:
            us_price: 美股收盘价 (USD)
            ratio: 换股比例 (1 ADR = ? 港股)

        Returns:
            理论港股价格 (HKD)
        """
        return us_price * self._usd_hkd_rate * ratio

    def calculate_spread(
        self,
        us_price: float,
        hk_price: float,
        ratio: float
    ) -> float:
        """
        计算价差百分比

        Args:
            us_price: 美股价格 (USD)
            hk_price: 港股价格 (HKD)
            ratio: 换股比例

        Returns:
            价差百分比 (正 = 港股溢价, 负 = 港股折价)
        """
        theoretical = self.calculate_theoretical_price(us_price, ratio)
        if theoretical <= 0:
            return 0.0
        return ((hk_price - theoretical) / theoretical) * 100

    def generate_signals(
        self,
        us_prices: Dict[str, float],
        hk_prices: Dict[str, float]
    ) -> List[ArbSignal]:
        """
        生成套利信号

        Args:
            us_prices: {us_code: price}
            hk_prices: {hk_code: price}

        Returns:
            套利信号列表
        """
        signals = []

        for pair in self.pairs:
            us_price = us_prices.get(pair.us_code)
            hk_price = hk_prices.get(pair.hk_code)

            if us_price is None or hk_price is None or us_price <= 0 or hk_price <= 0:
                logger.debug(
                    f"[ArbDualListed] 跳过 {pair.us_code}/{pair.hk_code}: "
                    f"价格缺失或无效 (US={us_price}, HK={hk_price})"
                )
                continue

            spread = self.calculate_spread(us_price, hk_price, pair.ratio)
            theoretical = self.calculate_theoretical_price(us_price, pair.ratio)

            # 检查是否超过阈值
            if abs(spread) < pair.threshold_pct:
                logger.debug(
                    f"[ArbDualListed] {pair.description} 价差 {spread:+.2f}% "
                    f"< 阈值 {pair.threshold_pct}%"
                )
                continue

            # 确定方向
            if spread > 0:
                # 港股溢价 → 卖港股 (次日制裁)
                direction = "SELL_HK"
                confidence = min(abs(spread) / 5.0, 1.0)  # 5% 价差 = 100% 置信度
                reason = (
                    f"{pair.description} 港股溢价 {spread:+.2f}% "
                    f"(理论 HKD {theoretical:.2f}, 实际 HKD {hk_price:.2f})"
                )
            else:
                # 港股折价 → 买港股 (当日可执行)
                direction = "BUY_HK"
                confidence = min(abs(spread) / 5.0, 1.0)
                reason = (
                    f"{pair.description} 港股折价 {spread:+.2f}% "
                    f"(理论 HKD {theoretical:.2f}, 实际 HKD {hk_price:.2f})"
                )

            signal = ArbSignal(
                us_code=pair.us_code,
                hk_code=pair.hk_code,
                us_price=us_price,
                hk_price=hk_price,
                theoretical_hk_price=theoretical,
                spread_pct=spread,
                direction=direction,
                confidence=confidence,
                reason=reason,
                timestamp=datetime.now().timestamp(),
            )
            signals.append(signal)

            logger.info(
                f"[ArbDualListed] 套利信号: {pair.description} | "
                f"{direction} | 价差 {spread:+.2f}% | "
                f"置信度 {confidence:.2f}"
            )

        return signals

    def format_report(self, signals: List[ArbSignal]) -> str:
        """格式化套利报告"""
        if not signals:
            return "📊 双重上市套利检查: 无套利机会 (价差均在阈值内)"

        lines = [
            "📊 双重上市套利机会",
            "",
            f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"发现机会: {len(signals)} 个",
            "",
        ]

        for s in signals:
            direction_emoji = "🔴" if s.direction == "SELL_HK" else "🟢"
            lines.append(
                f"{direction_emoji} **{s.us_code} ↔ {s.hk_code}**\n"
                f"   美股: ${s.us_price:.2f} | 港股: HKD {s.hk_price:.2f}\n"
                f"   理论价: HKD {s.theoretical_hk_price:.2f}\n"
                f"   价差: {s.spread_pct:+.2f}% | 方向: {s.direction}\n"
                f"   置信度: {s.confidence:.0%}\n"
            )

        lines.append("⚠️ 注意: 套利需考虑交易成本、汇率风险和流动性")

        return "\n".join(lines)


# ============================================================
#  全局单例
# ============================================================
_arb: Optional[DualListedArbitrage] = None


def get_dual_listed_arb() -> DualListedArbitrage:
    """获取双重上市套利策略单例"""
    global _arb
    if _arb is None:
        _arb = DualListedArbitrage()
    return _arb


def reset_arb():
    """重置单例 (用于测试)"""
    global _arb
    _arb = None
