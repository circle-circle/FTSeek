#!/usr/bin/env python3
"""审计 ②: "EOD 兜底豁免水下仓持夜" 规则全样本表现 (只读, 零副作用).

口径 (与 scripts/overnight_depth_gate_replay_v2.py 逐字一致, 便于对撞):
  样本   = trade_records.metadata_json.overnight_arm 存在的行 (权威豁免台账, RFC-067)
  反事实 = 标记日 (marked_at_et 当日) 收盘价平仓
  Δ      = 反事实 − 实际 = s*(EOD_close − exit_price)*qty   (s=+1 LONG / -1 SHORT)
           Δ>0 = 当时平掉更省 (豁免在烧钱); Δ<0 = 豁免修复了这笔 (比EOD平更好同行同往返费)
  收盘价 = data/replay_bars/daily_<sym>_*.json (覆盖 mark_date 的最长文件)
输出: /tmp/eod_exempt_audit.json + stdout 表
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from datetime import date as _date

ROOT = Path("/opt/ftseek/FTSeek-US")
DB = ROOT / "data" / "ftseek_us.db"
CACHE = ROOT / "data" / "replay_bars"
SHADOW = ROOT / "data" / "shadow" / "eod_depth_gate_shadow.jsonl"
FEE_LINE = 5.0


def load_bars_covering(symbol, d):
    best = []
    for f in CACHE.glob(f"daily_{symbol.replace('.', '_')}_*.json"):
        try:
            b = json.loads(f.read_text())
        except Exception as _e:
            print(f"[audit] 坏缓存文件跳过 {f.name}: {_e}", file=sys.stderr)
            continue
        if any(x[0] == d for x in b) and len(b) > len(best):
            best = b
    return best


def close_on(symbol, d):
    bars = load_bars_covering(symbol, d)
    if not bars:
        return None
    for x in bars:
        if x[0] == d:
            return float(x[4])
    return None


con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT id, symbol, direction, entry_time, exit_time, exit_reason,
           entry_price, exit_price, quantity, pnl_net, fees_est, realized_pnl,
           source_type, decision_id, metadata_json
    FROM trade_records
    WHERE json_extract(metadata_json,'$.overnight_arm') IS NOT NULL
    ORDER BY entry_time
""").fetchall()
con.close()

samples, skipped = [], defaultdict(int)
for r in rows:
    arm = json.loads(r["metadata_json"])["overnight_arm"]
    track = str(arm.get("source") or "")
    depth = float(arm.get("underwater_pct_net") or 0)
    mark_date = str(arm.get("marked_at_et") or "")[:10]
    if not mark_date:
        skipped["no_mark_date"] += 1
        continue
    s = 1.0 if r["direction"] == "LONG" else -1.0
    qty = float(r["quantity"] or 0)
    exit_px = float(r["exit_price"] or 0)
    closed = bool(r["exit_time"]) and exit_px > 0
    cl_mark = close_on(r["symbol"], mark_date)
    cl_entry = close_on(r["symbol"], str(r["entry_time"])[:10])
    row = {
        "id": r["id"], "symbol": r["symbol"], "direction": r["direction"],
        "track": track, "night_n": arm.get("night_n"), "depth_pct": depth,
        "marked_at_et": arm.get("marked_at_et"), "mark_date": mark_date,
        "entry_time": r["entry_time"], "exit_time": r["exit_time"],
        "exit_reason": r["exit_reason"], "entry": float(r["entry_price"] or 0),
        "exit": exit_px, "qty": qty, "s": s,
        "pnl_net": r["pnl_net"], "fees_est": r["fees_est"],
        "decision_id": str(r["decision_id"] or "")[:34],
        "eod_close_markday": cl_mark, "eod_close_entryday": cl_entry,
        "closed": closed,
    }
    if closed and cl_mark:
        row["delta_mark"] = s * (cl_mark - exit_px) * qty
    if closed and cl_entry:
        row["delta_entry"] = s * (cl_entry - exit_px) * qty
    if not closed:
        skipped["holding"] += 1
    elif not cl_mark:
        skipped["no_bars_markday"] += 1
    samples.append(row)

# ── 影子交叉核 (decision_price 15:52 vs 日K收盘, 同一决策时刻的两种取价) ──
shadow = []
if SHADOW.exists():
    for ln in SHADOW.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                shadow.append(json.loads(ln))
            except Exception as _e:
                print(f"[audit] 坏影子行跳过: {_e}", file=sys.stderr)

def summarize(tag, rows_, key):
    vals = [(r, r[key]) for r in rows_ if r.get(key) is not None]
    if not vals:
        print(f"  {tag:<26} n=0")
        return {}
    n = len(vals)
    tot = sum(v for _, v in vals)
    saved = sum(-v for _, v in vals if v < -FEE_LINE)   # 豁免比EOD平赚 (修复)
    burned = sum(v for _, v in vals if v > FEE_LINE)    # 豁免比EOD平亏 (烧钱)
    flat = sum(v for _, v in vals if abs(v) <= FEE_LINE)
    n_help = sum(1 for _, v in vals if v < -FEE_LINE)
    n_hurt = sum(1 for _, v in vals if v > FEE_LINE)
    act = sum(float(r["pnl_net"] or 0) for r, _ in vals)
    print(f"  {tag:<26} n={n:>2} 实际净Σ${act:>+9.1f} ΔΣ${tot:>+9.1f} "
          f"| 修复{n_help}(省${saved:>+8.1f}) 烧钱{n_hurt}(耗${burned:>+8.1f}) 微差${flat:>+6.1f}")
    return {"n": n, "actual_net_sum": round(act, 2), "delta_sum": round(tot, 2),
            "n_help": n_help, "saved": round(saved, 2), "n_hurt": n_hurt,
            "burned": round(burned, 2), "flat": round(flat, 2)}


