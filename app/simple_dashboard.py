"""
简化版 Dashboard 数据模块

基于当前已同步的持仓和资金数据，提供简单查询
"""
import sqlite3
import logging
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger("ftseek_us.simple_dashboard")

class SimpleDashboard:
    """简化版 Dashboard 数据查询"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            from app.core.paths import get_trade_records_db
            db_path = str(get_trade_records_db())
        self.db_path = db_path
    
    def get_account_summary(self) -> Dict[str, Any]:
        """获取账户概览"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 查询账户信息
            cursor.execute("SELECT * FROM account_info ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            
            if row:
                return {
                    "total_assets": row.get("total_assets", 0),
                    "available_cash": row.get("available_cash", 0),
                    "market_value": row.get("market_value", 0),
                    "unrealized_pnl": row.get("unrealized_pnl", 0),
                    "realized_pnl": row.get("realized_pnl", 0),
                    "total_pnl_percent": row.get("total_pnl_percent", 0),
                }
            return {}
        except Exception as e:
            logger.error(f"查询账户概览失败：{e}")
            return {}
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """获取持仓列表"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT symbol, direction, quantity, avg_entry_price, current_price,
                       market_value, unrealized_pnl, unrealized_pnl_percent, exec_time,
                       strategy_id
                FROM open_positions 
                WHERE position_status = 'OPEN'
                ORDER BY exec_time DESC
            """)
            
            positions = []
            for row in cursor.fetchall():
                strategy = row.get("strategy_id", "") or ""
                source = "SCALPING" if "scalping" in strategy.lower() else "MAIN_PIPELINE"
                positions.append({
                    "symbol": row.get("symbol", ""),
                    "direction": row.get("direction", ""),
                    "quantity": row.get("quantity", 0),
                    "avg_entry_price": row.get("avg_entry_price", 0),
                    "current_price": row.get("current_price", 0),
                    "market_value": row.get("market_value", 0),
                    "unrealized_pnl": row.get("unrealized_pnl", 0),
                    "unrealized_pnl_percent": row.get("unrealized_pnl_percent", 0),
                    "exec_time": row.get("exec_time", ""),
                    "source_type": source,  # P0 修复: 来源标注
                    "strategy_id": strategy,
                })
            
            return positions
        except Exception as e:
            logger.error(f"查询持仓失败：{e}")
            return []
    
    def get_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取交易记录"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM trade_records 
                ORDER BY trade_date DESC, exec_time DESC
                LIMIT ?
            """, (limit,))
            
            trades = []
            for row in cursor.fetchall():
                trades.append({
                    "symbol": row.get("symbol", ""),
                    "direction": row.get("direction", ""),
                    "quantity": row.get("quantity", 0),
                    "price": row.get("price", 0),
                    "pnl": row.get("pnl", 0),
                    "pnl_percent": row.get("pnl_percent", 0),
                    "trade_date": row.get("trade_date", ""),
                    "exec_time": row.get("exec_time", ""),
                })
            
            return trades
        except Exception as e:
            logger.error(f"查询交易记录失败：{e}")
            return []
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        positions = self.get_positions()
        trades = self.get_trades()
        account = self.get_account_summary()
        
        # 计算胜率
        winning_trades = [t for t in trades if t.get("pnl", 0) > 0]
        win_rate = len(winning_trades) / len(trades) if trades else 0
        
        # 计算总盈亏
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        
        return {
            "total_trades": len(trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(trades) - len(winning_trades),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "current_positions": len(positions),
            "total_assets": account.get("total_assets", 0),
            "available_cash": account.get("available_cash", 0),
        }
