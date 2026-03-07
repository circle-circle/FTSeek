"""
IntradayStrategy - 日内交易策略核心

整合 PreMarketAnalyzer、MomentumScanner、PatternRecognizer、CatalystTracker，
实现日内交易信号的生成、仓位管理和平仓决策。

交易时段划分:
- 09:00-09:30  盘前筛选    → 生成观察列表 (Top 20)
- 09:30-10:00  开盘确认    → 等待形态突破，设置入场条件
- 10:00-15:30  交易执行    → 突破入场，移动止盈
- 15:30-16:00  收盘清理    → 平仓决策，生成报告
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import time

from app.v4.analyzers.premarket import PreMarketAnalyzer
from app.v4.analyzers.momentum_scanner import MomentumScanner
from app.v4.analyzers.pattern_recognizer import PatternRecognizer
from app.v4.analyzers.catalyst_tracker import CatalystTracker


@dataclass
class IntradaySignal:
    """
    日内交易信号数据类

    Attributes:
        symbol: 股票代码
        direction: 交易方向 (LONG/SHORT/WATCH)
        entry_price: 入场价格
        stop_loss: 止损价格
        target_price: 目标价格
        position_size: 仓位数量
        confidence: 置信度 (0-1)
        holding_period: 预期持有时间 (intraday/swing)
        rationale: 交易理由
        generated_at: 信号生成时间戳
    """
    symbol: str
    direction: str          # LONG/SHORT/WATCH
    entry_price: float      # 入场价
    stop_loss: float        # 止损价
    target_price: float     # 目标价
    position_size: int      # 仓位数量
    confidence: float       # 置信度 (0-1)
    holding_period: str     # 预期持有时间 (intraday/swing)
    rationale: str          # 交易理由
    generated_at: float     # 生成时间


@dataclass
class TradingSession:
    """
    交易时段数据类

    Attributes:
        phase: 交易时段相位 (premarket/open/execution/close)
        start_time: 开始时间 (HH:MM 格式)
        end_time: 结束时间 (HH:MM 格式)
        active: 是否活跃
    """
    phase: str              # premarket/open/execution/close
    start_time: str
    end_time: str
    active: bool


class IntradayStrategy:
    """
    日内交易策略核心类

    整合四个分析器 (PreMarketAnalyzer、MomentumScanner、PatternRecognizer、
    CatalystTracker)，实现完整的日内交易决策流程。

    信号生成逻辑:
    - 催化事件 (25%): CatalystTracker 扫描
    - 技术形态 (30%): PatternRecognizer 检测
    - 动量扫描 (25%): MomentumScanner 分析
    - 盘前量比 (20%): PreMarketAnalyzer 评估

    信号决策阈值:
    - final_score >= 0.75: LONG (高置信度 0.85+)
    - final_score >= 0.60: LONG (中等置信度 0.65+)
    - final_score >= 0.40: WATCH (观察)
    - final_score < 0.40: SKIP (跳过)

    Attributes:
        premarket_analyzer: 盘前分析器实例
        momentum_scanner: 动量扫描器实例
        pattern_recognizer: 形态识别器实例
        catalyst_tracker: 催化事件追踪器实例
        config: 策略配置字典
    """

    # 交易时段定义
    SESSION_PHASES = {
        'premarket_screening': ('09:00', '09:30'),
        'open_confirmation': ('09:30', '10:00'),
        'trade_execution': ('10:00', '15:30'),
        'close_cleanup': ('15:30', '16:00'),
    }

    # 信号权重配置
    SCORE_WEIGHTS = {
        'catalyst': 0.25,
        'pattern': 0.30,
        'momentum': 0.25,
        'volume': 0.20,
    }

    # 信号决策阈值
    SIGNAL_THRESHOLDS = {
        'strong_long': 0.75,
        'medium_long': 0.60,
        'watch': 0.40,
    }

    def __init__(self, config: Optional[Dict] = None):
        """
        初始化日内交易策略

        Args:
            config: 策略配置字典，可选。可包含：
                    - futu_config: 富途 API 配置
                    - risk_params: 风控参数
                    - 其他自定义配置
        """
        self.config = config or {}

        # 初始化四个分析器
        futu_config = self.config.get('futu_config')
        self.premarket_analyzer = PreMarketAnalyzer(futu_config=futu_config)
        self.momentum_scanner = MomentumScanner(futu_config=futu_config)
        self.pattern_recognizer = PatternRecognizer(config=self.config.get('pattern_config'))
        self.catalyst_tracker = CatalystTracker(futu_config=futu_config)

        # 风控配置
        self.risk_params = self.config.get('risk_params', {
            'max_position_size': 1000,
            'min_confidence': 0.40,
            'max_risk_per_trade': 0.02,
        })

    def get_current_phase(self, current_time: str) -> str:
        """
        获取当前交易时段相位

        根据给定时间判断当前处于哪个交易阶段。

        Args:
            current_time: 当前时间，格式为 "HH:MM" (如 "09:30")

        Returns:
            str: 交易时段相位
                 - premarket_screening: 盘前筛选 (09:00-09:30)
                 - open_confirmation: 开盘确认 (09:30-10:00)
                 - trade_execution: 交易执行 (10:00-15:30)
                 - close_cleanup: 收盘清理 (15:30-16:00)
                 - closed: 非交易时间

        Example:
            >>> strategy = IntradayStrategy()
            >>> strategy.get_current_phase("09:15")
            'premarket_screening'
            >>> strategy.get_current_phase("14:00")
            'trade_execution'
        """
        try:
            # 解析时间
            hour, minute = map(int, current_time.split(':'))
            time_minutes = hour * 60 + minute

            # 检查各时段
            for phase, (start, end) in self.SESSION_PHASES.items():
                start_h, start_m = map(int, start.split(':'))
                end_h, end_m = map(int, end.split(':'))
                start_minutes = start_h * 60 + start_m
                end_minutes = end_h * 60 + end_m

                if start_minutes <= time_minutes < end_minutes:
                    return phase

            return 'closed'
        except (ValueError, AttributeError):
            return 'closed'

    def generate_premarket_watchlist(self, limit: int = 20) -> List[str]:
        """
        盘前生成观察列表

        在盘前时段扫描股票，根据盘前量比分析生成 Top N 观察列表。

        Args:
            limit: 返回的股票数量上限，默认 20

        Returns:
            List[str]: 观察列表，按量比降序排列的股票代码列表

        Example:
            >>> strategy = IntradayStrategy()
            >>> watchlist = strategy.generate_premarket_watchlist(limit=10)
            >>> len(watchlist) <= 10
            True
        """
        # 获取候选股票列表 (这里使用模拟数据，实际可从数据源获取)
        candidate_symbols = self._get_candidate_symbols()

        if not candidate_symbols:
            return []

        # 批量分析盘前成交量
        reports = self.premarket_analyzer.analyze_batch(candidate_symbols)

        # 按量比降序排序
        sorted_reports = sorted(
            reports,
            key=lambda r: r.volume_ratio,
            reverse=True
        )

        # 返回 Top N 股票代码
        watchlist = [report.symbol for report in sorted_reports[:limit]]

        return watchlist

    def _get_candidate_symbols(self) -> List[str]:
        """
        获取候选股票列表

        实际实现中可从数据源 (如富途 API) 获取股票池。
        这里使用模拟数据。

        Returns:
            List[str]: 候选股票代码列表
        """
        # 模拟股票池 - 实际应用中可从配置或 API 获取
        return [
            'US.AAPL', 'US.MSFT', 'US.GOOGL', 'US.AMZN', 'US.TSLA',
            'US.NVDA', 'US.META', 'US.NFLX', 'US.AMD', 'US.INTC',
            'US.MRNA', 'US.BNTX', 'US.PFE', 'US.JNJ', 'US.UNH',
            'US.JPM', 'US.BAC', 'US.GS', 'US.MS', 'US.C',
            'HK.00700', 'HK.09988', 'HK.03690', 'HK.01810', 'HK.09618',
            'US.GOLD', 'US.SILVER', 'US.OIL', 'US.GAS', 'US.COPPER',
        ]

    def generate_signal(self, market_data: Dict) -> Optional[IntradaySignal]:
        """
        生成交易信号 (核心接口)

        综合四个分析器的评分，生成交易信号。

        信号生成流程:
        1. 催化事件确认 (CatalystTracker) - 权重 25%
        2. 技术形态确认 (PatternRecognizer) - 权重 30%
        3. 动量确认 (MomentumScanner) - 权重 25%
        4. 盘前量比确认 (PreMarketAnalyzer) - 权重 20%
        5. 综合评分 → 信号决策

        Args:
            market_data: 市场数据字典，必须包含:
                        - symbol: 股票代码
                        - current_price: 当前价格
                        - volume: 成交量 (可选)
                        - klines: K 线数据 (可选，用于形态识别)

        Returns:
            Optional[IntradaySignal]: 交易信号，如果评分不足则返回 None

        Example:
            >>> strategy = IntradayStrategy()
            >>> market_data = {
            ...     'symbol': 'US.AAPL',
            ...     'current_price': 150.0,
            ...     'volume': 1000000
            ... }
            >>> signal = strategy.generate_signal(market_data)
        """
        # 检查必要字段
        symbol = market_data.get('symbol')
        if not symbol:
            return None

        current_price = market_data.get('current_price', 0.0)
        if current_price <= 0:
            return None

        # 1. 催化事件评分 (25%)
        catalyst_score = self._get_catalyst_score(symbol)

        # 2. 技术形态评分 (30%)
        klines = market_data.get('klines', [])
        pattern_score = self._get_pattern_score(symbol, klines=klines)

        # 3. 动量评分 (25%)
        momentum_score = self._get_momentum_score(symbol)

        # 4. 盘前量比评分 (20%)
        volume_score = self._get_volume_score(symbol)

        # 5. 综合评分
        final_score = self._calculate_final_score(
            catalyst_score,
            pattern_score,
            momentum_score,
            volume_score
        )

        # 6. 信号决策
        direction, confidence = self._determine_signal_direction(final_score)

        if direction is None:
            return None  # SKIP

        # 7. 计算止损和目标价
        stop_loss, target_price = self._calculate_price_levels(
            current_price,
            direction,
            confidence
        )

        # 8. 创建信号
        signal = IntradaySignal(
            symbol=symbol,
            direction=direction,
            entry_price=current_price,
            stop_loss=stop_loss,
            target_price=target_price,
            position_size=0,  # 待计算
            confidence=confidence,
            holding_period='intraday',
            rationale=self._generate_rationale(
                catalyst_score,
                pattern_score,
                momentum_score,
                volume_score
            ),
            generated_at=time.time()
        )

        # 9. 计算仓位
        signal.position_size = self.calculate_position_size(signal)

        return signal

    def _calculate_final_score(
        self,
        catalyst_score: float,
        pattern_score: float,
        momentum_score: float,
        volume_score: float
    ) -> float:
        """
        计算综合评分

        使用加权公式计算综合评分:
        final_score = (
            catalyst_score * 0.25 +
            pattern_score * 0.30 +
            momentum_score * 0.25 +
            volume_score * 0.20
        )

        Args:
            catalyst_score: 催化事件评分 (0-1)
            pattern_score: 技术形态评分 (0-1)
            momentum_score: 动量评分 (0-1)
            volume_score: 盘前量比评分 (0-1)

        Returns:
            float: 综合评分 (0-1)
        """
        final_score = (
            catalyst_score * self.SCORE_WEIGHTS['catalyst'] +
            pattern_score * self.SCORE_WEIGHTS['pattern'] +
            momentum_score * self.SCORE_WEIGHTS['momentum'] +
            volume_score * self.SCORE_WEIGHTS['volume']
        )

        # 确保分数在 0-1 范围内
        return max(0.0, min(1.0, final_score))

    def _determine_signal_direction(self, final_score: float) -> tuple:
        """
        根据综合评分确定信号方向和置信度

        Args:
            final_score: 综合评分 (0-1)

        Returns:
            tuple: (direction, confidence)
                   - direction: 'LONG', 'WATCH', 或 None (SKIP)
                   - confidence: 置信度 (0-1)
        """
        if final_score >= self.SIGNAL_THRESHOLDS['strong_long']:
            return 'LONG', min(1.0, 0.80 + (final_score - 0.75) * 2)
        elif final_score >= self.SIGNAL_THRESHOLDS['medium_long']:
            return 'LONG', 0.60 + (final_score - 0.60) * 0.5
        elif final_score >= self.SIGNAL_THRESHOLDS['watch']:
            return 'WATCH', 0.40 + (final_score - 0.40) * 0.5
        else:
            return None, 0.0

    def _calculate_price_levels(
        self,
        current_price: float,
        direction: str,
        confidence: float
    ) -> tuple:
        """
        计算止损价和目标价

        基于当前价格、方向和置信度计算关键价格水平。

        Args:
            current_price: 当前价格
            direction: 交易方向 ('LONG' 或 'SHORT')
            confidence: 置信度 (0-1)

        Returns:
            tuple: (stop_loss, target_price)
        """
        # 基于置信度调整风险回报比
        # 高置信度 → 更紧的止损，更高的目标
        risk_factor = 1.0 - confidence * 0.3  # 0.7-1.0

        if direction == 'LONG':
            # LONG: 止损在下方，目标在上方
            stop_loss_pct = 0.03 * risk_factor  # 3% 基础止损
            target_pct = 0.06 * (1.0 + confidence * 0.5)  # 6-9% 目标

            stop_loss = current_price * (1.0 - stop_loss_pct)
            target_price = current_price * (1.0 + target_pct)
        else:  # WATCH
            # WATCH 方向使用默认值
            stop_loss = current_price * 0.97
            target_price = current_price * 1.06

        return round(stop_loss, 2), round(target_price, 2)

    def _generate_rationale(
        self,
        catalyst_score: float,
        pattern_score: float,
        momentum_score: float,
        volume_score: float
    ) -> str:
        """
        生成交易理由

        Args:
            catalyst_score: 催化事件评分
            pattern_score: 技术形态评分
            momentum_score: 动量评分
            volume_score: 盘前量比评分

        Returns:
            str: 交易理由描述
        """
        reasons = []

        if catalyst_score >= 0.7:
            reasons.append("强催化事件")
        elif catalyst_score >= 0.5:
            reasons.append("中等催化事件")

        if pattern_score >= 0.7:
            reasons.append("技术形态突破")
        elif pattern_score >= 0.5:
            reasons.append("技术形态形成中")

        if momentum_score >= 0.7:
            reasons.append("动量强劲")
        elif momentum_score >= 0.5:
            reasons.append("动量中等")

        if volume_score >= 0.7:
            reasons.append("盘前放量")
        elif volume_score >= 0.5:
            reasons.append("盘前量比正常")

        return ", ".join(reasons) if reasons else "综合评分达标"

    def calculate_position_size(self, signal: IntradaySignal) -> int:
        """
        根据置信度和风控计算仓位

        仓位计算公式:
        position_size = base_size * confidence * risk_factor

        Args:
            signal: 交易信号

        Returns:
            int: 仓位数量 (股数)

        Example:
            >>> strategy = IntradayStrategy()
            >>> signal = IntradaySignal(
            ...     symbol='TEST',
            ...     direction='LONG',
            ...     entry_price=100.0,
            ...     stop_loss=95.0,
            ...     target_price=110.0,
            ...     position_size=0,
            ...     confidence=0.85,
            ...     holding_period='intraday',
            ...     rationale='Test',
            ...     generated_at=time.time()
            ... )
            >>> size = strategy.calculate_position_size(signal)
            >>> size > 0
            True
        """
        # WATCH 方向不建仓
        if signal.direction == 'WATCH':
            return 0

        # 低置信度不建仓
        if signal.confidence < self.risk_params.get('min_confidence', 0.40):
            return 0

        # 基础仓位 (根据配置)
        base_size = self.risk_params.get('max_position_size', 1000)

        # 根据置信度调整
        confidence_factor = signal.confidence

        # 根据风险回报比调整
        if signal.entry_price > 0 and signal.stop_loss > 0:
            risk_per_share = abs(signal.entry_price - signal.stop_loss)
            if risk_per_share > 0:
                max_risk = signal.entry_price * self.risk_params.get('max_risk_per_trade', 0.02)
                risk_factor = max_risk / risk_per_share / base_size
                risk_factor = min(1.0, risk_factor)
            else:
                risk_factor = 1.0
        else:
            risk_factor = 1.0

        # 计算最终仓位
        position_size = int(base_size * confidence_factor * risk_factor)

        # 确保最小仓位为 0
        return max(0, position_size)

    def should_close_position(self, position: Dict, current_price: float) -> bool:
        """
        判断是否应该平仓

        平仓条件:
        1. 触及止损价
        2. 达到目标价
        3. 收盘清理时段 (日内策略不留仓)

        Args:
            position: 仓位字典，包含:
                     - symbol: 股票代码
                     - entry_price: 入场价
                     - stop_loss: 止损价
                     - target_price: 目标价
                     - direction: 方向 (LONG/SHORT)
                     - size: 仓位大小
            current_price: 当前价格

        Returns:
            bool: 是否应该平仓

        Example:
            >>> strategy = IntradayStrategy()
            >>> position = {
            ...     'symbol': 'TEST',
            ...     'entry_price': 100.0,
            ...     'stop_loss': 95.0,
            ...     'target_price': 110.0,
            ...     'direction': 'LONG',
            ...     'size': 100
            ... }
            >>> strategy.should_close_position(position, 94.0)
            True
        """
        direction = position.get('direction', 'LONG')
        stop_loss = position.get('stop_loss', 0.0)
        target_price = position.get('target_price', 0.0)

        # 检查当前时段
        current_time = time.strftime('%H:%M', time.localtime())
        current_phase = self.get_current_phase(current_time)

        # 收盘清理时段强制平仓 (日内策略不留仓)
        if current_phase == 'close_cleanup':
            return True

        # LONG 方向平仓逻辑
        if direction == 'LONG':
            # 触及止损
            if current_price <= stop_loss:
                return True
            # 达到目标
            if current_price >= target_price:
                return True

        # SHORT 方向平仓逻辑
        elif direction == 'SHORT':
            # 触及止损 (SHORT 止损在上方)
            if current_price >= stop_loss:
                return True
            # 达到目标 (SHORT 目标在下方)
            if current_price <= target_price:
                return True

        return False

    def risk_check(self, signal: IntradaySignal) -> bool:
        """
        风控检查

        验证信号是否满足风控要求:
        1. 仓位大小 > 0
        2. 置信度在 0-1 范围内
        3. LONG 方向：止损价 < 入场价 < 目标价
        4. SHORT 方向：目标价 < 入场价 < 止损价

        Args:
            signal: 交易信号

        Returns:
            bool: 是否通过风控检查

        Example:
            >>> strategy = IntradayStrategy()
            >>> signal = IntradaySignal(
            ...     symbol='TEST',
            ...     direction='LONG',
            ...     entry_price=100.0,
            ...     stop_loss=95.0,
            ...     target_price=110.0,
            ...     position_size=100,
            ...     confidence=0.75,
            ...     holding_period='intraday',
            ...     rationale='Test',
            ...     generated_at=time.time()
            ... )
            >>> strategy.risk_check(signal)
            True
        """
        # 检查仓位大小
        if signal.position_size <= 0:
            return False

        # 检查置信度范围
        if not (0.0 <= signal.confidence <= 1.0):
            return False

        # 检查价格逻辑
        if signal.direction == 'LONG':
            # LONG: stop_loss < entry_price < target_price
            if not (signal.stop_loss < signal.entry_price < signal.target_price):
                return False
        elif signal.direction == 'SHORT':
            # SHORT: target_price < entry_price < stop_loss
            if not (signal.target_price < signal.entry_price < signal.stop_loss):
                return False
        elif signal.direction == 'WATCH':
            # WATCH 方向不需要严格的价格检查
            pass
        else:
            # 无效方向
            return False

        return True

    def _get_catalyst_score(self, symbol: str) -> float:
        """
        获取催化事件评分

        Args:
            symbol: 股票代码

        Returns:
            float: 催化事件评分 (0-1)
        """
        # 检查是否有即将发生的催化事件
        today_events = self.catalyst_tracker.get_today_catalysts()
        upcoming = self.catalyst_tracker.get_upcoming_catalysts(days=7)

        # 查找该 symbol 的事件
        symbol_events = [
            e for e in today_events + upcoming
            if e.symbol == symbol or symbol.endswith(e.symbol)
        ]

        if symbol_events:
            # 返回最高影响评分 (归一化到 0-1)
            max_impact = max(e.impact_score for e in symbol_events)
            return min(1.0, max_impact / 100.0)

        return 0.3  # 默认基础评分

    def _get_pattern_score(self, symbol: str, klines: List[Dict] = None) -> float:
        """
        获取技术形态评分

        Args:
            symbol: 股票代码
            klines: K 线数据列表，可选

        Returns:
            float: 形态评分 (0-1)
        """
        if klines is None:
            klines = []

        if not klines:
            # 没有 K 线数据时返回中等评分
            return 0.5

        report = self.pattern_recognizer.detect_all(symbol, klines)

        if not report.patterns:
            return 0.3  # 默认基础评分

        # 返回最高置信度的形态评分
        return max(p.confidence for p in report.patterns)

    def _get_momentum_score(self, symbol: str) -> float:
        """
        获取动量评分

        Args:
            symbol: 股票代码

        Returns:
            float: 动量评分 (0-1)
        """
        # 获取 Top picks 并查找该 symbol
        try:
            top_picks = self.momentum_scanner.get_top_picks(limit=50)
            for opp in top_picks:
                if opp.symbol == symbol or symbol.endswith(opp.symbol):
                    return opp.momentum_score / 100.0
        except Exception:
            pass

        return 0.5  # 默认中等评分

    def _get_volume_score(self, symbol: str) -> float:
        """
        获取盘前量比评分

        Args:
            symbol: 股票代码

        Returns:
            float: 量比评分 (0-1)
        """
        report = self.premarket_analyzer.analyze_volume(symbol)

        # 将量比转换为 0-1 评分
        # 量比 > 3 为异常，评分 1.0; 量比 < 1 评分较低
        volume_ratio = report.volume_ratio
        score = min(1.0, volume_ratio / 3.0)

        return score
