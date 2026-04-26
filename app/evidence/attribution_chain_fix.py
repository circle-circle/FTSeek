#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
归因链路修复 (Attribution Chain Fix) - Phase 1

职责:
1. 修复 trade_attribution 保存方法以包含所有字段
2. 确保归因结果正确写入数据库
3. 提供回溯查询接口
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.storage.repository import get_db as _pool_get_db  # B3-13 连接池

logger = logging.getLogger("ftseek.evidence.attribution_fix")


def fix_save_trade_attribution(db_conn, attribution_data: Dict[str, Any]) -> bool:
    """
    修复版归因数据保存 -- 包含所有字段。
    """
    try:
        cursor = db_conn.cursor()
        cursor.execute("""
            INSERT INTO trade_attribution
            (trace_id, result_tags, pnl_category, direction_tags, direction_correct,
             scene_tags, scene_correct, strategy_tags, strategy_fit, signal_tags,
             signal_quality, execution_tags, execution_quality, slippage_category,
             slippage_bps, risk_tags, risk_quality, position_quality, timing_tags,
             timing_quality, entry_timing, exit_timing, direction_score, scene_score,
             strategy_score, signal_score, execution_score, timing_score, risk_score,
             overall_score, success_factors, failure_factors,
             code, event_type, signal_price, actual_price, pnl, audit_data, timestamp, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            attribution_data.get('trace_id'),
            _to_json(attribution_data.get('result_tags')),
            attribution_data.get('pnl_category'),
            _to_json(attribution_data.get('direction_tags')),
            bool(attribution_data.get('direction_correct', False)),
            _to_json(attribution_data.get('scene_tags')),
            bool(attribution_data.get('scene_correct', False)),
            _to_json(attribution_data.get('strategy_tags')),
            bool(attribution_data.get('strategy_fit', False)),
            _to_json(attribution_data.get('signal_tags')),
            attribution_data.get('signal_quality'),
            _to_json(attribution_data.get('execution_tags')),
            attribution_data.get('execution_quality'),
            attribution_data.get('slippage_category'),
            float(attribution_data.get('slippage_bps', 0)),
            _to_json(attribution_data.get('risk_tags')),
            attribution_data.get('risk_quality'),
            attribution_data.get('position_quality'),
            _to_json(attribution_data.get('timing_tags')),
            attribution_data.get('timing_quality'),
            attribution_data.get('entry_timing'),
            attribution_data.get('exit_timing'),
            int(attribution_data.get('direction_score', 0)),
            int(attribution_data.get('scene_score', 0)),
            int(attribution_data.get('strategy_score', 0)),
            int(attribution_data.get('signal_score', 0)),
            int(attribution_data.get('execution_score', 0)),
            int(attribution_data.get('timing_score', 0)),
            int(attribution_data.get('risk_score', 0)),
            int(attribution_data.get('overall_score', 0)),
            _to_json(attribution_data.get('success_factors')),
            _to_json(attribution_data.get('failure_factors')),
            attribution_data.get('code', ''),
            attribution_data.get('event_type', ''),
            float(attribution_data.get('signal_price', 0)),
            float(attribution_data.get('actual_price', 0)),
            float(attribution_data.get('pnl', 0)),
            _to_json(attribution_data.get('audit_data')),
            attribution_data.get('timestamp', datetime.now().isoformat()),
            attribution_data.get('exit_reason', ''),
        ))
        db_conn.commit()
        logger.info(f"[归因保存] {attribution_data.get('trace_id')}: 成功保存")
        return True
    except Exception as e:
        logger.error(f"[归因保存] {attribution_data.get('trace_id')}: {e}", exc_info=True)
        return False


def _to_json(value):
    """将值转换为 JSON 字符串"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class AttributionQueryEngine:
    """
    归因回溯查询引擎 -- 提供 trace_id 全链视图。
    """
    
    def __init__(self, db_path: str = "data/ftseek.db"):
        self.db_path = db_path
    
    def _get_conn(self):
        # B3-13：连接池 + Proxy（sqlite3.Connection.close 只读，用代理类封装 close）
        from app.storage.repository import get_pool as _gp
        from app.data.cross_market_event_cache import _PooledConnProxy
        _pool = _gp(self.db_path)
        conn = _pool.get_connection()
        return _PooledConnProxy(_pool, conn)
    
    def get_full_chain(self, trace_id: str) -> Optional[Dict]:
        """
        获取指定 trace_id 的完整证据链。
        
        Returns:
            {
                'position': {...},
                'trade_chain': {...},
                'attribution': {...},
                'feedback': {...}
            }
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            
            # 1. 查询 positions
            cursor.execute(
                "SELECT * FROM positions WHERE trace_id = ? ORDER BY entry_time DESC LIMIT 1",
                (trace_id,)
            )
            pos_cols = [d[0] for d in cursor.description]
            pos_row = cursor.fetchone()
            position = dict(zip(pos_cols, pos_row)) if pos_row else None
            
            # 2. 查询 trade_chains
            cursor.execute(
                "SELECT * FROM trade_chains WHERE trade_id LIKE ? ORDER BY created_at DESC LIMIT 1",
                (f"%{trace_id}%",)
            )
            tc_cols = [d[0] for d in cursor.description]
            tc_row = cursor.fetchone()
            trade_chain = dict(zip(tc_cols, tc_row)) if tc_row else None
            
            # 3. 查询 trade_attribution
            cursor.execute(
                "SELECT * FROM trade_attribution WHERE trace_id = ? ORDER BY created_at DESC LIMIT 1",
                (trace_id,)
            )
            attr_cols = [d[0] for d in cursor.description]
            attr_row = cursor.fetchone()
            attribution = dict(zip(attr_cols, attr_row)) if attr_row else None
            
            # 4. 查询 trade_feedback
            cursor.execute(
                "SELECT * FROM trade_feedback WHERE trace_id = ? ORDER BY created_at DESC LIMIT 1",
                (trace_id,)
            )
            fb_cols = [d[0] for d in cursor.description]
            fb_row = cursor.fetchone()
            feedback = dict(zip(fb_cols, fb_row)) if fb_row else None
            
            return {
                "trace_id": trace_id,
                "position": position,
                "trade_chain": trade_chain,
                "attribution": attribution,
                "feedback": feedback,
            }
        finally:
            conn.close()
    
    def query_trades(
        self,
        start_time: str = None,
        end_time: str = None,
        code: str = None,
        strategy: str = None,
        pnl_min: float = None,
        pnl_max: float = None,
        limit: int = 50,
    ) -> List[Dict]:
        """
        查询交易列表，支持多维度过滤。
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            
            query = """
                SELECT p.trace_id, p.code, p.strategy, p.entry_time, p.exit_time,
                       p.entry_price, p.exit_price, p.realized_pl, p.pl_ratio,
                       p.status,
                       a.overall_score, a.direction_correct, a.pnl_category,
                       a.success_factors, a.failure_factors
                FROM positions p
                LEFT JOIN trade_attribution a ON p.trace_id = a.trace_id
                WHERE p.trace_id != '' AND p.trace_id IS NOT NULL
            """
            params = []
            
            if start_time:
                query += " AND p.entry_time >= ?"
                params.append(start_time)
            if end_time:
                query += " AND p.entry_time <= ?"
                params.append(end_time)
            if code:
                query += " AND p.code = ?"
                params.append(code)
            if strategy:
                query += " AND p.strategy LIKE ?"
                params.append(f"%{strategy}%")
            if pnl_min is not None:
                query += " AND p.realized_pl >= ?"
                params.append(pnl_min)
            if pnl_max is not None:
                query += " AND p.realized_pl <= ?"
                params.append(pnl_max)
            
            query += " ORDER BY p.entry_time DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            
            return [dict(zip(cols, row)) for row in rows]
        finally:
            conn.close()
    
    def get_attribution_summary(self, days: int = 30) -> Dict:
        """
        获取归因统计摘要。
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            
            # 基本统计
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN direction_correct = 1 THEN 1 ELSE 0 END) as correct_direction,
                    AVG(overall_score) as avg_score,
                    AVG(CASE WHEN realized_pl IS NOT NULL THEN realized_pl ELSE 0 END) as avg_pnl
                FROM positions p
                LEFT JOIN trade_attribution a ON p.trace_id = a.trace_id
                WHERE p.trace_id != '' AND p.trace_id IS NOT NULL
                AND p.exit_time IS NOT NULL
            """)
            
            row = cursor.fetchone()
            cols = [d[0] for d in cursor.description]
            summary = dict(zip(cols, row))
            
            # 按策略统计
            cursor.execute("""
                SELECT p.strategy, COUNT(*) as count,
                       AVG(CASE WHEN a.direction_correct = 1 THEN 1.0 ELSE 0.0 END) as win_rate,
                       AVG(a.overall_score) as avg_score
                FROM positions p
                LEFT JOIN trade_attribution a ON p.trace_id = a.trace_id
                WHERE p.trace_id != '' AND p.trace_id IS NOT NULL
                AND p.exit_time IS NOT NULL
                GROUP BY p.strategy
                ORDER BY count DESC
            """)
            
            strategy_stats = []
            for row in cursor.fetchall():
                strategy_stats.append({
                    "strategy": row[0],
                    "count": row[1],
                    "win_rate": row[2],
                    "avg_score": row[3],
                })
            
            summary["by_strategy"] = strategy_stats
            return summary
        finally:
            conn.close()
