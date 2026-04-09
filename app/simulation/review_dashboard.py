"""
港股模拟交易深度复盘大盘 API
提供收益、胜率、回撤、质量评分等深度分析
"""
from fastapi import APIRouter, Query, HTTPException
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

router = APIRouter(prefix="/api/v1/review", tags=["复盘大盘"])

DB_PATH = Path(__file__).parent.parent.parent / "data" / "simulation.db"

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

@router.get("/trades")
async def get_trades(
    market: Optional[str] = "HK",
    strategy: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100
):
    """
    获取交易列表（支持过滤）
    
    Args:
        market: 市场过滤 (HK/US)
        strategy: 策略过滤
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        limit: 返回数量限制
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
        SELECT 
            t.*,
            NULL as signal_strategy,
            NULL as signal_confidence,
            NULL as signal_reason
        FROM simulation_trades t
        
        WHERE t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*')
    """
    params = []
    
    if strategy:
        query += " AND s.strategy = ?"
        params.append(strategy)
    
    if start_date:
        query += " AND date(t.entry_time) >= ?"
        params.append(start_date)
    
    if end_date:
        query += " AND date(t.entry_time) <= ?"
        params.append(end_date)
    
    query += " ORDER BY t.entry_time DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    trades = []
    for row in rows:
        trade = dict(row)
        # 计算 PnL%
        if trade.get('entry_price') and trade.get('pnl') is not None:
            trade['pnl_percent'] = round(trade['pnl'] / (trade['entry_price'] * trade['quantity']) * 100, 2) if trade['entry_price'] > 0 else 0
        trades.append(trade)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": trades,
        "count": len(trades)
    }

