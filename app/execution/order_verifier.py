"""
订单执行验证器
下单后查询订单状态，确认是否真正成交
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class OrderVerifier:
    """订单验证器"""
    
    def __init__(self, trade_ctx, max_retries: int = 3, retry_interval: float = 2.0):
        self.trade_ctx = trade_ctx
        self.max_retries = max_retries
        self.retry_interval = retry_interval
    
    def verify_order(self, futu_order_id: str, timeout: float = 30.0) -> Dict[str, Any]:
        """
        验证订单状态
        
        Args:
            futu_order_id: 富途订单 ID
            timeout: 超时时间（秒）
        
        Returns:
            {
                "verified": bool,
                "status": str,  # filled, cancelled, rejected, pending
                "filled_qty": int,
                "filled_price": float,
                "error": str,
            }
        """
        import time
        from futu import RET_OK, OrderStatus
        
        result = {
            "verified": False,
            "status": "unknown",
            "filled_qty": 0,
            "filled_price": 0,
            "error": None,
        }
        
        start_time = time.time()
        retries = 0
        
        while time.time() - start_time < timeout:
            try:
                ret, data = self.trade_ctx.order_list_query(
                    order_id=futu_order_id
                )
                
                if ret == RET_OK and len(data) > 0:
                    order = data.iloc[0]
                    status = order.get('order_status', '')
                    filled_qty = order.get('dealt_qty', 0)
                    filled_price = order.get('dealt_avg_price', 0)
                    
                    result["status"] = str(status)
                    result["filled_qty"] = int(filled_qty) if filled_qty else 0
                    result["filled_price"] = float(filled_price) if filled_price else 0
                    
                    # 检查订单状态
                    if status in ['Filled_All', 'filled_all']:
                        result["verified"] = True
                        result["status"] = "filled"
                        logger.info(f"[订单验证] {futu_order_id} 完全成交 qty={filled_qty} price={filled_price}")
                        return result
                    
                    elif status in ['Cancelled', 'cancelled', 'Rejected', 'rejected']:
                        result["verified"] = False
                        result["status"] = "cancelled" if 'cancel' in str(status).lower() else "rejected"
                        result["error"] = f"订单{result['status']}"
                        logger.warning(f"[订单验证] {futu_order_id} {result['status']}")
                        return result
                    
                    elif status in ['Filled_Part', 'filled_part']:
                        result["verified"] = True  # 部分成交也算验证通过
                        result["status"] = "partial_filled"
                        logger.info(f"[订单验证] {futu_order_id} 部分成交 qty={filled_qty}/{order.get('qty', 0)}")
                    
                    else:
                        # 待成交状态，继续等待
                        logger.debug(f"[订单验证] {futu_order_id} 状态: {status}")
                
                else:
                    result["error"] = str(data) if data else "查询无结果"
                
            except Exception as e:
                result["error"] = str(e)
                logger.error(f"[订单验证] 查询失败: {e}")
            
            time.sleep(self.retry_interval)
            retries += 1
        
        # 超时
        result["status"] = "timeout"
        result["error"] = f"验证超时 ({timeout}s)"
        logger.warning(f"[订单验证] {futu_order_id} 超时")
        
        return result
    
    def verify_and_alert(self, futu_order_id: str, symbol: str, expected_qty: int) -> Dict[str, Any]:
        """
        验证订单并发送告警
        """
        result = self.verify_order(futu_order_id)
        
        if not result["verified"]:
            # 发送告警
            logger.error(
                f"[订单告警] {symbol} 订单验证失败! "
                f"order_id={futu_order_id} expected_qty={expected_qty} "
                f"status={result['status']} error={result['error']}"
            )
            # TODO: 发送钉钉告警
        
        return result


def verify_recent_orders(hours: int = 24) -> Dict[str, Any]:
    """
    验证最近 N 小时内的所有订单
    
    Returns:
        {
            "total": int,
            "verified": int,
            "failed": int,
            "details": list,
        }
    """
    from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
    import time
    
    result = {
        "total": 0,
        "verified": 0,
        "failed": 0,
        "details": [],
    }
    
    try:
        trd_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host='127.0.0.1',
            port=11111,
            is_encrypt=False
        )
        
        # 查询最近的订单
        ret, data = trd_ctx.history_order_list_query(
            start=datetime.now().strftime('%Y-%m-%d'),
            end=datetime.now().strftime('%Y-%m-%d'),
            trd_env=TrdEnv.SIMULATE,
        )
        
        if ret == RET_OK:
            for idx, order in data.iterrows():
                result["total"] += 1
                order_id = order.get('order_id', '')
                symbol = order.get('code', '')
                status = order.get('order_status', '')
                dealt_qty = order.get('dealt_qty', 0)
                
                is_filled = status in ['Filled_All', 'filled_all', 'Filled_Part', 'filled_part']
                
                if is_filled:
                    result["verified"] += 1
                else:
                    result["failed"] += 1
                
                result["details"].append({
                    "order_id": order_id,
                    "symbol": symbol,
                    "status": status,
                    "dealt_qty": dealt_qty,
                    "verified": is_filled,
                })
        
        trd_ctx.close()
        
    except Exception as e:
        logger.error(f"验证订单失败: {e}")
    
    return result


if __name__ == "__main__":
    import argparse
    import json
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", type=str, help="验证指定订单 ID")
    parser.add_argument("--recent", type=int, help="验证最近 N 小时的订单")
    args = parser.parse_args()
    
    if args.order_id:
        from futu import OpenSecTradeContext, TrdMarket
        trd_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host='127.0.0.1', port=11111)
        verifier = OrderVerifier(trd_ctx)
        result = verifier.verify_order(args.order_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        trd_ctx.close()
    
    elif args.recent:
        result = verify_recent_orders(args.recent)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    
    else:
        parser.print_help()
