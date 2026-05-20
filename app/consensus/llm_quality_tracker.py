#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 信号质量追踪器 (LLM Signal Quality Tracker)

功能:
- 记录 LLM signal_impact → 交易 → 结果的完整链路
- 按 signal_impact 分组统计胜率、平均收益、Profit Factor
- 当样本量充足时，自动校准 consensus_enhancer 中的 llm_factor 乘数
- 支持最小样本阈值保护（<10样本不调整）

核心理念:
  "在不确定性中找到确定性" — 用数据验证 LLM 判断是否真的有预测力。
  如果 block 信号的实际亏损率显著高于 random，说明 LLM 有真正的风险识别能力。
  如果 boost 信号的平均收益不高于 neutral，说明 LLM 的"增强"判断是噪音。

使用示例:
    tracker = get_llm_quality_tracker()

    # 交易入场时记录
    tracker.record_signal(code='HK.00700', signal_impact='boost',
                          event_id='evt_001', trade_id='tr_042',
                          score=3.2, final_score=3.68)

    # 交易退出时记录结果
    tracker.record_outcome(trade_id='tr_042', actual_return=0.035,
                           exit_reason='take_profit', holding_minutes=45)

    # 获取分组统计
    stats = tracker.get_impact_stats()
    # {'boost': {'n': 15, 'win_rate': 0.73, 'avg_return': 0.021, ...}, ...}

    # 获取校准建议
    cal = tracker.get_calibration_suggestions()
    # {'boost': {'current': 1.15, 'suggested': 1.20, 'reason': '胜率73%>60%'}, ...}
