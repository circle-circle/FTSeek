#!/usr/bin/env python3
"""F3 裁决: 用**已过闸的真实样本** + 缓存 5m bars 重算 adaptive trailing 各上限.

为何不用 replay_ext_entry_sources.py 直接跑: 入场开关 EXT_ENTRY_ALLOW_SIM=false 后, 它与执行器共用的
闸门助手 fail-closed ⇒ gates_passed=0 (工具不复现历史样本)。改为取它**昨天成功跑出的**逐笔明细
(reports/..._pretrailing.json 的 trades), 用 data/replay_cache 里的 K_5M(含延长时段) bars 逐笔重放退出臂。
入场 bar 判据与原工具一致: 当日 04:05 ET 那根 5m bar 的收盘 (并用 entry_px 交叉校验 <1%)。
未触发 trailing ⇒ 落到该样本原有的盘前收口价 (pre_close 臂) —— 与现役"延长时段收口/次日开盘"一致。
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
CACHE = ROOT / "data" / "replay_cache"
FEE_PCT = 0.085
CAPS = [1.2, 2.5, 4.0, 99.0]
FLOOR, TARGET = 0.4, 60.0


def hhmm(t):
    try:
        return int(t[11:13]) * 100 + int(t[14:16])
    except Exception:
        return -1


def load_bars(symbol):
    hits = list(CACHE.glob(f"{symbol.replace('.', '_')}_2026-06-22_2026-09-16_K_5M.json"))
    if not hits:
        return None
    d = json.loads(hits[0].read_text())
    rows = d.get("bars") if isinstance(d, dict) else d
    return rows or None


def trail_exit(bars_day, entry, i0, fallback, end_hhmm=925, arming=1.0):
    """返回 {cap: (fill_px, triggered)}.

    arming = **浮盈武装门槛**(%): 真实系统里 adaptive trailing 是"利润保护阶梯"的一环 —— 必须先进浮盈
    才武装; 若从入场即武装, FLOOR=0.4% 会让它退化成"任何 0.4% 回撤就卖"(2026-09-18 实测: 四档 cap 结果
    完全相同 ⇒ 该口径无效, 已修正为按 arming 门槛武装).
    """
    peak, active, hit = entry, False, {}
    for b in bars_day[i0 + 1:]:
        if hhmm(b["t"]) > end_hhmm:
            break
        peak = max(peak, float(b["h"]))
        if not active and float(b["c"]) >= entry * (1 + arming / 100):
            active = True
        if not active:
            continue
        peak_pnl = (peak / entry - 1) * 100
        for cap in CAPS:
            if cap in hit:
                continue
            dd = min(max(FLOOR, (100 - TARGET) / 100 * peak_pnl), cap)
            line = peak * (1 - dd / 100)
            if float(b["l"]) <= line:
                hit[cap] = (round(min(line, float(b["o"])), 6), True)
    return {cap: hit.get(cap, (fallback, False)) for cap in CAPS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(ROOT / "data/reports/replay_ext_entry_sources_latest_pretrailing.json"))
    ap.add_argument("--out", default=str(ROOT / "data/reports/replay_adaptive_trailing_latest.json"))
    args = ap.parse_args()

    rep = json.loads(Path(args.report).read_text())
    trades = rep.get("trades") or []
    summ = rep.get("summary", {})
    print(f"[样本] 来源报告: min_chg={summ.get('min_chg')} min_vol={summ.get('min_vol')} "
          f"session={summ.get('session')} end={summ.get('end')} gates={summ.get('gates_passed')}")

    bars_cache = {}
    rows = []
    skipped = 0
    for t in trades:
        sym, day, entry = t["symbol"], t["day"], float(t["entry_px"])
        bars = bars_cache.get(sym)
        if bars is None:
            bars = load_bars(sym)
            bars_cache[sym] = bars or []
        if not bars:
            skipped += 1
            continue
        day_bars = [b for b in bars if b["t"][:10] == day]
        if not day_bars:
            skipped += 1
            continue
        idx = [i for i, b in enumerate(day_bars) if hhmm(b["t"]) >= 405]
        if not idx:
            skipped += 1
            continue
        i0 = idx[0]
        if abs(float(day_bars[i0]["c"]) / entry - 1) > 0.01:      # 交叉校验入场 bar
            skipped += 1
            continue
        fb = entry * (1 + t["pre_close"]["ret_pct"] / 100) if t.get("pre_close") else None
        if fb is None:
            skipped += 1
            continue
        r = {"symbol": sym, "day": day, "entry": entry, "mfe": t.get("mfe_pct"),
             "pre_close_net": t["pre_close"]["net_pct"],
             "rule_net": t.get("rule", {}).get("net_pct"),
             "rth_close_net": t.get("rth_close", {}).get("net_pct")}
        for arm_lvl, arm_tag in ((1.0, "a1"), (2.0, "a2")):
            for cap, (px, trig) in trail_exit(day_bars, entry, i0, fb, arming=arm_lvl).items():
                g = (px / entry - 1) * 100
                r[f"trail_{str(cap).replace('.', '')}_{arm_tag}"] = {"net": round(g - FEE_PCT, 3), "trig": trig}
        rows.append(r)

    print(f"[样本] 逐笔可重放 {len(rows)} 笔 (跳过 {skipped}; 报告共 {len(trades)} 笔)")

    def agg(vals):
        vs = sorted(vals)
        return {"n": len(vs), "mean": round(sum(vs) / len(vs), 3), "median": round(vs[len(vs) // 2], 3),
                "worst": round(vs[0], 3), "best": round(vs[-1], 3),
                "win_rate_pct": round(sum(1 for x in vs if x > 0) / len(vs) * 100, 1),
                "total": round(sum(vs), 1)}

    arms = {}
    for cap in CAPS:
        for arm_tag in ("a1", "a2"):
            tag = f"trail_{str(cap).replace('.', '')}_{arm_tag}"
            vals = [r[tag]["net"] for r in rows if tag in r]
            if vals:
                a = agg(vals)
                a["triggered_pct"] = round(100 * sum(1 for r in rows if r.get(tag, {}).get("trig")) / len(vals), 1)
                arms[tag] = a
    for tag, key in (("rule", "rule_net"), ("pre_close", "pre_close_net"), ("rth_close", "rth_close_net")):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if vals:
            arms[tag] = agg(vals)
    mfe = [r["mfe"] for r in rows if r.get("mfe") is not None]
    out = {"generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
           "source_report": args.report, "caliber": "样本=原工具已过闸的盘前逐笔; 退出臂=本脚本重放",
           "n_replayed": len(rows), "n_skipped": skipped, "arms": arms}
    if mfe:
        ms = sorted(mfe)
        out["mfe"] = {"n": len(ms), "mean": round(sum(ms) / len(ms), 3), "median": round(ms[len(ms) // 2], 3),
                      "p90": round(ms[int(len(ms) * 0.9)], 3), "max": round(ms[-1], 3),
                      "ge3_pct": round(100 * sum(1 for x in ms if x >= 3) / len(ms), 1),
                      "ge5_pct": round(100 * sum(1 for x in ms if x >= 5) / len(ms), 1)}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n=== adaptive trailing 上限对照 (盘前, n={len(rows)}, 费用 {FEE_PCT}%/笔) ===")
    print(f"{'arm':>11} {'n':>4} {'mean':>8} {'median':>8} {'worst':>8} {'best':>8} {'WR%':>6} {'total':>9} {'触发%':>6}")
    for tag, a in arms.items():
        print(f"{tag:>11} {a['n']:>4} {a['mean']:>8} {a['median']:>8} {a['worst']:>8} {a['best']:>8} "
              f"{a['win_rate_pct']:>6} {a['total']:>9} {str(a.get('triggered_pct', '-')):>6}")
    if "mfe" in out:
        print("MFE:", json.dumps(out["mfe"], ensure_ascii=False))
    print(f"[out] {args.out}")


if __name__ == "__main__":
    main()