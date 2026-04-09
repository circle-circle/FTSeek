"""
模拟盘增强 API - 支持美股/港股区分和交易 - 信号 - 策略联动
"""
from fastapi import APIRouter, Query, HTTPException
from typing import List, Dict, Any, Optional
from datetime import datetime
import sqlite3
from pathlib import Path

router = APIRouter()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "simulation.db"

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

@router.get("/positions")
async def get_positions(market: Optional[str] = None):
    """
    获取持仓列表
    
    Args:
        market: 市场过滤 (HK/US)
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if market:
        market = market.upper()
        if market == 'HK':
            query = """
                SELECT *, 
                    CASE 
                        WHEN code LIKE 'HK.%' THEN 'HK'
                        WHEN code LIKE 'US.%' THEN 'US'
                        WHEN code LIKE 'HK.%' OR length(code) = 5 THEN 'HK'
                        ELSE 'UNKNOWN'
                    END as market_type
                FROM simulation_positions
                WHERE code LIKE 'HK.%' OR code LIKE 'HK%' OR length(code) = 5
            """
        elif market == 'US':
            query = """
                SELECT *, 
                    CASE 
                        WHEN code LIKE 'US.%' THEN 'US'
                        WHEN code LIKE 'HK.%' THEN 'HK'
                        ELSE 'UNKNOWN'
                    END as market_type
                FROM simulation_positions
                WHERE code LIKE 'US.%'
            """
        else:
            query = """
                SELECT *, 
                    CASE 
                        WHEN code LIKE 'HK.%' OR length(code) = 5 THEN 'HK'
                        WHEN code LIKE 'US.%' THEN 'US'
                        ELSE 'UNKNOWN'
                    END as market_type
                FROM simulation_positions
            """
    else:
        query = """
            SELECT *, 
                CASE 
                    WHEN code LIKE 'HK.%' OR length(code) = 5 THEN 'HK'
                    WHEN code LIKE 'US.%' THEN 'US'
                    ELSE 'UNKNOWN'
                END as market_type
            FROM simulation_positions
        """
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    positions = []
    for row in rows:
        pos = dict(row)
        # 格式化代码
        if 'code' in pos and pos['code'] and not pos['code'].startswith(('HK.', 'US.')):
            if len(pos['code']) == 5 and pos['code'].isdigit():
                pos['code'] = f"HK.{pos['code']}"
                pos['market_type'] = 'HK'
        positions.append(pos)
    
    conn.close()
    
    # 按市场分组统计
    hk_positions = [p for p in positions if p.get('market_type') == 'HK']
    us_positions = [p for p in positions if p.get('market_type') == 'US']
    
    return {
        "status": "ok",
        "data": positions,
        "summary": {
            "total": len(positions),
            "hk_count": len(hk_positions),
            "us_count": len(us_positions),
            "hk_market_value": sum(p.get('market_val', 0) or 0 for p in hk_positions),
            "us_market_value": sum(p.get('market_val', 0) or 0 for p in us_positions)
        }
    }

@router.get("/trades")
async def get_trades(
    market: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """
    获取交易记录（关联信号和策略）
    
    Args:
        market: 市场过滤 (HK/US)
        status: 状态过滤 (open/closed)
        limit: 返回数量限制
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 查询交易记录，关联信号和策略
    query = """
        SELECT 
            t.*,
            s.strategy as signal_strategy,
            s.confidence as signal_confidence,
            s.signal_time,
            s.reason as signal_reason
        FROM simulation_trades t
        LEFT JOIN simulation_signals s ON t.signal_id = s.id
        WHERE 1=1
    """
    params = []
    
    if market:
        market = market.upper()
        if market == 'HK':
            query += " AND (t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*'))"
        elif market == 'US':
            query += " AND t.code LIKE 'US.%'"
    
    if status:
        query += " AND t.status = ?"
        params.append(status)
    
    query += " ORDER BY t.entry_time DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    trades = []
    for row in rows:
        trade = dict(row)
        # 格式化代码
        if 'code' in trade and trade['code'] and not trade['code'].startswith(('HK.', 'US.')):
            if len(trade['code']) == 5 and trade['code'].isdigit():
                trade['code'] = f"HK.{trade['code']}"
                trade['market'] = 'HK'
            elif trade['code'].startswith('US.'):
                trade['market'] = 'US'
            else:
                trade['market'] = 'UNKNOWN'
        trades.append(trade)
    
    conn.close()
    
    # 按市场分组统计
    hk_trades = [t for t in trades if t.get('market') == 'HK']
    us_trades = [t for t in trades if t.get('market') == 'US']
    
    return {
        "status": "ok",
        "data": trades,
        "summary": {
            "total": len(trades),
            "hk_count": len(hk_trades),
            "us_count": len(us_trades),
            "open_count": sum(1 for t in trades if t.get('status') == 'open'),
            "closed_count": sum(1 for t in trades if t.get('status') == 'closed')
        }
    }

