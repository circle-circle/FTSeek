"""
交易归因引擎 (Trade Attribution Engine) - FTSeek v5.0

基于 GPT 模型分析建议实现的深度归因系统，支持：
1. 策略归因 - 哪个策略真的赚钱
2. 市场状态归因 - 哪些市场阶段适合哪个策略
3. 执行质量归因 - 滑点/延迟对收益的影响
4. 新闻影响归因 - 新闻事件对交易结果的贡献
5. 共识权重归因 - 各策略投票权重的合理性分析

核心数据模型:
- 信号层 → 决策层 → 执行层 → 结果层 (TradeChain 四层架构)
- 归因分析基于完整链路数据进行多维度拆解
"""
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
import json

from app import config
from app.storage.database import get_database, get_sqlite_connection

logger = logging.getLogger("ftseek.analytics.attribution")
logger.propagate = False


# ==================== 归因数据类 ====================

@dataclass
class StrategyAttribution:
    """策略归因结果"""
    strategy_name: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_pnl_percent: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0
    avg_holding_period: int = 0  # 平均持仓分钟数
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0

    # 质量评分 (0-100)
    quality_score: float = 0.0

    # 市场状态适配度
    regime_fit: Dict[str, float] = field(default_factory=dict)  # regime -> win_rate

    # 置信度校准
    avg_confidence: float = 0.0
    confidence_calibration: float = 0.0  # 置信度 vs 实际胜率的相关性

    def to_dict(self) -> Dict:
        return {
            "strategy_name": self.strategy_name,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": round(self.avg_pnl, 2),
            "avg_pnl_percent": round(self.avg_pnl_percent, 4),
            "max_win": round(self.max_win, 2),
            "max_loss": round(self.max_loss, 2),
            "avg_holding_period": self.avg_holding_period,
            "profit_factor": round(self.profit_factor, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "quality_score": round(self.quality_score, 2),
            "regime_fit": self.regime_fit,
            "avg_confidence": round(self.avg_confidence, 4),
            "confidence_calibration": round(self.confidence_calibration, 4),
        }


@dataclass
class MarketRegimeAttribution:
    """市场状态归因结果"""
    regime_name: str
    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl_percent: float = 0.0

    # 各策略在该市场状态下的表现
    strategy_performance: Dict[str, Dict] = field(default_factory=dict)

    # 最佳策略
    best_strategy: str = ""
    worst_strategy: str = ""

    def to_dict(self) -> Dict:
        return {
            "regime_name": self.regime_name,
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl_percent": round(self.avg_pnl_percent, 4),
            "strategy_performance": self.strategy_performance,
            "best_strategy": self.best_strategy,
            "worst_strategy": self.worst_strategy,
        }


@dataclass
class ExecutionAttribution:
    """执行质量归因结果"""
    total_trades: int = 0
    avg_slippage_bps: float = 0.0
    avg_latency_ms: int = 0
    total_cost: float = 0.0
    cost_drag: float = 0.0  # 成本拖累 (总成本/总收益)

    # 滑点分布
    slippage_distribution: Dict[str, int] = field(default_factory=dict)

    # 滑点对盈亏的影响
    slippage_impact: float = 0.0  # 滑点导致的盈亏差异

    # 订单类型对比
    market_order_performance: Dict = field(default_factory=dict)
    limit_order_performance: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "total_trades": self.total_trades,
            "avg_slippage_bps": round(self.avg_slippage_bps, 2),
            "avg_latency_ms": self.avg_latency_ms,
            "total_cost": round(self.total_cost, 2),
            "cost_drag": round(self.cost_drag, 4),
            "slippage_distribution": self.slippage_distribution,
            "slippage_impact": round(self.slippage_impact, 2),
            "market_order_performance": self.market_order_performance,
            "limit_order_performance": self.limit_order_performance,
        }


@dataclass
class ConsensusAttribution:
    """共识权重归因结果"""
    strategy_name: str
    weight: float = 0.0
    contribution: float = 0.0  # 对最终得分的贡献
    accuracy_when_followed: float = 0.0  # 跟随该策略时的胜率
    accuracy_when_ignored: float = 0.0  # 忽略该策略时的胜率
    weight_recommendation: float = 0.0  # 建议权重

    def to_dict(self) -> Dict:
        return {
            "strategy_name": self.strategy_name,
            "weight": round(self.weight, 4),
            "contribution": round(self.contribution, 4),
            "accuracy_when_followed": round(self.accuracy_when_followed, 4),
            "accuracy_when_ignored": round(self.accuracy_when_ignored, 4),
            "weight_recommendation": round(self.weight_recommendation, 4),
        }


