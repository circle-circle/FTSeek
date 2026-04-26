#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 归因链系统 (Attribution Chain)

功能:
1. 记录决策现场快照 (Input & Process) -> 写入 trade_attribution
2. 追踪交易执行偏差 (Slippage)
3. 计算最终归因 (Alpha vs Beta)

2026-04-26 收敛：
- 合并原 app/attribution.py 单文件（record_entry / record_exit / init_db）至本包。
- 新增 update_execution_result，修复 executor.py 长期 ImportError 导致的归因断链。
- 统一由 app/attribution/__init__.py 显式导出，避免 import shadowing。
- 【B3-13】统一走 app.storage.repository.get_db 连接池，避免裸 sqlite3.connect。
"""
import sqlite3
import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, Any

from app.storage.repository import get_db as _pool_get_db

logger = logging.getLogger("ftseek.attribution")

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"


@contextmanager
def _get_db_ctx():
    """统一通过连接池借出连接；进入 with 即可用，退出时自动归还/commit/rollback。"""
    with _pool_get_db(DB_PATH) as conn:
        yield conn


def ensure_attribution_schema():
    """确保归因表结构存在（兼容旧库一次性创建）"""
    try:
        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_attribution (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT,
                    code TEXT,
                    event_type TEXT,
                    signal_price REAL,
                    actual_price REAL,
                    pnl REAL,
                    audit_data TEXT,
                    timestamp TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_attr_trace ON trade_attribution(trace_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_attr_code  ON trade_attribution(code)")
        return True
    except Exception as e:
        logger.error(f"❌ 归因表初始化失败: {e}")
        return False


# -----------------------------------------------------------------------------
#  决策入口：拦截 / 批准
# -----------------------------------------------------------------------------

def record_rejection(trace_id: str, code: str, reason: str, context: Dict = None):
    """记录被拒绝的信号（Input/Process 断裂点）"""
    try:
        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO trade_attribution
                    (trace_id, code, event_type, signal_price, actual_price, pnl, audit_data, timestamp)
                    VALUES (?, ?, 'REJECT', 0.0, 0.0, 0.0, ?, ?)
                ''', (
                    trace_id,
                    code,
                    json.dumps({"rejection_reason": reason, "context": context or {}}, ensure_ascii=False),
                    datetime.now().isoformat(),
                ))
                logger.debug(f"📦 归因快照 [拒绝]: {trace_id} ({code})")
    except Exception as e:
        logger.error(f"记录拒绝事件失败: {e}")


def record_approval(trace_id: str, code: str, action: str, market_regime: str, scores: Dict, context: Dict = None):
    """记录被批准的信号（统一字段）"""
    try:
        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if cursor.fetchone():
                cursor.execute('''
                    UPDATE trade_attribution
                    SET event_type = 'APPROVED',
                        audit_data = ?,
                        signal_score = ?,
                        direction_tags = ?,
                        strategy_tags = ?
                    WHERE trace_id = ?
                ''', (
                    json.dumps({"scores": scores, "context": context or {}}, ensure_ascii=False),
                    scores.get('composite_score', 0),
                    json.dumps({"action": action}),
                    json.dumps({"regime": market_regime}),
                    trace_id,
                ))
            else:
                cursor.execute('''
                    INSERT INTO trade_attribution
                    (trace_id, code, event_type, signal_price, actual_price, pnl, audit_data, timestamp,
                     signal_score, direction_tags, strategy_tags)
                    VALUES (?, ?, 'APPROVED', 0.0, 0.0, 0.0, ?, ?, ?, ?, ?)
                ''', (
                    trace_id,
                    code,
                    json.dumps({"scores": scores, "context": context or {}}, ensure_ascii=False),
                    datetime.now().isoformat(),
                    scores.get('composite_score', 0),
                    json.dumps({"action": action}),
                    json.dumps({"regime": market_regime}),
                ))
            logger.debug(f"✅ 归因快照 [批准]: {trace_id} ({code})")
    except Exception as e:
        logger.error(f"记录批准事件失败: {e}")


# -----------------------------------------------------------------------------
#  执行入口：开仓 / 平仓 / 执行偏差
# -----------------------------------------------------------------------------

def record_entry(trace_id: str, code: str, signal_price: float, actual_price: float, context: Dict = None):
    """记录开仓执行归因（关注滑点）"""
    try:
        slippage = actual_price - signal_price if signal_price else 0
        slippage_pct = (slippage / signal_price) if signal_price else 0

        audit_data = {
            "signal_price": signal_price,
            "actual_price": actual_price,
            "slippage": round(slippage, 4),
            "slippage_pct": round(slippage_pct * 100, 2),
            "context": context or {},
        }

        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if cursor.fetchone():
                cursor.execute('''
                    UPDATE trade_attribution
                    SET event_type = 'OPEN',
                        signal_price = ?,
                        actual_price = ?,
                        audit_data = ?,
                        slippage_bps = ?,
                        timestamp = ?
                    WHERE trace_id = ?
                ''', (
                    signal_price, actual_price,
                    json.dumps(audit_data, ensure_ascii=False),
                    round(slippage_pct * 10000, 2),
                    datetime.now().isoformat(),
                    trace_id,
                ))
            else:
                cursor.execute('''
                    INSERT INTO trade_attribution
                    (trace_id, code, event_type, signal_price, actual_price, audit_data, timestamp, slippage_bps)
                    VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?)
                ''', (
                    trace_id, code, signal_price, actual_price,
                    json.dumps(audit_data, ensure_ascii=False),
                    datetime.now().isoformat(),
                    round(slippage_pct * 10000, 2),
                ))
    except Exception as e:
        logger.debug(f"归因记录失败 (Entry): {e}")


def record_exit(trace_id: str, code: str, exit_price: float, pnl: float, context: Dict = None):
    """记录平仓归因（关注结果与预期偏差）"""
    try:
        audit_data = {
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "context": context or {},
        }
        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO trade_attribution
                (trace_id, code, event_type, actual_price, pnl, audit_data, timestamp)
                VALUES (?, ?, 'CLOSE', ?, ?, ?, ?)
            ''', (
                trace_id, code, exit_price, pnl,
                json.dumps(audit_data, ensure_ascii=False),
                datetime.now().isoformat(),
            ))
    except Exception as e:
        logger.debug(f"归因记录失败 (Exit): {e}")


def update_execution_result(trace_id: str, actual_price: float, slippage: float):
    """
    【2026-04-26 新增】开仓落地后立即更新执行结果（真实成交价 / 滑点）。
    原 executor.py 调用 from app.attribution.chain import update_execution_result，
    但该函数此前从未定义，导致 ImportError 被 except 吞掉，归因执行偏差字段长期为空。
    """
    try:
        if not trace_id:
            return
        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trace_id, signal_price FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            row = cursor.fetchone()
            slip_bps = 0.0
            if row and row[1]:
                try:
                    slip_bps = round((slippage / float(row[1])) * 10000, 2)
                except Exception:
                    slip_bps = 0.0
            if row:
                cursor.execute('''
                    UPDATE trade_attribution
                    SET actual_price = ?,
                        slippage_bps = ?,
                        timestamp    = ?
                    WHERE trace_id = ?
                ''', (actual_price, slip_bps, datetime.now().isoformat(), trace_id))
            else:
                # 兜底：罕见情况下先到达执行结果，尚无 APPROVED/OPEN 记录
                cursor.execute('''
                    INSERT INTO trade_attribution
                    (trace_id, event_type, actual_price, slippage_bps, timestamp)
                    VALUES (?, 'EXEC', ?, ?, ?)
                ''', (trace_id, actual_price, slip_bps, datetime.now().isoformat()))
    except Exception as e:
        logger.debug(f"归因记录失败 (Execution): {e}")


def update_trade_attribution_result(
    trace_id: str,
    code: str,
    pnl: float,
    exit_reason: str,
    holding_hours: float,
    signal_strength: int = 0,
    signal_confidence: float = 0.0,
):
    """交易平仓后，更新 trade_attribution 表的归因数据"""
    try:
        with _get_db_ctx() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if cursor.fetchone() is None:
                logger.warning(f"归因记录不存在: {trace_id}，跳过更新")
                return

            is_win = pnl > 0
            pnl_category = "win" if is_win else "loss"
            result_tags = json.dumps({
                "pnl_category": pnl_category,
                "exit_reason": exit_reason,
                "is_win": is_win,
            })
            strategy_fit = 1 if (signal_confidence > 0.6 and is_win) else 0
            base_score = 60
            pnl_bonus = min(30, abs(pnl / 1000) * 100) if pnl > 0 else 0
            final_score = max(0, min(100, int(base_score + pnl_bonus)))

            cursor.execute('''
                UPDATE trade_attribution
                SET pnl               = ?,
                    pnl_category      = ?,
                    exit_reason       = ?,
                    result_tags       = ?,
                    direction_correct = ?,
                    strategy_fit      = ?,
                    overall_score     = ?,
                    success_factors   = ?,
                    failure_factors   = ?,
                    timestamp         = ?
                WHERE trace_id = ?
            ''', (
                pnl, pnl_category, exit_reason, result_tags,
                1 if is_win else 0, strategy_fit, final_score,
                json.dumps({"exit_reason": exit_reason}),
                json.dumps({}),
                datetime.now().isoformat(),
                trace_id,
            ))
    except Exception as e:
        logger.error(f"更新归因记录失败: {e}")


def get_attribution_report(days: int = 7) -> list:
    """获取归因报告数据"""
    try:
        with _get_db_ctx() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM trade_attribution
                WHERE date(timestamp) >= date('now', ?)
                ORDER BY timestamp DESC
            ''', (f'-{days} days',))
            rows = cursor.fetchall()

            report = []
            for row in rows:
                d = dict(row)
                if 'audit_data' in d and d['audit_data']:
                    try:
                        d['audit_data'] = json.loads(d['audit_data'])
                    except Exception:
                        pass
                report.append(d)
            return report
    except Exception as e:
        logger.error(f"获取归因报告失败: {e}")
        return []


# 模块加载时确保表结构存在
ensure_attribution_schema()
