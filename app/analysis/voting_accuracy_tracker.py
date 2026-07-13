"""
投票准确率追踪 (Voting Accuracy Tracker)

Task #26 (2026-05-27)

目标:
  - 追踪每次投票预测 vs 实际结果
  - 按模型/confidence/时间段汇报准确率
  - 识别模型退化趋势

数据存储:
  /opt/ftseek/FTSeek-US/data/ftseek_us.db -> voting_predictions 表
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
)


class VotingAccuracyTracker:
    """投票准确率追踪器"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _init_db(self):
        """初始化 voting_predictions 表"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS voting_predictions (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id     TEXT NOT NULL,
                        symbol          TEXT NOT NULL,
                        predicted_direction TEXT NOT NULL,
                        predicted_strength INTEGER NOT NULL,
                        predicted_confidence REAL DEFAULT 0.5,
                        model_votes     TEXT DEFAULT '{}',
                        actual_direction TEXT,
                        actual_pnl      REAL,
                        outcome_match   INTEGER,
                        prediction_date TEXT NOT NULL,
                        outcome_date    TEXT,
                        created_at      TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vp_symbol ON voting_predictions(symbol)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vp_date ON voting_predictions(prediction_date)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vp_decision ON voting_predictions(decision_id)"
                )
                conn.commit()
            finally:
                conn.close()

    def record_prediction(
        self,
        decision_id: str,
        symbol: str,
        predicted_direction: str,
        predicted_strength: int,
        predicted_confidence: float = 0.5,
        model_votes: Optional[Dict[str, Any]] = None,
    ):
        """
        投票时调用: 记录预测

        Args:
            decision_id: 决策ID (关联 decisions 表)
            symbol: 标的
            predicted_direction: LONG/SHORT/NEUTRAL
            predicted_strength: 0-100
            predicted_confidence: 0.0-1.0
            model_votes: 各模型投票明细 {"qwen": {"direction": "LONG", "strength": 70}, ...}
        """
        # 写入侧规范化: 折叠 US.US. 双前缀 + 归并别名(GOOGL->GOOG)
        symbol = normalize_symbol(symbol)
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO voting_predictions
                    (decision_id, symbol, predicted_direction, predicted_strength,
                     predicted_confidence, model_votes, prediction_date, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        symbol,
                        predicted_direction,
                        int(predicted_strength or 0),
                        float(predicted_confidence or 0.0),
                        json.dumps(model_votes or {}, ensure_ascii=False),
                        datetime.now().strftime('%Y-%m-%d'),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
            except Exception as e:
                logger.debug(f"[VotingAccuracy] record_prediction 失败: {e}")
            finally:
                conn.close()

    def update_outcome(
        self,
        symbol: str,
        actual_pnl: float,
        actual_direction: str,
        trade_date: Optional[str] = None,
    ):
        """
        平仓时调用: 回填实际结果

        匹配逻辑: 找到同 symbol + 同日 + 未回填的最近一条预测
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        with self._lock:
            conn = self._get_conn()
            try:
                # 期权/股票 symbol 兼容, 用 LIKE %核心代码% 匹配
                core = (symbol or "").split('.')[-1] or symbol or ""
                row = conn.execute(
                    """
                    SELECT id, predicted_direction FROM voting_predictions
                    WHERE symbol LIKE ? AND prediction_date = ? AND actual_direction IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (f"%{core}%", trade_date),
                ).fetchone()

                if not row:
                    # 兜底: 不限日期, 找最近一条
                    row = conn.execute(
                        """
                        SELECT id, predicted_direction FROM voting_predictions
                        WHERE symbol LIKE ? AND actual_direction IS NULL
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (f"%{core}%",),
                    ).fetchone()

                if row:
                    # outcome_match: 预测方向与实际方向一致
                    match = 1 if row['predicted_direction'] == actual_direction else 0
                    conn.execute(
                        """
                        UPDATE voting_predictions
                        SET actual_direction = ?, actual_pnl = ?,
                            outcome_match = ?, outcome_date = ?
                        WHERE id = ?
                        """,
                        (
                            actual_direction,
                            float(actual_pnl or 0.0),
                            match,
                            datetime.now().strftime('%Y-%m-%d'),
                            row['id'],
                        ),
                    )
                    conn.commit()
            except Exception as e:
                logger.debug(f"[VotingAccuracy] update_outcome 失败: {e}")
            finally:
                conn.close()

    def get_accuracy_report(self, period_days: int = 7) -> Dict[str, Any]:
        """
        生成准确率报告

        Returns:
            {
                'period_days': 7,
                'total_predictions': 50,
                'matched_predictions': 30,
                'overall_accuracy': 0.60,
                'by_model': {'qwen': {'accuracy': 0.65, 'total': 50}, ...},
                'by_confidence_tier': {'high': 0.70, 'medium': 0.55, 'low': 0.40},
                'trend': 'improving' | 'declining' | 'stable',
                'generated_at': '...'
            }
        """
        conn = self._get_conn()
        try:
            cutoff = (datetime.now() - timedelta(days=period_days)).strftime('%Y-%m-%d')

            rows = conn.execute(
                """
                SELECT predicted_direction, predicted_strength, predicted_confidence,
                       actual_direction, actual_pnl, outcome_match, model_votes
                FROM voting_predictions
                WHERE prediction_date >= ? AND outcome_match IS NOT NULL
                ORDER BY created_at ASC
                """,
                (cutoff,),
            ).fetchall()

            if not rows:
                return {
                    'period_days': period_days,
                    'total_predictions': 0,
                    'overall_accuracy': None,
                    'generated_at': datetime.now().isoformat(),
                }

            total = len(rows)
            matched = sum(1 for r in rows if r['outcome_match'] == 1)
            overall_accuracy = matched / total if total > 0 else 0.0

            # 按 confidence 分桶
            high_conf = [r for r in rows if (r['predicted_confidence'] or 0) >= 0.7]
            med_conf = [
                r for r in rows
                if 0.4 <= (r['predicted_confidence'] or 0) < 0.7
            ]
            low_conf = [r for r in rows if (r['predicted_confidence'] or 0) < 0.4]

            by_confidence = {
                'high': (
                    sum(1 for r in high_conf if r['outcome_match'] == 1) / len(high_conf)
                    if high_conf else None
                ),
                'medium': (
                    sum(1 for r in med_conf if r['outcome_match'] == 1) / len(med_conf)
                    if med_conf else None
                ),
                'low': (
                    sum(1 for r in low_conf if r['outcome_match'] == 1) / len(low_conf)
                    if low_conf else None
                ),
            }

            # 按模型分解
            by_model: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                try:
                    votes_raw = r['model_votes']
                    votes = json.loads(votes_raw) if votes_raw else {}
                    if not isinstance(votes, dict):
                        continue
                    actual_dir = r['actual_direction']
                    for model_id, vote_data in votes.items():
                        slot = by_model.setdefault(
                            model_id, {'total': 0, 'correct': 0}
                        )
                        slot['total'] += 1
                        model_dir = (
                            vote_data.get('direction', '')
                            if isinstance(vote_data, dict) else ''
                        )
                        if model_dir and model_dir == actual_dir:
                            slot['correct'] += 1
                except Exception:
                    continue

            for model_id, slot in by_model.items():
                t = slot['total']
                c = slot['correct']
                slot['accuracy'] = round(c / t, 4) if t > 0 else 0.0

            # 趋势判断 (前半 vs 后半)
            mid = len(rows) // 2
            if mid > 5:
                first_half_acc = sum(
                    1 for r in rows[:mid] if r['outcome_match'] == 1
                ) / mid
                second_half_acc = sum(
                    1 for r in rows[mid:] if r['outcome_match'] == 1
                ) / (len(rows) - mid)
                if second_half_acc > first_half_acc + 0.05:
                    trend = 'improving'
                elif second_half_acc < first_half_acc - 0.05:
                    trend = 'declining'
                else:
                    trend = 'stable'
            else:
                trend = 'insufficient_data'

            # 格式化 confidence 三档为 round 4
            by_confidence_fmt = {
                k: (round(v, 4) if isinstance(v, (int, float)) else v)
                for k, v in by_confidence.items()
            }

            return {
                'period_days': period_days,
                'total_predictions': total,
                'matched_predictions': matched,
                'overall_accuracy': round(overall_accuracy, 4),
                'by_model': by_model,
                'by_confidence_tier': by_confidence_fmt,
                'trend': trend,
                'generated_at': datetime.now().isoformat(),
            }
        finally:
            conn.close()

    # =========================================================================
    #  Task #32 (2026-05-27): 模型性能基准评估
    # =========================================================================

    def get_model_benchmark(
        self, model_id: str, period_days: int = 30
    ) -> Dict[str, Any]:
        """
        获取单个模型的性能基准

        Returns:
            {
                'model_id': 'qwen',
                'period_days': 30,
                'accuracy': 0.62,
                'total_votes': 100,
                'correct_votes': 62,
                'avg_latency_ms': 2500,
                'cost_per_call': 0.015,
                'by_direction': {'LONG': 0.65, 'SHORT': 0.58},
            }
        """
        conn = self._get_conn()
        try:
            cutoff = (
                datetime.now() - timedelta(days=period_days)
            ).strftime('%Y-%m-%d')

            rows = conn.execute(
                """
                SELECT model_votes, actual_direction, outcome_match,
                       predicted_confidence
                FROM voting_predictions
                WHERE prediction_date >= ? AND outcome_match IS NOT NULL
                """,
                (cutoff,),
            ).fetchall()

            if not rows:
                return {
                    'model_id': model_id,
                    'period_days': period_days,
                    'accuracy': None,
                    'total_votes': 0,
                }

            model_correct = 0
            model_total = 0
            by_direction = {
                'LONG': {'total': 0, 'correct': 0},
                'SHORT': {'total': 0, 'correct': 0},
            }

            for row in rows:
                try:
                    votes_raw = row['model_votes']
                    votes = json.loads(votes_raw) if votes_raw else {}
                    if not isinstance(votes, dict) or model_id not in votes:
                        continue
                    model_total += 1
                    vote = votes[model_id]
                    model_dir = (
                        vote.get('direction', '') if isinstance(vote, dict) else ''
                    )
                    actual_dir = row['actual_direction'] or ''

                    if model_dir == actual_dir:
                        model_correct += 1

                    if model_dir in by_direction:
                        by_direction[model_dir]['total'] += 1
                        if model_dir == actual_dir:
                            by_direction[model_dir]['correct'] += 1
                except Exception:
                    continue

            accuracy = model_correct / model_total if model_total > 0 else 0.0

            dir_accuracy: Dict[str, float] = {}
            for d, stats in by_direction.items():
                if stats['total'] > 0:
                    dir_accuracy[d] = round(
                        stats['correct'] / stats['total'], 4
                    )

            # 延迟和成本从 llm_audit 估算
            avg_latency = self._estimate_model_latency(model_id, period_days)
            cost_per_call = self._estimate_model_cost(model_id, period_days)

            return {
                'model_id': model_id,
                'period_days': period_days,
                'accuracy': round(accuracy, 4),
                'total_votes': model_total,
                'correct_votes': model_correct,
                'avg_latency_ms': avg_latency,
                'cost_per_call': cost_per_call,
                'by_direction': dir_accuracy,
            }
        finally:
            conn.close()

    def compare_models(self, period_days: int = 30) -> Dict[str, Any]:
        """
        所有模型横向对比

        Returns:
            {
                'period_days': 30,
                'models': {
                    'qwen': {'accuracy': 0.62, 'total_votes': 100, ...},
                    'minimax': {'accuracy': 0.55, 'total_votes': 98, ...},
                },
                'best_model': 'qwen',
                'recommendation': '...',
                'generated_at': '...'
            }
        """
        try:
            conn = self._get_conn()
            try:
                cutoff = (
                    datetime.now() - timedelta(days=period_days)
                ).strftime('%Y-%m-%d')
                rows = conn.execute(
                    """
                    SELECT model_votes FROM voting_predictions
                    WHERE prediction_date >= ? AND outcome_match IS NOT NULL
                    """,
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()

            # 收集所有出现过的模型 ID
            all_models = set()
            for row in rows:
                try:
                    votes_raw = row['model_votes']
                    votes = json.loads(votes_raw) if votes_raw else {}
                    if isinstance(votes, dict):
                        all_models.update(votes.keys())
                except Exception:
                    continue

            # 对每个模型获取 benchmark
            models: Dict[str, Any] = {}
            for model_id in all_models:
                benchmark = self.get_model_benchmark(model_id, period_days)
                if benchmark.get('total_votes', 0) > 0:
                    models[model_id] = benchmark

            # 找最佳模型
            best_model = (
                max(
                    models.items(),
                    key=lambda x: x[1].get('accuracy') or 0,
                )[0]
                if models else None
            )

            # 生成建议
            recommendation = self._generate_model_recommendation(models)

            return {
                'period_days': period_days,
                'models': models,
                'best_model': best_model,
                'recommendation': recommendation,
                'generated_at': datetime.now().isoformat(),
            }
        except Exception as e:
            logger.debug(f"[ModelBenchmark] compare_models 失败: {e}")
            return {
                'period_days': period_days,
                'models': {},
                'error': str(e),
            }

    def _estimate_model_latency(
        self, model_id: str, period_days: int
    ) -> Optional[float]:
        """从 audit 日志估算模型平均延迟"""
        try:
            audit_dir = (
                Path(__file__).resolve().parent.parent.parent
                / "data" / "llm_audit"
            )
            latencies = []
            for i in range(period_days):
                day = (
                    datetime.now() - timedelta(days=i)
                ).strftime('%Y-%m-%d')
                audit_file = audit_dir / f"audit_{day}.jsonl"
                if not audit_file.exists():
                    continue
                with open(audit_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if model_id in (entry.get('model', '') or ''):
                                lat = entry.get('latency_ms', 0) or 0
                                if lat > 0:
                                    latencies.append(lat)
                        except Exception:
                            continue
            return (
                round(sum(latencies) / len(latencies)) if latencies else None
            )
        except Exception:
            return None

    def _estimate_model_cost(
        self, model_id: str, period_days: int
    ) -> Optional[float]:
        """从 audit 日志估算模型每次调用成本"""
        try:
            audit_dir = (
                Path(__file__).resolve().parent.parent.parent
                / "data" / "llm_audit"
            )
            costs = []
            for i in range(min(period_days, 7)):  # 最多看 7 天
                day = (
                    datetime.now() - timedelta(days=i)
                ).strftime('%Y-%m-%d')
                audit_file = audit_dir / f"audit_{day}.jsonl"
                if not audit_file.exists():
                    continue
                with open(audit_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if model_id in (entry.get('model', '') or ''):
                                cost = entry.get('cost_usd', 0) or 0
                                if cost > 0:
                                    costs.append(cost)
                        except Exception:
                            continue
            return round(sum(costs) / len(costs), 5) if costs else None
        except Exception:
            return None

    def _generate_model_recommendation(self, models: Dict[str, Any]) -> str:
        """基于模型对比生成建议"""
        if not models:
            return "数据不足，无法生成建议"

        accuracies = {
            m: (d.get('accuracy') or 0) for m, d in models.items()
        }
        if not accuracies:
            return "数据不足，无法生成建议"

        best = max(accuracies, key=lambda k: accuracies[k])
        worst = min(accuracies, key=lambda k: accuracies[k])

        if accuracies[best] - accuracies[worst] > 0.15:
            return (
                f"建议: {worst} 准确率显著低于 {best} "
                f"({accuracies[worst]:.0%} vs {accuracies[best]:.0%})，"
                f"考虑降低其权重或替换"
            )
        elif all(v < 0.5 for v in accuracies.values()):
            return "警告: 所有模型准确率低于50%，建议审查 prompt 或输入数据质量"
        else:
            return f"模型表现均衡，最佳: {best} ({accuracies[best]:.0%})"


# 单例
_tracker_instance: Optional[VotingAccuracyTracker] = None
_tracker_lock = threading.Lock()


def get_voting_accuracy_tracker() -> VotingAccuracyTracker:
    global _tracker_instance
    if _tracker_instance is None:
        with _tracker_lock:
            if _tracker_instance is None:
                _tracker_instance = VotingAccuracyTracker()
    return _tracker_instance
