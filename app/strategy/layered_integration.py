#!/usr/bin/env python3
"""
分层策略集成层 (2026-04-09)

集成三层策略架构:
1. 事件驱动层 (突发新闻→LLM 分析→高优先级信号)
2. 跨市场套利层 (美港股联动→开盘快速下单)
3. 技术增强层 (多周期共振 + 积小胜仓位管理)

架构说明:
- 事件驱动和跨市场策略生成信号后，送入信号缓冲层
- 技术增强层作为 confluence_v2 的增强模块运行
- 所有信号经过共识引擎加权后执行
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from app.strategy.event_driven import EventDrivenStrategy, get_event_strategy
from app.strategy.cross_market import CrossMarketArbitrage, get_cross_market_strategy
from app.strategy.tech_enhancer import TechnicalEnhancer, SmallWinAccumulator, get_technical_enhancer, get_small_win_accumulator
from app.strategy.stock_pool import get_stock_pool_manager

logger = logging.getLogger("ftseek.strategy.layered_integration")
logger.propagate = False


@dataclass
class LayeredSignal:
    """分层策略信号"""
    signal: Any  # 原始 Signal 对象
    layer: str  # "event_driven" | "cross_market" | "technical"
    priority: int  # 1=高优先级，2=中优先级，3=低优先级
    confidence: float  # 0-1 置信度
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


class LayeredStrategyManager:
    """
    分层策略管理器

    统一管理三层策略架构的信号生成和集成
    """

    def __init__(self, config: dict = None):
        self.config = config or {}

        # 初始化各层策略
        self.event_strategy = get_event_strategy(config)
        self.cross_market_strategy = get_cross_market_strategy(config)
        self.tech_enhancer = get_technical_enhancer()
        self.accumulator = get_small_win_accumulator(
            base_position=config.get('base_position', 100)
        )

        # 股票池管理器
        self.stock_pool = get_stock_pool_manager()

        # 信号冷却 (防止重复)
        self._signal_cooldown: Dict[str, datetime] = {}

        # 多周期 K 线缓存 (用于技术增强层)
        self._kline_cache: Dict[str, Dict[str, List]] = {}

        logger.info("分层策略管理器已初始化")
        logger.info(f"  事件驱动策略：{'✅' if self.event_strategy else '❌'}")
        logger.info(f"  跨市场套利策略：{'✅' if self.cross_market_strategy else '❌'}")
        logger.info(f"  技术增强器：{'✅' if self.tech_enhancer else '❌'}")
        logger.info(f"  积小胜策略：{'✅' if self.accumulator else '❌'}")

    # ========== 事件驱动层 ==========

    def process_news(self, news_list: List[Dict]) -> List[LayeredSignal]:
        """
        处理新闻列表，生成事件驱动信号

        Args:
            news_list: 新闻列表 [{'id', 'title', 'content', 'source', 'published_at', 'related_stocks'}]

        Returns:
            分层信号列表
        """
        signals = []

        # 调用事件驱动策略
        event_signals = self.event_strategy.on_news(news_list)

        for signal in event_signals:
            # 检查冷却
            if self._is_in_cooldown(f"event_{signal.code}"):
                continue

            # 从 extra 提取置信度
            confidence = 0.0
            priority = 2
            if signal.extra:
                confidence = signal.extra.get('confidence', 0.0) / 100.0  # 转为 0-1
                priority = signal.extra.get('priority', 2)

            layered = LayeredSignal(
                signal=signal,
                layer="event_driven",
                priority=priority,
                confidence=confidence
            )
            signals.append(layered)

            # 设置冷却
            self._set_cooldown(f"event_{signal.code}", hours=1)

        if signals:
            logger.info(f"事件驱动层：生成 {len(signals)} 个信号")

        return signals

    # ========== 跨市场套利层 ==========

    def analyze_us_overnight(self, us_data: List[Dict]) -> List[LayeredSignal]:
        """
        分析美股隔夜表现，生成港股开盘套利信号

        Args:
            us_data: 美股隔夜数据列表
                [{'code', 'name', 'close_price', 'change_pct', 'volume', 'turnover'}]

        Returns:
            分层信号列表
        """
        signals = []

        # 转换为 OvernightChange 对象
        from app.strategy.cross_market import OvernightChange
        us_changes = []
        for item in us_data:
            change = OvernightChange(
                code=item.get('code', ''),
                name=item.get('name', ''),
                close_price=item.get('close_price', 0),
                change_pct=item.get('change_pct', 0),
                volume=item.get('volume', 0),
                turnover=item.get('turnover', 0),
                market='US',
                timestamp=datetime.now()
            )
            us_changes.append(change)

        # 分析美股→港股
        arb_signals = self.cross_market_strategy.analyze_us_to_hk(us_changes)

        for signal in arb_signals:
            if self._is_in_cooldown(f"arb_{signal.code}"):
                continue

            confidence = 0.0
            if signal.extra:
                confidence = signal.extra.get('confidence', 0.0) / 100.0

            layered = LayeredSignal(
                signal=signal,
                layer="cross_market",
                priority=1,  # 跨市场套利为高优先级
                confidence=confidence
            )
            signals.append(layered)

            self._set_cooldown(f"arb_{signal.code}", hours=12)

        if signals:
            logger.info(f"跨市场套利层 (美股→港股): 生成 {len(signals)} 个信号")

        return signals

    def analyze_hk_daytime(self, hk_data: List[Dict]) -> List[LayeredSignal]:
        """
        分析港股白天表现，生成美股盘前套利信号

        Args:
            hk_data: 港股白天数据列表

        Returns:
            分层信号列表
        """
        signals = []

        from app.strategy.cross_market import OvernightChange
        hk_changes = []
        for item in hk_data:
            change = OvernightChange(
                code=item.get('code', ''),
                name=item.get('name', ''),
                close_price=item.get('close_price', 0),
                change_pct=item.get('change_pct', 0),
                volume=item.get('volume', 0),
                turnover=item.get('turnover', 0),
                market='HK',
                timestamp=datetime.now()
            )
            hk_changes.append(change)

        # 分析港股→美股
        arb_signals = self.cross_market_strategy.analyze_hk_to_us(hk_changes)

        for signal in arb_signals:
            if self._is_in_cooldown(f"arb_{signal.code}"):
                continue

            confidence = signal.extra.get('confidence', 0.0) / 100.0 if signal.extra else 0.0

            layered = LayeredSignal(
                signal=signal,
                layer="cross_market",
                priority=1,
                confidence=confidence
            )
            signals.append(layered)

            self._set_cooldown(f"arb_{signal.code}", hours=12)

        if signals:
            logger.info(f"跨市场套利层 (港股→美股): 生成 {len(signals)} 个信号")

        return signals

    # ========== 技术增强层 ==========

    def enhance_signal(self, signal: Any, code: str, kline_type: str = "K_5M") -> Optional[LayeredSignal]:
        """
        使用技术增强层增强信号

        Args:
            signal: 原始信号 (来自 confluence_v2 或其他策略)
            code: 股票代码
            kline_type: K 线类型

        Returns:
            增强后的分层信号，无增强返回 None
        """
        if not signal:
            return None

        # 获取多周期 K 线
        klines = self._get_multi_timeframe_klines(code)

        # 检查多周期共振
        multi_tf_signal = self.tech_enhancer.check_multi_timeframe_alignment(
            code=code,
            klines_5m=klines.get('K_5M'),
            klines_15m=klines.get('K_15M'),
            klines_1h=klines.get('K_60M'),
            klines_1d=klines.get('K_DAY')
        )

        # 增强信号
        enhanced_signal = self.tech_enhancer.enhance_signal(signal, multi_tf_signal)

        # 根据胜率调整仓位
        win_rate = self.accumulator.get_recent_win_rate()
        position = self.accumulator.calculate_position(win_rate)
        enhanced_signal.quantity = position

        # 设置退出策略
        exit_info = self.accumulator.get_exit_strategy(
            entry_price=enhanced_signal.price,
            current_price=enhanced_signal.price
        )
        if enhanced_signal.extra is None:
            enhanced_signal.extra = {}
        enhanced_signal.extra['exit_strategy'] = exit_info
        enhanced_signal.extra['alignment_score'] = multi_tf_signal.alignment_score

        # 计算置信度 (基于共振得分)
        confidence = 0.5 + (multi_tf_signal.alignment_score * 0.3)  # 0.5-0.8

        layered = LayeredSignal(
            signal=enhanced_signal,
            layer="technical",
            priority=2 if multi_tf_signal.alignment_score >= 0.75 else 3,
            confidence=confidence
        )

        logger.debug(f"技术增强层：{code} 共振得分={multi_tf_signal.alignment_score:.0%} → 优先级={layered.priority}")

        return layered

    def _get_multi_timeframe_klines(self, code: str) -> Dict[str, List]:
        """
        获取多周期 K 线数据

        从 DataSubscriber 注入的 K 线缓存中获取（2026-04-09 优化）
        缓存结构: {code: {k_type: IndicatorBuffer}}
        """
        kline_cache = self._kline_cache.get(code, {})
        result = {}
        # 将 IndicatorBuffer 转换为 List 格式供技术增强层使用
        for k_type, buf in kline_cache.items():
            try:
                if hasattr(buf, 'close') and buf.size > 0:
                    # 将缓冲区数据转为简单列表格式
                    result[k_type] = [{
                        'close': c, 'high': h, 'low': l,
                    } for c, h, l in zip(buf.close, buf.high, buf.low)]
            except Exception:
                pass
        return result

    def update_kline_cache(self, code: str, kline_type: str, kline: Dict):
        """
        更新 K 线缓存

        Args:
            code: 股票代码
            kline_type: K 线类型 (K_5M, K_15M, K_60M, K_DAY)
            kline: K 线数据
        """
        if code not in self._kline_cache:
            self._kline_cache[code] = {}

        if kline_type not in self._kline_cache[code]:
            self._kline_cache[code][kline_type] = []

        # 添加到滑动窗口 (保留最近 100 条)
        self._kline_cache[code][kline_type].append(kline)
        if len(self._kline_cache[code][kline_type]) > 100:
            self._kline_cache[code][kline_type] = self._kline_cache[code][kline_type][-100:]

    # ========== 冷却管理 ==========

    def _is_in_cooldown(self, key: str) -> bool:
        """检查是否在冷却期"""
        if key not in self._signal_cooldown:
            return False

        cooldown_end = self._signal_cooldown[key]
        return datetime.now() < cooldown_end

    def _set_cooldown(self, key: str, hours: int = 1):
        """设置冷却时间"""
        self._signal_cooldown[key] = datetime.now() + timedelta(hours=hours)

    # ========== 交易结果记录 ==========

    def record_trade_result(self, code: str, pnl_pct: float):
        """
        记录交易结果，用于积小胜策略

        Args:
            code: 股票代码
            pnl_pct: 盈亏百分比
        """
        self.accumulator.record_trade(pnl_pct)
        logger.debug(f"记录交易结果：{code} PnL={pnl_pct:.1%} → 胜率={self.accumulator.get_recent_win_rate():.1%}")

    # ========== 状态查询 ==========

    def get_layer_summary(self) -> Dict:
        """获取各层策略摘要"""
        return {
            'event_driven': self.event_strategy.get_event_summary(),
            'cross_market': self.cross_market_strategy.get_arbitrage_summary() if self.cross_market_strategy else {},
            'technical': {
                'win_rate': self.accumulator.get_recent_win_rate(),
                'target_return': self.accumulator.target_return,
            }
        }


# 全局单例
_layered_manager: Optional[LayeredStrategyManager] = None


def get_layered_strategy_manager(config: dict = None) -> LayeredStrategyManager:
    """获取分层策略管理器单例"""
    global _layered_manager
    if _layered_manager is None:
        _layered_manager = LayeredStrategyManager(config)
    return _layered_manager


def init_layered_strategy_manager(config: dict = None) -> LayeredStrategyManager:
    """初始化分层策略管理器"""
    global _layered_manager
    _layered_manager = LayeredStrategyManager(config)
    return _layered_manager


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    manager = get_layered_strategy_manager()

    # 测试事件驱动层
    test_news = [
        {
            'id': 'test_001',
            'title': '腾讯控股 Q4 财报超预期 净利润增长 30%',
            'content': '腾讯控股发布 2025 年 Q4 财报...',
            'source': 'sina',
            'published_at': datetime.now().isoformat(),
            'related_stocks': ['HK.00700']
        }
    ]

    print("\n📊 事件驱动层测试:")
    event_signals = manager.process_news(test_news)
    for sig in event_signals:
        print(f"  {sig.signal.code} | 优先级={sig.priority} | 置信度={sig.confidence:.0%}")

    # 摘要
    print("\n分层策略摘要:")
    summary = manager.get_layer_summary()
    print(f"  事件驱动：{summary['event_driven']}")
    print(f"  跨市场：{summary['cross_market']}")
    print(f"  技术增强：{summary['technical']}")
