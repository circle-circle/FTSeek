
import sqlite3
import sys

print("[1] 本地数据库持仓 (ftseek.db):")
conn = sqlite3.connect("data/ftseek.db")
c = conn.cursor()
c.execute("SELECT code, quantity, entry_price, status FROM positions WHERE market='HK' AND status='OPEN'")
rows = c.fetchall()
local_codes = set()
for r in rows:
    print(f"  {r}")
    local_codes.add(r[0])
conn.close()

print("")
print("[2] 富途模拟盘持仓:")
sys.path.insert(0, "/opt/ftseek/FTSeek")
try:
    from futu import OpenSecTradeContext, TrdEnv
    ctx = OpenSecTradeContext(host="127.0.0.1", port=11111)
    ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
    futu_codes = set()
    if ret == 0:
        hk_data = data[data["code"].str.contains("HK")]
        for _, row in hk_data.iterrows():
            print(f"  {row['code']}, qty={row['qty']}, cost={row['cost_price']}")
            futu_codes.add(row['code'])
    else:
        print(f"Error: {data}")
    ctx.close()
except Exception as e:
    print(f"Error: {e}")
    futu_codes = set()

print("")
print("[3] 差异分析:")
print(f"  Futu 有但本地无: {futu_codes - local_codes}")
print(f"  本地有但 Futu 无: {local_codes - futu_codes}")
