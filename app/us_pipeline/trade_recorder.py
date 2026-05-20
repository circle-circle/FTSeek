import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ftseek.trade_recorder")

class TradeRecorder:
    """
    TradeRecorder 负责在交易执行时，原子性地将数据写入 DB。
    确保 `trades` 表记录完整、不可篡改、包含 TraceID。
    """
    
    DB_PATH = "data/ftseek.db"

    @staticmethod
    def record_trade(
        code: str, action: str, quantity: int, price: float,
        signal_id: Optional[str] = None, strategy: str = "unknown", 
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
        trade_id: Optional[str] = None
    ) -> str:
        """
        记录一笔新交易。
        返回 trade_id。
        """
        if not trade_id:
            trade_id = f"trade_{uuid.uuid4().hex[:8]}"
        
        trace_id = f"trace_{uuid.uuid4().hex[:8]}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        logger.info(f"[TradeRecorder] 记录新交易: {trade_id} | {code} {action} x {quantity} @ {price}")
        
        try:
            with sqlite3.connect(TradeRecorder.DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO trades (id, signal_id, code, action, quantity, entry_price, entry_time, status, strategy, trace_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
                    (trade_id, signal_id, code, action, quantity, price, now_str, strategy, trace_id)
                )
                conn.commit()
            return trade_id
        except Exception as e:
            logger.error(f"[TradeRecorder] 记录交易失败: {e}")
            return ""

    @staticmethod
    def update_trade_exit(
        trade_id: str, exit_price: float, exit_reason: str = "unknown", 
        strategy: str = "unknown"
    ) -> bool:
        """
        更新交易平仓信息。
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        logger.info(f"[TradeRecorder] 更新交易退出: {trade_id} | Price: {exit_price} | Reason: {exit_reason}")
        
        try:
            with sqlite3.connect(TradeRecorder.DB_PATH) as conn:
                cursor = conn.cursor()
            
                # 获取原交易数据
                cursor.execute("SELECT entry_price, quantity, action FROM trades WHERE id = ?", (trade_id,))
                row = cursor.fetchone()
            
                if not row:
                    logger.warning(f"Trade {trade_id} not found for exit update")
                    return False
            
                entry_price, quantity, action = row
            
                if action == "BUY":
                    pnl = (exit_price - entry_price) * abs(quantity)
                else:
                    pnl = (entry_price - exit_price) * abs(quantity)
                
                cursor.execute(
                    "UPDATE trades SET exit_price = ?, exit_time = ?, status = 'CLOSED', exit_reason = ?, pnl = ?, strategy = ? WHERE id = ?",
                    (exit_price, now_str, exit_reason, pnl, strategy, trade_id)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"[TradeRecorder] 更新退出失败: {e}")
            return False
