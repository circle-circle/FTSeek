#!/usr/bin/env python3
"""开盘第一刻持仓核对 (2026-09-16, 一次性).

09:31 ET 取全部持仓开盘价 vs SL, 并对照 06:25 盘前 last, 回答:
  哪些盘前盈利兑现了 / 哪些蒸发了 / 哪些已触 SL.
只读+打印, 零落库 (供 9/16 开盘即时推送给用户).
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "ftseek_us.db"
# 9/16 06:25 ET 盘前 last (实测快照, 对照基准)
PREMARKET_LAST = {
    "US.UBER": 71.43, "US.NOK": 9.84, "US.NVDA": 212.17,
    "US.ASML": 1591.48, "US.MU": 927.6,
}

def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    pos = conn.execute(
        "SELECT symbol,direction,size,avg_entry_price,current_sl "
        "FROM open_positions WHERE size>0").fetchall()
    conn.close()
    if not pos:
        print("无持仓"); return

    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    snap = {}
    try:
        ret, df = ctx.get_market_snapshot([p[0] for p in pos])
        if ret == RET_OK:
            for _, r in df.iterrows():
                snap[r["code"]] = r
    finally:
        ctx.close()

    print(f"{'sym':<8}{'entry':>9}{'SL':>9}{'open_now':>9}{'vs_entry':>9}{'vs_pre':>8}{'SL?':>5}")
    for s, d, q, e, sl in pos:
        r = snap.get(s)
        now = float(r.get("last_price", 0) or 0) if r is not None else 0.0
        sign = -1 if d in ("SHORT", "SELL_SHORT") else 1
        vs_entry = (now - e) / e * 100 * sign if now and e else 0
        pre = PREMARKET_LAST.get(s)
        vs_pre = (now - pre) / pre * 100 * sign if now and pre else 0
        sl_hit = "HIT" if (now and sl and sign * (sl - now) >= 0) else ""
        print(f"{s:<8}{e:>9.2f}{sl:>9.2f}{now:>9.2f}{vs_entry:>+8.2f}%{vs_pre:>+7.2f}%{sl_hit:>5}")

if __name__ == "__main__":
    main()
