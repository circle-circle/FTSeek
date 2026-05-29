"""
LLM 投入产出追踪器 (LLM ROI Tracker)

Task #31 (2026-05-27)

目标:
  - 聚合每日 LLM token 使用和美元成本
  - 关联有 LLM 贡献的交易 PnL
  - 输出 ROI 指标

数据源:
  - LLM 调用日志: data/llm_audit/ 目录下的 audit 文件
  - 交易记录: ftseek_us.db -> trade_records
  - 决策记录: ftseek_us.db / decisions.db -> decisions (有投票=有LLM贡献)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
)
DECISIONS_DB_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "decisions.db"
)


class LLMROITracker:
    """LLM 投入产出追踪器"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.decisions_db = DECISIONS_DB_PATH
        # LLM audit 日志目录 (model_call.py 写入的 cost/token 记录)
        self.audit_dir = (
            Path(__file__).resolve().parent.parent.parent / "data" / "llm_audit"
        )

    def calculate_daily_roi(self, date: Optional[str] = None) -> Dict[str, Any]:
        """
        计算指定日期的 LLM ROI

        Returns:
            {
                'date': '2026-05-27',
                'cost': {...},
                'revenue': {...},
                'roi': {...},
                'generated_at': '...'
            }
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')

        # 成本计算
        cost = self._get_llm_costs(date)

        # 收益计算 (有 LLM 贡献的交易)
        revenue = self._get_llm_attributed_revenue(date)

        # ROI 计算
        total_cost = cost.get('total_usd', 0) or 0
        attributed_pnl = revenue.get('llm_attributed_pnl', 0) or 0
        llm_trade_count = revenue.get('llm_trade_count', 0) or 0

        roi = {
            'value': round(attributed_pnl / total_cost, 2) if total_cost > 0 else 0,
            'cost_per_trade': (
                round(total_cost / llm_trade_count, 4)
                if llm_trade_count > 0 else 0
            ),
            'pnl_per_dollar_spent': (
                round(attributed_pnl / total_cost, 2) if total_cost > 0 else 0
            ),
        }

        return {
            'date': date,
            'cost': cost,
            'revenue': revenue,
            'roi': roi,
            'generated_at': datetime.now().isoformat(),
        }

    def _get_llm_costs(self, date: str) -> Dict[str, Any]:
        """聚合指定日期的 LLM 成本"""
        result: Dict[str, Any] = {
            'total_usd': 0,
            'by_feature': {},
            'total_tokens': 0,
            'total_calls': 0,
        }

        # 方法 1: 从 llm_audit 目录读取 (model_call.py 的 audit 日志)
        try:
            audit_file = self.audit_dir / f"audit_{date}.jsonl"
            if audit_file.exists():
                with open(audit_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            feature = entry.get('feature') or 'other'
                            cost = entry.get('cost_usd', 0) or 0
                            tokens = entry.get('total_tokens', 0) or 0

                            result['total_usd'] += cost
                            result['total_tokens'] += tokens
                            result['total_calls'] += 1
                            result['by_feature'][feature] = (
                                result['by_feature'].get(feature, 0) + cost
                            )
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.debug(f"[LLM-ROI] audit 文件读取失败: {e}")

        # 方法 2: Fallback — 从 DB 的 llm_calls 表（如果存在）
        if result['total_calls'] == 0:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT feature, SUM(cost_usd) as total_cost,
                           SUM(total_tokens) as total_tokens, COUNT(*) as calls
                    FROM llm_calls
                    WHERE date(created_at) = ?
                    GROUP BY feature
                    """,
                    (date,),
                ).fetchall()
                conn.close()

                for row in rows:
                    feature = row['feature'] or 'other'
                    cost = row['total_cost'] or 0
                    result['total_usd'] += cost
                    result['total_tokens'] += row['total_tokens'] or 0
                    result['total_calls'] += row['calls'] or 0
                    result['by_feature'][feature] = cost
            except Exception as e:
                logger.debug(f"[LLM-ROI] DB fallback 失败: {e}")

        # 四舍五入
        result['total_usd'] = round(result['total_usd'], 4)
        result['by_feature'] = {
            k: round(v, 4) for k, v in result['by_feature'].items()
        }
        return result

    def _get_llm_attributed_revenue(self, date: str) -> Dict[str, Any]:
        """获取有 LLM 贡献的交易收益"""
        result: Dict[str, Any] = {
            'llm_attributed_pnl': 0,
            'llm_trade_count': 0,
            'total_pnl': 0,
            'total_trade_count': 0,
        }

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # 获取当日所有已平仓交易
            trades = conn.execute(
                """
                SELECT symbol, direction, realized_pnl, trace_id
                FROM trade_records
                WHERE trade_date = ? AND exit_price IS NOT NULL
                """,
                (date,),
            ).fetchall()

            result['total_trade_count'] = len(trades)
            result['total_pnl'] = sum((t['realized_pnl'] or 0) for t in trades)

            conn.close()

            # 判断哪些交易有 LLM 贡献 (在 decisions 表中有对应记录)
            decisions_available = False
            try:
                dconn = sqlite3.connect(self.decisions_db)
                dconn.row_factory = sqlite3.Row
                decisions_available = True

                for trade in trades:
                    symbol = trade['symbol'] or ''
                    ticker = symbol.split('.')[-1] if '.' in symbol else symbol
                    if not ticker:
                        continue
                    # 检查是否有该标的该日的投票决策
                    try:
                        dec = dconn.execute(
                            """
                            SELECT decision_id FROM decisions
                            WHERE symbol LIKE ? AND date(created_at) = ?
                            LIMIT 1
                            """,
                            (f"%{ticker}%", date),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        # decisions 表结构不匹配,视为不可用
                        decisions_available = False
                        break

                    if dec:
                        result['llm_attributed_pnl'] += trade['realized_pnl'] or 0
                        result['llm_trade_count'] += 1

                dconn.close()
            except Exception:
                decisions_available = False

            # 如果 decisions.db 不可用，默认所有交易都有 LLM 贡献
            if not decisions_available:
                result['llm_attributed_pnl'] = result['total_pnl']
                result['llm_trade_count'] = result['total_trade_count']

        except Exception as e:
            logger.debug(f"[LLM-ROI] 收益计算失败: {e}")

        result['llm_attributed_pnl'] = round(result['llm_attributed_pnl'], 2)
        result['total_pnl'] = round(result['total_pnl'], 2)
        return result

    def get_weekly_summary(self, end_date: Optional[str] = None) -> Dict[str, Any]:
        """获取周度 ROI 汇总"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        end = datetime.strptime(end_date, '%Y-%m-%d')
        daily_reports = []

        for i in range(7):
            day = (end - timedelta(days=i)).strftime('%Y-%m-%d')
            try:
                report = self.calculate_daily_roi(day)
                daily_reports.append(report)
            except Exception as e:
                logger.debug(f"[LLM-ROI] 日报 {day} 生成失败: {e}")

        total_cost = sum(
            (r.get('cost', {}).get('total_usd') or 0) for r in daily_reports
        )
        total_pnl = sum(
            (r.get('revenue', {}).get('llm_attributed_pnl') or 0)
            for r in daily_reports
        )
        total_trades = sum(
            (r.get('revenue', {}).get('llm_trade_count') or 0)
            for r in daily_reports
        )

        return {
            'period': (
                f"{(end - timedelta(days=6)).strftime('%Y-%m-%d')} ~ {end_date}"
            ),
            'total_cost_usd': round(total_cost, 2),
            'total_attributed_pnl': round(total_pnl, 2),
            'total_llm_trades': total_trades,
            'weekly_roi': (
                round(total_pnl / total_cost, 2) if total_cost > 0 else 0
            ),
            'avg_daily_cost': round(total_cost / 7, 2),
            'generated_at': datetime.now().isoformat(),
        }


# 单例
_tracker_instance: Optional[LLMROITracker] = None


def get_llm_roi_tracker() -> LLMROITracker:
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = LLMROITracker()
    return _tracker_instance
