#!/usr/bin/env python3
"""取价 + 复算: EOD 豁免仓 反事实(标记日 EOD 平仓) vs 实际结局.
价格源 1: Futu 日K (K_DAY, autype=NONE) 收盘价 — 与 overnight_depth_gate_replay_v2 同口径
价格源 2: data/shadow/eod_depth_gate_shadow.jsonl 的 decision_price (15:52 决策时刻实价)
只读 + 缓存 /tmp/futu_daily_cache. 输出 /tmp/eod_exempt_prices.json
"""
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow" / "eod_depth_gate_shadow.jsonl"
CACHE = Path("/tmp/futu_daily_cache")
CACHE.mkdir(exist_ok=True)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT id, symbol, direction, entry_time, exit_time, exit_reason, entry_price,
           exit_price, quantity, pnl_net, fees_est, decision_id, metadata_json
    FROM trade_records
    WHERE json_extract(metadata_json,'$.overnight_arm') IS NOT NULL
    ORDER BY entry_time
""").fetchall()
con.close()

syms = sorted({r["symbol"] for r in rows})

from futu import OpenQuoteContext, KLType, AuType, RET_OK

qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
daily = {}
try:
    for sym in syms:
        ck = CACHE / f"daily_{sym.replace('.', '_')}.json"
        if ck.exists():
            daily[sym] = json.loads(ck.read_text())
            continue
        bars, page_key = [], None
        while True:
            kwargs = dict(code=sym, ktype=KLType.K_DAY, autype=AuType.NONE,
                          start="2026-08-18", end="2026-09-23", max_count=1000)
            if page_key:
                kwargs["page_req_key"] = page_key
            ret, data, page_key = qctx.request_history_kline(**kwargs)
            if ret != RET_OK:
                print(f"  !! {sym} 取日K失败: {data}")
                break
            for _, x in data.iterrows():
                bars.append([str(x["time_key"])[:10], float(x["open"]), float(x["high"]),
                             float(x["low"]), float(x["close"])])
            if page_key is None:
                break
            time.sleep(1.2)
        daily[sym] = bars
        ck.write_text(json.dumps(bars))
        time.sleep(0.6)
finally:
    try:
        qctx.close()
    except Exception as _e:
        print(f"[audit] qctx.close 异常: {_e}", file=sys.stderr)

shadow = []
for ln in SHADOW.read_text().splitlines():
    ln = ln.strip()
    if ln:
        try:
            shadow.append(json.loads(ln))
        except Exception as _e:
            print(f"[audit] 坏影子行跳过: {_e}", file=sys.stderr)

def close_on(sym, d):
    for b in daily.get(sym, []):
        if b[0] == d:
            return b[4]
    return None

def dec_px_for(sym, arm_date, entry_price, qty):
    """影子决策价: 优先 (symbol, arm_date) 唯一行; 多行取第一行 (同日多夜只落首行)."""
    hit = [s for s in shadow if s.get("symbol") == sym and s.get("arm_date") == arm_date]
    if not hit:
        return None, "no_shadow"
    h = hit[0]
    same = abs(float(h.get("entry_price") or 0) - entry_price) < 1e-6 and \
           abs(float(h.get("qty") or 0) - qty) < 1e-6
    return float(h.get("decision_price") or 0) or None, ("ok" if same else "entry/qty不一致")

samples = []
for r in rows:
    arm = json.loads(r["metadata_json"])["overnight_arm"]
    md = str(arm.get("marked_at_et") or "")[:10]
    ed = str(r["entry_time"])[:10]
    s = 1.0 if r["direction"] == "LONG" else -1.0
    qty = float(r["quantity"] or 0)
    ex = float(r["exit_price"] or 0)
    closed = bool(r["exit_time"]) and ex > 0
    c_md, c_ed = close_on(r["symbol"], md), close_on(r["symbol"], ed)
    dec, dtag = dec_px_for(r["symbol"], md, float(r["entry_price"] or 0), qty)
    x = {
        "id": r["id"], "symbol": r["symbol"], "dir": r["direction"],
        "track": str(arm.get("source") or ""), "night_n": arm.get("night_n"),
        "depth": float(arm.get("underwater_pct_net") or 0),
        "mark_date": md, "entry_date": ed, "entry": float(r["entry_price"] or 0),
        "exit": ex, "qty": qty, "s": s, "pnl_net": r["pnl_net"],
        "exit_reason": r["exit_reason"], "closed": closed,
        "close_md": c_md, "close_ed": c_ed, "dec_px": dec, "dec_tag": dtag,
        "decision_id": str(r["decision_id"] or "")[:30],
    }
    if closed and c_md:
        x["d_md"] = s * (c_md - ex) * qty          # 反事实(标记日收盘平) − 实际
    if closed and c_ed:
        x["d_ed"] = s * (c_ed - ex) * qty
    if closed and dec:
        x["d_dec"] = s * (dec - ex) * qty          # 反事实(15:52实价平) − 实际
    samples.append(x)

Path("/tmp/eod_exempt_prices.json").write_text(json.dumps(
    {"samples": samples, "daily": daily}, ensure_ascii=False))
print(f"样本 {len(samples)} | 日K覆盖 {sum(1 for x in samples if x['close_md'])} | "
      f"有15:52实价 {sum(1 for x in samples if x['dec_px'])}")

print("\n=== 代理误差: 日K收盘 vs 15:52 决策实价 (同一天) ===")
errs = []
for x in samples:
    if x["close_md"] and x["dec_px"]:
        e = (x["close_md"] - x["dec_px"]) / x["dec_px"] * 100
        errs.append(abs(e))
        print(f"  {x['mark_date']} {x['symbol']:<9} 15:52={x['dec_px']:>9.3f} 收盘={x['close_md']:>9.3f} "
              f"差={e:>+6.2f}%  ({x['dec_tag']})")
if errs:
    print(f"  → |差| 中位 {sorted(errs)[len(errs)//2]:.2f}% / 最大 {max(errs):.2f}% (n={len(errs)})")

print("\n=== 逐笔 Δ (Δ>0 = 豁免烧钱 / Δ<0 = 豁免赚钱) ===")
print(f"{'id':>5} {'mark':>10} {'symbol':>9} {'track':>19} {'n':>2} {'depth%':>7} "
      f"{'实际净$':>9} {'Δ_收盘$':>9} {'Δ_1552$':>9} {'Δ_入场日$':>9}  exit")
for x in sorted(samples, key=lambda y: str(y["mark_date"])):
    f = lambda k: f"{x[k]:>+9.1f}" if x.get(k) is not None else "        -"
    print(f"{x['id']:>5} {x['mark_date']:>10} {x['symbol']:>9} {x['track']:>19} "
          f"{str(x['night_n']):>2} {x['depth']:>+7.2f} "
          f"{(x['pnl_net'] if x['pnl_net'] is not None else 0):>+9.1f} "
          f"{f('d_md')} {f('d_dec')} {f('d_ed')}  {x['exit_reason']}")