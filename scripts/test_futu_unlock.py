#!/usr/bin/env python3
"""测试富途实盘解锁 (使用 FUTU_UNLOCK_PWD_MD5)"""
import os
import sys

# 加载 .env.us
env_file = os.path.join(os.path.dirname(__file__), "..", ".env.us")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
print(f"MD5 长度: {len(pwd_md5)} 字符")
if not pwd_md5:
    print("❌ FUTU_UNLOCK_PWD_MD5 未设置")
    sys.exit(1)

from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK

host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
port = int(os.getenv("FUTU_OPEND_PORT", "11111"))

trd_ctx = OpenSecTradeContext(
    filter_trdmarket=TrdMarket.US,
    host=host, port=port, is_encrypt=False,
)

ret, acc_list = trd_ctx.get_acc_list()
if ret != RET_OK:
    print(f"❌ 获取账户列表失败: {acc_list}")
    trd_ctx.close()
    sys.exit(1)

real_accs = acc_list[acc_list["trd_env"] == TrdEnv.REAL]
print(f"真实账户: {len(real_accs)} 个")

if real_accs.empty:
    print("❌ 未找到真实账户")
    trd_ctx.close()
    sys.exit(1)

acc_id = real_accs.iloc[0]["acc_id"]
print(f"账户 ID: {acc_id}")

print("尝试 unlock_trade (MD5)...")
ret, data = trd_ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)

if ret == RET_OK:
    print("✅ 解锁成功！查询账户资金...")
    ret2, funds = trd_ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
    if ret2 == RET_OK:
        row = funds.iloc[0]
        for col in ["cash", "total_assets", "power", "market_val", "nav"]:
            val = row.get(col)
            if val is not None:
                print(f"  {col}: ${val:,.2f}")
else:
    print(f"❌ 解锁失败: {data}")

trd_ctx.close()
