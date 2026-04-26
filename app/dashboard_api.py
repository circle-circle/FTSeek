"""
FTSeek 深度复盘 Dashboard API
提供交易数据、策略分析、收益趋势、成长趋势等数据接口
"""
from fastapi import APIRouter, Query, HTTPException
from app.storage.repository import get_db
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

router = APIRouter(prefix="/api/dashboard", tags=["深度复盘 Dashboard"])

# 数据库路径
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")


def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

@router.get("/kpi")
async def get_kpi(
    timeframe: str = Query("day", enum=["day", "week", "month"], description="时间维度")
):
    """
    获取核心 KPI 指标
    
    Args:
        timeframe: 时间维度 (day/week/month)
        
    Returns:
        {
            "pnl": 2282.5,              # 盈亏
            "win_rate": 0.714,           # 胜率
            "profit_loss_ratio": 1.17,   # 盈亏比
            "sharpe_ratio": 0.82,        # 夏普比率
            "max_drawdown": -0.05,       # 最大回撤
            "total_trades": 17,          # 交易笔数
            "winning_trades": 12,        # 盈利笔数
            "losing_trades": 5,          # 亏损笔数
            "avg_win": 189.4,            # 平均盈利
            "avg_loss": -97.8,           # 平均亏损
            "timeframe": "day"           # 时间维度
        }
    """
    # 计算时间范围
    now = datetime.now()
    if timeframe == "day":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif timeframe == "week":
        start_date = now - timedelta(days=now.weekday())
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 查询交易数据
    cursor.execute("""
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0) as avg_loss
        FROM trades
        WHERE datetime(exit_time) >= datetime(?)
          AND pnl != 0
    """, (start_date.isoformat(),))
    
    row = cursor.fetchone()
    
    if row:
        data = dict(row)
        total_trades = data['total_trades'] or 0
        winning_trades = data['winning_trades'] or 0
        losing_trades = data['losing_trades'] or 0
        total_pnl = data['total_pnl'] or 0
        avg_win = data['avg_win'] or 0
        avg_loss = abs(data['avg_loss'] or 0)
        
        # 计算胜率
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        # 计算盈亏比
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        
        # 夏普比率 (基于DB查询日收益率)
        sharpe_ratio = 0.0
        max_drawdown = 0.0
        try:
            cursor.execute("""
                SELECT 
                    date(exit_time) as trade_date,
                    SUM(pnl) as daily_pnl,
                    SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as daily_win,
                    SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as daily_loss
                FROM trades
                WHERE datetime(exit_time) >= datetime(?)
                  AND pnl != 0
                GROUP BY date(exit_time)
                ORDER BY trade_date
            """, (start_date.isoformat(),))
            daily_rows = cursor.fetchall()
            
            if len(daily_rows) > 1:
                import statistics
                daily_pnl_values = [r['daily_pnl'] for r in daily_rows]
                mean_return = statistics.mean(daily_pnl_values)
                std_return = statistics.stdev(daily_pnl_values)
                if std_return > 0:
                    sharpe_ratio = round(mean_return / std_return * (252 ** 0.5), 4)
                
                # 最大回撤 (基于累计盈亏曲线)
                cumulative = 0
                peak = 0
                for r in daily_rows:
                    cumulative += r['daily_pnl']
                    if cumulative > peak:
                        peak = cumulative
                    drawdown = (cumulative - peak) / max(peak, 1) if peak > 0 else 0
                    if drawdown < max_drawdown:
                        max_drawdown = drawdown
                max_drawdown = round(max_drawdown, 4)
        except Exception:
            sharpe_ratio = 0.0
            max_drawdown = 0.0
        
        result = {
            "pnl": total_pnl,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "timeframe": timeframe
        }
    else:
        result = {
            "pnl": 0,
            "win_rate": 0,
            "profit_loss_ratio": 0,
            "sharpe_ratio": 0,
            "max_drawdown": 0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "timeframe": timeframe
        }
    
    conn.close()
    return {"status": "ok", "data": result}