@router.get("/pnl/summary")
async def get_pnl_summary(
    group_by: str = Query("strategy", description="分组维度：strategy/stock/date")
):
    """
    获取收益统计
    
    Args:
        group_by: 分组维度 (strategy/stock/date)
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if group_by == 'strategy':
        query = """
            SELECT 
                COALESCE(s.strategy, 'Unknown') as group_key,
                COUNT(t.id) as total_trades,
                SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN t.pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                COALESCE(SUM(t.pnl), 0) as total_pnl,
                COALESCE(AVG(t.pnl), 0) as avg_pnl,
                COALESCE(AVG(t.pnl_percent), 0) as avg_pnl_percent
            FROM simulation_trades t
            
            WHERE (t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*'))
              AND t.status = 'closed'
            GROUP BY s.strategy
            ORDER BY total_pnl DESC
        """
    elif group_by == 'stock':
        query = """
            SELECT 
                t.code as group_key,
                COUNT(t.id) as total_trades,
                SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                COALESCE(SUM(t.pnl), 0) as total_pnl,
                COALESCE(AVG(t.pnl), 0) as avg_pnl
            FROM simulation_trades t
            WHERE (t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*'))
              AND t.status = 'closed'
            GROUP BY t.code
            ORDER BY total_pnl DESC
        """
    else:  # date
        query = """
            SELECT 
                date(t.entry_time) as group_key,
                COUNT(t.id) as total_trades,
                SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                COALESCE(SUM(t.pnl), 0) as total_pnl
            FROM simulation_trades t
            WHERE (t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*'))
              AND t.status = 'closed'
            GROUP BY date(t.entry_time)
            ORDER BY group_key DESC
        """
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    summary = []
    for row in rows:
        item = dict(row)
        item['win_rate'] = round(item['winning_trades'] / item['total_trades'] * 100, 2) if item['total_trades'] > 0 else 0
        summary.append(item)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": summary
    }

@router.get("/winrate")
async def get_winrate(
    period: str = Query("all", description="统计周期：7d/30d/90d/all")
):
    """
    获取胜率统计
    
    Args:
        period: 统计周期
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 计算日期范围
    if period != 'all':
        days = int(period.replace('d', ''))
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        date_filter = "AND date(t.entry_time) >= ?"
        params = [start_date]
    else:
        date_filter = ""
        params = []
    
    query = f"""
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN t.pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            COALESCE(SUM(t.pnl), 0) as total_pnl,
            COALESCE(AVG(t.pnl), 0) as avg_win,
            COALESCE(AVG(CASE WHEN t.pnl < 0 THEN t.pnl ELSE 0 END), 0) as avg_loss
        FROM simulation_trades t
        WHERE (t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*'))
          AND t.status = 'closed'
          {date_filter}
    """
    
    cursor.execute(query, params)
    row = cursor.fetchone()
    
    if row:
        stats = dict(row)
        stats['win_rate'] = round(stats['winning_trades'] / stats['total_trades'] * 100, 2) if stats['total_trades'] > 0 else 0
        stats['profit_factor'] = abs(stats['avg_win'] / stats['avg_loss']) if stats['avg_loss'] != 0 else 999.99
    else:
        stats = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0,
            'win_rate': 0,
            'profit_factor': 0
        }
    
    conn.close()
    
    return {
        "status": "ok",
        "data": stats
    }

@router.get("/drawdown")
async def get_drawdown(
    period: str = Query("30d", description="分析周期：7d/30d/90d/1y/all")
):
    """
    获取回撤分析
    
    Args:
        period: 分析周期
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 计算日期范围
    if period != 'all':
        days = int(period.replace('d', '').replace('y', '0'))
        if 'y' in period:
            days = days * 365
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        date_filter = "AND date(entry_time) >= ?"
        params = [start_date]
    else:
        date_filter = ""
        params = []
    
    # 计算累计收益曲线
    query = f"""
        SELECT 
            date(entry_time) as trade_date,
            SUM(pnl) OVER (ORDER BY entry_time) as cumulative_pnl
        FROM simulation_trades
        WHERE (code LIKE 'HK.%' OR code LIKE 'HK%' OR (length(code) = 5 AND code GLOB '[0-9]*'))
          AND status = 'closed'
          {date_filter}
        ORDER BY entry_time
    """
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    # 计算回撤
    equity_curve = []
    peak = 0
    max_drawdown = 0
    current_drawdown = 0
    
    for row in rows:
        equity = float(row['cumulative_pnl']) if row['cumulative_pnl'] else 0
        equity_curve.append({'date': row['trade_date'], 'equity': equity})
        
        if equity > peak:
            peak = equity
        
        if peak > 0:
            dd = (peak - equity) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd
            current_drawdown = dd
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "max_drawdown": round(max_drawdown, 2),
            "current_drawdown": round(current_drawdown, 2),
            "equity_curve": equity_curve[-30:]  # 最近 30 天
        }
    }

@router.get("/quality/{trade_id}")
async def get_trade_quality(trade_id: str):
    """
    获取单笔交易质量评分
    
    Args:
        trade_id: 交易 ID
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 获取交易和信号数据
    query = """
        SELECT 
            t.*,
            s.signal_time,
            s.signal_price,
            s.strategy,
            s.confidence,
            s.reason
        FROM simulation_trades t
        
        WHERE t.id = ?
    """
    
    cursor.execute(query, (trade_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Trade not found")
    
    trade = dict(row)
    
    # 计算质量评分
    # 入场时机分 (基于滑点)
    if trade.get('signal_price') and trade.get('entry_price'):
        slippage = abs(trade['entry_price'] - trade['signal_price']) / trade['signal_price'] * 100
        entry_score = max(0, min(100, 100 - slippage * 10))
    else:
        entry_score = 80  # 默认分
    
    # 出场时机分 (基于 PnL)
    if trade.get('pnl_percent') is not None:
        if trade['pnl_percent'] > 10:
            exit_score = 100
        elif trade['pnl_percent'] > 5:
            exit_score = 90
        elif trade['pnl_percent'] > 0:
            exit_score = 80
        elif trade['pnl_percent'] > -5:
            exit_score = 60
        else:
            exit_score = 40
    else:
        exit_score = 50  # 未平仓
    
    # 仓位管理分 (简化)
    position_score = 80
    
    # 综合评分
    total_score = 0.4 * entry_score + 0.4 * exit_score + 0.2 * position_score
    
    # 生成改进建议
    suggestions = []
    if entry_score < 70:
        suggestions.append("建议优化入场时机，减少滑点")
    if exit_score < 70:
        suggestions.append("建议优化出场策略，提高止盈止损执行")
    if trade.get('pnl_percent', 0) < -5:
        suggestions.append("建议加强止损纪律")
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "trade_id": trade_id,
            "scores": {
                "entry_score": round(entry_score, 2),
                "exit_score": round(exit_score, 2),
                "position_score": position_score,
                "total_score": round(total_score, 2)
            },
            "suggestions": suggestions,
            "trade_details": trade
        }
    }

@router.get("/suggestions")
async def get_suggestions(
    strategy: Optional[str] = None,
    stock: Optional[str] = None
):
    """
    获取改进建议
    
    Args:
        strategy: 策略过滤
        stock: 标的过滤
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 分析交易模式
    filters = []
    params = []
    
    filters.append("(t.code LIKE 'HK.%' OR t.code LIKE 'HK%' OR (length(t.code) = 5 AND t.code GLOB '[0-9]*'))")
    filters.append("t.status = 'closed'")
    
    if strategy:
        filters.append("s.strategy = ?")
        params.append(strategy)
    
    if stock:
        filters.append("t.code = ?")
        params.append(stock)
    
    filter_str = " AND ".join(filters)
    
    query = f"""
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            AVG(t.pnl) as avg_pnl,
            AVG(CASE WHEN t.pnl > 0 THEN t.pnl ELSE 0 END) as avg_win,
            AVG(CASE WHEN t.pnl < 0 THEN t.pnl ELSE 0 END) as avg_loss
        FROM simulation_trades t
        
        WHERE {filter_str}
    """
    
    cursor.execute(query, params)
    row = cursor.fetchone()
    
    suggestions = []
    
    if row:
        stats = dict(row)
        win_rate = stats['winning_trades'] / stats['total_trades'] * 100 if stats['total_trades'] > 0 else 0
        
        if win_rate < 40:
            suggestions.append({
                "category": "胜率",
                "issue": f"当前胜率{win_rate:.1f}%偏低",
                "suggestion": "建议优化入场信号质量，提高筛选标准"
            })
        
        if stats['avg_loss'] != 0:
            profit_factor = abs(stats['avg_win'] / stats['avg_loss'])
            if profit_factor < 1.5:
                suggestions.append({
                    "category": "盈亏比",
                    "issue": f"当前盈亏比{profit_factor:.2f}偏低",
                    "suggestion": "建议优化止盈止损策略，提高盈利交易持有时间"
                })
        
        if stats['avg_pnl'] and stats['avg_pnl'] < 0:
            suggestions.append({
                "category": "整体收益",
                "issue": "平均交易亏损",
                "suggestion": "建议暂停该策略，重新回测和优化"
            })
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "suggestions": suggestions,
            "analysis_period": "all",
            "filters": {"strategy": strategy, "stock": stock}
        }
    }

@router.get("/dashboard")
async def get_dashboard_summary():
    """
    获取大盘总览
    """
    # 调用其他 API 组合数据
    pnl_response = await get_pnl_summary(group_by="strategy")
    winrate_response = await get_winrate(period="all")
    drawdown_response = await get_drawdown(period="30d")
    
    return {
        "status": "ok",
        "data": {
            "pnl_summary": pnl_response['data'],
            "winrate": winrate_response['data'],
            "drawdown": drawdown_response['data']
        }
    }

@router.get("/stats/summary")
async def get_stats_summary(
    period: str = Query("today", description="统计周期：today/week/month/all"),
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD")
):
    """
    获取交易统计摘要 (支持自定义时间范围)
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 构建日期过滤
    if period == 'today':
        date_filter = "WHERE date(entry_time) = date('now')"
    elif period == 'week':
        date_filter = "WHERE date(entry_time) >= date('now', '-7 days')"
    elif period == 'month':
        date_filter = "WHERE date(entry_time) >= date('now', '-30 days')"
    elif start_date and end_date:
        date_filter = f"WHERE date(entry_time) BETWEEN '{start_date}' AND '{end_date}'"
    else:
        date_filter = ""
    
    # 基础统计
    query = f"""
        SELECT 
            COALESCE(COUNT(*), 0) as total_trades,
            COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as winning_trades,
            COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) as losing_trades,
            COALESCE(SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END), 0) as closed_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0) as avg_loss
        FROM simulation_trades
        {date_filter} AND status = 'closed'
    """
    
    cursor.execute(query)
    row = cursor.fetchone()
    
    if row:
        stats = dict(row)
        # 类型安全检查：确保所有字段都是数字
        closed_trades = stats.get('closed_trades') or 0
        winning_trades = stats.get('winning_trades') or 0
        avg_win = stats.get('avg_win') or 0
        avg_loss = stats.get('avg_loss') or 0
        
        stats['win_rate'] = round(winning_trades / closed_trades * 100, 2) if closed_trades > 0 else 0
        stats['profit_factor'] = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 999.99
    else:
        stats = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'closed_trades': 0,
            'total_pnl': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'win_rate': 0,
            'profit_factor': 0
        }
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "period": period,
            "stats": stats
        }
    }

@router.get("/stats/trend")
async def get_stats_trend(
    period: str = Query("month", description="统计周期：week/month"),
    group_by: str = Query("day", description="分组维度：day/week")
):
    """
    获取收益趋势数据
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if group_by == 'day':
        date_format = "date(entry_time)"
    else:
        date_format = "strftime('%Y-W%W', entry_time)"
    
    query = f"""
        SELECT 
            {date_format} as period,
            COUNT(*) as trades,
            SUM(pnl) as pnl,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses
        FROM simulation_trades
        WHERE status = 'closed'
        AND date(entry_time) >= date('now', '-30 days')
        GROUP BY {date_format}
        ORDER BY period
    """
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    trend_data = []
    for row in rows:
        d = dict(row)
        d['win_rate'] = round(d['wins'] / (d['wins'] + d['losses']) * 100, 2) if (d['wins'] + d['losses']) > 0 else 0
        trend_data.append(d)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "period": period,
            "group_by": group_by,
            "trend": trend_data
        }
    }
