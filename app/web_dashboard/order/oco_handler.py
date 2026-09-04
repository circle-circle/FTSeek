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
        
        # DEB-20260903 #123: 遍历所有行 — 原仅处理 ret_data.iloc[0] 首条,
        # 一次推送含多行时后续订单事件被静默丢弃
        for _i in range(len(ret_data)):
            row = ret_data.iloc[_i]
            self._handle_order_row(row)

        return ret_code, ret_data

    def _handle_order_row(self, row) -> None:
        """处理单行订单推送: SL/TP 成交时取消配对单 (OCO)."""
        # 2. 检查订单状态
        order_id = str(row.get("order_id", ""))
        order_status = str(row.get("order_status", ""))
        symbol = str(row.get("code", ""))
        
        # 只处理全部成交
        if "FILLED_ALL" not in order_status:
            return
        
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
                return
            
            sl_id, tp_id = row_db
            
            # 4. 判断当前成交的是 SL 还是 TP，取消配对单
            if order_id == sl_id and tp_id:
                # SL 成交，取消 TP
                logger.info(f"[OCO] {symbol} SL 成交 (order_id={order_id})，取消 TP (order_id={tp_id})")
                # DEB-20260903 #123: 检查撤单返回值 — 原未检查即清 DB,
                # 撤单失败时 TP 仍在 broker 端活跃但 DB 已无记录 = 孤儿挂单
                _cancel_ok = self.service._cancel_broker_order(tp_id)
                self._clear_db_records(symbol, clear_sl=True, clear_tp=_cancel_ok)
                if not _cancel_ok:
                    logger.error(
                        f"[OCO] {symbol} TP (order_id={tp_id}) 撤单失败, "
                        f"DB 保留 tp_broker_order_id 供人工/后续处理 (防孤儿挂单)"
                    )
                
            elif order_id == tp_id and sl_id:
                # TP 成交，取消 SL
                logger.info(f"[OCO] {symbol} TP 成交 (order_id={order_id})，取消 SL (order_id={sl_id})")
                _cancel_ok = self.service._cancel_broker_order(sl_id)
                self._clear_db_records(symbol, clear_sl=_cancel_ok, clear_tp=True)
                if not _cancel_ok:
                    logger.error(
                        f"[OCO] {symbol} SL (order_id={sl_id}) 撤单失败, "
                        f"DB 保留 sl_broker_order_id 供人工/后续处理 (防孤儿挂单)"
                    )
        
        except Exception as e:
            logger.error(f"[OCO] {symbol} 处理失败: {e}")

    def _clear_db_records(self, symbol: str, clear_sl: bool, clear_tp: bool) -> None:
        """清除 DB 中的 SL/TP 订单 ID (仅清已确认的腿, 撤单失败的腿保留 ID)."""
        sets, params = [], []
        if clear_sl:
            sets.append("sl_broker_order_id=NULL")
        if clear_tp:
            sets.append("tp_broker_order_id=NULL")
        if not sets:
            return
        conn = self.service._db()
        params.append(symbol)
        conn.execute(
            f"UPDATE open_positions SET {', '.join(sets)} WHERE symbol=?",
            tuple(params),
        )
        conn.commit()
        conn.close()
