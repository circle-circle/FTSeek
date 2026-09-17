#!/usr/bin/env python3
"""F3 裁决回放 (2026-09-18 用户令「按建议执行」): adaptive trailing 上限 1.2 / 2.5 / 4.0 / 无上限 对照.

做法: 薄包装 `replay_ext_entry_sources.py`(复用其候选源/闸门/入场/缓存取数), 用 monkey-patch 在
每条已过闸的样本上**追加** trailing 退出臂, 再从它的明细 (trades) 里自行聚合 —— 因为它的汇总循环
用的是固定 tag 元组, 不认识新臂。

trailing 语义 (对齐 app/playbook/adaptive_trailing.py):
    dd(peak) = clamp((100-target_capture)/100 × peak_pnl, floor, cap);  line = peak × (1 - dd/100)
    · 仅在**进入浮盈后**才激活 (利润保护语义, 避免 0.4% 地板在亏损段乱触发)
    · 逐 bar 用 low 判触发, 成交价取 min(line, bar_open) (开盘即跳空在 line 之下 ⇒ 更差价成交)
    · 未触发 ⇒ 落到该 session 的既定收口 (盘前 09:25 强平 / 盘后 次日开盘)
"""
import argparse
import importlib.util
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REPLAY = ROOT / "scripts" / "replay_ext_entry_sources.py"
REPORT = ROOT / "data" / "reports" / "replay_adaptive_trailing_latest.json"

CAPS = (1.2, 2.5, 4.0, 99.0)          # 99 = 事实上无上限(纯 40% 捕获)
FLOOR = 0.4
TARGET_CAPTURE = 60.0