print("═══ 逐笔: 实际结局 vs 反事实(标记日EOD平) ═══")
print(f"{'id':>5} {'mark_date':>10} {'symbol':>9} {'dir':>5} {'track':>19} {'n':>2} "
      f"{'depth%':>7} {'qty':>6} {'实际净$':>9} {'Δ_mark$':>9} {'Δ_entry$':>9}  exit_reason")
for x in sorted(samples, key=lambda y: str(y["marked_at_et"])):
    print(f"{x['id']:>5} {x['mark_date']:>10} {x['symbol']:>9} {x['direction']:>5} "
          f"{x['track']:>19} {str(x['night_n']):>2} {x['depth_pct']:>+7.2f} {x['qty']:>6.0f} "
          f"{(x['pnl_net'] if x['pnl_net'] is not None else 0):>+9.1f} "
          f"{(x['delta_mark'] if x.get('delta_mark') is not None else float('nan')):>+9.1f} "
          f"{(x['delta_entry'] if x.get('delta_entry') is not None else float('nan')):>+9.1f}  "
          f"{x['exit_reason']}")

closed_rows = [x for x in samples if x.get("delta_mark") is not None]
print(f"\n样本: 台账{len(samples)} 已平{len(closed_rows)} 跳过={dict(skipped)}")

print("\n═══ 汇总 (Δ = 反事实EOD平 − 实际; Δ>0 = 豁免烧钱) ═══")
agg = {}
agg["all"] = summarize("全部豁免仓", closed_rows, "delta_mark")
print("  -- 按臂 --")
for tr in sorted({x["track"] for x in closed_rows}):
    agg[f"track:{tr}"] = summarize(f"{tr}", [x for x in closed_rows if x["track"] == tr], "delta_mark")
print("  -- 按深度带 (决策时刻净水深) --")
bands = [("浅水 |d|<=1%", 0, 1), ("中水 1-2.5%", 1, 2.5), ("深水 >2.5%", 2.5, 999)]
for name, lo, hi in bands:
    agg[f"band:{name}"] = summarize(name, [x for x in closed_rows if lo < abs(x["depth_pct"]) <= hi], "delta_mark")
print("  -- 按夜数 --")
for k, sel in [("1夜", lambda x: (x["night_n"] or 1) == 1), ("多夜", lambda x: (x["night_n"] or 1) > 1)]:
    agg[f"night:{k}"] = summarize(k, [x for x in closed_rows if sel(x)], "delta_mark")
print("  -- 按出场原因 --")
for reason in sorted({x["exit_reason"] for x in closed_rows}):
    agg[f"exit:{reason}"] = summarize(str(reason), [x for x in closed_rows if x["exit_reason"] == reason], "delta_mark")
print("\n口径敏感性: Δ 用「入场日EOD」而非「标记日EOD」做反事实")
for tr in sorted({x["track"] for x in closed_rows}):
    summarize(f"entryday-{tr}", [x for x in closed_rows if x["track"] == tr and x.get("delta_entry") is not None], "delta_entry")

print("\n═══ 影子交叉核: 同一决策时刻的 decision_price(15:52实价) vs 日K收盘 ═══")
for srow in shadow:
    if not srow.get("settled"):
        continue
    tid = srow.get("trade_id")
    match = [x for x in samples if x["id"] == tid]
    if not match:
        print(f"  {srow['arm_date']} {srow['symbol']:<9} trade_id={tid} 台账无对应行 (匹配疑点)")
        continue
    m = match[0]
    cl = close_on(srow["symbol"], srow["arm_date"]) or close_on(m["symbol"], m["mark_date"])
    dec = float(srow.get("decision_price") or 0)
    if cl and dec:
        d_qty = float(m["qty"] or 0)
        cf_shadow = m["s"] * (dec - m["exit"]) * d_qty if m["exit"] else None
        print(f"  {srow['arm_date']} {srow['symbol']:<9} id={tid:<5} dec15:52={dec:>9.3f} "
              f"dailyclose={cl:>9.3f} 差={abs(dec-cl)/cl*100:>5.2f}% | 影子Δ={srow.get('cf_delta')} "
              f"我的Δ_mark={m.get('delta_mark') if m.get('delta_mark') is None else round(m['delta_mark'],2)} "
              f"我的Δ_dec={None if cf_shadow is None else round(cf_shadow,2)}")

Path("/tmp/eod_exempt_audit.json").write_text(json.dumps(
    {"samples": samples, "agg": agg, "skipped": dict(skipped)}, ensure_ascii=False, indent=1))
print("\n落盘 /tmp/eod_exempt_audit.json")