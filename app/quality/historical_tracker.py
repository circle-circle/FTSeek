"""
历史准确率追踪器 (Historical Tracker)

功能:
- 记录每个策略的历史信号结果
- 计算策略的胜率、盈亏比、质量加权胜率
- 提供历史准确率得分用于信号质量评估
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek.quality.historical_tracker")
logger.propagate = False


@dataclass
class SignalOutcome:
    """信号结果记录"""
    code: str                              # 股票代码
    strategy: str                          # 策略名称
    signal_id: str                         # 信号 ID
    pnl_pct: float                         # 盈亏百分比
    quality_score: float                   # 当时的质量分
    action: str                            # 买卖方向
    entry_time: str                        # 开仓时间
    exit_time: str = ""                    # 平仓时间
    market_regime: str = ""                # 当时的市场状态
    holding_periods: int = 0               # 持仓周期数


@dataclass
class StrategyStats:
    """策略统计数据"""
    strategy_name: str
    total_signals: int = 0
    winning_signals: int = 0
    losing_signals: int = 0
    avg_pnl: float = 0.0                   # 平均盈亏
    win_rate: float = 0.0                  # 胜率
    avg_win: float = 0.0                   # 平均盈利
    avg_loss: float = 0.0                  # 平均亏损
    profit_factor: float = 0.0             # 盈亏比
    avg_quality_score: float = 0.0         # 平均质量分
    quality_weighted_winrate: float = 0.0  # 质量加权胜率


class HistoricalTracker:
    """
    历史准确率追踪器

    使用 SQLite 数据库持久化存储信号结果
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        初始化追踪器

        Args:
            db_path: 数据库路径，默认使用 data/quality_history.db
        """
        if db_path is None:
            db_path = "/opt/ftseek/FTSeek/data/quality_history.db"

        self.db_path = db_path

        # 确保目录存在
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        # 内存缓存 {strategy: [SignalOutcome]}
        self._cache: Dict[str, List[SignalOutcome]] = {}
        self._cache_max_size = 1000  # 每个策略最多缓存 1000 条

        # 策略统计缓存 {strategy: StrategyStats}
        self._stats_cache: Dict[str, StrategyStats] = {}
        self._stats_cache_ttl = timedelta(minutes=5)
        self._stats_cache_time: Dict[str, datetime] = {}

        self._init_db()
        logger.info(f"历史准确率追踪器已初始化 | 数据库：{db_path}")

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    pnl_pct REAL NOT NULL,
                    quality_score REAL NOT NULL,
                    action TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    market_regime TEXT,
                    holding_periods INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_strategy ON signal_outcomes(strategy)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_code ON signal_outcomes(code)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_entry_time ON signal_outcomes(entry_time)
            ''')

            conn.commit()
        finally:
            conn.close()

    def record_outcome(
        self,
        code: str,
        strategy: str,
        signal_id: str,
        pnl_pct: float,
        quality_score: float,
        action: str = "BUY",
        entry_time: Optional[str] = None,
        exit_time: Optional[str] = None,
        market_regime: str = "",
        holding_periods: int = 0
    ):
        """
        记录信号结果

        Args:
            code: 股票代码
            strategy: 策略名称
            signal_id: 信号 ID
            pnl_pct: 盈亏百分比
            quality_score: 质量分
            action: 买卖方向
            entry_time: 开仓时间
            exit_time: 平仓时间
            market_regime: 市场状态
            holding_periods: 持仓周期数
        """
        if entry_time is None:
            entry_time = datetime.now().isoformat()
        if exit_time is None:
            exit_time = datetime.now().isoformat()

        outcome = SignalOutcome(
            code=code,
            strategy=strategy,
            signal_id=signal_id,
            pnl_pct=pnl_pct,
            quality_score=quality_score,
            action=action,
            entry_time=entry_time,
            exit_time=exit_time,
            market_regime=market_regime,
            holding_periods=holding_periods,
        )

        # 更新内存缓存
        if strategy not in self._cache:
            self._cache[strategy] = []

        self._cache[strategy].append(outcome)

        # 限制缓存大小
        if len(self._cache[strategy]) > self._cache_max_size:
            self._cache[strategy] = self._cache[strategy][-self._cache_max_size:]

        # 写入数据库
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO signal_outcomes
                (code, strategy, signal_id, pnl_pct, quality_score, action, entry_time, exit_time, market_regime, holding_periods)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, strategy, signal_id, pnl_pct, quality_score, action, entry_time, exit_time, market_regime, holding_periods))
            conn.commit()
        finally:
            conn.close()

        # 使统计缓存失效
        self._stats_cache.pop(strategy, None)

        logger.debug(f"[结果记录] {code} | {strategy} | 盈亏={pnl_pct:.2%} | 质量={quality_score:.3f}")

    def get_strategy_accuracy(self, strategy: str) -> float:
        """
        获取策略的历史准确率

        Args:
            strategy: 策略名称

        Returns:
            准确率得分 (0-1)，无数据返回 0.5
        """
        stats = self.get_strategy_stats(strategy)
        return stats.quality_weighted_winrate

    def get_strategy_stats(self, strategy: str) -> StrategyStats:
        """
        获取策略统计数据

        Args:
            strategy: 策略名称

        Returns:
            策略统计数据
        """
        # 检查缓存
        now = datetime.now()
        if strategy in self._stats_cache:
            cache_time = self._stats_cache_time.get(strategy)
            if cache_time and (now - cache_time) < self._stats_cache_ttl:
                return self._stats_cache[strategy]

        # 从数据库统计
        stats = self._calculate_strategy_stats(strategy)

        # 更新缓存
        self._stats_cache[strategy] = stats
        self._stats_cache_time[strategy] = now

        return stats

    def _calculate_strategy_stats(self, strategy: str) -> StrategyStats:
        """
        从数据库计算策略统计

        Args:
            strategy: 策略名称

        Returns:
            策略统计数据
        """
        stats = StrategyStats(strategy_name=strategy)

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # 获取所有信号
            cursor.execute('''
                SELECT pnl_pct, quality_score FROM signal_outcomes
                WHERE strategy = ?
                ORDER BY entry_time DESC
                LIMIT 100
            ''', (strategy,))

            rows = cursor.fetchall()

            if not rows:
                # 无数据，返回默认值
                stats.win_rate = 0.5
                stats.quality_weighted_winrate = 0.5
                return stats

            # 计算统计
            stats.total_signals = len(rows)
            pnl_list = [r[0] for r in rows]
            quality_list = [r[1] for r in rows]

            # 胜率
            winning = [p for p in pnl_list if p > 0]
            losing = [p for p in pnl_list if p <= 0]
            stats.winning_signals = len(winning)
            stats.losing_signals = len(losing)
            stats.win_rate = len(winning) / len(pnl_list) if pnl_list else 0

            # 平均盈亏
            stats.avg_pnl = sum(pnl_list) / len(pnl_list)

            # 平均盈利和亏损
            stats.avg_win = sum(winning) / len(winning) if winning else 0
            stats.avg_loss = abs(sum(losing) / len(losing)) if losing else 0

            # 盈亏比
            if stats.avg_loss > 0:
                stats.profit_factor = stats.avg_win / stats.avg_loss

            # 平均质量分
            stats.avg_quality_score = sum(quality_list) / len(quality_list)

            # 质量加权胜率 (高质量信号的胜率权重更高)
            # 公式：Σ(质量分 * 信号结果) / Σ质量分
            # 信号结果：赢=1, 输=0
            weighted_sum = 0
            quality_sum = 0
            for pnl, quality in zip(pnl_list, quality_list):
                outcome = 1 if pnl > 0 else 0
                weighted_sum += quality * outcome
                quality_sum += quality

            stats.quality_weighted_winrate = weighted_sum / quality_sum if quality_sum > 0 else 0.5

        finally:
            conn.close()

        return stats

    def get_recent_signals(self, strategy: str, limit: int = 20) -> List[SignalOutcome]:
        """
        获取最近的信号结果

        Args:
            strategy: 策略名称
            limit: 返回数量

        Returns:
            信号结果列表
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT code, strategy, signal_id, pnl_pct, quality_score, action,
                       entry_time, exit_time, market_regime, holding_periods
                FROM signal_outcomes
                WHERE strategy = ?
                ORDER BY entry_time DESC
                LIMIT ?
            ''', (strategy, limit))

            rows = cursor.fetchall()
            return [
                SignalOutcome(
                    code=r[0],
                    strategy=r[1],
                    signal_id=r[2],
                    pnl_pct=r[3],
                    quality_score=r[4],
                    action=r[5],
                    entry_time=r[6],
                    exit_time=r[7],
                    market_regime=r[8],
                    holding_periods=r[9],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def clear_old_data(self, days: int = 90) -> int:
        """
        清理旧数据

        Args:
            days: 保留最近 N 天的数据

        Returns:
            删除的记录数
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute('''
                DELETE FROM signal_outcomes WHERE entry_time < ?
            ''', (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
            logger.info(f"已清理 {deleted} 条 {days} 天前的数据")
            return deleted
        finally:
            conn.close()
