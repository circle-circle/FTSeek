#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件绩效追踪器 (Event Performance Tracker)

功能:
- 记录事件→交易→结果的完整链路
- 分析各类事件的 실제绩效
- 支持策略自学习优化

核心流程:
事件生成 → 交易执行 → 结果记录 → 绩效分析 → 策略优化

使用示例:
    tracker = EventPerformanceTracker()

    # 记录事件生成
    tracker.record_event(
        event_id='evt_001',
        event_type='earnings',
        predicted_direction='buy',
        target_stocks=['HK.00700']
    )

    # 记录交易退出
    tracker.record_outcome(
        event_id='evt_001',
        actual_return=0.035,
        max_drawdown=-0.01,
        holding_minutes=45,
        exit_reason='take_profit'
    )

    # 获取绩效分析
    stats = tracker.get_performance_stats('earnings')
    # stats = {'win_rate': 0.65, 'avg_return': 0.02, ...}
"""

import logging
import sqlite3
import json
import time
from app.storage.repository import get_db
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from enum import Enum

logger = logging.getLogger("ftseek.consensus.performance")
logger.propagate = False


class ExitReason(str, Enum):
    """退出原因"""
    TAKE_PROFIT = "take_profit"           # 止盈退出
    STOP_LOSS = "stop_loss"               # 止损退出
    TRAILING_STOP = "trailing_stop"       # 追踪止损
    TIME_EXIT = "time_exit"               # 时间退出
    SCENARIO_CHANGE = "scenario_change"   # 场景失效
    MANUAL = "manual"                     # 人工干预
    SIGNAL_REVERSAL = "signal_reversal"   # 信号反转


@dataclass
class EventRecord:
    """事件记录"""
    # 基础信息
    event_id: str
    event_type: str
    event_title: str = ""
    news_id: str = ""

    # 预测信息
    predicted_direction: str = ""  # buy/sell
    predicted_duration_minutes: int = 0
    predicted_return: float = 0.0
    confidence: float = 0.0

    # 目标股票
    target_stocks: List[str] = field(default_factory=list)
    # [{'code': 'HK.00700', 'direction': 'long', 'weight': 0.5}]

    # 信号元数据
    arb_score: float = 0.0
    news_sentiment: float = 0.0
    lead_lag_strength: float = 0.0

    # 时间信息
    created_at: float = field(default_factory=time.time)
    traded_at: Optional[float] = None
    exited_at: Optional[float] = None

    # 状态
    status: str = "pending"  # pending/trading/completed/failed

    def to_dict(self) -> Dict:
        return {
            'event_id': self.event_id,
            'event_type': self.event_type,
            'event_title': self.event_title,
            'news_id': self.news_id,
            'predicted_direction': self.predicted_direction,
            'predicted_duration_minutes': self.predicted_duration_minutes,
            'predicted_return': self.predicted_return,
            'confidence': self.confidence,
            'target_stocks': json.dumps(self.target_stocks),
            'arb_score': self.arb_score,
            'news_sentiment': self.news_sentiment,
            'lead_lag_strength': self.lead_lag_strength,
            'created_at': self.created_at,
            'traded_at': self.traded_at,
            'exited_at': self.exited_at,
            'status': self.status,
        }


@dataclass
class TradeOutcome:
    """交易结果记录"""
    # 关联信息
    event_id: str
    trade_id: str

    # 交易信息
    code: str
    direction: str  # long/short
    entry_price: float
    exit_price: float
    quantity: int

    # 结果信息
    actual_return: float         # 实际收益率 (%)
    max_drawdown: float          # 最大回撤 (%)
    max_profit: float            # 最大浮盈 (%)
    holding_minutes: int         # 持仓分钟数
    exit_reason: str             # 退出原因

    # 对比预测
    duration_prediction_error: float = 0.0  # 持续时间预测误差
    direction_correct: bool = False          # 方向预测是否正确

    # 元数据
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'event_id': self.event_id,
            'trade_id': self.trade_id,
            'code': self.code,
            'direction': self.direction,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'quantity': self.quantity,
            'actual_return': self.actual_return,
            'max_drawdown': self.max_drawdown,
            'max_profit': self.max_profit,
            'holding_minutes': self.holding_minutes,
            'exit_reason': self.exit_reason,
            'duration_prediction_error': self.duration_prediction_error,
            'direction_correct': self.direction_correct,
            'recorded_at': self.recorded_at,
        }


@dataclass
class PerformanceStats:
    """绩效统计"""
    event_type: str

    # 样本统计
    total_events: int = 0
    completed_events: int = 0

    # 胜率
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0

    # 收益统计
    avg_return: float = 0.0
    total_return: float = 0.0
    best_return: float = 0.0
    worst_return: float = 0.0
    std_return: float = 0.0

    # 风险统计
    avg_drawdown: float = 0.0
    max_drawdown: float = 0.0

    # 时间统计
    avg_holding_minutes: float = 0.0

    # 预测准确度
    direction_accuracy: float = 0.0
    duration_prediction_error: float = 0.0

    # 时间范围
    from_date: str = ""
    to_date: str = ""

    def to_dict(self) -> Dict:
        return {
            'event_type': self.event_type,
            'total_events': self.total_events,
            'completed_events': self.completed_events,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': self.win_rate,
            'avg_return': self.avg_return,
            'total_return': self.total_return,
            'best_return': self.best_return,
            'worst_return': self.worst_return,
            'std_return': self.std_return,
            'avg_drawdown': self.avg_drawdown,
            'max_drawdown': self.max_drawdown,
            'avg_holding_minutes': self.avg_holding_minutes,
            'direction_accuracy': self.direction_accuracy,
            'duration_prediction_error': self.duration_prediction_error,
            'from_date': self.from_date,
            'to_date': self.to_date,
        }


class EventPerformanceTracker:
    """
    事件绩效追踪器

    记录事件→交易→结果的完整链路，支持策略自学习
    """

    def __init__(self, db_path: str = None):
        """
        初始化追踪器

        Args:
            db_path: 数据库路径
        """
        self._db_path = db_path or str(Path(__file__).parent.parent / "data" / "event_performance.db")
        self._init_db()

        # 内存缓存
        self._pending_events: Dict[str, EventRecord] = {}
        self._recent_outcomes: List[TradeOutcome] = []

        logger.info(f"事件绩效追踪器已初始化 | db={self._db_path}")

    def _init_db(self):
        """初始化数据库"""
        try:
            with get_db(self._db_path) as conn:
                cursor = conn.cursor()

                # 事件记录表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS event_records (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    event_title TEXT,
                    news_id TEXT,
                    predicted_direction TEXT,
                    predicted_duration_minutes INTEGER,
                    predicted_return REAL,
                    confidence REAL,
                    target_stocks TEXT,
                    arb_score REAL,
                    news_sentiment REAL,
                    lead_lag_strength REAL,
                    created_at REAL,
                    traded_at REAL,
                    exited_at REAL,
                    status TEXT
                )
                """)

                # 交易结果表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trade_outcomes (
                        trade_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        code TEXT NOT NULL,
                        direction TEXT,
                        entry_price REAL,
                        exit_price REAL,
                        quantity INTEGER,
                        actual_return REAL,
                        max_drawdown REAL,
                        max_profit REAL,
                        holding_minutes INTEGER,
                        exit_reason TEXT,
                        duration_prediction_error REAL,
                        direction_correct INTEGER,
                        recorded_at REAL
                    )
                """)

                # 索引
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_event_type
                    ON event_records(event_type)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_outcome_event
                    ON trade_outcomes(event_id)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_outcome_code
                    ON trade_outcomes(code)
                """)

            logger.debug(f"数据库已初始化：{self._db_path}")
        except Exception as e:
            logger.warning(f"数据库初始化失败：{e}")

    def record_event(self,
                    event_id: str,
                    event_type: str,
                    predicted_direction: str,
                    target_stocks: List[Dict],
                    event_title: str = "",
                    news_id: str = "",
                    predicted_duration_minutes: int = 0,
                    predicted_return: float = 0.0,
                    confidence: float = 0.0,
                    arb_score: float = 0.0,
                    news_sentiment: float = 0.0,
                    lead_lag_strength: float = 0.0):
        """
        记录事件生成

        Args:
            event_id: 事件 ID
            event_type: 事件类型
            predicted_direction: 预测方向 (buy/sell)
            target_stocks: 目标股票列表
            event_title: 事件标题
            news_id: 关联新闻 ID
            predicted_duration_minutes: 预测持续时间
            predicted_return: 预测收益
            confidence: 置信度
            arb_score: 套利评分
            news_sentiment: 新闻情绪
            lead_lag_strength: Lead-Lag 强度
        """
        record = EventRecord(
            event_id=event_id,
            event_type=event_type,
            event_title=event_title,
            news_id=news_id,
            predicted_direction=predicted_direction,
            predicted_duration_minutes=predicted_duration_minutes,
            predicted_return=predicted_return,
            confidence=confidence,
            target_stocks=target_stocks,
            arb_score=arb_score,
            news_sentiment=news_sentiment,
            lead_lag_strength=lead_lag_strength,
        )

        # 内存缓存
        self._pending_events[event_id] = record

        # 持久化
        self._persist_event(record)

        logger.debug(f"[事件记录] {event_id}: {event_type} → {predicted_direction}")

    def _persist_event(self,record: EventRecord):
        """持久化事件记录"""
        try:
            with get_db(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO event_records
                (event_id, event_type, event_title, news_id, predicted_direction,
                 predicted_duration_minutes, predicted_return, confidence,
                 target_stocks, arb_score, news_sentiment, lead_lag_strength,
                 created_at, traded_at, exited_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.event_id, record.event_type, record.event_title, record.news_id,
                record.predicted_direction, record.predicted_duration_minutes,
                record.predicted_return, record.confidence,
                json.dumps(record.target_stocks), record.arb_score, record.news_sentiment,
                record.lead_lag_strength, record.created_at, record.traded_at,
                record.exited_at, record.status
            ))
        except Exception as e:
            logger.warning(f"持久化事件记录失败：{e}")

    def update_event_status(self, event_id: str, status: str, traded_at: float = None):
        """更新事件状态"""
        try:
            with get_db(self._db_path) as conn:
                cursor = conn.cursor()

                update_fields = ["status = ?"]
                params = [status]

                if traded_at is not None:
                    update_fields.append("traded_at = ?")
                    params.append(traded_at)

                params.append(event_id)

                cursor.execute(f"""
                    UPDATE event_records
                    SET {', '.join(update_fields)}
                    WHERE event_id = ?
                """, params)

            # 更新内存缓存
            if event_id in self._pending_events:
                self._pending_events[event_id].status = status
                if traded_at:
                    self._pending_events[event_id].traded_at = traded_at

            logger.debug(f"[状态更新] {event_id}: {status}")
        except Exception as e:
            logger.warning(f"更新事件状态失败：{e}")

    def record_outcome(self,
                      event_id: str,
                      trade_id: str,
                      code: str,
                      direction: str,
                      entry_price: float,
                      exit_price: float,
                      quantity: int,
                      actual_return: float,
                      max_drawdown: float,
                      max_profit: float,
                      holding_minutes: int,
                      exit_reason: str,
                      predicted_duration: int = 0,
                      predicted_direction: str = ""):
        """
        记录交易结果

        Args:
            event_id: 关联事件 ID
            trade_id: 交易 ID
            code: 股票代码
            direction: 交易方向 (long/short)
            entry_price: 入场价
            exit_price: 出场价
            quantity: 数量
            actual_return: 实际收益率
            max_drawdown: 最大回撤
            max_profit: 最大浮盈
            holding_minutes: 持仓分钟数
            exit_reason: 退出原因
            predicted_duration: 预测持续时间
            predicted_direction: 预测方向
        """
        # 计算预测误差
        duration_error = abs(holding_minutes - predicted_duration) if predicted_duration else 0
        direction_correct = (
            (predicted_direction == 'buy' and actual_return > 0) or
            (predicted_direction == 'sell' and actual_return < 0)
        )

        outcome = TradeOutcome(
            event_id=event_id,
            trade_id=trade_id,
            code=code,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            actual_return=actual_return,
            max_drawdown=max_drawdown,
            max_profit=max_profit,
            holding_minutes=holding_minutes,
            exit_reason=exit_reason,
            duration_prediction_error=duration_error,
            direction_correct=direction_correct,
        )

        # 内存缓存
        self._recent_outcomes.append(outcome)

        # 持久化
        self._persist_outcome(outcome)

        # 更新事件状态
        self.update_event_status(event_id, "completed", traded_at=time.time())

        logger.info(
            f"[结果记录] {trade_id}: {code} 收益={actual_return:+.2%}, "
            f"持仓={holding_minutes}分钟，退出={exit_reason}"
        )

    def _persist_outcome(self, outcome: TradeOutcome):
        """持久化交易结果"""
        try:
            with get_db(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trade_outcomes
                    (trade_id, event_id, code, direction, entry_price, exit_price,
                     quantity, actual_return, max_drawdown, max_profit,
                     holding_minutes, exit_reason, duration_prediction_error,
                     direction_correct, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    outcome.trade_id, outcome.event_id, outcome.code, outcome.direction,
                    outcome.entry_price, outcome.exit_price, outcome.quantity,
                    outcome.actual_return, outcome.max_drawdown, outcome.max_profit,
                    outcome.holding_minutes, outcome.exit_reason,
                    outcome.duration_prediction_error, 1 if outcome.direction_correct else 0,
                    outcome.recorded_at
                ))
        except Exception as e:
            logger.warning(f"持久化交易结果失败：{e}")

    def get_performance_stats(self,
                             event_type: str = None,
                             days: int = 30) -> PerformanceStats:
        """
        获取绩效统计

        Args:
            event_type: 事件类型 (None=全部)
            days: 统计天数

        Returns:
            PerformanceStats: 绩效统计
        """
        try:
            with get_db(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # 时间范围
                from_timestamp = time.time() - (days * 24 * 60 * 60)
                from_date = datetime.fromtimestamp(from_timestamp).strftime('%Y-%m-%d')
                to_date = datetime.now().strftime('%Y-%m-%d')

                # 基础查询
                event_filter = "AND e.event_type = ?" if event_type else ""
                event_param = [event_type] if event_type else []

                # 统计查询
                cursor.execute(f"""
                    SELECT
                        COUNT(*) as total_events,
                        COUNT(CASE WHEN o.trade_id IS NOT NULL THEN 1 END) as completed_events,
                        COUNT(CASE WHEN o.actual_return > 0 THEN 1 END) as wins,
                        COUNT(CASE WHEN o.actual_return <= 0 THEN 1 END) as losses,
                        AVG(o.actual_return) as avg_return,
                        SUM(o.actual_return) as total_return,
                        MAX(o.actual_return) as best_return,
                        MIN(o.actual_return) as worst_return,
                        AVG(o.max_drawdown) as avg_drawdown,
                        MIN(o.max_drawdown) as max_drawdown,
                        AVG(o.holding_minutes) as avg_holding_minutes,
                        AVG(CASE WHEN o.direction_correct = 1 THEN 1 ELSE 0 END) as direction_accuracy,
                        AVG(o.duration_prediction_error) as duration_prediction_error
                    FROM event_records e
                    LEFT JOIN trade_outcomes o ON e.event_id = o.event_id
                    WHERE e.created_at >= ?
                    {event_filter}
                """, [from_timestamp] + event_param)

                row = cursor.fetchone()

                if row and row['total_events'] > 0:
                    stats = PerformanceStats(
                        event_type=event_type or "all",
                        total_events=row['total_events'] or 0,
                        completed_events=row['completed_events'] or 0,
                        wins=row['wins'] or 0,
                        losses=row['losses'] or 0,
                        win_rate=(row['wins'] or 0) / max(1, row['completed_events'] or 1),
                        avg_return=row['avg_return'] or 0.0,
                        total_return=row['total_return'] or 0.0,
                        best_return=row['best_return'] or 0.0,
                        worst_return=row['worst_return'] or 0.0,
                        avg_drawdown=row['avg_drawdown'] or 0.0,
                        max_drawdown=row['max_drawdown'] or 0.0,
                        avg_holding_minutes=row['avg_holding_minutes'] or 0.0,
                        direction_accuracy=row['direction_accuracy'] or 0.0,
                        duration_prediction_error=row['duration_prediction_error'] or 0.0,
                        from_date=from_date,
                        to_date=to_date,
                    )
                else:
                    stats = PerformanceStats(
                        event_type=event_type or "all",
                        from_date=from_date,
                        to_date=to_date,
                    )

            return stats

        except Exception as e:
            logger.warning(f"获取绩效统计失败：{e}")
            return PerformanceStats(event_type=event_type or "all")

    def get_recent_events(self,
                         event_type: str = None,
                         status: str = None,
                         limit: int = 20) -> List[EventRecord]:
        """获取最近事件"""
        try:
            with get_db(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                filters = []
                params = []

                if event_type:
                    filters.append("event_type = ?")
                    params.append(event_type)
                if status:
                    filters.append("status = ?")
                    params.append(status)

                where_clause = " WHERE " + " AND ".join(filters) if filters else ""

                cursor.execute(f"""
                    SELECT * FROM event_records
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ?
                """, params + [limit])

                rows = cursor.fetchall()

            return [
                EventRecord(
                    event_id=row['event_id'],
                    event_type=row['event_type'],
                    event_title=row['event_title'] or "",
                    news_id=row['news_id'] or "",
                    predicted_direction=row['predicted_direction'] or "",
                    predicted_duration_minutes=row['predicted_duration_minutes'] or 0,
                    predicted_return=row['predicted_return'] or 0.0,
                    confidence=row['confidence'] or 0.0,
                    target_stocks=json.loads(row['target_stocks']) if row['target_stocks'] else [],
                    arb_score=row['arb_score'] or 0.0,
                    news_sentiment=row['news_sentiment'] or 0.0,
                    lead_lag_strength=row['lead_lag_strength'] or 0.0,
                    created_at=row['created_at'],
                    traded_at=row['traded_at'],
                    exited_at=row['exited_at'],
                    status=row['status'] or "pending",
                )
                for row in rows
            ]

        except Exception as e:
            logger.warning(f"获取最近事件失败：{e}")
            return []

    def get_optimization_suggestions(self, event_type: str = None) -> List[Dict]:
        """
        获取优化建议

        基于绩效分析给出策略优化建议
        """
        suggestions = []
        stats = self.get_performance_stats(event_type)

        if stats.total_events < 10:
            return [{"type": "info", "message": "数据不足，需要更多样本"}]

        # 胜率过低
        if stats.win_rate < 0.45:
            suggestions.append({
                "type": "warning",
                "category": "win_rate",
                "message": f"胜率偏低 ({stats.win_rate:.1%})，建议检查入场条件",
                "suggestion": "提高信号阈值或改进事件分析模型"
            })

        # 平均持仓时间过长
        if stats.avg_holding_minutes > 120:
            suggestions.append({
                "type": "warning",
                "category": "holding_time",
                "message": f"平均持仓时间过长 ({stats.avg_holding_minutes:.0f}分钟)",
                "suggestion": "考虑更积极的退出策略"
            })

        # 平均回撤过大
        if abs(stats.avg_drawdown) > 0.03:
            suggestions.append({
                "type": "risk",
                "category": "drawdown",
                "message": f"平均回撤过大 ({stats.avg_drawdown:.1%})",
                "suggestion": "收紧止损或降低仓位"
            })

        # 预测准确率低
        if stats.direction_accuracy < 0.5:
            suggestions.append({
                "type": "warning",
                "category": "prediction",
                "message": f"方向预测准确率低 ({stats.direction_accuracy:.1%})",
                "suggestion": "重新校准情绪分析模型"
            })

        return suggestions


# 全局单例
_tracker: Optional[EventPerformanceTracker] = None


def get_event_performance_tracker() -> EventPerformanceTracker:
    """获取全局 EventPerformanceTracker 单例"""
    global _tracker
    if _tracker is None:
        _tracker = EventPerformanceTracker()
    return _tracker


def reset_tracker():
    """重置追踪器 (用于测试)"""
    global _tracker
    _tracker = None