"""

import logging
import sqlite3
import json
import time
import math
from app.storage.repository import get_db
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek.llm_quality")
logger.propagate = False

# 当前 consensus_enhancer.py 中的乘数映射（SSOT: 配置驱动，此处仅用于校准参考）
CURRENT_LLM_FACTORS = {
    'boost': 1.15,
    'neutral': 1.00,
    'weaken': 0.80,
    'block': 0.30,
}

# 最小样本阈值：低于此值不进行校准
MIN_SAMPLES_FOR_CALIBRATION = 10

# 统计窗口：只分析最近 N 天的数据（避免历史噪音污染）
STATS_WINDOW_DAYS = 30


@dataclass
class LLMSignalRecord:
    """LLM 信号入场记录"""
    trade_id: str
    code: str
    signal_impact: str       # boost / neutral / weaken / block
    event_id: str = ""
    original_score: float = 0.0
    final_score: float = 0.0
    llm_factor_applied: float = 1.0
    entry_price: float = 0.0
    quantity: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'trade_id': self.trade_id,
            'code': self.code,
            'signal_impact': self.signal_impact,
            'event_id': self.event_id,
            'original_score': self.original_score,
            'final_score': self.final_score,
            'llm_factor_applied': self.llm_factor_applied,
            'entry_price': self.entry_price,
            'quantity': self.quantity,
            'timestamp': self.timestamp,
        }


@dataclass
class LLMOutcomeRecord:
    """LLM 信号对应的交易结果"""
    trade_id: str
    code: str
    signal_impact: str       # 从入场记录继承
    actual_return: float
    max_drawdown: float = 0.0
    max_profit: float = 0.0
    exit_reason: str = ""
    holding_minutes: int = 0
    direction_correct: bool = False  # LLM 判断是否与实际方向一致
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'trade_id': self.trade_id,
            'code': self.code,
            'signal_impact': self.signal_impact,
            'actual_return': self.actual_return,
            'max_drawdown': self.max_drawdown,
            'max_profit': self.max_profit,
            'exit_reason': self.exit_reason,
            'holding_minutes': self.holding_minutes,
            'direction_correct': int(self.direction_correct),
            'timestamp': self.timestamp,
        }


class LLMQualityTracker:
    """
    LLM 信号质量追踪器

    核心职责:
    1. 追踪每个 signal_impact 类型的实际交易表现
    2. 定期生成分组统计（胜率、均收、PF、最大回撤）
    3. 当样本充足时，提供乘数校准建议
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path or str(Path(__file__).parent.parent / "data" / "llm_quality.db")
        self._init_db()
        logger.info(f"[LLM质量追踪] 已初始化 | db={self._db_path}")

    def _init_db(self):
        """初始化数据库表"""
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()

                # 信号入场记录
                c.execute("""
                    CREATE TABLE IF NOT EXISTS llm_signals (
                        trade_id TEXT PRIMARY KEY,
                        code TEXT NOT NULL,
                        signal_impact TEXT NOT NULL,
                        event_id TEXT,
                        original_score REAL,
                        final_score REAL,
                        llm_factor_applied REAL,
                        entry_price REAL,
                        quantity INTEGER,
                        timestamp REAL
                    )
                """)

                # 交易结果记录
                c.execute("""
                    CREATE TABLE IF NOT EXISTS llm_outcomes (
                        trade_id TEXT PRIMARY KEY,
                        code TEXT NOT NULL,
                        signal_impact TEXT NOT NULL,
                        actual_return REAL,
                        max_drawdown REAL,
                        max_profit REAL,
                        exit_reason TEXT,
                        holding_minutes INTEGER,
                        direction_correct INTEGER,
                        timestamp REAL
                    )
                """)

                # 校准历史记录
                c.execute("""
                    CREATE TABLE IF NOT EXISTS calibration_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL,
                        impact_type TEXT,
                        current_factor REAL,
                        suggested_factor REAL,
                        sample_size INTEGER,
                        win_rate REAL,
                        avg_return REAL,
                        reason TEXT
                    )
                """)

                # 索引
                c.execute("CREATE INDEX IF NOT EXISTS idx_signal_impact ON llm_signals(signal_impact)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_signal_timestamp ON llm_signals(timestamp)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_outcome_impact ON llm_outcomes(signal_impact)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_outcome_timestamp ON llm_outcomes(timestamp)")

            logger.debug(f"[LLM质量追踪] 数据库已初始化: {self._db_path}")
        except Exception as e:
            logger.warning(f"[LLM质量追踪] 数据库初始化失败: {e}")

    def record_signal(self, trade_id: str, code: str, signal_impact: str,
                      event_id: str = "", original_score: float = 0.0,
                      final_score: float = 0.0, llm_factor_applied: float = 1.0,
                      entry_price: float = 0.0, quantity: int = 0):
        """
        记录 LLM 信号入场

        Args:
            trade_id: 交易 ID
            code: 股票代码
            signal_impact: boost / neutral / weaken / block
            event_id: 关联事件 ID
            original_score: 原始信号评分
            final_score: LLM 加权后的最终评分
            llm_factor_applied: 实际应用的 LLM 乘数
            entry_price: 入场价
            quantity: 数量
        """
        record = LLMSignalRecord(
            trade_id=trade_id,
            code=code,
            signal_impact=signal_impact,
            event_id=event_id,
            original_score=original_score,
            final_score=final_score,
            llm_factor_applied=llm_factor_applied,
            entry_price=entry_price,
            quantity=quantity,
        )
        self._persist_signal(record)
        logger.debug(f"[LLM信号记录] {code} impact={signal_impact} "
                     f"score={original_score:.2f}→{final_score:.2f} (×{llm_factor_applied})")

    def record_outcome(self, trade_id: str, code: str, signal_impact: str,
                       actual_return: float, max_drawdown: float = 0.0,
                       max_profit: float = 0.0, exit_reason: str = "",
                       holding_minutes: int = 0):
        """
        记录交易退出结果

        Args:
            trade_id: 交易 ID
            code: 股票代码
            signal_impact: signal_impact 类型（从入场记录继承）
            actual_return: 实际收益率
            max_drawdown: 最大回撤
            max_profit: 最大浮盈
            exit_reason: 退出原因
            holding_minutes: 持仓分钟数
        """
        # boost/weaken=long → return>0 为正确; block=avoid → 需特殊处理
        direction_correct = self._check_direction_correct(signal_impact, actual_return)

        outcome = LLMOutcomeRecord(
            trade_id=trade_id,
            code=code,
            signal_impact=signal_impact,
            actual_return=actual_return,
            max_drawdown=max_drawdown,
            max_profit=max_profit,
            exit_reason=exit_reason,
            holding_minutes=holding_minutes,
            direction_correct=direction_correct,
        )
        self._persist_outcome(outcome)
        logger.info(f"[LLM结果记录] {code} impact={signal_impact} "
                    f"return={actual_return:+.2%} correct={direction_correct}")

    def _check_direction_correct(self, signal_impact: str, actual_return: float) -> bool:
        """
        判断 LLM 信号的方向是否与实际走势一致

        - boost: 预期上涨 → actual_return > 0 为正确
        - weaken: 预期走弱 → actual_return < 0 为正确（如果做了空）
                  但如果系统实际做多，则 return < 0 说明 weaken 判断正确
        - block: 预期风险 → 如果该股实际大跌(return < -0.02)，说明 block 判断正确
        - neutral: 无明确方向 → 总是 True（不纳入方向统计）
        """
        if signal_impact == 'boost':
            return actual_return > 0
        elif signal_impact == 'weaken':
            return actual_return < 0
        elif signal_impact == 'block':
            return actual_return < -0.02  # 实际跌幅>2% 才算 block 判断正确
        else:  # neutral
            return True

    def _persist_signal(self, record: LLMSignalRecord):
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT OR REPLACE INTO llm_signals
                    (trade_id, code, signal_impact, event_id, original_score,
                     final_score, llm_factor_applied, entry_price, quantity, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.trade_id, record.code, record.signal_impact,
                    record.event_id, record.original_score, record.final_score,
                    record.llm_factor_applied, record.entry_price,
                    record.quantity, record.timestamp
                ))
        except Exception as e:
            logger.warning(f"[LLM质量追踪] 持久化信号失败: {e}")

    def _persist_outcome(self, outcome: LLMOutcomeRecord):
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT OR REPLACE INTO llm_outcomes
                    (trade_id, code, signal_impact, actual_return, max_drawdown,
                     max_profit, exit_reason, holding_minutes, direction_correct, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    outcome.trade_id, outcome.code, outcome.signal_impact,
                    outcome.actual_return, outcome.max_drawdown,
                    outcome.max_profit, outcome.exit_reason,
                    outcome.holding_minutes, outcome.direction_correct,
                    outcome.timestamp
                ))
        except Exception as e:
            logger.warning(f"[LLM质量追踪] 持久化结果失败: {e}")

    def get_impact_stats(self, window_days: int = None) -> Dict[str, Dict]:
        """
        获取按 signal_impact 分组的绩效统计

        Returns:
            {
                'boost': {'n': 15, 'win_rate': 0.73, 'avg_return': 0.021, ...},
                'neutral': {...},
                'weaken': {...},
                'block': {...}
            }
        """
        window_days = window_days or STATS_WINDOW_DAYS
        cutoff = time.time() - (window_days * 86400)

        stats = {}
        for impact in ['boost', 'neutral', 'weaken', 'block']:
            stats[impact] = self._compute_group_stats(impact, cutoff)
        return stats

    def _compute_group_stats(self, impact: str, cutoff: float) -> Dict:
        """计算单个 signal_impact 分组的统计指标"""
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()

                # 获取该分组的所有 outcome
                c.execute("""
                    SELECT actual_return, max_drawdown, max_profit,
                           holding_minutes, direction_correct, exit_reason
                    FROM llm_outcomes
                    WHERE signal_impact = ? AND timestamp >= ?
                    ORDER BY timestamp DESC
                """, (impact, cutoff))
                rows = c.fetchall()

                if not rows:
                    return {
                        'n': 0, 'win_rate': 0.0, 'avg_return': 0.0,
                        'total_return': 0.0, 'best_return': 0.0,
                        'worst_return': 0.0, 'avg_drawdown': 0.0,
                        'max_drawdown': 0.0, 'profit_factor': 0.0,
                        'avg_holding_minutes': 0.0, 'direction_accuracy': 0.0,
                        'sample_sufficient': False,
                    }

                returns = [r[0] for r in rows]
                drawdowns = [r[1] for r in rows]
                profits = [r[2] for r in rows]
                holdings = [r[3] for r in rows]
                corrects = [r[4] for r in rows]

                n = len(returns)
                wins = sum(1 for r in returns if r > 0)
                losses = n - wins
                win_rate = wins / n if n > 0 else 0.0

                total_return = sum(returns)
                avg_return = total_return / n
                best_return = max(returns)
                worst_return = min(returns)

                gross_profit = sum(r for r in returns if r > 0)
                gross_loss = abs(sum(r for r in returns if r < 0))
                profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

                avg_dd = sum(drawdowns) / n
                max_dd = min(drawdowns)
                avg_holding = sum(holdings) / n
                dir_accuracy = sum(corrects) / n if n > 0 else 0.0

                # 标准差
                if n > 1:
                    mean = avg_return
                    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
                    std_return = math.sqrt(variance)
                else:
                    std_return = 0.0

                return {
                    'n': n,
                    'wins': wins,
                    'losses': losses,
                    'win_rate': round(win_rate, 4),
                    'avg_return': round(avg_return, 6),
                    'total_return': round(total_return, 6),
                    'best_return': round(best_return, 6),
                    'worst_return': round(worst_return, 6),
                    'std_return': round(std_return, 6),
                    'gross_profit': round(gross_profit, 6),
                    'gross_loss': round(gross_loss, 6),
                    'profit_factor': round(profit_factor, 4) if profit_factor != float('inf') else 999.0,
                    'avg_drawdown': round(avg_dd, 6),
                    'max_drawdown': round(max_dd, 6),
                    'avg_holding_minutes': round(avg_holding, 1),
                    'direction_accuracy': round(dir_accuracy, 4),
                    'sample_sufficient': n >= MIN_SAMPLES_FOR_CALIBRATION,
                }

        except Exception as e:
            logger.warning(f"[LLM质量追踪] 计算 {impact} 统计失败: {e}")
            return {
                'n': 0, 'win_rate': 0.0, 'avg_return': 0.0,
                'sample_sufficient': False,
            }

    def get_calibration_suggestions(self) -> Dict[str, Dict]:
        """
        基于历史数据生成 LLM factor 乘数校准建议

        校准逻辑:
        - boost: 如果胜率>60%且PF>1.5，建议提高乘数（当前信号被低估）
        - weaken: 如果实际亏损率>60%，建议降低乘数（当前信号被高估）
        - block: 如果被 block 的股票实际大跌率>50%，建议降低乘数（拦截正确）
        - neutral: 基准，不调整

        Returns:
            {
                'boost': {'current': 1.15, 'suggested': 1.20,
                          'reason': '胜率73%>60%, PF=1.8, 信号被低估'},
                ...
            }
        """
        stats = self.get_impact_stats()
        suggestions = {}

        for impact, current_factor in CURRENT_LLM_FACTORS.items():
            s = stats.get(impact, {})
            n = s.get('n', 0)

            if n < MIN_SAMPLES_FOR_CALIBRATION:
                suggestions[impact] = {
                    'current': current_factor,
                    'suggested': current_factor,
                    'reason': f'样本不足 (n={n}<{MIN_SAMPLES_FOR_CALIBRATION})',
                    'sample_size': n,
                }
                continue

            win_rate = s.get('win_rate', 0)
            avg_return = s.get('avg_return', 0)
            pf = s.get('profit_factor', 0)

            suggested = current_factor
            reason = ""

            if impact == 'boost':
                if win_rate > 0.65 and pf > 1.5:
                    suggested = min(1.30, current_factor + 0.05)
                    reason = f'胜率{win_rate:.0%}>65%, PF={pf:.1f}, 信号价值被低估'
                elif win_rate < 0.40:
                    suggested = max(1.00, current_factor - 0.05)
                    reason = f'胜率{win_rate:.0%}<40%, 信号价值被高估'
                else:
                    reason = f'表现正常 (胜率{win_rate:.0%}, PF={pf:.1f})'

            elif impact == 'weaken':
                # weaken 的 win_rate 是"做空正确率"，即实际亏损的比例
                if win_rate > 0.55:
                    suggested = max(0.60, current_factor - 0.05)
                    reason = f'预判正确率{win_rate:.0%}>55%, 应该更大程度降权'
                elif win_rate < 0.35:
                    suggested = min(0.95, current_factor + 0.05)
                    reason = f'预判正确率{win_rate:.0%}<35%, 降权过度'
                else:
                    reason = f'表现正常 (预判正确率{win_rate:.0%})'

            elif impact == 'block':
                # block 的 direction_accuracy 是"实际大跌比例"
                dir_acc = s.get('direction_accuracy', 0)
                if dir_acc > 0.50:
                    suggested = max(0.15, current_factor - 0.05)
                    reason = f'拦截正确率{dir_acc:.0%}>50%, 应更严格拦截'
                elif dir_acc < 0.25:
                    suggested = min(0.50, current_factor + 0.10)
                    reason = f'拦截正确率{dir_acc:.0%}<25%, 误杀过多机会'
                else:
                    reason = f'表现正常 (拦截正确率{dir_acc:.0%})'

            else:  # neutral
                suggested = current_factor
                reason = '基准组，不调整'

            suggestions[impact] = {
                'current': current_factor,
                'suggested': round(suggested, 4),
                'reason': reason,
                'sample_size': n,
                'win_rate': win_rate,
                'avg_return': avg_return,
                'profit_factor': pf,
            }

        return suggestions

    def log_calibration(self, impact_type: str, current: float, suggested: float,
                        stats: Dict, reason: str):
        """记录一次校准决策到数据库"""
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO calibration_log
                    (timestamp, impact_type, current_factor, suggested_factor,
                     sample_size, win_rate, avg_return, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    time.time(), impact_type, current, suggested,
                    stats.get('n', 0), stats.get('win_rate', 0),
                    stats.get('avg_return', 0), reason
                ))
            logger.info(f"[LLM校准] {impact_type}: {current}→{suggested} | {reason}")
        except Exception as e:
            logger.warning(f"[LLM质量追踪] 记录校准失败: {e}")

    def get_recent_signals(self, limit: int = 20) -> List[Dict]:
        """获取最近的 LLM 信号记录（用于诊断）"""
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT s.trade_id, s.code, s.signal_impact, s.original_score,
                           s.final_score, s.llm_factor_applied,
                           o.actual_return, o.exit_reason
                    FROM llm_signals s
                    LEFT JOIN llm_outcomes o ON s.trade_id = o.trade_id
                    ORDER BY s.timestamp DESC
                    LIMIT ?
                """, (limit,))
                rows = c.fetchall()
                return [
                    {
                        'trade_id': r[0], 'code': r[1], 'signal_impact': r[2],
                        'original_score': r[3], 'final_score': r[4],
                        'llm_factor': r[5], 'actual_return': r[6],
                        'exit_reason': r[7],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning(f"[LLM质量追踪] 查询最近信号失败: {e}")
            return []

    def find_open_signal_by_code(self, code: str) -> Optional[Dict]:
        """
        查找某只股票最近的未平仓 LLM 信号记录
        （通过 LEFT JOIN 检查 outcome 是否存在来判断是否已平仓）
        """
        try:
            with get_db(self._db_path) as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT s.trade_id, s.signal_impact, s.original_score,
                           s.final_score, s.llm_factor_applied, s.entry_price,
                           s.quantity, s.timestamp
                    FROM llm_signals s
                    LEFT JOIN llm_outcomes o ON s.trade_id = o.trade_id
                    WHERE s.code = ? AND o.trade_id IS NULL
                    ORDER BY s.timestamp DESC
                    LIMIT 1
                """, (code,))
                row = c.fetchone()
                if row:
                    return {
                        'trade_id': row[0], 'signal_impact': row[1],
                        'original_score': row[2], 'final_score': row[3],
                        'llm_factor_applied': row[4], 'entry_price': row[5],
                        'quantity': row[6], 'timestamp': row[7],
                    }
        except Exception as e:
            logger.warning(f"[LLM质量追踪] 查找未平仓信号失败 ({code}): {e}")
        return None

    def generate_report(self) -> str:
        """生成可读的质量报告（用于日志/通知）"""
        stats = self.get_impact_stats()
        calibrations = self.get_calibration_suggestions()

        lines = ["═══ LLM Signal Quality Report ═══"]
        for impact in ['boost', 'neutral', 'weaken', 'block']:
            s = stats.get(impact, {})
            c = calibrations.get(impact, {})
            n = s.get('n', 0)

            if n == 0:
                lines.append(f"\n[{impact.upper()}] 无数据")
                continue

            lines.append(f"\n[{impact.upper()}] n={n} | "
                        f"WR={s['win_rate']:.0%} | "
                        f"AR={s['avg_return']:+.2%} | "
                        f"PF={s['profit_factor']:.2f} | "
                        f"MaxDD={s['max_drawdown']:+.2%}")
            lines.append(f"  乘数: {c['current']} → {c['suggested']} "
                        f"({c['reason']})")

        lines.append("\n" + "=" * 40)
        return "\n".join(lines)


# Singleton
_instance = None

def get_llm_quality_tracker(db_path: str = None) -> LLMQualityTracker:
    """获取 LLM 质量追踪器单例"""
    global _instance
    if _instance is None:
        _instance = LLMQualityTracker(db_path)
    return _instance


def reset_tracker():
    """测试用：重置追踪器"""
    global _instance
    _instance = None
