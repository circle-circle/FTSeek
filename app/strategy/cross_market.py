#!/usr/bin/env python3
"""
跨市场套利策略层

利用美港股白天/夜间双向表现，实现开盘快速下单和获取收益。

核心逻辑:
1. 美股夜间表现 → 港股开盘套利 (9:30 AM)
2. 港股白天表现 → 美股盘前/盘后套利 (9:30 PM HK)
3. ADR 溢价/折价套利
4. 期货联动套利

配置:
- config/hk_us_mapping.json: 港股 - 美股映射关系
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.stock_pool import get_stock_pool_manager, CrossMarketPair

logger = logging.getLogger("ftseek.strategy.cross_market")
logger.propagate = False


class ArbitrageType(str, Enum):
    """套利类型"""
    US_TO_HK = "us_to_hk"     # 美股→港股
    HK_TO_US = "hk_to_us"     # 港股→美股
    ADR_PREMIUM = "adr_premium"  # ADR 溢价
    ADR_DISCOUNT = "adr_discount"  # ADR 折价
    FUTURES_LINK = "futures_link"  # 期货联动


@dataclass
class OvernightChange:
    """隔夜涨跌数据"""
    code: str
    name: str
    close_price: float
    change_pct: float  # 涨跌幅 (%)
    volume: float
    turnover: float
    market: str  # US/HK
    timestamp: datetime


@dataclass
class ArbitrageSignal:
    """套利信号"""
    type: ArbitrageType
    hk_code: str
    us_code: str
    direction: str  # long/short
    reason: str
    confidence: float  # 0-100%
    expected_return: float  # 预期收益 (%)
    entry_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None


class CrossMarketArbitrage(BaseStrategy):
    """
    跨市场套利策略

    利用美港股时间差和价格联动进行套利
    """

    name = "cross_market_arbitrage"

    # 配置参数
    US_TO_HK_THRESHOLD = 0.03   # 美股涨>3% 触发港股套利
    HK_TO_US_THRESHOLD = 0.03   # 港股涨>3% 触发美股套利
    ADR_PREMIUM_THRESHOLD = 0.05  # ADR 溢价>5% 触发套利
    MIN_CONFIDENCE = 70          # 最小置信度

    def __init__(self, config: dict = None):
        super().__init__(config or {})

        self.stock_pool = get_stock_pool_manager()

        # 隔夜数据缓存
        self._us_overnight: Dict[str, OvernightChange] = {}
        self._hk_daytime: Dict[str, OvernightChange] = {}

        # 信号冷却
        self._signal_cooldown: Dict[str, datetime] = {}

        logger.info("跨市场套利策略已初始化")
        logger.info(f"  美股→港股阈值：{self.US_TO_HK_THRESHOLD:.1%}")
        logger.info(f"  港股→美股阈值：{self.HK_TO_US_THRESHOLD:.1%}")
        logger.info(f"  ADR 溢价阈值：{self.ADR_PREMIUM_THRESHOLD:.1%}")

    def analyze_us_to_hk(self, us_data: List[OvernightChange]) -> List[Signal]:
        """
        分析美股夜间表现，生成港股开盘套利信号

        逻辑:
        - 美股 ADR 隔夜大涨→港股开盘追涨
        - 美股 ADR 隔夜大跌→港股开盘做空/回避

        Args:
            us_data: 美股隔夜数据列表

        Returns:
            港股套利信号列表
        """
        # 缓存数据
        for item in us_data:
            self._us_overnight[item.code] = item

        signals = []

        for us_change in us_data:
            # 获取对应港股
            pair = self.stock_pool.get_us_hk_pair(us_change.code)
            if not pair or not pair.enabled:
                continue

            hk_code = pair.hk_code

            # 检查冷却
            if self._is_in_cooldown(hk_code):
                logger.debug(f"{hk_code} 在冷却期，跳过")
                continue

            # 生成信号
            if us_change.change_pct > self.US_TO_HK_THRESHOLD:
                # 大涨→做多
                signal = self._create_arb_signal(
                    pair=pair,
                    direction='long',
                    us_change=us_change.change_pct,
                    entry_type='market_open'
                )
                if signal:
                    signals.append(signal)

            elif us_change.change_pct < -self.US_TO_HK_THRESHOLD:
                # 大跌→做空
                signal = self._create_arb_signal(
                    pair=pair,
                    direction='short',
                    us_change=us_change.change_pct,
                    entry_type='market_open'
                )
                if signal:
                    signals.append(signal)

        logger.info(f"分析美股→港股：生成 {len(signals)} 个信号")
        return signals

    def analyze_hk_to_us(self, hk_data: List[OvernightChange]) -> List[Signal]:
        """
        分析港股白天表现，生成美股盘前/盘后套利信号

        逻辑:
        - 港股大涨→美股盘前做多对应 ADR/期货
        - 港股大跌→美股盘前做空对应 ADR/期货

        Args:
            hk_data: 港股白天数据列表

        Returns:
            美股套利信号列表
        """
        # 缓存数据
        for item in hk_data:
            self._hk_daytime[item.code] = item

        signals = []

        for hk_change in hk_data:
            # 获取对应美股
            pair = self.stock_pool.get_hk_us_pair(hk_change.code)
            if not pair or not pair.enabled:
                continue

            us_code = pair.us_code

            # 检查冷却
            if self._is_in_cooldown(us_code):
                logger.debug(f"{us_code} 在冷却期，跳过")
                continue

            # 生成信号
            if hk_change.change_pct > self.HK_TO_US_THRESHOLD:
                # 大涨→做多
                signal = self._create_us_arb_signal(
                    pair=pair,
                    direction='long',
                    hk_change=hk_change.change_pct,
                    entry_type='pre_market'
                )
                if signal:
                    signals.append(signal)

            elif hk_change.change_pct < -self.HK_TO_US_THRESHOLD:
                # 大跌→做空
                signal = self._create_us_arb_signal(
                    pair=pair,
                    direction='short',
                    hk_change=hk_change.change_pct,
                    entry_type='pre_market'
                )
                if signal:
                    signals.append(signal)

        logger.info(f"分析港股→美股：生成 {len(signals)} 个信号")
        return signals

    def analyze_adr_premium(self) -> List[Signal]:
        """
        分析 ADR 溢价/折价套利机会

        逻辑:
        - ADR 相对港股溢价>5%→做空 ADR/做多港股
        - ADR 相对港股折价>5%→做多 ADR/做空港股

        Returns:
            ADR 套利信号列表
        """
        signals = []
        pairs = self.stock_pool.get_enabled_correlation_pairs()

        for pair in pairs:
            # 获取价格数据 (实际应从 API 获取)
            hk_price = self._get_hk_price(pair.hk_code)
            us_price = self._get_us_price(pair.us_code)

            if not hk_price or not us_price:
                continue

            # 计算等价价格 (考虑汇率)
            usd_hkd_rate = 7.8  # 简化处理
            hk_equiv_usd = hk_price / usd_hkd_rate

            # 除零保护：当 hk_equiv_usd 为 0 或很小时跳过
            if hk_equiv_usd <= 0.001:
                logger.debug(f"跳过 ADR 溢价计算：{pair.hk_code} 等价价格={hk_equiv_usd:.4f}")
                continue

            # 计算溢价率
            premium = (us_price - hk_equiv_usd) / hk_equiv_usd

            # 检查冷却
            if self._is_in_cooldown(pair.hk_code):
                continue

            if premium > self.ADR_PREMIUM_THRESHOLD:
                # ADR 溢价→做空 ADR/做多港股
                signal = Signal(
                    code=pair.hk_code,
                    action=SignalAction.BUY,
                    price=hk_price,
                    quantity=100,
                    reason=f"ADR 溢价套利：溢价{premium:.1%} > {self.ADR_PREMIUM_THRESHOLD:.1%}",
                    strategy=self.name,
                    extra={
                        'arb_type': 'adr_premium',
                        'us_code': pair.us_code,
                        'premium': premium,
                    }
                )
                signals.append(signal)

            elif premium < -self.ADR_PREMIUM_THRESHOLD:
                # ADR 折价→做多 ADR/做空港股
                signal = Signal(
                    code=pair.hk_code,
                    action=SignalAction.SELL,
                    price=hk_price,
                    quantity=100,
                    reason=f"ADR 折价套利：折价{abs(premium):.1%} > {self.ADR_PREMIUM_THRESHOLD:.1%}",
                    strategy=self.name,
                    extra={
                        'arb_type': 'adr_discount',
                        'us_code': pair.us_code,
                        'premium': premium,
                    }
                )
                signals.append(signal)

        logger.info(f"分析 ADR 溢价：生成 {len(signals)} 个信号")
        return signals

    def _create_arb_signal(self, pair: CrossMarketPair, direction: str,
                          us_change: float, entry_type: str) -> Optional[Signal]:
        """创建港股套利信号"""
        # 计算置信度
        confidence = min(100, 50 + abs(us_change) * 10)

        if confidence < self.MIN_CONFIDENCE:
            return None

        # 确定操作方向
        action = SignalAction.BUY if direction == 'long' else SignalAction.SELL

        # 预期收益 (简化)
        expected_return = us_change * 0.7  # 保守估计

        # 生成信号
        signal = Signal(
            code=pair.hk_code,
            action=action,
            price=0.0,  # 开盘市价单
            quantity=200 if direction == 'long' else 100,
            reason=f"跨市场套利：{pair.us_name} 隔夜{direction} {us_change:.1%} → {pair.hk_name}",
            strategy=self.name,
            extra={
                'arb_type': 'us_to_hk',
                'us_code': pair.us_code,
                'us_change': us_change,
                'direction': direction,
                'entry_type': entry_type,
                'confidence': confidence,
                'expected_return': expected_return,
            }
        )

        # 设置冷却
        self._set_cooldown(pair.hk_code)

        return signal

    def _create_us_arb_signal(self, pair: CrossMarketPair, direction: str,
                             hk_change: float, entry_type: str) -> Optional[Signal]:
        """创建美股套利信号"""
        # 计算置信度
        confidence = min(100, 50 + abs(hk_change) * 10)

        if confidence < self.MIN_CONFIDENCE:
            return None

        # 确定操作方向
        action = SignalAction.BUY if direction == 'long' else SignalAction.SELL

        # 预期收益
        expected_return = hk_change * 0.7

        signal = Signal(
            code=pair.us_code,
            action=action,
            price=0.0,  # 盘前市价单
            quantity=100,
            reason=f"跨市场套利：{pair.hk_name} 白天{direction} {hk_change:.1%} → {pair.us_name}",
            strategy=self.name,
            extra={
                'arb_type': 'hk_to_us',
                'hk_code': pair.hk_code,
                'hk_change': hk_change,
                'direction': direction,
                'entry_type': entry_type,
                'confidence': confidence,
                'expected_return': expected_return,
            }
        )

        # 设置冷却
        self._set_cooldown(pair.us_code)

        return signal

    def _get_hk_price(self, hk_code: str) -> Optional[float]:
        """获取港股价格 (实际应从 API 获取)"""
        # 简化实现
        if hk_code in self._hk_daytime:
            return self._hk_daytime[hk_code].close_price
        return None

    def _get_us_price(self, us_code: str) -> Optional[float]:
        """获取美股价格 (实际应从 API 获取)"""
        # 简化实现
        if us_code in self._us_overnight:
            return self._us_overnight[us_code].close_price
        return None

    def _is_in_cooldown(self, code: str) -> bool:
        """检查是否在冷却期"""
        if code not in self._signal_cooldown:
            return False

        cooldown_end = self._signal_cooldown[code]
        return datetime.now() < cooldown_end

    def _set_cooldown(self, code: str, hours: int = 12):
        """设置冷却时间"""
        self._signal_cooldown[code] = datetime.now() + timedelta(hours=hours)

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """K 线回调 (不支持)"""
        return None

    def set_us_overnight_data(self, us_data: List[OvernightChange]):
        """设置美股隔夜数据"""
        self._us_overnight = {item.code: item for item in us_data}
        logger.info(f"更新美股隔夜数据：{len(self._us_overnight)} 只")

    def set_hk_daytime_data(self, hk_data: List[OvernightChange]):
        """设置港股白天数据"""
        self._hk_daytime = {item.code: item for item in hk_data}
        logger.info(f"更新港股白天数据：{len(self._hk_daytime)} 只")

    def get_arbitrage_summary(self) -> Dict:
        """获取套利摘要"""
        return {
            'us_overnight_count': len(self._us_overnight),
            'hk_daytime_count': len(self._hk_daytime),
            'enabled_pairs': len(self.stock_pool.get_enabled_correlation_pairs()),
            'active_cooldowns': len([k for k, v in self._signal_cooldown.items()
                                    if datetime.now() < v]),
        }


# 全局单例
_strategy: Optional[CrossMarketArbitrage] = None


def get_cross_market_strategy(config: dict = None) -> CrossMarketArbitrage:
    """获取跨市场套利策略单例"""
    global _strategy
    if _strategy is None:
        _strategy = CrossMarketArbitrage(config)
    return _strategy


def init_cross_market_strategy(config: dict = None) -> CrossMarketArbitrage:
    """初始化跨市场套利策略"""
    global _strategy
    _strategy = CrossMarketArbitrage(config)
    return _strategy


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    strategy = get_cross_market_strategy()

    # 模拟美股隔夜数据
    us_data = [
        OvernightChange(
            code='BABA',
            name='Alibaba',
            close_price=85.5,
            change_pct=0.045,  # +4.5%
            volume=15000000,
            turnover=1280000000,
            market='US',
            timestamp=datetime.now()
        ),
        OvernightChange(
            code='JD',
            name='JD.com',
            close_price=28.3,
            change_pct=-0.038,  # -3.8%
            volume=8000000,
            turnover=226000000,
            market='US',
            timestamp=datetime.now()
        ),
    ]

    # 分析美股→港股
    signals = strategy.analyze_us_to_hk(us_data)

    print("\n📊 美股→港股套利信号:")
    for signal in signals:
        print(f"  {signal.code} {signal.action.value} | {signal.reason[:60]}")

    # 套利摘要
    print("\n套利摘要:")
    summary = strategy.get_arbitrage_summary()
    print(f"  美股隔夜数据：{summary['us_overnight_count']} 只")
    print(f"  启用配对：{summary['enabled_pairs']} 对")
