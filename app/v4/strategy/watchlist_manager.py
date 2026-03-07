"""
WatchlistManager - 观察列表管理系统

动态管理股票观察列表，整合 PreMarketAnalyzer 和 SignalFusion 生成优先级排序的观察列表。
支持盘前生成、盘中动态更新、失效检测和自动清理。

核心功能:
1. 盘前生成 Top 20 观察列表
2. 盘中动态更新评分和状态
3. 自动检测失效标的
4. 优先级排序和 Top 推荐

优先级评分公式:
priority_score = (
    premarket_score × 0.25 +
    momentum_score × 0.30 +
    pattern_score × 0.30 +
    catalyst_score × 0.15
) × 100

失效条件:
- 价格偏离入场价超过 5%
- 形态已突破 (错过入场点)
- 催化事件已过
- 交易时段结束 (16:00 后)
"""

from dataclasses import dataclass
from typing import List, Dict, Optional, Any
import time

from app.v4.analyzers.premarket import PreMarketAnalyzer
from app.v4.strategy.signal_fusion import SignalFusion


@dataclass
class WatchlistEntry:
    """
    观察列表条目

    代表单只股票在观察列表中的完整信息。

    Attributes:
        symbol: 股票代码 (如 "US.AAPL")
        name: 股票名称
        priority_score: 优先级评分 (0-100), 用于排序
        premarket_score: 盘前评分 (0-1)
        momentum_score: 动量评分 (0-1)
        pattern_score: 形态评分 (0-1)
        catalyst_score: 催化评分 (0-1)
        entry_price: 建议入场价
        stop_loss: 止损价
        target_price: 目标价
        added_at: 添加时间戳
        is_stale: 是否失效
    """
    symbol: str
    name: str
    priority_score: float  # 优先级评分 (0-100)
    premarket_score: float  # 盘前评分 (0-1)
    momentum_score: float  # 动量评分 (0-1)
    pattern_score: float  # 形态评分 (0-1)
    catalyst_score: float  # 催化评分 (0-1)
    entry_price: float  # 建议入场价
    stop_loss: float  # 止损价
    target_price: float  # 目标价
    added_at: float  # 添加时间戳
    is_stale: bool = False  # 是否失效


@dataclass
class WatchlistReport:
    """
    观察列表报告

    包含观察列表的完整快照和汇总信息。

    Attributes:
        entries: 观察列表条目列表
        total_count: 总条目数
        scan_time: 扫描时间戳
        market_condition: 市场状况 (bullish/neutral/bearish)
        top_picks: Top 5 推荐股票代码列表
    """
    entries: List[WatchlistEntry]
    total_count: int
    scan_time: float
    market_condition: str
    top_picks: List[str]  # Top 5 推荐


