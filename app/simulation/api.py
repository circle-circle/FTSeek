"""
模拟盘 API 接口
提供模拟盘监控所需的数据接口
"""
import logging
import sqlite3
from app.storage.repository import get_db

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query, HTTPException
from pathlib import Path

from app import config

logger = logging.getLogger("ftseek.simulation.api")
logger.propagate = False  # 防止日志重复

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

# 数据库路径
DB_PATH = Path(__file__).parent.parent.parent / "data" / "simulation.db"


def get_db_connection():
    """获取数据库连接"""
    if not DB_PATH.exists():
        logger.warning(f"模拟盘数据库不存在：{DB_PATH}")
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def _check_token(token: str):
    """Token 校验"""
    import secrets
    if not secrets.compare_digest(token, config.API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


@router.get("/metrics")
async def get_simulation_metrics(token: str = Query(...)):
    """
    获取模拟盘核心指标
    
    返回：
    - 总资金
    - 可用资金
    - 持仓市值
    - 总盈亏
    - 总盈亏比例
    - 持仓数量
    - 今日交易次数
    """
    _check_token(token)
    
    conn = get_db_connection()
    if conn is None:
        return {
            "status": "error",
            "message": "模拟盘数据库不存在",
            "data": None
        }
    
    try:
        cursor = conn.cursor()
        
        # 获取账户信息
        cursor.execute("""
            SELECT current_capital, available_cash, total_pnl, total_pnl_percent, 
                   total_fees, trading_day
            FROM simulation_account 
            WHERE account_id = 'SIMULATION_MAIN'
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        account = cursor.fetchone()
        
        if not account:
            return {
                "status": "error",
                "message": "账户信息不存在",
                "data": None
            }
        
        # 获取持仓数量和市值
        cursor.execute("""
            SELECT COUNT(*) as position_count, 
                   SUM(quantity * current_price) as market_value,
                   SUM(unrealized_pnl) as unrealized_pnl
            FROM simulation_positions 
            WHERE quantity > 0
        """)
        position_stats = cursor.fetchone()
        
        # 获取今日交易次数
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("""
            SELECT COUNT(*) as trade_count
            FROM simulation_trades
            WHERE DATE(entry_time) = ?
        """, (today,))
        today_trades = cursor.fetchone()
        
        # 计算持仓市值
        current_capital = account['current_capital']
        available_cash = account['available_cash']
        market_value = position_stats['market_value'] or 0
        position_value = current_capital - available_cash
        
        # 计算交易统计数据
        cursor.execute("""
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END) as realized_pnl
            FROM simulation_trades
            WHERE status = 'closed'
        """)
        trade_stats = cursor.fetchone()
        
        total_trades = trade_stats['total_trades'] or 0
        winning_trades = trade_stats['winning_trades'] or 0
        losing_trades = trade_stats['losing_trades'] or 0
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        realized_pnl = trade_stats['realized_pnl'] or 0
        
        # 计算总回报
        initial_capital = account['current_capital'] - (account['total_pnl'] or 0)
        total_return = (account['total_pnl'] or 0) / initial_capital if initial_capital > 0 else 0
        
        # 计算夏普比率 (简化版：假设无风险利率为 0)
        # 实际应该基于日收益率计算，这里使用近似值
        sharpe_ratio = total_return / 0.15 if total_return > 0 else 0  # 假设年化波动率 15%
        
        # 计算利润因子
        cursor.execute("""
            SELECT 
                SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as total_profit,
                SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END) as total_loss
            FROM simulation_trades
            WHERE status = 'closed'
        """)
        profit_stats = cursor.fetchone()
        total_profit = profit_stats['total_profit'] or 0
        total_loss = profit_stats['total_loss'] or 0
        profit_factor = total_profit / total_loss if total_loss > 0 else (float('inf') if total_profit > 0 else 0)
        
        # 计算最大回撤 (简化版)
        max_drawdown = abs(account['total_pnl_percent'] or 0) / 100 if account['total_pnl_percent'] else 0
        
        metrics = {
            # 前端期望的字段
            "win_rate": win_rate,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "total_trades": total_trades,
            "total_return": total_return,
            "total_pnl": account['total_pnl'] or 0,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
            "profit_factor": profit_factor if profit_factor != float('inf') else 999.99,
            "initial_capital": initial_capital,
            "current_capital": current_capital,
            "available_cash": available_cash,
            # 额外字段
            "position_value": position_value,
            "market_value": market_value,
            "total_fees": account['total_fees'] or 0,
            "unrealized_pnl": position_stats['unrealized_pnl'] or 0,
            "position_count": position_stats['position_count'] or 0,
            "today_trades": today_trades['trade_count'] or 0,
            "trading_day": account['trading_day'],
            "update_time": datetime.now().isoformat()
        }
        
        return {
            "status": "ok",
            "data": metrics
        }
        
    except Exception as e:
        logger.error(f"获取模拟盘指标失败：{e}")
        return {
            "status": "error",
            "message": str(e),
            "data": None
        }
    finally:
        conn.close()


@router.get("/positions")
async def get_simulation_positions(token: str = Query(...)):
    """
    获取模拟盘持仓列表
    
    返回：
    - 持仓代码
    - 持仓名称
    - 持仓数量
    - 成本价
    - 当前价
    - 盈亏
    - 盈亏比例
    - 持仓方向
    """
    _check_token(token)
    
    conn = get_db_connection()
    if conn is None:
        return {
            "status": "error",
            "message": "模拟盘数据库不存在",
            "data": []
        }
    
    try:
        cursor = conn.cursor()
        
        # 获取持仓列表，关联获取股票名称
        cursor.execute("""
            SELECT p.code, p.quantity, p.avg_cost, p.current_price, 
                   p.direction, p.unrealized_pnl, p.unrealized_pnl_percent,
                   p.entry_time, p.updated_at,
                   t.name as stock_name
            FROM simulation_positions p
            LEFT JOIN (
                SELECT code, MAX(name) as name 
                FROM simulation_trades 
                WHERE name IS NOT NULL 
                GROUP BY code
            ) t ON p.code = t.code
            WHERE p.quantity > 0
            ORDER BY p.unrealized_pnl_percent DESC
        """)
        
        rows = cursor.fetchall()
        positions = []
        
        for row in rows:
            positions.append({
                "code": row['code'],
                "name": row['stock_name'] or 'Unknown',
                "quantity": row['quantity'],
                "avg_cost": row['avg_cost'],
                "current_price": row['current_price'],
                "direction": row['direction'],
                "unrealized_pnl": row['unrealized_pnl'] or 0,
                "unrealized_pnl_percent": row['unrealized_pnl_percent'] or 0,
                "entry_time": row['entry_time'],
                "update_time": row['updated_at']
            })
        
        return {
            "status": "ok",
            "data": positions,
            "count": len(positions)
        }
        
    except Exception as e:
        logger.error(f"获取模拟盘持仓失败：{e}")
        return {
            "status": "error",
            "message": str(e),
            "data": []
        }
    finally:
        conn.close()


@router.get("/trades")
async def get_simulation_trades(
    token: str = Query(...),
    limit: int = Query(50, description="返回交易数量"),
    status: Optional[str] = Query(None, description="交易状态：open/closed")
):
    """
    获取模拟盘交易记录
    
    返回：
    - 交易 ID
    - 股票代码
    - 股票名称
    - 买卖方向
    - 开仓价
    - 平仓价
    - 数量
    - 盈亏
    - 盈亏比例
    - 开仓时间
    - 平仓时间
    - 平仓原因
    - 状态
    """
    _check_token(token)
    
    conn = get_db_connection()
    if conn is None:
        return {
            "status": "error",
            "message": "模拟盘数据库不存在",
            "data": []
        }
    
    try:
        cursor = conn.cursor()
        
        # 构建查询
        query = """
            SELECT id, code, name, action, entry_price, exit_price, 
                   quantity, pnl, pnl_percent, fees, strategy, 
                   entry_time, exit_time, exit_reason, status
            FROM simulation_trades
        """
        params = []
        
        if status:
            query += " WHERE status = ?"
            params.append(status)
        
        query += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        trades = []
        for row in rows:
            trades.append({
                "id": row['id'],
                "code": row['code'],
                "name": row['name'] or 'Unknown',
                "action": row['action'],
                "entry_price": row['entry_price'],
                "exit_price": row['exit_price'],
                "quantity": row['quantity'],
                "pnl": row['pnl'] or 0,
                "pnl_percent": row['pnl_percent'] or 0,
                "fees": row['fees'] or 0,
                "strategy": row['strategy'] or 'Unknown',
                "entry_time": row['entry_time'],
                "exit_time": row['exit_time'],
                "exit_reason": row['exit_reason'],
                "status": row['status']
            })
        
        return {
            "status": "ok",
            "data": trades,
            "count": len(trades)
        }
        
    except Exception as e:
        logger.error(f"获取模拟盘交易记录失败：{e}")
        return {
            "status": "error",
            "message": str(e),
            "data": []
        }
    finally:
        conn.close()


@router.get("/capital_curve")
async def get_capital_curve(
    token: str = Query(...),
    days: int = Query(30, description="返回天数")
):
    """
    获取资金曲线数据
    
    返回：
    - 日期
    - 总资金
    - 持仓市值
    - 可用资金
    - 累计盈亏
    """
    _check_token(token)
    
    conn = get_db_connection()
    if conn is None:
        return {
            "status": "error",
            "message": "模拟盘数据库不存在",
            "data": []
        }
    
    try:
        cursor = conn.cursor()
        
        # 获取最近 N 天的账户快照（按 trading_day 分组）
        # 由于当前设计是单条记录，我们返回最近的历史记录
        cursor.execute("""
            SELECT trading_day, current_capital, available_cash, 
                   total_pnl, total_pnl_percent, created_at
            FROM simulation_account
            ORDER BY created_at DESC
            LIMIT ?
        """, (days,))
        
        rows = cursor.fetchall()
        curve_data = []
        
        for row in rows:
            curve_data.append({
                "date": row['trading_day'] or row['created_at'][:10],
                "total_capital": row['current_capital'],
                "available_cash": row['available_cash'],
                "position_value": row['current_capital'] - row['available_cash'],
                "cumulative_pnl": row['total_pnl'] or 0,
                "cumulative_pnl_percent": row['total_pnl_percent'] or 0
            })
        
        # 按日期排序
        curve_data.sort(key=lambda x: x['date'])
        
        return {
            "status": "ok",
            "data": curve_data,
            "count": len(curve_data)
        }
        
    except Exception as e:
        logger.error(f"获取资金曲线失败：{e}")
        return {
            "status": "error",
            "message": str(e),
            "data": []
        }
    finally:
        conn.close()
