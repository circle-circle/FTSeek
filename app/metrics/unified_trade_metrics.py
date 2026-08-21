"""
统一交易口径层 (Unified Trade Metrics)

目标: 为复盘、OKR、Dashboard 提供单一、一致的交易统计真相源。

设计原则:
1. source_type / owner_strategy / trade_mode 三列语义统一, 所有报告走同一过滤常量。
2. 胜率/PnL/IC/触发率等核心指标只在此层实现一次, 上层只做展示。
3. Playbook 系统 OKR 只统计 Playbook 相关交易, 排除已废弃/禁用策略。
"""
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime, date
import sqlite3
import json

from app.storage.db_pool import get_db
from app.playbook.db import PlaybookDB


# ─── 统一来源分类学 ───────────────────────────────────────────────
# 所有报告层必须复用这些常量, 禁止自行硬编码.

class SourceTaxonomy:
    """统一 source_type 白名单 / 黑名单"""

    # Playbook 系统相关 (含已废弃的 SHADOW, 用于历史对比)
    PLAYBOOK_ALL = ('PLAYBOOK_SHADOW', 'PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'MANUAL_PLAYBOOK')

    # 当前活跃的真实/模拟 Playbook 交易
    PLAYBOOK_ACTIVE = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')

    # 已废弃/禁用策略, 默认从 OKR/复盘主统计中排除
    EXCLUDED = ('PLAYBOOK_SHADOW', 'SCALPING')

    # 非 Playbook 的主线/辅助策略
    NON_PLAYBOOK = ('MAIN_PIPELINE', 'ORB_ENGINE', 'MANUAL', 'AUTO_MAIN')


# ─── 统一指标数据类 ───────────────────────────────────────────────
@dataclass
class TradeStats:
    total: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── 核心统一查询 ───────────────────────────────────────────────
class UnifiedMetrics:
    """统一交易指标查询入口"""

    def __init__(self):
        self._db = get_db("ftseek_us")
        self._playbook_db = PlaybookDB()
        self._playbook_db.ensure_tables()

    # ── trade_records 查询 ───────────────────────────────────────
    def get_trade_records_stats(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_types: Tuple[str, ...] = None,
        exclude_source_types: Tuple[str, ...] = None,
    ) -> TradeStats:
        """
        统一交易记录统计.

        Args:
            start_date: 起始日期 (含), 格式 'YYYY-MM-DD'
            end_date: 结束日期 (含), 格式 'YYYY-MM-DD'
            source_types: 只包含这些 source_type (None 表示不过滤)
            exclude_source_types: 排除这些 source_type (None 表示不排除)
        """
        conn = self._db.connection()
        where_clauses = ["exit_price > 0"]
        params: List[Any] = []

        if start_date:
            where_clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("trade_date <= ?")
            params.append(end_date)
        if source_types:
            placeholders = ", ".join("?" for _ in source_types)
            where_clauses.append(f"source_type IN ({placeholders})")
            params.extend(source_types)
        if exclude_source_types:
            placeholders = ", ".join("?" for _ in exclude_source_types)
            where_clauses.append(f"source_type NOT IN ({placeholders})")
            params.extend(exclude_source_types)

        sql = f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN realized_pnl = 0 THEN 1 ELSE 0 END) as breakeven,
                SUM(realized_pnl) as total_pnl
            FROM trade_records
            WHERE {' AND '.join(where_clauses)}
        """
        row = conn.execute(sql, tuple(params)).fetchone()
        if not row:
            return TradeStats()

        total = row["total"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        has_outcome = wins + losses
        return TradeStats(
            total=int(total),
            wins=int(wins),
            losses=int(losses),
            breakeven=int(row["breakeven"] or 0),
            total_pnl=float(row["total_pnl"] or 0),
            win_rate=(wins / has_outcome * 100) if has_outcome > 0 else 0.0,
        )

    def get_playbook_trade_stats(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> TradeStats:
        """Playbook 系统交易统计 (排除已废弃/禁用策略)"""
        return self.get_trade_records_stats(
            start_date=start_date,
            end_date=end_date,
            source_types=SourceTaxonomy.PLAYBOOK_ALL,
            exclude_source_types=SourceTaxonomy.EXCLUDED,
        )

    # ── playbook_instructions 查询 ─────────────────────────────────
    def get_playbook_instruction_stats(self, limit: int = 500) -> Dict[str, Any]:
        """Playbook 指令级统计"""
        instructions = self._playbook_db.get_recent_instructions(limit=limit, include_skip=True)
        total = len(instructions)
        skip_count = sum(1 for i in instructions if i.get('direction') == 'SKIP')
        non_skip = total - skip_count
        triggered = sum(1 for i in instructions if i.get('triggered'))
        executed = sum(1 for i in instructions if i.get('executed'))
        wins = sum(1 for i in instructions if i.get('outcome') == 'win')
        losses = sum(1 for i in instructions if i.get('outcome') == 'loss')
        has_outcome = wins + losses

        # PnL 只统计已执行且终态 win/loss 的指令
        total_pnl = sum(
            (i.get('pnl') or 0)
            for i in instructions
            if i.get('executed') and i.get('outcome') in ('win', 'loss')
        )

        return {
            "total": total,
            "skip_count": skip_count,
            "non_skip": non_skip,
            "triggered": triggered,
            "executed": executed,
            "wins": wins,
            "losses": losses,
            "has_outcome": has_outcome,
            "win_rate": (wins / has_outcome * 100) if has_outcome > 0 else 0.0,
            "trigger_rate": (triggered / non_skip * 100) if non_skip > 0 else 0.0,
            "total_pnl": total_pnl,
        }

    # ── 周度 PnL ───────────────────────────────────────────────────
    def get_weekly_pnl(self, source_types: Tuple[str, ...] = None, weeks: int = 8) -> Dict[str, Dict[str, Any]]:
        """按周聚合已执行 win/loss 指令的 PnL"""
        instructions = self._playbook_db.get_recent_instructions(limit=1000)
        weekly: Dict[str, Dict[str, Any]] = {}
        for inst in instructions:
            if not (inst.get('executed') and inst.get('outcome') in ('win', 'loss')):
                continue
            d = inst.get('date')
            if not d:
                continue
            wk = datetime.strptime(d, '%Y-%m-%d').strftime('%Y-W%W')
            if wk not in weekly:
                weekly[wk] = {'pnl': 0.0, 'trades': 0, 'wins': 0, 'losses': 0}
            p = inst.get('pnl') or 0
            weekly[wk]['pnl'] += p
            weekly[wk]['trades'] += 1
            if p > 0:
                weekly[wk]['wins'] += 1
            elif p < 0:
                weekly[wk]['losses'] += 1

        # 只返回最近 N 周
        sorted_keys = sorted(weekly.keys(), reverse=True)[:weeks]
        return {k: weekly[k] for k in sorted_keys}


# ─── 便捷函数 ───────────────────────────────────────────────────
def get_unified_metrics() -> UnifiedMetrics:
    return UnifiedMetrics()
