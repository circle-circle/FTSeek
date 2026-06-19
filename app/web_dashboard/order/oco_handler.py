"""
事件驱动的 OCO (One-Cancels-Other) 处理器

当 SL 或 TP 订单成交时，自动取消配对订单。
利用 Futu 推送机制，无需轮询。
"""

import logging
from typing import TYPE_CHECKING

from futu.trade.trade_response_handler import TradeOrderHandlerBase

if TYPE_CHECKING:
    from app.web_dashboard.manual_order_service import ManualOrderService

logger = logging.getLogger(__name__)


class OCOHandler(TradeOrderHandlerBase):
    """Futu 订单推送处理器 - 实现 OCO 逻辑"""
    
    def __init__(self, service: "ManualOrderService"):
        super().__init__()
        self.service = service
    
    def on_recv_rsp(self, rsp_pb):
        """处理订单推送事件"""
        # 1. 调用父类解包
        ret_code, ret_data = super().on_recv_rsp(rsp_pb)
        
        if ret_code != 0 or ret_data is None or ret_data.empty:
            return ret_code, ret_data
        
        # 2. 检查订单状态
        row = ret_data.iloc[0]
        order_id = str(row.get("order_id", ""))
        order_status = str(row.get("order_status", ""))
        symbol = str(row.get("code", ""))
        
        # 只处理全部成交
        if "FILLED_ALL" not in order_status:
            return ret_code, ret_data
        
        # 3. 查询 DB 中的 SL/TP 配对
        try:
            self.service._ensure_sl_column()
            conn = self.service._db()
            cursor = conn.execute(
                "SELECT sl_broker_order_id, tp_broker_order_id FROM open_positions WHERE symbol=?",
                (symbol,)
            )
            row_db = cursor.fetchone()
            conn.close()
            
            if not row_db:
                return ret_code, ret_data
            
            sl_id, tp_id = row_db
            
            # 4. 判断当前成交的是 SL 还是 TP，取消配对单
            if order_id == sl_id and tp_id:
                # SL 成交，取消 TP
                logger.info(f"[OCO] {symbol} SL 成交 (order_id={order_id})，取消 TP (order_id={tp_id})")
                self.service._cancel_broker_order(tp_id)
                
                # 清除 DB 记录
                conn = self.service._db()
                conn.execute(
                    "UPDATE open_positions SET sl_broker_order_id=NULL, tp_broker_order_id=NULL WHERE symbol=?",
                    (symbol,)
                )
                conn.commit()
                conn.close()
                
            elif order_id == tp_id and sl_id:
                # TP 成交，取消 SL
                logger.info(f"[OCO] {symbol} TP 成交 (order_id={order_id})，取消 SL (order_id={sl_id})")
                self.service._cancel_broker_order(sl_id)
                
                # 清除 DB 记录
                conn = self.service._db()
                conn.execute(
                    "UPDATE open_positions SET sl_broker_order_id=NULL, tp_broker_order_id=NULL WHERE symbol=?",
                    (symbol,)
                )
                conn.commit()
                conn.close()
        
        except Exception as e:
            logger.error(f"[OCO] {symbol} 处理失败: {e}")
        
        return ret_code, ret_data