@router.get("/strategy-performance")
async def get_strategy_performance():
    """
    获取策略表现（按策略分组统计）
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
        SELECT 
            COALESCE(s.strategy, 'Unknown') as strategy_name,
            COUNT(t.id) as total_trades,
            SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN t.pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            COALESCE(SUM(t.pnl), 0) as total_pnl,
            COALESCE(AVG(t.pnl), 0) as avg_pnl,
            COALESCE(AVG(t.pnl_percent), 0) as avg_pnl_percent
        FROM simulation_trades t
        LEFT JOIN simulation_signals s ON t.signal_id = s.id
        WHERE t.status = 'closed'
        GROUP BY s.strategy
        ORDER BY total_pnl DESC
    """
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    strategies = []
    for row in rows:
        strat = dict(row)
        strat['win_rate'] = strat['winning_trades'] / strat['total_trades'] * 100 if strat['total_trades'] > 0 else 0
        strategies.append(strat)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": strategies
    }

@router.get("/signal-linkage")
async def get_signal_linkage(signal_id: Optional[str] = None):
    """
    获取信号联动信息（信号→交易→持仓）
    
    Args:
        signal_id: 信号 ID，不传则返回所有
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if signal_id:
        query = """
            SELECT 
                s.id as signal_id,
                s.code,
                s.action,
                s.strategy,
                s.confidence,
                s.signal_time,
                s.reason,
                t.id as trade_id,
                t.entry_price,
                t.quantity,
                t.status as trade_status,
                t.pnl,
                p.id as position_id,
                p.current_quantity,
                p.unrealized_pnl
            FROM simulation_signals s
            LEFT JOIN simulation_trades t ON s.id = t.signal_id
            LEFT JOIN simulation_positions p ON t.code = p.code AND p.direction = s.action
            WHERE s.id = ?
            ORDER BY s.signal_time DESC
        """
        cursor.execute(query, (signal_id,))
    else:
        query = """
            SELECT 
                s.id as signal_id,
                s.code,
                s.action,
                s.strategy,
                s.confidence,
                s.signal_time,
                t.id as trade_id,
                t.entry_price,
                t.quantity,
                t.status as trade_status,
                t.pnl
            FROM simulation_signals s
            LEFT JOIN simulation_trades t ON s.id = t.signal_id
            ORDER BY s.signal_time DESC
            LIMIT 100
        """
        cursor.execute(query)
    
    rows = cursor.fetchall()
    
    linkages = []
    for row in rows:
        linkage = dict(row)
        # 格式化代码
        if 'code' in linkage and linkage['code'] and not linkage['code'].startswith(('HK.', 'US.')):
            if len(linkage['code']) == 5 and linkage['code'].isdigit():
                linkage['code'] = f"HK.{linkage['code']}"
        linkages.append(linkage)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": linkages
    }

@router.get("/dashboard-summary")
async def get_dashboard_summary():
    """
    大盘总览（区分美股/港股）
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 港股持仓统计
    cursor.execute("""
        SELECT 
            COUNT(*) as count,
            COALESCE(SUM(quantity * current_price), 0) as market_value,
            COALESCE(SUM(unrealized_pnl), 0) as unrealized_pnl
        FROM simulation_positions
        WHERE code LIKE 'HK.%' OR code LIKE 'HK%' OR (length(code) = 5 AND code GLOB '[0-9]*')
    """)
    hk_stats = dict(cursor.fetchone())
    
    # 美股持仓统计
    cursor.execute("""
        SELECT 
            COUNT(*) as count,
            COALESCE(SUM(quantity * current_price), 0) as market_value,
            COALESCE(SUM(unrealized_pnl), 0) as unrealized_pnl
        FROM simulation_positions
        WHERE code LIKE 'US.%'
    """)
    us_stats = dict(cursor.fetchone())
    
    # 今日交易统计
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute("""
        SELECT 
            COUNT(*) as count,
            COALESCE(SUM(pnl), 0) as total_pnl
        FROM simulation_trades
        WHERE date(entry_time) = ?
    """, (today,))
    today_stats = dict(cursor.fetchone())
    
    # 策略统计
    cursor.execute("""
        SELECT 
            COUNT(DISTINCT strategy) as strategy_count
        FROM simulation_signals
    """)
    strategy_count = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "hk_positions": {
                "count": hk_stats.get('count', 0),
                "market_value": hk_stats.get('market_value', 0),
                "unrealized_pnl": hk_stats.get('unrealized_pnl', 0)
            },
            "us_positions": {
                "count": us_stats.get('count', 0),
                "market_value": us_stats.get('market_value', 0),
                "unrealized_pnl": us_stats.get('unrealized_pnl', 0)
            },
            "today_trades": {
                "count": today_stats.get('count', 0),
                "total_pnl": today_stats.get('total_pnl', 0)
            },
            "strategies": {
                "count": strategy_count
            }
        }
    }
