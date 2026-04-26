"""
FTSeek 深度复盘 API
提供完整的交易记录查询、策略分析、收益追踪、胜率统计等功能
支持按周期、时间范围、策略等多维度筛选
"""
from fastapi import APIRouter, Query, HTTPException
from app.storage.repository import get_db
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

router = APIRouter(prefix="/api/review", tags=["深度复盘"])

# 数据库路径
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")


def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

@router.get("/trades")
async def get_trades(
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    strategy: Optional[str] = Query(None, description="策略名称"),
    code: Optional[str] = Query(None, description="股票代码"),
    action: Optional[str] = Query(None, enum=["BUY", "SELL"], description="交易方向"),
    status: Optional[str] = Query(None, enum=["OPEN", "CLOSED"], description="交易状态"),
    limit: int = Query(100, ge=1, le=1000, description="返回记录数")
):
    """
    获取交易记录列表（支持多维度筛选）
    
    Returns:
        {
            "trades": [...],
            "total": 77,
            "filters": {...}
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 构建查询
    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    
    if start_date:
        query += " AND date(entry_time) >= ?"
        params.append(start_date)
    
    if end_date:
        query += " AND date(entry_time) <= ?"
        params.append(end_date)
    
    if strategy:
        query += " AND strategy LIKE ?"
        params.append(f"%{strategy}%")
    
    if code:
        query += " AND code = ?"
        params.append(code)
    
    if action:
        query += " AND action = ?"
        params.append(action)
    
    if status:
        query += " AND status = ?"
        params.append(status)
    
    query += " ORDER BY entry_time DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    trades = [dict(row) for row in cursor.fetchall()]
    
    # 获取总数
    count_query = "SELECT COUNT(*) FROM trades WHERE 1=1"
    count_params = []
    
    if start_date:
        count_query += " AND date(entry_time) >= ?"
        count_params.append(start_date)
    if end_date:
        count_query += " AND date(entry_time) <= ?"
        count_params.append(end_date)
    if strategy:
        count_query += " AND strategy LIKE ?"
        count_params.append(f"%{strategy}%")
    if code:
        count_query += " AND code = ?"
        count_params.append(code)
    if action:
        count_query += " AND action = ?"
        count_params.append(action)
    if status:
        count_query += " AND status = ?"
        count_params.append(status)
    
    cursor.execute(count_query, count_params)
    total = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "trades": trades,
            "total": total,
            "filters": {
                "start_date": start_date,
                "end_date": end_date,
                "strategy": strategy,
                "code": code,
                "action": action,
                "status": status
            }
        }
    }


@router.get("/summary")
async def get_trade_summary(
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    period: Optional[str] = Query(None, enum=["day", "week", "month", "all"], description="时间周期")
):
    """
    获取交易汇总统计
    
    Returns:
        {
            "total_trades": 77,
            "winning_trades": 45,
            "losing_trades": 32,
            "win_rate": 0.584,
            "total_pnl": 12500.50,
            "avg_pnl": 162.34,
            "avg_win": 450.20,
            "avg_loss": -280.15,
            "profit_factor": 1.61,
            "max_drawdown": -0.08,
            "sharpe_ratio": 1.25
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 计算时间范围
    now = datetime.now()
    if period == "day":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "week":
        start_date = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "month":
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    
    # 查询统计数据
    query = """
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COALESCE(AVG(pnl), 0) as avg_pnl,
            COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl ELSE NULL END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl ELSE NULL END), 0) as avg_loss,
            COALESCE(MAX(pnl), 0) as max_win,
            COALESCE(MIN(pnl), 0) as max_loss
        FROM trades
        WHERE status = 'CLOSED' AND pnl != 0
    """
    params = []
    
    if start_date:
        query += " AND datetime(exit_time) >= datetime(?)"
        params.append(start_date)
    if end_date:
        query += " AND datetime(exit_time) <= datetime(?)"
        params.append(end_date)
    
    cursor.execute(query, params)
    row = dict(cursor.fetchone())
    
    # 计算衍生指标
    total_trades = row['total_trades'] or 0
    winning_trades = row['winning_trades'] or 0
    losing_trades = row['losing_trades'] or 0
    total_pnl = row['total_pnl'] or 0
    avg_win = row['avg_win'] or 0
    avg_loss = abs(row['avg_loss'] or 0)
    
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(row['avg_pnl'] or 0, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(-avg_loss, 2),
            "max_win": round(row['max_win'] or 0, 2),
            "max_loss": round(row['max_loss'] or 0, 2),
            "profit_factor": round(profit_factor, 2),
            "period": period or "all"
        }
    }


@router.get("/strategy-stats")
async def get_strategy_statistics(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    """
    获取各策略的统计表现
    
    Returns:
        {
            "strategies": [
                {
                    "name": "confluence_v2",
                    "total_trades": 45,
                    "win_rate": 0.62,
                    "total_pnl": 8500.50,
                    "avg_pnl": 188.90,
                    "profit_factor": 1.75
                },
                ...
            ]
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
        SELECT 
            strategy,
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COALESCE(AVG(pnl), 0) as avg_pnl,
            COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl ELSE NULL END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl ELSE NULL END), 0) as avg_loss
        FROM trades
        WHERE status = 'CLOSED' AND pnl != 0
    """
    params = []
    
    if start_date:
        query += " AND datetime(exit_time) >= datetime(?)"
        params.append(start_date)
    if end_date:
        query += " AND datetime(exit_time) <= datetime(?)"
        params.append(end_date)
    
    query += " GROUP BY strategy ORDER BY total_pnl DESC"
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    strategies = []
    for row in rows:
        row_dict = dict(row)
        total = row_dict['total_trades']
        wins = row_dict['winning_trades']
        avg_win = row_dict['avg_win'] or 0
        avg_loss = abs(row_dict['avg_loss'] or 0)
        
        strategies.append({
            "name": row_dict['strategy'],
            "total_trades": total,
            "winning_trades": wins,
            "win_rate": round(wins / total if total > 0 else 0, 3),
            "total_pnl": round(row_dict['total_pnl'], 2),
            "avg_pnl": round(row_dict['avg_pnl'], 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(-avg_loss, 2),
            "profit_factor": round(avg_win / avg_loss if avg_loss > 0 else 0, 2)
        })
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "strategies": strategies,
            "count": len(strategies)
        }
    }