@router.get("/equity-curve")
async def get_equity_curve(
    days: int = Query(30, ge=1, le=365, description="天数")
):
    """
    获取收益曲线数据
    
    Returns:
        {
            "dates": ["2026-03-01", "2026-03-02", ...],
            "daily_pnl": [100.5, -50.2, ...],
            "cumulative_pnl": [100.5, 50.3, ...],
            "win_rate_7d": [0.6, 0.57, ...],
            "win_rate_30d": [0.65, 0.63, ...]
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 查询每日盈亏
    cursor.execute("""
        SELECT 
            date(exit_time) as trade_date,
            SUM(pnl) as daily_pnl,
            COUNT(*) as trade_count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_count
        FROM trades
        WHERE date(exit_time) >= date('now', ?)
          AND pnl != 0
        GROUP BY date(exit_time)
        ORDER BY trade_date
    """, (f'-{days} days',))
    
    rows = cursor.fetchall()
    
    dates = []
    daily_pnl = []
    cumulative_pnl = []
    win_rate_7d = []
    win_rate_30d = []
    
    cumulative = 0
    pnl_history = []
    win_history = []
    
    for row in rows:
        date = row['trade_date']
        pnl = row['daily_pnl']
        trades = row['trade_count']
        wins = row['winning_count']
        
        dates.append(date)
        daily_pnl.append(pnl)
        cumulative += pnl
        cumulative_pnl.append(cumulative)
        
        pnl_history.append(pnl)
        win_history.append(wins / trades if trades > 0 else 0)
        
        # 7 日胜率
        if len(pnl_history) >= 7:
            recent_wins = sum(win_history[-7:])
            win_rate_7d.append(recent_wins / 7)
        else:
            win_rate_7d.append(None)
        
        # 30 日胜率
        if len(pnl_history) >= 30:
            recent_wins = sum(win_history[-30:])
            win_rate_30d.append(recent_wins / 30)
        else:
            win_rate_30d.append(None)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "dates": dates,
            "daily_pnl": daily_pnl,
            "cumulative_pnl": cumulative_pnl,
            "win_rate_7d": win_rate_7d,
            "win_rate_30d": win_rate_30d
        }
    }


@router.get("/strategy-performance")
async def get_strategy_performance():
    """
    获取策略表现分析
    
    Returns:
        {
            "strategies": [
                {
                    "name": "confluence_v2",
                    "pnl": 1500.5,
                    "win_rate": 0.65,
                    "trade_count": 20,
                    "avg_pnl": 75.0
                },
                ...
            ]
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            trigger_strategy as strategy,
            COUNT(*) as trade_count,
            SUM(pnl) as total_pnl,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            AVG(pnl) as avg_pnl
        FROM trades
        WHERE pnl != 0
        GROUP BY trigger_strategy
        ORDER BY total_pnl DESC
    """)
    
    rows = cursor.fetchall()
    
    strategies = []
    for row in rows:
        strategies.append({
            "name": row['strategy'],
            "pnl": row['total_pnl'],
            "win_rate": row['winning_trades'] / row['trade_count'] if row['trade_count'] > 0 else 0,
            "trade_count": row['trade_count'],
            "avg_pnl": row['avg_pnl']
        })
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "strategies": strategies
        }
    }


@router.get("/trades")
async def get_trades(
    timeframe: str = Query("day", enum=["day", "week", "month"]),
    strategy: Optional[str] = Query(None, description="策略名称"),
    direction: Optional[str] = Query(None, enum=["BUY", "SELL"]),
    limit: int = Query(100, ge=1, le=1000)
):
    """
    获取交易明细
    
    Returns:
        {
            "trades": [
                {
                    "id": "xxx",
                    "code": "HK.00700",
                    "direction": "BUY",
                    "strategy": "confluence_v2",
                    "entry_time": "2026-04-01T10:30:00",
                    "exit_time": "2026-04-01T14:30:00",
                    "entry_price": 300.0,
                    "exit_price": 305.5,
                    "quantity": 100,
                    "pnl": 550.0,
                    "pnl_percent": 0.0183,
                    "holding_period": "4h"
                },
                ...
            ],
            "total": 150
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 构建查询条件
    conditions = ["pnl != 0"]
    params = []
    
    if strategy:
        conditions.append("trigger_strategy = ?")
        params.append(strategy)
    
    if direction:
        conditions.append("action = ?")
        params.append(direction)
    
    where_clause = " AND ".join(conditions)
    
    cursor.execute(f"""
        SELECT 
            id,
            code,
            action as direction,
            trigger_strategy as strategy,
            entry_time,
            exit_time,
            entry_price,
            exit_price,
            quantity,
            pnl,
            (pnl / (entry_price * quantity)) as pnl_percent
        FROM trades
        WHERE {where_clause}
        ORDER BY exit_time DESC
        LIMIT ?
    """, params + [limit])
    
    rows = cursor.fetchall()
    
    trades = []
    for row in rows:
        # 计算持仓时长
        try:
            entry = datetime.fromisoformat(row['entry_time'])
            exit_dt = datetime.fromisoformat(row['exit_time'])
            delta = exit_dt - entry
            hours = delta.total_seconds() / 3600
            if hours < 24:
                holding_period = f"{int(hours)}h"
            else:
                holding_period = f"{int(hours / 24)}d"
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug(f"持仓时长计算失败：{e}")
            holding_period = "N/A"
        
        trades.append({
            "id": row['id'],
            "code": row['code'],
            "direction": row['direction'],
            "strategy": row['strategy'],
            "entry_time": row['entry_time'],
            "exit_time": row['exit_time'],
            "entry_price": row['entry_price'],
            "exit_price": row['exit_price'],
            "quantity": row['quantity'],
            "pnl": row['pnl'],
            "pnl_percent": row['pnl_percent'],
            "holding_period": holding_period
        })
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "trades": trades,
            "total": len(trades)
        }
    }


