#!/usr/bin/env python3
"""E1 存量预演 (DEB-20260815 §11.2) — 纯信号强度 top-N vs DA 实际选股, 同日开盘结算.

口径: 信号日开盘价结算 (E1 前瞻影子臂将用"次一交易日开盘价"正式判据, 本存量预演
  用信号日开盘是因为 DA 实际成交也发生在信号日盘中, 保持两臂同口径可比)。
两臂:
  臂P (纯强度): 每日 DA 信号按 strength 排序取 top-3, 开盘价买入持至当日收盘。
  臂D (DA实际): playbook_instructions 中 DA 当日实际成交单, 开盘价买入持至当日收盘。
alpha 剥离: 减 SPY 同日开→收收益。
"""
import json, sqlite3, time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

REPO = Path("/opt/ftseek/FTSeek-US")
SNAP_DB = REPO / "data" / "signal_snapshots.db"
DB = REPO / "data" / "ftseek_us.db"
CACHE_DIR = REPO / "data" / "replay_bars"
OUT = REPO / "data" / "e1_preview_2026-08-15.json"
MIN_DATE = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

# SPY 同日开→收
def load_daily_fresh(sym):
    start = (datetime.strptime(MIN_DATE, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    cache = CACHE_DIR / f"daily_{sym.replace('.', '_')}_{start}_{datetime.now().strftime('%Y-%m-%d')}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from futu import OpenQuoteContext, KLType, RET_OK, AuType
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        for _ in (1, 2):
            ret, data, _ = ctx.request_history_kline(
                sym, ktype=KLType.K_DAY, start=start,
                end=datetime.now().strftime("%Y-%m-%d"), max_count=400, autype=AuType.NONE)
            if ret == RET_OK and data is not None and len(data) > 0:
                bars = [[str(r["time_key"])[:10], float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])] for _, r in data.iterrows()]
                bars.sort(key=lambda b: b[0])
                cache.write_text(json.dumps(bars))
                return bars
            time.sleep(3)
    finally:
        ctx.close()
    return []

spy = {b[0]: b for b in load_daily_fresh("US.SPY")}

# 日线缓存 (E2 已拉)
daily = {}
for p in CACHE_DIR.glob("daily_*_*.json"):
    try:
        parts = p.stem.split("_")
        sym = ".".join(parts[1:-2])
        daily[sym] = {b[0]: b for b in json.loads(p.read_text())}
    except Exception:
        pass

# 1. DA 信号 by day
con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
sigs = con.execute(f"""
    SELECT symbol, trade_date, direction, strength FROM signal_snapshots
    WHERE trade_date>='{MIN_DATE}' AND strategy_id='deep_analysis'
      AND direction IN ('LONG','SHORT') AND fwd_ret_t1 IS NOT NULL
""").fetchall()
con.close()
by_day = defaultdict(list)
seen = set()
for r in sigs:
    k = (r["symbol"], r["trade_date"], r["direction"])
    if k in seen:
        continue
    seen.add(k)
    by_day[r["trade_date"]].append(dict(r))

# 2. DA 实际成交 by day (playbook_instructions DA 来源, 当日非SKIP)
con2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con2.row_factory = sqlite3.Row
instr = con2.execute(f"""
    SELECT symbol, direction, created_at FROM playbook_instructions
    WHERE decision_id LIKE 'DA-%' AND direction IN ('BUY','SELL_SHORT')
      AND substr(created_at,1,10) >= '{MIN_DATE}'
""").fetchall()
con2.close()
da_by_day = defaultdict(list)
for r in instr:
    d = r["created_at"][:10]
    da_by_day[d].append(r["symbol"])

def day_ret(sym, date, direction):
    """信号日开盘→收盘方向化收益% (LONG); SHORT 反向."""
    bars = daily.get(sym, {})
    if date not in bars:
        return None
    o, c = bars[date][1], bars[date][4]
    sign = 1.0 if direction in ("LONG", "BUY") else -1.0
    return (c / o - 1) * 100 * sign

def spy_ret(date):
    if date not in spy:
        return None
    return (spy[date][4] / spy[date][1] - 1) * 100

TOP_N = 3
arm_p, arm_d = [], []
for date in sorted(by_day):
    sr = spy_ret(date)
    if sr is None:
        continue
    # 臂P: top-3 strength (LONG only, SHORT 信号在 E2 已证灾难性, 预演保守起见分开报)
    longs = sorted([s for s in by_day[date] if s["direction"] == "LONG"],
                   key=lambda x: -x["strength"])[:TOP_N]
    for s in longs:
        r = day_ret(s["symbol"], date, "LONG")
        if r is not None:
            arm_p.append({"date": date, "sym": s["symbol"], "ret": r, "alpha": r - sr})
    # 臂D: DA 实际选股
    for sym in set(da_by_day.get(date, [])):
        r = day_ret(sym, date, "LONG")
        if r is not None:
            arm_d.append({"date": date, "sym": sym, "ret": r, "alpha": r - sr})

def stats(sub, key):
    n = len(sub)
    if not n:
        return None
    v = sum(x[key] for x in sub) / n
    return {"n": n, "avg": round(v, 3),
            "sum": round(sum(x[key] for x in sub), 2)}

sp, sd = stats(arm_p, "alpha"), stats(arm_d, "alpha")
print(f"=== E1 存量预演: 信号日开盘→收盘, alpha(减SPY同日) ===")
print(f"臂P (纯强度top-{TOP_N} LONG): {sp}")
print(f"臂D (DA实际选股 LONG):       {sd}")
if sp and sd:
    print(f"差 (P-D): {sp['avg'] - sd['avg']:+.3f}%/笔 alpha")
print(f"\n笔数: 臂P {len(arm_p)}, 臂D {len(arm_d)}")

OUT.write_text(json.dumps({"arm_p_topN": sp, "arm_d_actual": sd,
                           "per_trade_p": arm_p, "per_trade_d": arm_d,
                           "note": "信号日开盘→收盘口径, 非次一交易日开盘正式判据"},
                          ensure_ascii=False, indent=1))
print(f"产出: {OUT}")
