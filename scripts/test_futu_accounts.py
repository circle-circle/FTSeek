#!/usr/bin/env python3
"""列出所有富途真实账户及其资金"""
import os

env_file = os.path.join(os.path.dirname(__file__), "..", ".env.us")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK

host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")

trd_ctx = OpenSecTradeContext(
    filter_trdmarket=TrdMarket.US, host=host, port=port, is_encrypt=False,
)

ret, acc_list = trd_ctx.get_acc_list()
if ret != RET_OK:
    print(f"获取账户列表失败: {acc_list}")
    trd_ctx.close()
    exit(1)

print("=" * 70)
print("所有账户列表:")
print("=" * 70)
for _, row in acc_list.iterrows():
    env = "真实" if row["trd_env"] == TrdEnv.REAL else "模拟"
    print(f"  acc_id={row['acc_id']}  环境={env}  市场={row.get('trd_market_auth', '?')}")

# 解锁
ret, _ = trd_ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)
if ret != RET_OK:
    print("解锁失败")
    trd_ctx.close()
    exit(1)

print("\n" + "=" * 70)
print("逐个查询真实账户资金:")
print("=" * 70)

real_accs = acc_list[acc_list["trd_env"] == TrdEnv.REAL]
for _, acc_row in real_accs.iterrows():
    acc_id = acc_row["acc_id"]
    print(f"\n--- 账户 {acc_id} ---")
    ret2, funds = trd_ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
    if ret2 == RET_OK:
        row = funds.iloc[0]
        for col in funds.columns:
            val = row[col]
            if isinstance(val, (int, float)):
                if abs(val) > 0.01:
                    print(f"  {col:20s}: {val:>15,.2f}")
            else:
                print(f"  {col:20s}: {val}")
    else:
        print(f"  查询失败: {funds}")

# 也查持仓
print("\n" + "=" * 70)
print("逐个查询真实账户持仓:")
print("=" * 70)
for _, acc_row in real_accs.iterrows():
    acc_id = acc_row["acc_id"]
    print(f"\n--- 账户 {acc_id} 持仓 ---")
    ret3, positions = trd_ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
    if ret3 == RET_OK:
        if positions.empty:
            print("  (空仓)")
        else:
            for _, p in positions.iterrows():
                code = p.get("code", "?")
                qty = p.get("qty", 0)
                price = p.get("cur_price", 0)
                pnl = p.get(" unrealized_pl", p.get("unrealized_pl", 0))
                mkt_val = p.get("market_val", 0)
                print(f"  {code:12s}  {qty:>8.0f}股  现价${price:.2f}  市值${mkt_val:,.2f}")
    else:
        print(f"  查询失败: {positions}")

trd_ctx.close()