@router.get("/signal-quality")
async def get_signal_quality():
    """
    获取信号质量分析 (置信度 vs 实际收益)
    
    Returns:
        {
            "signals": [
                {"confidence": 0.8, "pnl": 150.5, "strategy": "confluence_v2"},
                ...
            ],
            "correlation": 0.65  # 置信度与收益的相关性
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 查询信号数据 (假设有 signal_confidence 字段)
    cursor.execute("""
        SELECT 
            signal_confidence as confidence,
            pnl,
            trigger_strategy as strategy
        FROM trades
        WHERE signal_confidence IS NOT NULL
          AND pnl != 0
        ORDER BY entry_time DESC
        LIMIT 500
    """)
    
    rows = cursor.fetchall()
    
    signals = []
    for row in rows:
        signals.append({
            "confidence": row['confidence'],
            "pnl": row['pnl'],
            "strategy": row['strategy']
        })
    
    # 计算皮尔逊相关系数 (置信度 vs 收益)
    if len(signals) >= 2:
        import statistics
        confidences = [s['confidence'] for s in signals]
        pnls = [s['pnl'] for s in signals]
        try:
            mean_conf = statistics.mean(confidences)
            mean_pnl = statistics.mean(pnls)
            std_conf = statistics.stdev(confidences)
            std_pnl = statistics.stdev(pnls)
            if std_conf > 0 and std_pnl > 0:
                covariance = sum((c - mean_conf) * (p - mean_pnl) for c, p in zip(confidences, pnls)) / (len(signals) - 1)
                correlation = round(covariance / (std_conf * std_pnl), 4)
            else:
                correlation = 0.0
        except (statistics.StatisticsError, ZeroDivisionError):
            correlation = 0.0
    else:
        correlation = 0.0  # 数据不足
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "signals": signals,
            "correlation": correlation
        }
    }


@router.get("/growth-trend")
async def get_growth_trend():
    """
    获取成长趋势分析 (周度/月度对比)
    
    Returns:
        {
            "weekly": {
                "this_week": {
                    "pnl": 2282.5,
                    "win_rate": 0.714,
                    "trade_count": 17,
                    "avg_pnl": 134.3
                },
                "last_week": {
                    "pnl": -1342.5,
                    "win_rate": 0.2,
                    "trade_count": 10,
                    "avg_pnl": -134.3
                },
                "improvement": {
                    "pnl_change": 3625.0,
                    "win_rate_change": 0.514,
                    "trade_count_change": 7
                }
            },
            "monthly": {
                "this_month": {...},
                "last_month": {...},
                "improvement": {...}
            }
        }
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    def get_period_stats(days_ago: int):
        """获取指定时间段的统计数据"""
        cursor.execute("""
            SELECT 
                SUM(pnl) as total_pnl,
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                AVG(pnl) as avg_pnl
            FROM trades
            WHERE date(exit_time) >= date('now', ?)
              AND date(exit_time) < date('now', ?)
              AND pnl != 0
        """, (f'-{days_ago} days', f'-{days_ago - 7} days'))
        
        row = cursor.fetchone()
        if row and row['trade_count'] > 0:
            return {
                "pnl": row['total_pnl'] or 0,
                "win_rate": row['winning_trades'] / row['trade_count'],
                "trade_count": row['trade_count'],
                "avg_pnl": row['avg_pnl'] or 0
            }
        return {"pnl": 0, "win_rate": 0, "trade_count": 0, "avg_pnl": 0}
    
    # 本周 vs 上周
    this_week = get_period_stats(0)
    last_week = get_period_stats(7)

    # 本月 vs 上月 (简化为 30 天)
    # 注：完整实现需要按日历月计算
    this_month = get_period_stats(0)
    last_month = get_period_stats(30)
    
    conn.close()
    
    return {
        "status": "ok",
        "data": {
            "weekly": {
                "this_week": this_week,
                "last_week": last_week,
                "improvement": {
                    "pnl_change": this_week['pnl'] - last_week['pnl'],
                    "win_rate_change": this_week['win_rate'] - last_week['win_rate'],
                    "trade_count_change": this_week['trade_count'] - last_week['trade_count']
                }
            },
            "monthly": {
                "this_month": this_month,
                "last_month": last_month,
                "improvement": {
                    "pnl_change": this_month['pnl'] - last_month['pnl'],
                    "win_rate_change": this_month['win_rate'] - last_month['win_rate'],
                    "trade_count_change": this_month['trade_count'] - last_month['trade_count']
                }
            }
        }
    }


