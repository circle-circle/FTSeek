"""
SignalFusion - 多因子信号融合系统

整合多个分析器模块，生成统一的交易信号：
- PreMarketAnalyzer: 盘前量比分析
- MomentumScanner: 动量催化扫描
- PatternRecognizer: 技术形态识别
- CatalystTracker: 催化事件追踪

融合权重配置:
- premarket: 0.20 (盘前量比分析)
- momentum: 0.25 (动量催化扫描)
- pattern: 0.30 (技术形态识别)
- catalyst: 0.25 (催化事件追踪)

决策阈值:
- BUY (强烈推荐): score >= 0.75
- WATCH (观察): 0.50 <= score < 0.75
- SKIP (跳过): score < 0.50
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import time

from app.v4.analyzers.premarket import PreMarketAnalyzer
from app.v4.analyzers.momentum_scanner import MomentumScanner
from app.v4.analyzers.pattern_recognizer import PatternRecognizer
from app.v4.analyzers.catalyst_tracker import CatalystTracker


@dataclass
class FusionSignal:
    """
    融合信号数据类

    整合多个分析器维度的评分，生成统一的交易信号。

    Attributes:
        symbol: 股票代码
        action: 交易动作 (BUY/SELL/WATCH/SKIP)
        direction: 方向 (LONG/SHORT)
        confidence: 置信度 (0-1)
        premarket_score: 盘前量比评分 (0-1)
        momentum_score: 动量催化评分 (0-1)
        pattern_score: 技术形态评分 (0-1)
        catalyst_score: 催化事件评分 (0-1)
        final_score: 融合后的最终评分 (0-1)
        entry_price: 入场价格
        stop_loss: 止损价格
        target_price: 目标价格
        position_ratio: 建议仓位比例 (0-1)
        rationale: 决策理由说明
        generated_at: 生成时间戳
    """
    symbol: str
    action: str  # BUY/SELL/WATCH/SKIP
    direction: str  # LONG/SHORT
    confidence: float  # 置信度 (0-1)
    premarket_score: float
    momentum_score: float
    pattern_score: float
    catalyst_score: float
    final_score: float
    entry_price: float
    stop_loss: float
    target_price: float
    position_ratio: float
    rationale: str
    generated_at: float


@dataclass
class FusionReport:
    """
    融合报告数据类

    包含多个标的的扫描结果和汇总信息。

    Attributes:
        signals: 融合信号列表
        scan_time: 扫描时间戳
        market_condition: 市场状况 (bullish/neutral/bearish)
        top_picks: 推荐标的列表 (按 final_score 降序)
    """
    signals: List[FusionSignal]
    scan_time: float
    market_condition: str
    top_picks: List[str]


class SignalFusion:
    """
    多因子信号融合系统

    整合四个分析器模块，使用加权评分公式生成统一的交易信号。

    Attributes:
        premarket: 盘前量比分析器
        momentum: 动量催化扫描器
        pattern: 技术形态识别器
        catalyst: 催化事件追踪器
        weights: 各维度权重配置

    Example:
        >>> fusion = SignalFusion()
        >>> market_data = {
        ...     'price': 150.0,
        ...     'volume': 1000000,
        ...     'premarket_volume_ratio': 2.5,
        ...     'price_momentum': 75.0,
        ...     'volume_momentum': 80.0,
        ...     'catalyst_strength': 85.0,
        ...     'klines': [...]
        ... }
        >>> signal = fusion.generate_signal("US.AAPL", market_data)
        >>> print(f"{signal.symbol}: {signal.action} (confidence: {signal.confidence:.2f})")
    """

    # 默认权重配置
    DEFAULT_WEIGHTS = {
        'premarket': 0.20,  # 盘前量比分析
        'momentum': 0.25,   # 动量催化扫描
        'pattern': 0.30,    # 技术形态识别
        'catalyst': 0.25,   # 催化事件追踪
    }

    # 决策阈值
    BUY_THRESHOLD = 0.75
    WATCH_THRESHOLD = 0.50

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化 SignalFusion

        整合四个分析器模块，并加载权重配置。

        Args:
            config: 配置字典，可包含 weights 自定义权重

        Example:
            >>> # 使用默认配置
            >>> fusion = SignalFusion()
            >>>
            >>> # 使用自定义权重
            >>> custom_config = {
            ...     'weights': {
            ...         'premarket': 0.30,
            ...         'momentum': 0.30,
            ...         'pattern': 0.20,
            ...         'catalyst': 0.20
            ...     }
            ... }
            >>> fusion = SignalFusion(config=custom_config)
        """
        config = config or {}

        # 初始化四个分析器
        self.premarket = PreMarketAnalyzer()
        self.momentum = MomentumScanner()
        self.pattern = PatternRecognizer()
        self.catalyst = CatalystTracker()

        # 加载权重配置
        self.weights = config.get('weights', self.DEFAULT_WEIGHTS.copy())

    def calculate_score(self, symbol: str, market_data: Dict[str, Any]) -> Dict[str, float]:
        """
        计算各维度评分

        调用四个分析器模块，分别计算各维度的评分 (归一化到 0-1 范围)。

        Args:
            symbol: 股票代码
            market_data: 市场数据字典，包含:
                - price: 当前价格
                - volume: 成交量
                - premarket_volume_ratio: 盘前量比
                - price_momentum: 价格动量 (0-100)
                - volume_momentum: 成交量动量 (0-100)
                - catalyst_strength: 催化强度 (0-100)
                - klines: K 线数据列表

        Returns:
            Dict[str, float]: 各维度评分字典
                {
                    'premarket': 0.8,
                    'momentum': 0.7,
                    'pattern': 0.9,
                    'catalyst': 0.6
                }

        Example:
            >>> fusion = SignalFusion()
            >>> market_data = {
            ...     'price': 150.0,
            ...     'premarket_volume_ratio': 2.5,
            ...     'price_momentum': 75.0,
            ...     'volume_momentum': 80.0,
            ...     'catalyst_strength': 85.0,
            ...     'klines': [...]
            ... }
            >>> scores = fusion.calculate_score("US.AAPL", market_data)
            >>> print(scores)
            {'premarket': 0.83, 'momentum': 0.78, 'pattern': 0.70, 'catalyst': 0.85}
        """
        # 1. 盘前量比评分 (基于 volume_ratio)
        premarket_report = self.premarket.analyze_volume(symbol)
        # 将量比转换为 0-1 分数：volume_ratio >= 3.0 为满分
        premarket_score = min(1.0, premarket_report.volume_ratio / 3.0)

        # 2. 动量催化评分 (基于 price_momentum, volume_momentum, catalyst_strength)
        price_mom = market_data.get('price_momentum', 50.0)
        volume_mom = market_data.get('volume_momentum', 50.0)
        catalyst_str = market_data.get('catalyst_strength', 50.0)
        # 归一化到 0-1
        momentum_score = (price_mom * 0.4 + volume_mom * 0.3 + catalyst_str * 0.3) / 100.0
        momentum_score = max(0.0, min(1.0, momentum_score))

        # 3. 技术形态评分 (基于 K 线检测)
        klines = market_data.get('klines', [])
        pattern_score = 0.5  # 默认中等分数
        if klines:
            pattern_report = self.pattern.detect_all(symbol, klines)
            if pattern_report.patterns:
                # 取最高置信度的形态
                best_pattern = pattern_report.patterns[0]
                pattern_score = best_pattern.confidence
            else:
                # 没有检测到形态，基于价格趋势给分
                if len(klines) >= 10:
                    prev_close = klines[-10]['close']
                    recent_trend = (klines[-1]['close'] - prev_close) / prev_close
                    pattern_score = max(0.0, min(1.0, 0.5 + recent_trend * 10))
        pattern_score = max(0.0, min(1.0, pattern_score))

        # 4. 催化事件评分 (基于 CatalystTracker)
        catalyst_score = 0.5  # 默认中等分数
        try:
            # 获取该股票的催化事件
            symbol_clean = symbol.replace('US.', '').replace('HK.', '')
            today_catalysts = self.catalyst.get_today_catalysts()
            upcoming_catalysts = self.catalyst.get_upcoming_catalysts(days=7)

            # 检查是否有该股票的事件
            has_today = any(c.symbol == symbol_clean for c in today_catalysts)
            has_upcoming = any(c.symbol == symbol_clean for c in upcoming_catalysts)

            if has_today:
                catalyst_score = 0.9  # 今日有事件，高分
            elif has_upcoming:
                catalyst_score = 0.7  # 未来有事件，中高分
            else:
                catalyst_score = 0.5  # 无事件，中等分数
        except Exception:
            # 如果催化剂追踪器出错，使用默认分数
            catalyst_score = 0.5

        return {
            'premarket': premarket_score,
            'momentum': momentum_score,
            'pattern': pattern_score,
            'catalyst': catalyst_score,
        }

    def fuse_scores(self, scores: Dict[str, float]) -> float:
        """
        融合评分公式

        使用加权平均公式计算最终评分：
        final_score = (
            premarket × 0.20 +
            momentum × 0.25 +
            pattern × 0.30 +
            catalyst × 0.25
        )

        Args:
            scores: 各维度评分字典
                {
                    'premarket': 0.8,
                    'momentum': 0.7,
                    'pattern': 0.9,
                    'catalyst': 0.6
                }

        Returns:
            float: 融合后的最终评分 (0-1)

        Example:
            >>> fusion = SignalFusion()
            >>> scores = {
            ...     'premarket': 0.8,
            ...     'momentum': 0.7,
            ...     'pattern': 0.9,
            ...     'catalyst': 0.6
            ... }
            >>> final_score = fusion.fuse_scores(scores)
            >>> print(f"{final_score:.4f}")
            0.7550
        """
        final_score = (
            scores.get('premarket', 0.0) * self.weights['premarket'] +
            scores.get('momentum', 0.0) * self.weights['momentum'] +
            scores.get('pattern', 0.0) * self.weights['pattern'] +
            scores.get('catalyst', 0.0) * self.weights['catalyst']
        )

        # 确保分数在 0-1 范围内
        return max(0.0, min(1.0, final_score))

    def make_decision(self, final_score: float) -> str:
        """
        根据分数生成决策

        决策阈值:
        - BUY:    score >= 0.75 (强烈推荐)
        - WATCH:  0.50 <= score < 0.75 (观察)
        - SKIP:   score < 0.50 (跳过)

        Args:
            final_score: 融合后的最终评分 (0-1)

        Returns:
            str: 决策动作 (BUY/WATCH/SKIP)

        Example:
            >>> fusion = SignalFusion()
            >>> fusion.make_decision(0.80)
            'BUY'
            >>> fusion.make_decision(0.60)
            'WATCH'
            >>> fusion.make_decision(0.30)
            'SKIP'
        """
        if final_score >= self.BUY_THRESHOLD:
            return 'BUY'
        elif final_score >= self.WATCH_THRESHOLD:
            return 'WATCH'
        else:
            return 'SKIP'

    def _calculate_position_ratio(self, final_score: float) -> float:
        """
        根据最终评分计算建议仓位比例

        仓位配置:
        - score >= 0.75: 100% (满仓)
        - 0.60 <= score < 0.75: 70%
        - 0.50 <= score < 0.60: 30%
        - score < 0.50: 0% (不操作)

        Args:
            final_score: 融合后的最终评分 (0-1)

        Returns:
            float: 建议仓位比例 (0-1)
        """
        if final_score >= 0.75:
            return 1.0
        elif final_score >= 0.60:
            return 0.7
        elif final_score >= 0.50:
            return 0.3
        else:
            return 0.0

    def _calculate_prices(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        action: str
    ) -> tuple:
        """
        计算入场价、止损价和目标价

        Args:
            symbol: 股票代码
            market_data: 市场数据
            action: 交易动作

        Returns:
            tuple: (entry_price, stop_loss, target_price)
        """
        current_price = market_data.get('price', 100.0)

        # 基于波动率计算止损和目标
        # 简化：使用固定百分比
        stop_loss_pct = 0.05  # 5% 止损
        target_pct = 0.10     # 10% 目标

        entry_price = current_price

        if action == 'BUY':
            stop_loss = current_price * (1 - stop_loss_pct)
            target_price = current_price * (1 + target_pct)
        elif action == 'SELL':
            stop_loss = current_price * (1 + stop_loss_pct)
            target_price = current_price * (1 - target_pct)
        else:
            # WATCH/SKIP: 使用当前价格作为参考
            stop_loss = current_price * (1 - stop_loss_pct)
            target_price = current_price * (1 + target_pct)

        return entry_price, stop_loss, target_price

    def _generate_rationale(
        self,
        symbol: str,
        scores: Dict[str, float],
        final_score: float,
        action: str
    ) -> str:
        """
        生成决策理由说明

        Args:
            symbol: 股票代码
            scores: 各维度评分
            final_score: 最终评分
            action: 交易动作

        Returns:
            str: 决策理由说明
        """
        # 找出最强和最弱的维度
        max_dim = max(scores, key=scores.get)
        min_dim = min(scores, key=scores.get)

        rationale_parts = [
            f"{symbol} - {action}",
            f"综合评分：{final_score:.2f}",
            f"最强维度：{max_dim} ({scores[max_dim]:.2f})",
            f"最弱维度：{min_dim} ({scores[min_dim]:.2f})",
        ]

        if action == 'BUY':
            rationale_parts.append("建议：强烈推荐，多个维度表现优异")
        elif action == 'WATCH':
            rationale_parts.append("建议：保持观察，等待更明确信号")
        else:
            rationale_parts.append("建议：跳过，信号不够明确")

        return " | ".join(rationale_parts)

    def generate_signal(
        self,
        symbol: str,
        market_data: Dict[str, Any]
    ) -> FusionSignal:
        """
        生成完整融合信号

        整合所有分析器结果，生成包含完整信息的交易信号。

        Args:
            symbol: 股票代码
            market_data: 市场数据字典

        Returns:
            FusionSignal: 完整的融合信号

        Example:
            >>> fusion = SignalFusion()
            >>> market_data = {
            ...     'price': 150.0,
            ...     'volume': 1000000,
            ...     'premarket_volume_ratio': 3.0,
            ...     'price_momentum': 80.0,
            ...     'volume_momentum': 85.0,
            ...     'catalyst_strength': 90.0,
            ...     'klines': [...]
            ... }
            >>> signal = fusion.generate_signal("US.AAPL", market_data)
            >>> print(f"{signal.symbol}: {signal.action} @ {signal.entry_price}")
        """
        # 1. 计算各维度评分
        scores = self.calculate_score(symbol, market_data)

        # 2. 融合评分
        final_score = self.fuse_scores(scores)

        # 3. 生成决策
        action = self.make_decision(final_score)

        # 4. 确定方向 (简化：默认 LONG)
        direction = 'LONG'

        # 5. 计算价格
        entry_price, stop_loss, target_price = self._calculate_prices(
            symbol, market_data, action
        )

        # 6. 计算仓位比例
        position_ratio = self._calculate_position_ratio(final_score)

        # 7. 生成理由说明
        rationale = self._generate_rationale(symbol, scores, final_score, action)

        # 8. 创建信号
        signal = FusionSignal(
            symbol=symbol,
            action=action,
            direction=direction,
            confidence=final_score,
            premarket_score=scores['premarket'],
            momentum_score=scores['momentum'],
            pattern_score=scores['pattern'],
            catalyst_score=scores['catalyst'],
            final_score=final_score,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_price=target_price,
            position_ratio=position_ratio,
            rationale=rationale,
            generated_at=time.time(),
        )

        return signal

    def scan_multiple(
        self,
        symbols: List[str],
        market_data: Optional[Dict[str, Any]] = None
    ) -> FusionReport:
        """
        扫描多个标的生成报告

        对多个股票执行信号融合分析，生成综合报告。

        Args:
            symbols: 股票代码列表
            market_data: 市场数据字典 (可选，所有股票共用相同数据)

        Returns:
            FusionReport: 融合报告，包含所有信号和汇总信息

        Example:
            >>> fusion = SignalFusion()
            >>> symbols = ["US.AAPL", "US.NVDA", "US.TSLA"]
            >>> market_data = {...}
            >>> report = fusion.scan_multiple(symbols, market_data)
            >>> print(f"扫描完成：{len(report.signals)} 个信号")
            >>> print(f"推荐标的：{report.top_picks}")
        """
        market_data = market_data or {}
        signals = []

        # 为每个标的生成信号
        for symbol in symbols:
            try:
                signal = self.generate_signal(symbol, market_data)
                signals.append(signal)
            except Exception as e:
                # 如果单个标的失败，记录错误并继续
                print(f"警告：生成 {symbol} 信号失败：{e}")
                continue

        # 按 final_score 降序排序
        signals.sort(key=lambda s: s.final_score, reverse=True)

        # 生成 top_picks (取前 N 个，N 根据信号数量动态调整)
        top_count = min(5, len(signals))
        top_picks = [s.symbol for s in signals[:top_count] if s.final_score >= 0.50]

        # 如果所有信号都低于 0.5，取最高的 1-2 个
        if not top_picks and signals:
            top_picks = [signals[0].symbol]

        # 确定市场状况 (简化：基于平均分数)
        if signals:
            avg_score = sum(s.final_score for s in signals) / len(signals)
            if avg_score >= 0.65:
                market_condition = 'bullish'
            elif avg_score >= 0.45:
                market_condition = 'neutral'
            else:
                market_condition = 'bearish'
        else:
            market_condition = 'neutral'

        return FusionReport(
            signals=signals,
            scan_time=time.time(),
            market_condition=market_condition,
            top_picks=top_picks,
        )