@router.get("/equity-curve")
async def get_equity_curve(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    group_by: str = Query("day", enum=["hour", "day", "week", "month"])
):
    """
    获取收益曲线数据
    
    Returns:
        {
            "curve": [
                {"date": "2026-03-01", "cumulative_pnl": 1200.50, "trades": 5},
                ...
            ]
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 根据group_by选择时间格式
    if group_by == "hour":
        time_format = "%Y-%m-%d %H:00:00"
    elif group_by == "day":
        time_format = "%Y-%m-%d"
    elif group_by == "week":
        time_format = "%Y-W%W"
    else:  # month
        time_format = "%Y-%m"
    
    query = f"""
        SELECT 
            strftime('{time_format}', exit_time) as period,
            COUNT(*) as trade_count,
            SUM(pnl) as period_pnl,
            AVG(pnl) as avg_pnl
        FROM trades
        WHERE status = 'CLOSED' AND pnl != 0
    """
    params = []
    
    if start_date:
        query += " AND datetime(exit_time) >= datetime(?)"
        params.append(start_date)
    if end_date:
        query += " AND datetime(exit_time) <= datetime(?)"
        params.append(end_date)
    
    query += " GROUP BY period ORDER BY period ASC"
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    # 计算累计收益
    curve = []
    cumulative_pnl = 0
    
    for row in rows:
        row_dict = dict(row)
        cumulative_pnl += row_dict['period_pnl'] or 0
        
        curve.append({
            "period": row_dict['period'],
            "trade_count": row_dict['trade_count'],
            "period_pnl": round(row_dict['period_pnl'] or 0, 2),
            "avg_pnl": round(row_dict['avg_pnl'] or 0, 2),
            "cumulative_pnl": round(cumulative_pnl, 2)
        })
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "curve": curve,
            "group_by": group_by
        }
    }


@router.get("/code-stats")
async def get_code_statistics(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100)
):
    """
    获取各股票的交易统计
    
    Returns:
        {
            "codes": [
                {
                    "code": "HK.00700",
                    "total_trades": 15,
                    "win_rate": 0.67,
                    "total_pnl": 3500.00,
                    "avg_pnl": 233.33
                },
                ...
            ]
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
        SELECT 
            code,
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COALESCE(AVG(pnl), 0) as avg_pnl
        FROM trades
        WHERE status = 'CLOSED' AND pnl != 0
    """
    params = []
    
    if start_date:
        query += " AND datetime(exit_time) >= datetime(?)"
        params.append(start_date)
    if end_date:
        query += " AND datetime(exit_time) <= datetime(?)"
        params.append(end_date)
    
    query += " GROUP BY code ORDER BY total_pnl DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    codes = []
    for row in rows:
        row_dict = dict(row)
        total = row_dict['total_trades']
        wins = row_dict['winning_trades']
        
        codes.append({
            "code": row_dict['code'],
            "total_trades": total,
            "winning_trades": wins,
            "win_rate": round(wins / total if total > 0 else 0, 3),
            "total_pnl": round(row_dict['total_pnl'], 2),
            "avg_pnl": round(row_dict['avg_pnl'], 2)
        })
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "codes": codes,
            "count": len(codes)
        }
    }


@router.get("/best-worst-trades")
async def get_best_worst_trades(
    limit: int = Query(10, ge=1, le=50)
):
    """
    获取最佳和最差交易
    
    Returns:
        {
            "best_trades": [...],
            "worst_trades": [...]
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 最佳交易
    cursor.execute("""
        SELECT * FROM trades 
        WHERE status = 'CLOSED' AND pnl != 0
        ORDER BY pnl DESC LIMIT ?
    """, (limit,))
    best_trades = [dict(row) for row in cursor.fetchall()]
    
    # 最差交易
    cursor.execute("""
        SELECT * FROM trades 
        WHERE status = 'CLOSED' AND pnl != 0
        ORDER BY pnl ASC LIMIT ?
    """, (limit,))
    worst_trades = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "best_trades": best_trades,
            "worst_trades": worst_trades
        }
    }


@router.get("/filters")
async def get_available_filters():
    """
    获取可用的筛选选项（策略列表、股票代码列表等）
    
    Returns:
        {
            "strategies": ["confluence_v2", "supertrend", ...],
            "codes": ["HK.00700", "HK.09988", ...],
            "date_range": {"min": "2026-01-01", "max": "2026-04-02"}
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 获取所有策略
    cursor.execute("SELECT DISTINCT strategy FROM trades WHERE strategy IS NOT NULL ORDER BY strategy")
    strategies = [row[0] for row in cursor.fetchall()]
    
    # 获取所有股票代码
    cursor.execute("SELECT DISTINCT code FROM trades WHERE code IS NOT NULL ORDER BY code")
    codes = [row[0] for row in cursor.fetchall()]
    
    # 获取日期范围
    cursor.execute("""
        SELECT 
            MIN(date(entry_time)) as min_date,
            MAX(date(entry_time)) as max_date
        FROM trades
    """)
    date_range = dict(cursor.fetchone())
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "strategies": strategies,
            "codes": codes,
            "date_range": date_range
        }
    }
