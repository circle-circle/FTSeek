#!/usr/bin/env python3
"""富途账户快速检查 - 简化版"""
import sys
import os
os.environ["FUTU_LOG_LEVEL"] = "ERROR"

from futu import OpenSecTradeContext, TrdMarket, TrdEnv

print("富途连接测试...")

# 先测试模拟环境 (更快)
trd_ctx = OpenSecTradeContext(
    host='127.0.0.1',
    port=11111,
    filter_trdmarket=TrdMarket.HK
)

ret, acc_data = trd_ctx.get_acc_list()
if ret == 0:
    print(f"✓ 账户列表获取成功")
    print(f"  账户数：{len(acc_data)}")
    for idx, row in acc_data.iterrows():
        env = "真实" if row['trd_env'] == 0 else "模拟"
        print(f"  [{idx+1}] {row['acc_id'][:16]}... ({env})")
else:
    print(f"✗ 失败：{acc_data}")

trd_ctx.close()
print("\n完成!")