# ============================================================
# 【新增 2026-04-02】决策缓冲层与系统优化监控 API
# ============================================================

@router.get("/decision-buffer/stats")
async def get_decision_buffer_stats():
    """
    获取决策缓冲层统计信息
    
    Returns:
        {
            "total_received": 100,      # 总接收信号数
            "total_approved": 75,       # 批准数
            "total_rejected": 25,       # 拒绝数
            "approval_rate": 0.75,      # 批准率
            "rejection_reasons": {...}, # 拒绝原因分布
            "pending_queue_size": 3     # 当前待处理队列长度
        }
    """
    try:
        from app.decision_buffer import get_decision_buffer
        buffer = get_decision_buffer()
        stats = buffer.get_stats()
        
        # 计算批准率
        total = stats.get('total_received', 0)
        approved = stats.get('total_approved', 0)
        approval_rate = approved / total if total > 0 else 0
        
        return {
            "status": "ok",
            "data": {
                **stats,
                "approval_rate": round(approval_rate, 3),
                "pending_queue_size": len(buffer._pending_queue),
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"获取决策缓冲层统计失败: {str(e)}"
        }


@router.get("/decision-buffer/recent")
async def get_recent_decisions(limit: int = Query(20, ge=1, le=100)):
    """
    获取最近的决策记录
    
    Args:
        limit: 返回记录数 (1-100)
    
    Returns:
        {
            "decisions": [
                {
                    "decision_id": "HK.00700_BUY_1712048400",
                    "code": "HK.00700",
                    "action": "BUY",
                    "score": 2.35,
                    "confidence": 0.75,
                    "status": "APPROVED",
                    "rejection_reason": "",
                    "created_at": "2026-04-02T14:30:15",
                    "execution_time_ms": 125.5
                },
                ...
            ]
        }
    """
    try:
        from app.decision_buffer import get_decision_buffer
        buffer = get_decision_buffer()
        
        # 获取最近决策记录
        recent = list(buffer._decision_history)[-limit:]
        decisions = [record.to_dict() for record in recent]
        
        return {
            "status": "ok",
            "data": {
                "decisions": decisions,
                "count": len(decisions)
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"获取决策记录失败: {str(e)}"
        }


@router.get("/config/validation")
async def get_config_validation():
    """
    获取配置校验结果
    
    Returns:
        {
            "is_valid": true,
            "errors": [],
            "warnings": [],
            "infos": [],
            "last_check": "2026-04-02T14:30:00"
        }
    """
    try:
        from config.config_validator import get_config_validator
        validator = get_config_validator()
        
        # 执行校验
        result = validator.validate_all()
        
        return {
            "status": "ok",
            "data": {
                "is_valid": result.is_valid,
                "errors": [
                    {
                        "category": e.category,
                        "message": e.message,
                        "field": e.field,
                        "suggestion": e.suggestion
                    }
                    for e in result.errors
                ],
                "warnings": [
                    {
                        "category": w.category,
                        "message": w.message,
                        "field": w.field,
                        "suggestion": w.suggestion
                    }
                    for w in result.warnings
                ],
                "infos": [
                    {
                        "category": i.category,
                        "message": i.message,
                        "file": i.file
                    }
                    for i in result.infos
                ],
                "summary": result.summary(),
                "last_check": datetime.now().isoformat()
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"配置校验失败: {str(e)}"
        }


@router.get("/trace/{trace_id}")
async def get_trace_detail(trace_id: str):
    """
    根据trace_id查询决策链路详情
    
    Args:
        trace_id: 决策链路追踪ID (格式: TRC-XXXXXXXXXXXX)
    
    Returns:
        {
            "trace_id": "TRC-959982D9A394",
            "code": "HK.00700",
            "action": "BUY",
            "score": 2.35,
            "confidence": 0.75,
            "decision_status": "APPROVED",
            "timeline": [
                {
                    "timestamp": "2026-04-02T14:30:15.123",
                    "stage": "consensus",
                    "message": "共识达成: 3策略同意"
                },
                {
                    "timestamp": "2026-04-02T14:30:15.234",
                    "stage": "decision_buffer",
                    "message": "决策缓冲层批准"
                },
                ...
            ]
        }
    """
    try:
        from app.decision_buffer import get_decision_buffer
        buffer = get_decision_buffer()
        
        # 在决策历史中查找
        for record in buffer._decision_history:
            if trace_id in record.decision_id or trace_id == getattr(record.consensus_result, 'trace_id', ''):
                return {
                    "status": "ok",
                    "data": {
                        "trace_id": trace_id,
                        "decision": record.to_dict(),
                        "timeline": [
                            {
                                "timestamp": datetime.fromtimestamp(record.created_at).isoformat(),
                                "stage": "received",
                                "message": f"信号接收: {record.code} {record.action.value}"
                            },
                            {
                                "timestamp": datetime.fromtimestamp(record.approved_at or record.created_at).isoformat(),
                                "stage": "decision",
                                "message": f"决策结果: {record.status.value}"
                            },
                            {
                                "timestamp": datetime.fromtimestamp(record.executed_at or record.created_at).isoformat(),
                                "stage": "execution",
                                "message": f"执行状态: {record.status.value}"
                            }
                        ]
                    }
                }
        
        return {
            "status": "not_found",
            "message": f"未找到trace_id: {trace_id}"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"查询trace失败: {str(e)}"
        }


@router.get("/system/optimization-status")
async def get_optimization_status():
    """
    获取系统优化状态总览
    
    Returns:
        {
            "optimizations": [
                {
                    "name": "决策缓冲层",
                    "status": "active",
                    "metrics": {...}
                },
                {
                    "name": "新闻情绪缓存",
                    "status": "active",
                    "cache_hit_rate": 0.85
                },
                ...
            ]
        }
    """
    try:
        from app.decision_buffer import get_decision_buffer
        
        # 获取决策缓冲层统计
        buffer = get_decision_buffer()
        buffer_stats = buffer.get_stats()
        
        # 构建优化状态
        optimizations = [
            {
                "name": "决策缓冲层",
                "status": "active",
                "version": "1.0.0",
                "metrics": {
                    "total_signals": buffer_stats.get('total_received', 0),
                    "approval_rate": round(
                        buffer_stats.get('total_approved', 0) / max(1, buffer_stats.get('total_received', 1)),
                        3
                    ),
                    "pending_queue": len(buffer._pending_queue)
                }
            },
            {
                "name": "新闻情绪缓存",
                "status": "active",
                "version": "1.0.0",
                "metrics": {
                    "cache_ttl": "30分钟",
                    "note": "缓存命中率需运行后统计"
                }
            },
            {
                "name": "多周期增量计算",
                "status": "active",
                "version": "1.0.0",
                "metrics": {
                    "periods": 7,
                    "optimization": "避免重复MA计算"
                }
            },
            {
                "name": "配置校验器",
                "status": "active",
                "version": "1.0.0",
                "metrics": {
                    "validation_rules": 6,
                    "last_check": datetime.now().isoformat()
                }
            },
            {
                "name": "决策链路追踪",
                "status": "active",
                "version": "1.0.0",
                "metrics": {
                    "trace_format": "TRC-XXXXXXXXXXXX",
                    "coverage": "全链路"
                }
            }
        ]
        
        return {
            "status": "ok",
            "data": {
                "optimizations": optimizations,
                "total_count": len(optimizations),
                "active_count": sum(1 for o in optimizations if o['status'] == 'active')
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"获取优化状态失败: {str(e)}"
        }
