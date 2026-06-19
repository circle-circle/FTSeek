#!/usr/bin/env python3
"""查找所有富途账户（含不同市场）"""
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

# 用 NONE 查所有市场
markets = [
    ("US", TrdMarket.US),
    ("HK", TrdMarket.HK),
    ("CN", TrdMarket.CN),
    ("NONE(全部)", TrdMarket.NONE),
]

for label, market in markets:
    print(f"\n{'='*60}")
    print(f"市场过滤: {label}")
    print(f"{'='*60}")
    try:
        trd_ctx = OpenSecTradeContext(
            filter_trdmarket=market, host=host, port=port, is_encrypt=False,
        )
        ret, acc_list = trd_ctx.get_acc_list()
        if ret == RET_OK:
            if acc_list.empty:
                print("  (无账户)")
            else:
                for _, row in acc_list.iterrows():
                    env = "真实" if row["trd_env"] == TrdEnv.REAL else "模拟"
                    acc_id = row["acc_id"]
                    print(f"  acc_id={acc_id}  环境={env}")
                    # 检查是否匹配用户描述的账户
                    if str(acc_id).startswith("1001") and str(acc_id).endswith("8691"):
                        print(f"  ↑↑↑ ⭐ 匹配! 1001...8691")
        else:
            print(f"  查询失败: {acc_list}")
        trd_ctx.close()
    except Exception as e:
        print(f"  异常: {e}")

# 用 NONE 解锁并查询所有真实账户资金
print(f"\n{'='*60}")
print("用 NONE 市场查询所有真实账户资金")
print(f"{'='*60}")

trd_ctx = OpenSecTradeContext(
    filter_trdmarket=TrdMarket.NONE, host=host, port=port, is_encrypt=False,
)
ret, acc_list = trd_ctx.get_acc_list()
if ret == RET_OK:
    ret2, _ = trd_ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)
    if ret2 == RET_OK:
        real_accs = acc_list[acc_list["trd_env"] == TrdEnv.REAL]
        for _, acc_row in real_accs.iterrows():
            acc_id = acc_row["acc_id"]
            match = "⭐ 匹配!" if str(acc_id).startswith("1001") and str(acc_id).endswith("8691") else ""
            print(f"\n--- 真实账户 {acc_id} {match} ---")
            ret3, funds = trd_ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
            if ret3 == RET_OK:
                row = funds.iloc[0]
                for col in ["currency", "cash", "total_assets", "power", "market_val",
                            "us_cash", "usd_assets", "hk_cash", "hkd_assets",
                            "available_funds", "unrealized_pl", "realized_pl",
                            "risk_level", "risk_status", "is_pdt"]:
                    val = row.get(col)
                    if val is not None and val != "N/A":
                        print(f"  {col:22s}: {val}")
            else:
                print(f"  资金查询失败: {funds}")
    else:
        print("解锁失败")
trd_ctx.close()