def _load_replay():
    spec = importlib.util.spec_from_file_location("replay_ext_entry_sources", REPLAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _add_trail_arms(mod, res, bars_day, entry_bar_i, end_hhmm, fallback_px, entry):
    """在 res 上追加 trail_12/25/40/99 四臂."""
    t0 = bars_day[entry_bar_i]["t"]
    peak = entry
    active = False
    hit = {}
    for b in bars_day:
        if b["t"] <= t0:
            continue
        if mod._hhmm(b["t"]) > end_hhmm:
            break
        peak = max(peak, float(b["h"]))
        if not active and float(b["c"]) >= entry:
            active = True                      # 进入浮盈后才跟踪
        if not active:
            continue
        peak_pnl = (peak / entry - 1) * 100
        for cap in CAPS:
            if cap in hit:
                continue
            dd = min(max(FLOOR, (100 - TARGET_CAPTURE) / 100 * peak_pnl), cap)
            line = peak * (1 - dd / 100)
            if float(b["l"]) <= line:
                hit[cap] = (round(min(line, float(b["o"])), 6), b["t"])
    for cap in CAPS:
        tag = "trail_" + str(cap).replace(".", "")
        px, t = hit.get(cap, (None, None))
        triggered = px is not None
        if not triggered:
            px = fallback_px
        if px:
            g = (px / entry - 1) * 100
            res[tag] = {"ret_pct": round(g, 3), "net_pct": round(g - mod.FEE_PCT, 3),
                        "trail_triggered": triggered, "trail_exit_t": t, "trail_cap": cap}
    res["trail_mfe_gated"] = {"ret_pct": 0, "net_pct": 0, "trail_note":
                              "trail_12 = 现役配置; trail_25 = 建议; trail_99 = 纯 60% 捕获(无上限)"}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--end", default="2026-09-16")
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours"])
    ap.add_argument("--source", default="both", choices=["beat", "watchlist", "both"])
    ap.add_argument("--min-chg", type=float, default=None)
    ap.add_argument("--min-vol", type=float, default=None)
    args = ap.parse_args()

    mod = _load_replay()
    mod._simulate = "orig"
    # 保留原函数引用
    orig_pre = mod.__dict__["_simulate"]
    orig_ah = mod.__dict__["_simulate_ah"]

    def patched_pre(bars_day, prev_close, entry_bar_i, decide):
        res = orig_pre(bars_day, prev_close, entry_bar_i, decide)
        entry = bars_day[entry_bar_i]["c"]
        fb = entry * (1 + res.get("pre_close", {}).get("ret_pct", 0) / 100) if res.get("pre_close") else None
        return _add_trail_arms(mod, res, bars_day, entry_bar_i, mod.PREMARKET_END, fb, entry)

    def patched_ah(bars_day, next_day_bars, entry_bar_i, decide):
        res = orig_ah(bars_day, next_day_bars, entry_bar_i, decide)
        entry = bars_day[entry_bar_i]["c"]
        fb = entry * (1 + res.get("next_open", {}).get("ret_pct", 0) / 100) if res.get("next_open") else None
        return _add_trail_arms(mod, res, bars_day, entry_bar_i, 2000, fb, entry)

    mod._simulate = patched_pre
    mod._simulate_ah = patched_ah

    # 保底: 原报告另存
    old = ROOT / "data" / "reports" / "replay_ext_entry_sources_latest.json"
    if old.exists():
        shutil.copy(old, ROOT / "data" / "reports" / "replay_ext_entry_sources_latest_pretrailing.json")

    argv = ["replay_ext_entry_sources.py", "--days", str(args.days), "--end", args.end,
            "--session", args.session, "--source", args.source]
    if args.min_chg is not None:
        argv += ["--min-chg", str(args.min_chg)]
    if args.min_vol is not None:
        argv += ["--min-vol", str(args.min_vol)]
    sys.argv = argv
    mod.main()

    data = json.loads(old.read_text())
    trades = data.get("trades") or []
    out = {"generated_at": datetime.utcnow().isoformat() + "Z", "session": args.session,
           "end": args.end, "days": args.days, "n_passed": len(trades), "arms": {}}
    tags = list(CAPS and ("trail_" + str(c).replace(".", "") for c in CAPS))
    tags += ["rule", "pre_close", "rth_close", "session_close", "next_open", "next_rth_close", "intraday_sl"]
    for tag in tags:
        vals = [t[tag]["net_pct"] for t in trades if tag in t]
        trig = [t[tag].get("trail_triggered") for t in trades if tag in t and "trail_triggered" in t[tag]]
        if not vals:
            continue
        vs = sorted(vals)
        out["arms"][tag] = {
            "n": len(vals), "mean": round(sum(vals) / len(vals), 3),
            "median": round(vs[len(vs) // 2], 3), "worst": round(vs[0], 3), "best": round(vs[-1], 3),
            "win_rate_pct": round(sum(1 for x in vals if x > 0) / len(vals) * 100, 1),
            "total": round(sum(vals), 1),
            "trail_triggered_pct": round(100 * sum(1 for x in trig if x) / len(trig), 1) if trig else None,
        }
    mfe = [t["mfe_pct"] for t in trades if t.get("mfe_pct") is not None]
    if mfe:
        ms = sorted(mfe)
        out["mfe"] = {"n": len(mfe), "mean": round(sum(mfe) / len(mfe), 3), "median": round(ms[len(ms) // 2], 3),
                      "p90": round(ms[int(len(ms) * 0.9)], 3), "max": round(ms[-1], 3),
                      "ge3_pct": round(100 * sum(1 for x in mfe if x >= 3) / len(mfe), 1)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("\n=== adaptive trailing 上限对照 (session=%s, n=%d) ===" % (args.session, len(trades)))
    print(f"{'arm':>12} {'n':>4} {'mean':>8} {'median':>8} {'worst':>8} {'best':>8} {'WR%':>6} {'total':>9} {'触发%':>6}")
    for tag, a in out["arms"].items():
        print(f"{tag:>12} {a['n']:>4} {a['mean']:>8} {a['median']:>8} {a['worst']:>8} {a['best']:>8} "
              f"{a['win_rate_pct']:>6} {a['total']:>9} {str(a['trail_triggered_pct']):>6}")
    if "mfe" in out:
        print("MFE:", json.dumps(out["mfe"], ensure_ascii=False))
    print(f"[trailing] 明细 → {REPORT}")


if __name__ == "__main__":
    main()