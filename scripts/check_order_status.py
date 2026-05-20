#!/usr/bin/env python3
"""
查询期权订单状态
"""
import sys
sys.path.insert(0, "/opt/ftseek/FTSeek-US")

from app.execution.broker_adapter import FutuBrokerAdapter
from futu import TrdEnv, RET_OK

def check_order_status():
    adapter = FutuBrokerAdapter()
    adapter.connect()
    
    trd_env = TrdEnv.SIMULATE if adapter.trade_env == 1 else TrdEnv.REAL
    
    order_ids = ['7729429', '7729484', '7729316', '7729293']  # US.MU 和 US.SNDK
    
    print("=== 期权订单状态查询 ===")
    for oid in order_ids:
        ret, data = adapter.trade_ctx.order_list_query(order_id=oid, trd_env=trd_env)
        if ret == RET_OK and not data.empty:
            row = data.iloc[0]
            status = row.get('order_status', 'N/A')
            code = row.get('code', 'N/A')
            qty = row.get('qty', 0)
            dealt = row.get('dealt_qty', 0)
            price = row.get('price', 0)
            avg_price = row.get('dealt_avg_price', 0)
            print(f"订单 {oid}: 代码={code}, 状态={status}, 数量={qty}, 成交={dealt}, 价格={price}, 成交均价={avg_price}")
        else:
            print(f"订单 {oid}: 查询失败")
    
    adapter.disconnect()

if __name__ == "__main__":
    check_order_status()