@dataclass
class NewsAttribution:
    """新闻影响归因结果"""
    total_trades_with_news: int = 0
    total_trades_without_news: int = 0
    win_rate_with_news: float = 0.0
    win_rate_without_news: float = 0.0
    pnl_with_news: float = 0.0
    pnl_without_news: float = 0.0

    # 新闻情绪影响
    positive_news_performance: Dict = field(default_factory=dict)
    negative_news_performance: Dict = field(default_factory=dict)

    # 新闻驱动交易的额外收益
    news_premium: float = 0.0  # 有新闻 vs 无新闻的收益差

    def to_dict(self) -> Dict:
        return {
            "total_trades_with_news": self.total_trades_with_news,
            "total_trades_without_news": self.total_trades_without_news,
            "win_rate_with_news": round(self.win_rate_with_news, 4),
            "win_rate_without_news": round(self.win_rate_without_news, 4),
            "pnl_with_news": round(self.pnl_with_news, 2),
            "pnl_without_news": round(self.pnl_without_news, 2),
            "positive_news_performance": self.positive_news_performance,
            "negative_news_performance": self.negative_news_performance,
            "news_premium": round(self.news_premium, 2),
        }


# ==================== 归因引擎主类 ====================

class AttributionEngine:
    """
    交易归因引擎

    基于 TradeChain 四层架构，对交易结果进行多维度归因分析
    """

    def __init__(self, db_path: str = None):
        """
        初始化归因引擎

        Args:
            db_path: 数据库路径，默认使用配置目录
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = config.DATA_DIR / "ftseek.db"

        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 初始化归因专用表
        self._init_attribution_tables()

        logger.info(f"交易归因引擎已初始化：{self.db_path}")

    def _init_attribution_tables(self):
        """初始化归因专用表"""
        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()

            # 策略表现归因表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_attribution (
                    id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate REAL,
                    total_pnl REAL,
                    avg_pnl REAL,
                    avg_pnl_percent REAL,
                    profit_factor REAL,
                    sharpe_ratio REAL,
                    max_drawdown REAL,
                    quality_score REAL,
                    regime_fit TEXT,  -- JSON 字符串
                    avg_confidence REAL,
                    confidence_calibration REAL,
                    computed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategy_attribution_name
                ON strategy_attribution(strategy_name)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategy_attribution_period
                ON strategy_attribution(period_start, period_end)
            """)

            # 市场状态归因表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_regime_attribution (
                    id TEXT PRIMARY KEY,
                    regime_name TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_trades INTEGER DEFAULT 0,
                    win_rate REAL,
                    total_pnl REAL,
                    avg_pnl_percent REAL,
                    strategy_performance TEXT,  -- JSON 字符串
                    best_strategy TEXT,
                    worst_strategy TEXT,
                    computed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_regime_attribution_name
                ON market_regime_attribution(regime_name)
            """)

            # 执行质量归因表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_attribution (
                    id TEXT PRIMARY KEY,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_trades INTEGER DEFAULT 0,
                    avg_slippage_bps REAL,
                    avg_latency_ms INTEGER,
                    total_cost REAL,
                    cost_drag REAL,
                    slippage_distribution TEXT,  -- JSON 字符串
                    slippage_impact REAL,
                    market_order_performance TEXT,  -- JSON 字符串
                    limit_order_performance TEXT,  -- JSON 字符串
                    computed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 共识权重归因表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS consensus_attribution (
                    id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    weight REAL,
                    contribution REAL,
                    accuracy_when_followed REAL,
                    accuracy_when_ignored REAL,
                    weight_recommendation REAL,
                    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(strategy_name, period_start, period_end)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_consensus_attribution_name
                ON consensus_attribution(strategy_name)
            """)

            # 新闻影响归因表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS news_attribution (
                    id TEXT PRIMARY KEY,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_trades_with_news INTEGER DEFAULT 0,
                    total_trades_without_news INTEGER DEFAULT 0,
                    win_rate_with_news REAL,
                    win_rate_without_news REAL,
                    pnl_with_news REAL,
                    pnl_without_news REAL,
                    positive_news_performance TEXT,  -- JSON 字符串
                    negative_news_performance TEXT,  -- JSON 字符串
                    news_premium REAL,
                    computed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            logger.info("归因专用表结构初始化完成")

    # ==================== 策略归因分析 ====================

    def analyze_strategy_performance(
        self,
        start_date: str = None,
        end_date: str = None,
        days: int = 30
    ) -> List[StrategyAttribution]:
        """
        分析各策略的表现

        Args:
            start_date: 开始日期 (ISO 格式)
            end_date: 结束日期 (ISO 格式)
            days: 查询天数范围（当 start_date 为 None 时使用）

        Returns:
            策略归因结果列表
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
        if not end_date:
            end_date = datetime.now().isoformat()

        results = []

        # 从 signal_outcomes 表获取数据
        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()

            # 获取各策略的基础统计
            cursor.execute("""
                SELECT
                    strategy,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN pnl_pct < 0 THEN 1 ELSE 0 END) as losing_trades,
                    AVG(pnl_pct) as avg_pnl_pct,
                    SUM(pnl_pct) as total_pnl_pct,
                    MAX(pnl_pct) as max_win,
                    MIN(pnl_pct) as max_loss,
                    AVG(holding_periods) as avg_holding_periods,
                    AVG(quality_score) as avg_quality_score,
                    AVG(sentiment_score) as avg_sentiment,
                    AVG(consensus_score) as avg_consensus
                FROM signal_outcomes
                WHERE timestamp BETWEEN ? AND ?
                GROUP BY strategy
                ORDER BY total_trades DESC
            """, (start_date, end_date))

            strategy_stats = cursor.fetchall()

            for row in strategy_stats:
                stats = dict(row)
                total = stats['winning_trades'] + stats['losing_trades']
                win_rate = stats['winning_trades'] / total if total > 0 else 0

                # 计算盈亏比
                cursor.execute("""
                    SELECT
                        COALESCE(SUM(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE 0 END), 0) as total_profit,
                        ABS(COALESCE(SUM(CASE WHEN pnl_pct < 0 THEN pnl_pct ELSE 0 END), 0)) as total_loss
                    FROM signal_outcomes
                    WHERE strategy = ? AND timestamp BETWEEN ? AND ?
                """, (stats['strategy'], start_date, end_date))

                pf_row = dict(cursor.fetchone())
                total_loss = pf_row['total_loss'] or 0
                profit_factor = (pf_row['total_profit'] or 0) / total_loss if total_loss > 0 else 0

                # 计算市场状态适配度
                cursor.execute("""
                    SELECT
                        market_regime,
                        COUNT(*) as trades,
                        AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) as win_rate
                    FROM signal_outcomes
                    WHERE strategy = ? AND timestamp BETWEEN ? AND ?
                    GROUP BY market_regime
                """, (stats['strategy'], start_date, end_date))

                regime_fit = {row['market_regime']: round(row['win_rate'], 4)
                             for row in cursor.fetchall()}

                # 计算置信度校准
                cursor.execute("""
                    SELECT
                        AVG(consensus_score) as avg_confidence,
                        AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) as actual_win_rate
                    FROM signal_outcomes
                    WHERE strategy = ? AND timestamp BETWEEN ? AND ?
                """, (stats['strategy'], start_date, end_date))

                cal_row = dict(cursor.fetchone())
                confidence_calibration = cal_row['actual_win_rate'] - (cal_row['avg_confidence'] or 0)

                # 创建归因结果
                attribution = StrategyAttribution(
                    strategy_name=stats['strategy'],
                    total_trades=stats['total_trades'] or 0,
                    winning_trades=stats['winning_trades'] or 0,
                    losing_trades=stats['losing_trades'] or 0,
                    win_rate=win_rate,
                    total_pnl=stats['total_pnl_pct'] or 0,
                    avg_pnl=stats['avg_pnl_pct'] or 0,
                    avg_pnl_percent=stats['avg_pnl_pct'] or 0,
                    max_win=stats['max_win'] or 0,
                    max_loss=stats['max_loss'] or 0,
                    avg_holding_period=int(stats['avg_holding_periods'] or 0),
                    profit_factor=profit_factor,
                    quality_score=stats['avg_quality_score'] or 0,
                    regime_fit=regime_fit,
                    avg_confidence=cal_row['avg_confidence'] or 0,
                    confidence_calibration=confidence_calibration,
                )

                results.append(attribution)

        return results

    # ==================== 市场状态归因分析 ====================

    def analyze_market_regime_performance(
        self,
        start_date: str = None,
        end_date: str = None,
        days: int = 30
    ) -> List[MarketRegimeAttribution]:
        """
        分析各市场状态下的表现

        Args:
            start_date: 开始日期
            end_date: 结束日期
            days: 查询天数范围

        Returns:
            市场状态归因结果列表
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
        if not end_date:
            end_date = datetime.now().isoformat()

        results = []

        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()

            # 获取各市场状态的基础统计
            cursor.execute("""
                SELECT
                    market_regime,
                    COUNT(*) as total_trades,
                    AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                    SUM(pnl_pct) as total_pnl,
                    AVG(pnl_pct) as avg_pnl
                FROM signal_outcomes
                WHERE timestamp BETWEEN ? AND ?
                GROUP BY market_regime
                ORDER BY total_trades DESC
            """, (start_date, end_date))

            regime_stats = cursor.fetchall()

            for row in regime_stats:
                stats = dict(row)
                regime = stats['market_regime']

                # 获取各策略在该市场状态下的表现
                cursor.execute("""
                    SELECT
                        strategy,
                        COUNT(*) as trades,
                        AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                        AVG(pnl_pct) as avg_pnl
                    FROM signal_outcomes
                    WHERE market_regime = ? AND timestamp BETWEEN ? AND ?
                    GROUP BY strategy
                    ORDER BY avg_pnl DESC
                """, (regime, start_date, end_date))

                strategy_perf = {}
                best_strategy = ""
                best_pnl = float('-inf')
                worst_strategy = ""
                worst_pnl = float('inf')

                for s_row in cursor.fetchall():
                    s_stats = dict(s_row)
                    strategy_name = s_stats['strategy']
                    strategy_perf[strategy_name] = {
                        'trades': s_stats['trades'],
                        'win_rate': round(s_stats['win_rate'], 4),
                        'avg_pnl': round(s_stats['avg_pnl'] or 0, 4),
                    }

                    pnl = s_stats['avg_pnl'] or 0
                    if pnl > best_pnl:
                        best_pnl = pnl
                        best_strategy = strategy_name
                    if pnl < worst_pnl:
                        worst_pnl = pnl
                        worst_strategy = strategy_name

                attribution = MarketRegimeAttribution(
                    regime_name=regime,
                    total_trades=stats['total_trades'] or 0,
                    win_rate=stats['win_rate'] or 0,
                    total_pnl=stats['total_pnl'] or 0,
                    avg_pnl_percent=stats['avg_pnl'] or 0,
                    strategy_performance=strategy_perf,
                    best_strategy=best_strategy,
                    worst_strategy=worst_strategy,
                )

                results.append(attribution)

        return results

    # ==================== 执行质量归因分析 ====================

    def analyze_execution_quality(
        self,
        start_date: str = None,
        end_date: str = None,
        days: int = 30
    ) -> ExecutionAttribution:
        """
        分析执行质量

        Args:
            start_date: 开始日期
            end_date: 结束日期
            days: 查询天数范围

        Returns:
            执行质量归因结果
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
        if not end_date:
            end_date = datetime.now().isoformat()

        # 从 trade_chains 中获取执行层数据
        # 由于 TradeChain 存储在 JSON 文件中，需要从 SQLite 的 trades 表获取近似数据

        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()

            # 获取基础执行统计
            cursor.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    COUNT(CASE WHEN status = 'closed' THEN 1 END) as closed_trades,
                    COALESCE(SUM(fees), 0) as total_cost,
                    COALESCE(SUM(pnl), 0) as total_pnl
                FROM trades
                WHERE entry_time BETWEEN ? AND ?
            """, (start_date, end_date))

            stats = dict(cursor.fetchone())

            # 计算成本拖累
            total_pnl = stats['total_pnl'] or 0
            total_cost = stats['total_cost'] or 0
            cost_drag = total_cost / abs(total_pnl) if total_pnl != 0 else 0

            attribution = ExecutionAttribution(
                total_trades=stats['total_trades'] or 0,
                avg_slippage_bps=0,  # 需要从 TradeChain JSON 文件中解析
                avg_latency_ms=0,
                total_cost=total_cost,
                cost_drag=cost_drag,
            )

            # 注：详细的滑点分布需要从 TradeChain 文件中获取
            # 这里提供框架，实际使用时可扩展

            return attribution

    # ==================== 共识权重归因分析 ====================

    def analyze_consensus_weights(
        self,
        start_date: str = None,
        end_date: str = None,
        days: int = 30
    ) -> List[ConsensusAttribution]:
        """
        分析共识权重的合理性

        Args:
            start_date: 开始日期
            end_date: 结束日期
            days: 查询天数范围

        Returns:
            共识权重归因结果列表
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
        if not end_date:
            end_date = datetime.now().isoformat()

        results = []

        # 需要分析每个策略的投票与实际结果的关系
        # 由于当前数据结构限制，这里提供框架实现

        # 从 weight_calculator 获取当前权重配置
        try:
            from app.consensus.weight_calculator import DEFAULT_WEIGHTS
            strategies = list(DEFAULT_WEIGHTS.keys())
        except ImportError:
            strategies = ['confluence_v2', 'supertrend', 'sentiment_ai']

        for strategy in strategies:
            # 计算跟随该策略时的胜率 vs 忽略时的胜率
            # 这里需要更复杂的数据关联，暂时返回框架结构

            attribution = ConsensusAttribution(
                strategy_name=strategy,
                weight=1.0,  # 从 weight_calculator 获取
                contribution=0.0,
                accuracy_when_followed=0.0,
                accuracy_when_ignored=0.0,
                weight_recommendation=1.0,
            )
            results.append(attribution)

        return results

    # ==================== 新闻影响归因分析 ====================

    def analyze_news_impact(
        self,
        start_date: str = None,
        end_date: str = None,
        days: int = 30
    ) -> NewsAttribution:
        """
        分析新闻对交易结果的影响

        Args:
            start_date: 开始日期
            end_date: 结束日期
            days: 查询天数范围

        Returns:
            新闻影响归因结果
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
        if not end_date:
            end_date = datetime.now().isoformat()

        # 从 signal_outcomes 和 news 表关联分析
        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()

            # 获取有新闻时的交易统计
            cursor.execute("""
                SELECT
                    COUNT(DISTINCT o.code || o.timestamp) as total_trades,
                    AVG(CASE WHEN o.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                    SUM(o.pnl_pct) as total_pnl
                FROM signal_outcomes o
                INNER JOIN news n ON o.code = n.code
                WHERE o.timestamp BETWEEN ? AND ?
                AND n.published_at <= o.timestamp
                AND (o.timestamp - n.published_at) <= 86400  -- 24 小时内
            """, (start_date, end_date))

            with_news_stats = dict(cursor.fetchone())

            # 获取无新闻时的交易统计
            cursor.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                    SUM(pnl_pct) as total_pnl
                FROM signal_outcomes
                WHERE timestamp BETWEEN ? AND ?
                AND code NOT IN (
                    SELECT DISTINCT code FROM news
                    WHERE published_at BETWEEN ? AND ?
                )
            """, (start_date, end_date, start_date, end_date))

            without_news_stats = dict(cursor.fetchone())

            # 计算新闻溢价
            pnl_with_news = with_news_stats['total_pnl'] or 0
            pnl_without_news = without_news_stats['total_pnl'] or 0
            trades_with = with_news_stats['total_trades'] or 1
            trades_without = without_news_stats['total_trades'] or 1

            avg_pnl_with = pnl_with_news / trades_with
            avg_pnl_without = pnl_without_news / trades_without
            news_premium = avg_pnl_with - avg_pnl_without

            attribution = NewsAttribution(
                total_trades_with_news=with_news_stats['total_trades'] or 0,
                total_trades_without_news=without_news_stats['total_trades'] or 0,
                win_rate_with_news=with_news_stats['win_rate'] or 0,
                win_rate_without_news=without_news_stats['win_rate'] or 0,
                pnl_with_news=pnl_with_news,
                pnl_without_news=pnl_without_news,
                news_premium=news_premium,
            )

            return attribution

    # ==================== 综合归因报告 ====================

    def generate_attribution_report(
        self,
        period: str = "30d",
        include_strategy: bool = True,
        include_regime: bool = True,
        include_execution: bool = True,
        include_consensus: bool = True,
        include_news: bool = True,
    ) -> Dict[str, Any]:
        """
        生成综合归因报告

        Args:
            period: 时间周期 (7d/30d/90d)
            include_strategy: 是否包含策略归因
            include_regime: 是否包含市场状态归因
            include_execution: 是否包含执行质量归因
            include_consensus: 是否包含共识权重归因
            include_news: 是否包含新闻影响归因

        Returns:
            综合归因报告字典
        """
        # 解析周期
        days_map = {'7d': 7, '30d': 30, '90d': 90}
        days = days_map.get(period, 30)

        start_date = (datetime.now() - timedelta(days=days)).isoformat()
        end_date = datetime.now().isoformat()

        report = {
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "generated_at": datetime.now().isoformat(),
        }

        # 策略归因
        if include_strategy:
            strategy_results = self.analyze_strategy_performance(start_date, end_date)
            report["strategy_attribution"] = [s.to_dict() for s in strategy_results]

            # 计算策略质量评分排名
            sorted_strategies = sorted(strategy_results, key=lambda x: x.quality_score, reverse=True)
            if sorted_strategies:
                report["best_strategy"] = sorted_strategies[0].strategy_name
                report["worst_strategy"] = sorted_strategies[-1].strategy_name

        # 市场状态归因
        if include_regime:
            regime_results = self.analyze_market_regime_performance(start_date, end_date)
            report["market_regime_attribution"] = [r.to_dict() for r in regime_results]

        # 执行质量归因
        if include_execution:
            execution_result = self.analyze_execution_quality(start_date, end_date)
            report["execution_attribution"] = execution_result.to_dict()

        # 共识权重归因
        if include_consensus:
            consensus_results = self.analyze_consensus_weights(start_date, end_date)
            report["consensus_attribution"] = [c.to_dict() for c in consensus_results]

        # 新闻影响归因
        if include_news:
            news_result = self.analyze_news_impact(start_date, end_date)
            report["news_attribution"] = news_result.to_dict()

        # 关键发现
        report["key_insights"] = self._generate_key_insights(report)

        return report

    def _generate_key_insights(self, report: Dict) -> List[str]:
        """生成关键发现"""
        insights = []

        # 策略相关发现
        if "strategy_attribution" in report and report["strategy_attribution"]:
            strategies = report["strategy_attribution"]
            best = max(strategies, key=lambda x: x.get('win_rate', 0))
            worst = min(strategies, key=lambda x: x.get('win_rate', 0))

            if best['win_rate'] > 0.6:
                insights.append(f"最佳策略：{best['strategy_name']} (胜率{best['win_rate']:.0%})")
            if worst['win_rate'] < 0.4:
                insights.append(f"需优化策略：{worst['strategy_name']} (胜率{worst['win_rate']:.0%})")

        # 市场状态相关发现
        if "market_regime_attribution" in report and report["market_regime_attribution"]:
            regimes = report["market_regime_attribution"]
            best_regime = max(regimes, key=lambda x: x.get('win_rate', 0))

            if best_regime['best_strategy']:
                insights.append(
                    f"{best_regime['regime_name']} 市场下，"
                    f"{best_regime['best_strategy']} 表现最佳"
                )

        # 执行质量相关发现
        if "execution_attribution" in report:
            exec_attr = report["execution_attribution"]
            if exec_attr.get('cost_drag', 0) > 0.1:
                insights.append(
                    f"成本拖累较高 ({exec_attr['cost_drag']:.1%}), "
                    f"考虑优化执行策略"
                )

        # 新闻影响相关发现
        if "news_attribution" in report:
            news_attr = report["news_attribution"]
            if abs(news_attr.get('news_premium', 0)) > 0.02:
                direction = "正向" if news_attr['news_premium'] > 0 else "负向"
                insights.append(
                    f"新闻对交易有{direction}影响 "
                    f"(溢价{news_attr['news_premium']:.2%})"
                )

        return insights

    # ==================== 策略权重建议 ====================



    # ============================================================
    #  Merged from strategy_attribution.py - 策略维度归因分析
    # ============================================================

    def analyze_strategy_by_regime_cross(self, start_date: str = None, end_date: str = None,
                                         days: int = 30) -> Dict[Tuple[str, str], Dict]:
        """
        策略×市场状态交叉分析 (from strategy_attribution.py)
        
        Returns:
            Dict[Tuple[str, str], Dict]: (策略，市场状态) -> 指标
        """
        if not start_date:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
        if not end_date:
            end_date = datetime.now().isoformat()

        results = {}
        
        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()
            
            # 从 trade_chains 获取数据
            cursor.execute("""
                SELECT 
                    json_extract(decision_layer, '\$.decision_reason') as strategy,
                    json_extract(signal_layer, '\$.market_regime') as regime,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN json_extract(result_layer, '\$.pnl') > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(COALESCE(json_extract(result_layer, '\$.pnl'), 0)) as total_pnl
                FROM trade_chains
                WHERE is_closed = 1 AND created_at BETWEEN ? AND ?
                GROUP BY strategy, regime
                ORDER BY total_trades DESC
            """, (start_date, end_date))
            
            for row in cursor.fetchall():
                strategy = row[0] or 'unknown'
                regime = row[1] or 'unknown'
                total_trades = row[2] or 0
                winning_trades = row[3] or 0
                total_pnl = row[4] or 0
                
                win_rate = winning_trades / total_trades if total_trades > 0 else 0
                avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
                
                key = (strategy, regime)
                results[key] = {
                    'strategy': strategy,
                    'regime': regime,
                    'total_trades': total_trades,
                    'winning_trades': winning_trades,
                    'total_pnl': round(total_pnl, 2),
                    'win_rate': round(win_rate, 4),
                    'avg_pnl': round(avg_pnl, 2),
                }
        
        return results

    def identify_noise_signals(self, min_freq: int = 3, max_win_rate: float = 0.35,
                               days: int = 90) -> List[Dict]:
        """
        识别高噪声信号 (from strategy_attribution.py)
        
        标准:
        - 信号频率 >= min_freq
        - 胜率 <= max_win_rate
        
        Returns:
            List[Dict]: 噪声信号列表
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    json_extract(decision_layer, '\$.decision_reason') as signal,
                    COUNT(*) as total,
                    SUM(CASE WHEN json_extract(result_layer, '\$.pnl') > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(COALESCE(json_extract(result_layer, '\$.pnl'), 0)) as total_pnl
                FROM trade_chains
                WHERE is_closed = 1 AND created_at > ?
                GROUP BY signal
                HAVING total >= ?
            """, (cutoff, min_freq))
            
            noise_signals = []
            for row in cursor.fetchall():
                signal = row[0] or 'unknown'
                total = row[1] or 0
                wins = row[2] or 0
                total_pnl = row[3] or 0
                
                win_rate = wins / total if total > 0 else 0
                
                if win_rate <= max_win_rate:
                    noise_signals.append({
                        'signal': signal,
                        'total_trades': total,
                        'win_rate': round(win_rate, 4),
                        'total_pnl': round(total_pnl, 2),
                        'avg_pnl': round(total_pnl / total, 2) if total > 0 else 0,
                        'reason': f'高频低胜率 (win_rate={win_rate:.0%})',
                    })
            
            return sorted(noise_signals, key=lambda x: x['total_pnl'])

    def identify_overfit_signals(self, days: int = 90) -> List[Dict]:
        """
        识别过拟合信号 (from strategy_attribution.py)
        
        标准:
        - 在特定市场状态过度集中 (>80%)
        - 在其他市场状态表现显著下降
        
        Returns:
            List[Dict]: 过拟合信号列表
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        with get_sqlite_connection(str(self.db_path)) as conn:
            cursor = conn.cursor()
            
            # 获取策略×市场状态交叉统计
            cursor.execute("""
                SELECT 
                    json_extract(decision_layer, '\$.decision_reason') as strategy,
                    json_extract(signal_layer, '\$.market_regime') as regime,
                    COUNT(*) as count,
                    SUM(COALESCE(json_extract(result_layer, '\$.pnl'), 0)) as pnl
                FROM trade_chains
                WHERE is_closed = 1 AND created_at > ?
                GROUP BY strategy, regime
            """, (cutoff,))
            
            # 按策略分组
            strategy_data = {}
            for row in cursor.fetchall():
                strategy = row[0] or 'unknown'
                regime = row[1] or 'unknown'
                count = row[2] or 0
                pnl = row[3] or 0
                
                if strategy not in strategy_data:
                    strategy_data[strategy] = {'total': 0, 'regimes': {}}
                strategy_data[strategy]['total'] += count
                strategy_data[strategy]['regimes'][regime] = {'count': count, 'pnl': pnl}
            
            overfit_signals = []
            for strategy, data in strategy_data.items():
                if data['total'] < 5:  # 样本太少，跳过
                    continue
                
                # 找出集中度最高的市场状态
                max_regime = max(data['regimes'].items(), key=lambda x: x[1]['count'])
                max_ratio = max_regime[1]['count'] / data['total']
                
                if max_ratio > 0.8:
                    concentrated_pnl = max_regime[1]['pnl']
                    other_pnl = sum(r['pnl'] for r, v in data['regimes'].items() if r != max_regime[0])
                    other_count = data['total'] - max_regime[1]['count']
                    
                    overfit_signals.append({
                        'signal': strategy,
                        'concentrated_regime': max_regime[0],
                        'concentration_ratio': round(max_ratio, 4),
                        'trades_in_regime': max_regime[1]['count'],
                        'total_trades': data['total'],
                        'avg_pnl_in_regime': round(concentrated_pnl / max_regime[1]['count'], 2) if max_regime[1]['count'] > 0 else 0,
                        'avg_pnl_other': round(other_pnl / other_count, 2) if other_count > 0 else 0,
                        'reason': f'过度集中于{max_regime[0]} ({max_ratio:.0%})',
                    })
            
            return sorted(overfit_signals, key=lambda x: x['concentration_ratio'], reverse=True)

    def get_full_attribution_report(self, period: str = "30d") -> Dict:
        """
        获取完整归因报告 (整合所有分析维度)
        
        Returns:
            Dict: 包含所有维度的分析报告
        """
        days_map = {'7d': 7, '30d': 30, '90d': 90}
        days = days_map.get(period, 30)
        
        start_date = (datetime.now() - timedelta(days=days)).isoformat()
        end_date = datetime.now().isoformat()
        
        # 获取统计
        stats = self.get_trade_chain_statistics(start_date, end_date)
        
        return {
            'generated_at': datetime.now().isoformat(),
            'period': period,
            'start_date': start_date,
            'end_date': end_date,
            'total_trades': stats.get('total_trades', 0),
            'closed_trades': stats.get('closed_trades', 0),
            'winning_trades': stats.get('winning_trades', 0),
            'win_rate': stats.get('winning_trades', 0) / max(stats.get('closed_trades', 1), 1),
            'total_pnl': stats.get('total_pnl', 0),
            'avg_pnl': stats.get('avg_pnl', 0),
            'avg_slippage_bps': stats.get('avg_slippage_bps', 0),
            'avg_latency_ms': stats.get('avg_latency_ms', 0),
            'by_strategy': {s['strategy_name']: s.to_dict() for s in self.analyze_strategy_performance(start_date, end_date)},
            'by_market_regime': {r['regime_name']: r.to_dict() for r in self.analyze_market_regime_performance(start_date, end_date)},
            'strategy_regime_cross': self.analyze_strategy_by_regime_cross(start_date, end_date),
            'noise_signals': self.identify_noise_signals(days=days),
            'overfit_signals': self.identify_overfit_signals(days=days),
        }
    def get_weight_recommendations(
        self,
        days: int = 30
    ) -> Dict[str, float]:
        """
        基于历史表现给出策略权重建议

        Args:
            days: 分析天数范围

        Returns:
            建议权重字典 {strategy_name: recommended_weight}
        """
        start_date = (datetime.now() - timedelta(days=days)).isoformat()
        end_date = datetime.now().isoformat()

        # 获取策略表现
        strategy_results = self.analyze_strategy_performance(start_date, end_date)

        if not strategy_results:
            return {}

        # 基于质量评分计算建议权重
        total_quality = sum(s.quality_score for s in strategy_results)
        if total_quality == 0:
            return {}

        recommendations = {}
        for strategy in strategy_results:
            # 质量评分越高，建议权重越高
            normalized_score = strategy.quality_score / total_quality
            # 映射到 0.5-2.0 范围
            recommended_weight = 0.5 + normalized_score * 1.5
            recommendations[strategy.strategy_name] = round(recommended_weight, 3)

        return recommendations


# ==================== 全局单例 ====================

_attribution_engine: Optional[AttributionEngine] = None


def get_attribution_engine(db_path: str = None) -> AttributionEngine:
    """获取归因引擎单例"""
    global _attribution_engine
    if _attribution_engine is None:
        _attribution_engine = AttributionEngine(db_path)
    return _attribution_engine


def init_attribution_engine(db_path: str = None) -> AttributionEngine:
    """初始化归因引擎"""
    global _attribution_engine
    _attribution_engine = AttributionEngine(db_path)
    return _attribution_engine