class WatchlistManager:
    """
    观察列表管理器

    整合 PreMarketAnalyzer 和 SignalFusion，生成和管理动态观察列表。
    支持盘前生成、盘中更新、失效检测和自动清理。

    Attributes:
        premarket: 盘前分析器实例
        fusion: 信号融合器实例
        max_entries: 最大观察列表条目数 (默认 20)
        watchlist: 当前观察列表

    Example:
        >>> manager = WatchlistManager()
        >>> candidates = ["US.AAPL", "US.NVDA", "US.TSLA"]
        >>> report = manager.generate_premarket_watchlist(candidates)
        >>> print(f"生成 {report.total_count} 只股票的观察列表")
        >>> print(f"Top 推荐：{report.top_picks}")
    """

    # 默认配置
    DEFAULT_MAX_ENTRIES = 20
    DEFAULT_STALE_PRICE_DEVIATION = 0.05  # 5% 价格偏离阈值

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化观察列表管理器

        Args:
            config: 配置字典，可选。可包含:
                - max_entries: 最大观察列表条目数
                - stale_price_deviation: 失效价格偏离阈值

        Example:
            >>> # 使用默认配置
            >>> manager = WatchlistManager()
            >>>
            >>> # 使用自定义配置
            >>> config = {
            ...     'max_entries': 30,
            ...     'stale_price_deviation': 0.03
            ... }
            >>> manager = WatchlistManager(config=config)
        """
        config = config or {}

        # 初始化分析器
        self.premarket = PreMarketAnalyzer()
        self.fusion = SignalFusion()

        # 加载配置
        self.max_entries = config.get('max_entries', self.DEFAULT_MAX_ENTRIES)
        self.stale_price_deviation = config.get(
            'stale_price_deviation',
            self.DEFAULT_STALE_PRICE_DEVIATION
        )

        # 观察列表
        self.watchlist: List[WatchlistEntry] = []

    def _calculate_priority_score(
        self,
        premarket_score: float,
        momentum_score: float,
        pattern_score: float,
        catalyst_score: float
    ) -> float:
        """
        计算优先级评分

        使用加权公式计算综合优先级评分 (0-100):
        priority_score = (
            premarket_score × 0.25 +
            momentum_score × 0.30 +
            pattern_score × 0.30 +
            catalyst_score × 0.15
        ) × 100

        Args:
            premarket_score: 盘前评分 (0-1)
            momentum_score: 动量评分 (0-1)
            pattern_score: 形态评分 (0-1)
            catalyst_score: 催化评分 (0-1)

        Returns:
            float: 优先级评分 (0-100)

        Example:
            >>> manager = WatchlistManager()
            >>> score = manager._calculate_priority_score(0.8, 0.7, 0.9, 0.6)
            >>> print(f"{score:.2f}")
            76.00
        """
        priority_score = (
            premarket_score * 0.25 +
            momentum_score * 0.30 +
            pattern_score * 0.30 +
            catalyst_score * 0.15
        ) * 100

        # 确保在 0-100 范围内
        return max(0.0, min(100.0, priority_score))

    def _is_price_stale(self, entry: WatchlistEntry, current_price: float) -> bool:
        """
        检测价格是否失效

        如果当前价格偏离入场价超过阈值 (默认 5%),则认为失效。

        Args:
            entry: 观察列表条目
            current_price: 当前市场价格

        Returns:
            bool: 是否因价格偏离而失效

        Example:
            >>> manager = WatchlistManager()
            >>> entry = WatchlistEntry(..., entry_price=150.0, ...)
            >>> manager._is_price_stale(entry, 160.0)  # 偏离 6.67%
            True
            >>> manager._is_price_stale(entry, 155.0)  # 偏离 3.33%
            False
        """
        if entry.entry_price <= 0:
            return False

        deviation = abs(current_price - entry.entry_price) / entry.entry_price
        return deviation > self.stale_price_deviation

    def _is_market_closed(self) -> bool:
        """
        检测交易时段是否结束

        简化实现：假设 16:00 后为收盘。

        Returns:
            bool: 是否已收盘

        Note:
            实际实现应检查具体交易所的交易时间。
        """
        current_hour = time.localtime().tm_hour
        return current_hour >= 16

    def _create_entry_from_signal(
        self,
        symbol: str,
        signal: Any,
        name: Optional[str] = None
    ) -> WatchlistEntry:
        """
        从融合信号创建观察列表条目

        Args:
            symbol: 股票代码
            signal: SignalFusion 生成的 FusionSignal
            name: 股票名称，可选

        Returns:
            WatchlistEntry: 新创建的观察列表条目
        """
        # 计算优先级评分
        priority_score = self._calculate_priority_score(
            signal.premarket_score,
            signal.momentum_score,
            signal.pattern_score,
            signal.catalyst_score
        )

        # 使用信号中的信息或默认值
        stock_name = name or symbol

        entry = WatchlistEntry(
            symbol=symbol,
            name=stock_name,
            priority_score=priority_score,
            premarket_score=signal.premarket_score,
            momentum_score=signal.momentum_score,
            pattern_score=signal.pattern_score,
            catalyst_score=signal.catalyst_score,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            added_at=time.time(),
            is_stale=False
        )

        return entry

    def _determine_market_condition(
        self,
        entries: List[WatchlistEntry]
    ) -> str:
        """
        根据观察列表确定市场状况

        Args:
            entries: 观察列表条目

        Returns:
            str: 市场状况 (bullish/neutral/bearish)
        """
        if not entries:
            return 'neutral'

        # 计算平均优先级评分
        avg_score = sum(e.priority_score for e in entries) / len(entries)

        if avg_score >= 70:
            return 'bullish'
        elif avg_score >= 50:
            return 'neutral'
        else:
            return 'bearish'

    def generate_premarket_watchlist(
        self,
        candidate_symbols: List[str]
    ) -> WatchlistReport:
        """
        盘前生成观察列表 (Top 20)

        流程:
        1. 扫描候选股票池
        2. 计算各维度评分
        3. 按优先级排序
        4. 选取 Top 20
        5. 生成报告

        Args:
            candidate_symbols: 候选股票代码列表

        Returns:
            WatchlistReport: 观察列表报告

        Example:
            >>> manager = WatchlistManager()
            >>> candidates = ["US.AAPL", "US.NVDA", "US.TSLA", "US.MSFT"]
            >>> report = manager.generate_premarket_watchlist(candidates)
            >>> print(f"生成 {report.total_count} 只股票的观察列表")
            >>> print(f"Top 推荐：{report.top_picks}")
        """
        entries = []

        # 1. 扫描候选股票池，计算各维度评分
        for symbol in candidate_symbols:
            try:
                # 使用 SignalFusion 生成信号
                # 注意：这里使用简化的 market_data，实际应传入真实数据
                market_data = {
                    'price': 100.0,  # 默认价格，实际应从数据源获取
                    'volume': 1000000,
                    'price_momentum': 50.0,
                    'volume_momentum': 50.0,
                    'catalyst_strength': 50.0,
                    'klines': [],
                }

                signal = self.fusion.generate_signal(symbol, market_data)

                # 创建观察列表条目
                entry = self._create_entry_from_signal(symbol, signal)
                entries.append(entry)

            except Exception as e:
                # 单个股票失败不影响其他股票
                print(f"警告：处理 {symbol} 失败：{e}")
                continue

        # 2. 按优先级排序 (降序)
        entries.sort(key=lambda e: e.priority_score, reverse=True)

        # 3. 选取 Top N (不超过 max_entries)
        selected_entries = entries[:self.max_entries]

        # 4. 更新观察列表
        self.watchlist = selected_entries

        # 5. 生成 Top 推荐 (Top 5 或更少)
        top_count = min(5, len(selected_entries))
        top_picks = [e.symbol for e in selected_entries[:top_count]]

        # 6. 生成报告
        report = WatchlistReport(
            entries=selected_entries,
            total_count=len(candidate_symbols),
            scan_time=time.time(),
            market_condition=self._determine_market_condition(selected_entries),
            top_picks=top_picks
        )

        return report

    def update_watchlist(self, market_data: Dict[str, Any]) -> WatchlistReport:
        """
        盘中动态更新观察列表

        流程:
        1. 更新现有条目的评分
        2. 移除失效标的 (is_stale=True)
        3. 添加新发现的机会
        4. 重新排序

        Args:
            market_data: 市场数据字典，键为股票代码，值为数据字典
                {
                    "US.AAPL": {
                        "price": 150.0,
                        "volume": 1000000,
                        ...
                    },
                    ...
                }

        Returns:
            WatchlistReport: 更新后的观察列表报告

        Example:
            >>> manager = WatchlistManager()
            >>> market_data = {
            ...     "US.AAPL": {"price": 151.0, "volume": 1200000},
            ...     "US.NVDA": {"price": 455.0, "volume": 800000}
            ... }
            >>> report = manager.update_watchlist(market_data)
        """
        updated_entries = []

        # 1. 更新现有条目
        for entry in self.watchlist:
            symbol = entry.symbol

            # 获取该股票的市场数据
            stock_data = market_data.get(symbol, {})
            current_price = stock_data.get('price', entry.entry_price)

            # 检测是否失效
            if self._is_price_stale(entry, current_price):
                entry.is_stale = True
            elif self._is_market_closed():
                entry.is_stale = True

            # 如果未失效，尝试更新评分
            if not entry.is_stale and stock_data:
                try:
                    # 重新生成信号以更新评分
                    signal = self.fusion.generate_signal(symbol, stock_data)

                    # 更新评分
                    entry.premarket_score = signal.premarket_score
                    entry.momentum_score = signal.momentum_score
                    entry.pattern_score = signal.pattern_score
                    entry.catalyst_score = signal.catalyst_score

                    # 重新计算优先级
                    entry.priority_score = self._calculate_priority_score(
                        signal.premarket_score,
                        signal.momentum_score,
                        signal.pattern_score,
                        signal.catalyst_score
                    )

                    # 更新价格信息
                    entry.entry_price = signal.entry_price
                    entry.stop_loss = signal.stop_loss
                    entry.target_price = signal.target_price
                except Exception:
                    # 更新失败，保持原值
                    pass

            updated_entries.append(entry)

        # 2. 移除失效标的
        active_entries = [e for e in updated_entries if not e.is_stale]

        # 3. 重新排序
        active_entries.sort(key=lambda e: e.priority_score, reverse=True)

        # 4. 更新观察列表
        self.watchlist = active_entries

        # 5. 生成 Top 推荐
        top_count = min(5, len(active_entries))
        top_picks = [e.symbol for e in active_entries[:top_count]]

        # 6. 生成报告
        report = WatchlistReport(
            entries=active_entries,
            total_count=len(active_entries),
            scan_time=time.time(),
            market_condition=self._determine_market_condition(active_entries),
            top_picks=top_picks
        )

        return report

    def add_symbol(self, symbol: str, scores: Dict[str, float]) -> bool:
        """
        添加股票到观察列表

        Args:
            symbol: 股票代码
            scores: 评分字典，包含:
                - premarket: 盘前评分 (0-1)
                - momentum: 动量评分 (0-1)
                - pattern: 形态评分 (0-1)
                - catalyst: 催化评分 (0-1)

        Returns:
            bool: 是否添加成功

        Example:
            >>> manager = WatchlistManager()
            >>> scores = {
            ...     'premarket': 0.8,
            ...     'momentum': 0.7,
            ...     'pattern': 0.9,
            ...     'catalyst': 0.6
            ... }
            >>> success = manager.add_symbol("US.AAPL", scores)
        """
        # 检查是否已存在
        if any(e.symbol == symbol for e in self.watchlist):
            return False

        # 计算优先级评分
        premarket = scores.get('premarket', 0.5)
        momentum = scores.get('momentum', 0.5)
        pattern = scores.get('pattern', 0.5)
        catalyst = scores.get('catalyst', 0.5)

        priority_score = self._calculate_priority_score(
            premarket, momentum, pattern, catalyst
        )

        # 创建新条目
        new_entry = WatchlistEntry(
            symbol=symbol,
            name=symbol,  # 默认使用代码作为名称
            priority_score=priority_score,
            premarket_score=premarket,
            momentum_score=momentum,
            pattern_score=pattern,
            catalyst_score=catalyst,
            entry_price=100.0,  # 默认价格
            stop_loss=95.0,
            target_price=110.0,
            added_at=time.time(),
            is_stale=False
        )

        # 添加到列表
        self.watchlist.append(new_entry)

        # 如果超过最大条目数，移除优先级最低的
        if len(self.watchlist) > self.max_entries:
            self.watchlist.sort(key=lambda e: e.priority_score, reverse=True)
            self.watchlist = self.watchlist[:self.max_entries]

        return True

    def remove_symbol(self, symbol: str) -> bool:
        """
        从观察列表移除股票

        Args:
            symbol: 股票代码

        Returns:
            bool: 是否移除成功

        Example:
            >>> manager = WatchlistManager()
            >>> success = manager.remove_symbol("US.AAPL")
        """
        original_length = len(self.watchlist)

        # 过滤掉要移除的股票
        self.watchlist = [e for e in self.watchlist if e.symbol != symbol]

        # 检查是否成功移除
        return len(self.watchlist) < original_length

    def get_top_picks(self, limit: int = 5) -> List[WatchlistEntry]:
        """
        获取 Top N 推荐

        按优先级评分降序返回 Top N 只股票。

        Args:
            limit: 返回数量限制，默认 5

        Returns:
            List[WatchlistEntry]: Top N 观察列表条目

        Example:
            >>> manager = WatchlistManager()
            >>> top_picks = manager.get_top_picks(limit=3)
            >>> for entry in top_picks:
            ...     print(f"{entry.symbol}: {entry.priority_score:.2f}")
        """
        # 按优先级降序排序
        sorted_entries = sorted(
            self.watchlist,
            key=lambda e: e.priority_score,
            reverse=True
        )

        # 返回 Top N
        return sorted_entries[:limit]

    def mark_stale(self, symbol: str) -> bool:
        """
        标记股票为失效

        Args:
            symbol: 股票代码

        Returns:
            bool: 是否标记成功

        Example:
            >>> manager = WatchlistManager()
            >>> success = manager.mark_stale("US.AAPL")
        """
        for entry in self.watchlist:
            if entry.symbol == symbol:
                entry.is_stale = True
                return True

        return False

    def cleanup_stale(self) -> int:
        """
        清理所有失效股票

        移除所有 is_stale=True 的条目。

        Returns:
            int: 清理的股票数量

        Example:
            >>> manager = WatchlistManager()
            >>> removed = manager.cleanup_stale()
            >>> print(f"清理了 {removed} 只失效股票")
        """
        original_count = len(self.watchlist)

        # 保留未失效的条目
        self.watchlist = [e for e in self.watchlist if not e.is_stale]

        removed_count = original_count - len(self.watchlist)
        return removed_count

    def get_entry(self, symbol: str) -> Optional[WatchlistEntry]:
        """
        获取指定股票的观察列表条目

        Args:
            symbol: 股票代码

        Returns:
            WatchlistEntry 或 None (如果不存在)

        Example:
            >>> manager = WatchlistManager()
            >>> entry = manager.get_entry("US.AAPL")
            >>> if entry:
            ...     print(f"{entry.symbol}: {entry.priority_score:.2f}")
        """
        for entry in self.watchlist:
            if entry.symbol == symbol:
                return entry
        return None

    def clear(self) -> None:
        """
        清空观察列表

        Example:
            >>> manager = WatchlistManager()
            >>> manager.clear()
        """
        self.watchlist = []

    def __len__(self) -> int:
        """返回观察列表中的股票数量"""
        return len(self.watchlist)

    def __str__(self) -> str:
        """返回观察列表的字符串表示"""
        return f"WatchlistManager({len(self.watchlist)}/{self.max_entries} entries)"

    def __repr__(self) -> str:
        """返回观察列表的详细表示"""
        return (
            f"WatchlistManager(max_entries={self.max_entries}, "
            f"watchlist_size={len(self.watchlist)})"
        )
