#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保本影子实验 A/B 配对结算 (2026-08-19, DEB-20260819 §三十六 P1-a/P1-b).

三部分:
  [B] B_buffer_exit 配对结算: B锚(净保本线+0.15%)被碰到的反事实出场 vs 该仓真实净结局.
      join: symbol+direction+entry(±0.2%)+B.ts∈[entry_time,exit_time]; 排除 TEST 冒烟.
      口径: 双边净 (反事实扣该仓真实往返费率); 主判据=配对差均值+单侧符号检验(α=0.05,n≥30).
  [A] A_no_be_yet 结算 (武装线 0.5→0.8% 反事实): 真实保本出场后用1分K重放当日剩余:
      浮盈(bar收盘)≥0.8% 才武装 → 武装后止损=净保本线, 未武装止损=初始SL, 当日未触发收盘平.
      近似边界(已知偏差): bar收盘≈轮询报价 / 忽略 TIME_EXIT+trail lock+隔夜续持(W_WEEK).
  [W] 开盘窗武装量化: SL_BREAKEVEN/SL_TRAIL_LOCK 出场中, 峰值(time_to_mfe)落在
      开盘窗(<09:45, 含盘前打印)的案例 → 冷却窗峰值不武装(gating)反事实重放.

用法: PYTHONPATH=. venv/bin/python scripts/breakeven_shadow_settle.py
落盘: data/breakeven_shadow_settlement.json
"""
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
BARS_1M = DATA / "replay_bars_1m"
DB = DATA / "ftseek_us.db"

BE_ARM_PCT = 0.5          # 线上 breakeven_at_pct (config 未覆写, 代码默认)
A_ARM_PCT = 0.8           # 影子A变体武装线
COOLDOWN_SL_PCT = 4.0     # 规则0 开盘冷却放宽
WINDOW_END_HHMM = 9 * 60 + 45  # 冷却窗 = 开盘前15min → 09:45
DEFAULT_CUSHION_PCT = 0.08


def load_bars(sym):
    p = BARS_1M / (sym.replace(".", "_") + ".json")
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return [(b[0], float(b[4])) for b in raw]  # (ts, close)
    except Exception:
        return None


def profit_pct(direction, entry, price):
    if direction == "SHORT":
        return (entry - price) / entry * 100
    return (price - entry) / entry * 100


def sign_test_p(diffs):
    """单侧精确二项检验: H1 = P(diff>0) > 0.5. 平局(=0)剔除."""
    vals = [d for d in diffs if d != 0]
    n = len(vals)
    if n == 0:
        return None
    wins = sum(1 for d in vals if d > 0)
    p = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n
    return {"n": n, "wins": wins, "p_one_sided": round(p, 4)}


def agg(diffs, label):
    if not diffs:
        return {"label": label, "n": 0}
    s = sorted(diffs)
    return {
        "label": label, "n": len(diffs),
        "mean_pp": round(sum(diffs) / len(diffs), 3),
        "median_pp": round(s[len(s) // 2], 3),
        "win_pct": round(sum(1 for d in diffs if d > 0) / len(diffs) * 100, 1),
        "min_pp": round(s[0], 3), "max_pp": round(s[-1], 3),
        "sign_test": sign_test_p(diffs),
    }


def to_aware(dt):
    """部分历史记录 entry/exit_time 无时区 → 按 ET 补 (zoneinfo DST 自适)."""
    if dt is not None and dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            return dt.replace(tzinfo=ZoneInfo("America/New_York"))
        except Exception:
            from datetime import timezone
            return dt.replace(tzinfo=timezone(timedelta(hours=-4)))
    return dt


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    trades = []
    for r in c.execute(
        "SELECT id, symbol, direction, entry_price, exit_price, quantity, realized_pnl, "
        "fees_est, pnl_net, pnl_pct, exit_reason, entry_time, exit_time, mfe_pct, metadata_json "
        "FROM trade_records WHERE exit_time IS NOT NULL ORDER BY id"
    ):
        m = {}
        try:
            m = json.loads(r["metadata_json"] or "{}")
        except Exception:
            pass
        trades.append({
            "id": r["id"], "symbol": r["symbol"], "direction": r["direction"],
            "entry": float(r["entry_price"] or 0), "qty": float(r["quantity"] or 0),
            "pnl_net": r["pnl_net"], "fees_est": r["fees_est"] or 0,
            "realized": float(r["realized_pnl"] or 0), "exit_reason": r["exit_reason"],
            "entry_dt": to_aware(datetime.fromisoformat(r["entry_time"]) if r["entry_time"] else None),
            "exit_dt": to_aware(datetime.fromisoformat(r["exit_time"]) if r["exit_time"] else None),
            "meta": m,
        })
    for t in trades:
        t["notional"] = t["entry"] * t["qty"] if t["entry"] > 0 and t["qty"] > 0 else 0
        net = t["pnl_net"] if t["pnl_net"] is not None else (t["realized"] - t["fees_est"])
        t["net_pct"] = net / t["notional"] * 100 if t["notional"] > 0 else None
        cush = t["meta"].get("_be_cushion_fee_aware")
        if cush is None and t["notional"] > 0:
            cush = t["fees_est"] / t["notional"] * 100
        t["cushion_pct"] = float(cush) if cush is not None else DEFAULT_CUSHION_PCT

    def match_trade(sym, direction, entry, ts_dt):
        best = None
        for t in trades:
            if t["symbol"] != sym or t["direction"] != direction or not t["entry_dt"]:
                continue
            if abs(t["entry"] - entry) / entry > 0.002:
                continue
            if not (t["entry_dt"] <= ts_dt <= t["exit_dt"]):
                continue
            d = abs(t["entry"] - entry)
            if best is None or d < best[0]:
                best = (d, t)
        return best[1] if best else None

    # ══ [B] B_buffer_exit 配对结算 ══
    shadow = [json.loads(l) for l in (DATA / "breakeven_shadow.jsonl").open(encoding="utf-8")]
    b_pairs, b_pending, b_unmatched = [], [], []
    for rec in shadow:
        if rec.get("variant") != "B_buffer_exit" or rec.get("symbol") == "TEST":
            continue
        ts_dt = datetime.fromisoformat(rec["ts"])
        t = match_trade(rec["symbol"], rec["direction"], rec["entry_price"], ts_dt)
        if t is None:
            b_unmatched.append(rec)
            continue
        if t["net_pct"] is None:
            b_pending.append({"symbol": rec["symbol"], "ts": rec["ts"]})
            continue
        x = rec["shadow_exit_price"]
        b_net = profit_pct(rec["direction"], rec["entry_price"], x) - t["cushion_pct"]
        b_pairs.append({
            "symbol": rec["symbol"], "direction": rec["direction"], "trade_id": t["id"],
            "real_reason": t["exit_reason"], "mfe": rec.get("mfe_pct"),
            "b_net_pct": round(b_net, 3), "real_net_pct": round(t["net_pct"], 3),
            "diff_pp": round(b_net - t["net_pct"], 3),
            "diff_usd": round((b_net - t["net_pct"]) / 100 * t["notional"], 2),
        })
    b_diffs = [p["diff_pp"] for p in b_pairs]
    b_out = {
        "summary": agg(b_diffs, "B_buffer_exit vs 真实净结局 (全体)"),
        "by_direction": [agg([p["diff_pp"] for p in b_pairs if p["direction"] == d], f"direction={d}")
                         for d in ("LONG", "SHORT")],
        "by_mfe_split": [
            agg([p["diff_pp"] for p in b_pairs if (p["mfe"] or 0) < 0.65], "mfe<0.65 (次级,描述性)"),
            agg([p["diff_pp"] for p in b_pairs if (p["mfe"] or 0) >= 0.65], "mfe>=0.65 (次级,描述性)"),
        ],
        "real_reason_dist": {},
        "n_records": len([r for r in shadow if r.get("variant") == "B_buffer_exit" and r.get("symbol") != "TEST"]),
        "n_pending": len(b_pending), "n_unmatched": len(b_unmatched),
        "pairs": b_pairs,
    }
    for p in b_pairs:
        b_out["real_reason_dist"][p["real_reason"]] = b_out["real_reason_dist"].get(p["real_reason"], 0) + 1

    # ══ [C] C_late_buffer_touch 结算 (时间门控缓冲出场, 预注册于 DEB §三十七) ══
    #  C = 锚触碰时 held_minutes ≥ 60 的首次触碰反事实 (gate=60 为 B 数据事后选定切点)。
    #  历史基线: B 记录中 held≥60 的子集 (C 基建上线前). 翻开关判据: n≥30 + 单侧
    #  符号检验 p<0.05 + 均值>0 (三条全满足; B 教训: 84%胜率但均值≈0 不可翻)。
    c_pairs = []
    for rec in shadow:
        if rec.get("variant") != "C_late_buffer_touch" or rec.get("symbol") == "TEST":
            continue
        ts_dt = datetime.fromisoformat(rec["ts"])
        t = match_trade(rec["symbol"], rec["direction"], rec["entry_price"], ts_dt)
        if t is None or t["net_pct"] is None:
            continue
        x = rec["shadow_exit_price"]
        c_net = profit_pct(rec["direction"], rec["entry_price"], x) - t["cushion_pct"]
        c_pairs.append({
            "symbol": rec["symbol"], "direction": rec["direction"], "trade_id": t["id"],
            "real_reason": t["exit_reason"], "mfe": rec.get("mfe_pct"),
            "held_min": rec.get("held_minutes"),
            "c_net_pct": round(c_net, 3), "real_net_pct": round(t["net_pct"], 3),
            "diff_pp": round(c_net - t["net_pct"], 3),
            "diff_usd": round((c_net - t["net_pct"]) / 100 * t["notional"], 2),
        })
    # 历史基线: B 配对里 held≥60 的子集 (与 C 同口径)
    b_held = {}
    for rec in shadow:
        if rec.get("variant") == "B_buffer_exit" and rec.get("symbol") != "TEST":
            for p in b_pairs:
                if p["symbol"] == rec["symbol"] and p["mfe"] == rec.get("mfe_pct"):
                    b_held[id(p)] = rec.get("held_minutes")
                    break
    c_baseline = [p for p in b_pairs if (b_held.get(id(p)) or 0) >= 60]
    c_out = {
        "summary": agg([p["diff_pp"] for p in c_pairs], "C_late_buffer_touch vs 真实净结局"),
        "baseline_b_held_ge60": agg([p["diff_pp"] for p in c_baseline],
                                    "历史基线: B记录 held≥60min 子集 (探索性)"),
        "flip_criteria": "n>=30 AND 单侧符号检验p<0.05 AND 均值>0 (三条全满足才翻)",
        "n_records": len([r for r in shadow if r.get("variant") == "C_late_buffer_touch"
                          and r.get("symbol") != "TEST"]),
        "pairs": c_pairs,
    }

    # ══ [A] A_no_be_yet 结算 (0.8% 武装线反事实, bar重放) ══
    a_pairs, a_nobars, a_unmatched = [], [], []
    for rec in shadow:
        if rec.get("variant") != "A_no_be_yet" or rec.get("symbol") == "TEST":
            continue
        ts_dt = datetime.fromisoformat(rec["ts"])
        t = match_trade(rec["symbol"], rec["direction"], rec["entry_price"], ts_dt)
        if t is None:
            a_unmatched.append(rec)
            continue
        bars = load_bars(rec["symbol"])
        if not bars:
            a_nobars.append(rec["symbol"])
            continue
        entry, direction = rec["entry_price"], rec["direction"]
        is_short = direction == "SHORT"
        initial_sl = float(rec.get("initial_sl") or 0)
        cush = t["cushion_pct"]
        be_line = entry * (1 - cush / 100) if is_short else entry * (1 + cush / 100)
        exit_key = t["exit_dt"].strftime("%Y-%m-%d %H:%M:%S")
        armed = False
        exit_px = None
        exit_why = ""
        day_bars = [b for b in bars if b[0][:10] == t["exit_dt"].strftime("%Y-%m-%d")
                    and b[0] > exit_key]
        for ts, p in day_bars:
            prof = profit_pct(direction, entry, p)
            if not armed and prof >= A_ARM_PCT:
                armed = True
            if armed and ((is_short and p >= be_line) or (not is_short and p <= be_line)):
                exit_px, exit_why = p, "BE"
                break
            if initial_sl > 0 and ((is_short and p >= initial_sl) or (not is_short and p <= initial_sl)):
                exit_px, exit_why = p, "SL_INITIAL"
                break
        if exit_px is None:
            if day_bars:
                exit_px, exit_why = day_bars[-1][1], "EOD"
            else:
                continue
        counter_net = profit_pct(direction, entry, exit_px) - cush
        a_pairs.append({
            "symbol": rec["symbol"], "direction": direction, "trade_id": t["id"],
            "real_reason": t["exit_reason"], "mfe": rec.get("mfe_at_trigger"),
            "counter_exit_why": exit_why,
            "counter_net_pct": round(counter_net, 3), "real_net_pct": round(t["net_pct"], 3),
            "diff_pp": round(counter_net - t["net_pct"], 3),
            "partial_day": t["exit_dt"].date() == datetime.now().date(),
        })
    a_diffs = [p["diff_pp"] for p in a_pairs]
    a_out = {
        "summary": agg(a_diffs, "A变体(武装0.8%) vs 真实 (当日SL-or-EOD近似)"),
        "exit_why_dist": {},
        "n_records": len([r for r in shadow if r.get("variant") == "A_no_be_yet" and r.get("symbol") != "TEST"]),
        "n_nobars": a_nobars, "n_unmatched": len(a_unmatched),
        "pairs": a_pairs,
        "caveats": "bar收盘近似轮询报价; 忽略TIME_EXIT/trail锁利/隔夜续持(W_WEEK); 当日未触发按收盘平; 8/19为盘中部分日",
    }
    for p in a_pairs:
        a_out["exit_why_dist"][p["counter_exit_why"]] = a_out["exit_why_dist"].get(p["counter_exit_why"], 0) + 1

    # ══ [W] 开盘窗武装量化 (冷却窗峰值不武装 gating 反事实) ══
    w_cases, w_normal = [], []
    for t in trades:
        if t["exit_reason"] not in ("SL_BREAKEVEN", "SL_TRAIL_LOCK"):
            continue
        if not t["entry_dt"] or t["entry_dt"] < datetime(2026, 7, 14, tzinfo=t["entry_dt"].tzinfo):
            continue
        t2m = t["meta"].get("time_to_mfe")
        if t2m is None:
            continue
        peak_dt = t["entry_dt"] + timedelta(minutes=float(t2m))
        mins = peak_dt.hour * 60 + peak_dt.minute
        row = {"trade_id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
               "entry_dt": t["entry_dt"].isoformat(), "peak_dt": peak_dt.isoformat(),
               "mfe": t["meta"].get("mfe_pct"), "reason": t["exit_reason"],
               "real_net_pct": round(t["net_pct"], 3) if t["net_pct"] is not None else None}
        (w_cases if mins < WINDOW_END_HHMM else w_normal).append(row)

    w_pairs = []
    for case in w_cases:
        t = next(x for x in trades if x["id"] == case["trade_id"])
        bars = load_bars(t["symbol"])
        if not bars or t["net_pct"] is None:
            case["sim"] = "no_bars_or_net"
            continue
        entry, direction = t["entry"], t["direction"]
        is_short = direction == "SHORT"
        initial_sl = float(t["meta"].get("stop_loss") or 0)
        cush = t["cushion_pct"]
        be_line = entry * (1 - cush / 100) if is_short else entry * (1 + cush / 100)
        cooldown_sl = entry * (1 + COOLDOWN_SL_PCT / 100) if is_short else entry * (1 - COOLDOWN_SL_PCT / 100)
        entry_key = t["entry_dt"].strftime("%Y-%m-%d %H:%M:%S")
        exit_day = t["exit_dt"].strftime("%Y-%m-%d")
        armed = False
        exit_px = None
        exit_why = ""
        for ts, p in bars:
            if ts <= entry_key or ts[:10] > exit_day:
                continue
            hhmm = int(ts[11:13]) * 60 + int(ts[14:16])
            in_window = hhmm < WINDOW_END_HHMM
            if not in_window and not armed and profit_pct(direction, entry, p) >= BE_ARM_PCT:
                armed = True
            if in_window:
                sl = cooldown_sl
            else:
                sl = be_line if armed else initial_sl
            if sl > 0 and ((is_short and p >= sl) or (not is_short and p <= sl)):
                exit_px, exit_why = p, ("WINDOW_COOLDOWN_SL" if in_window else ("BE" if armed else "SL_INITIAL"))
                break
        if exit_px is None:
            day_bars = [b for b in bars if b[0][:10] == exit_day and b[0] > entry_key]
            if day_bars:
                exit_px, exit_why = day_bars[-1][1], "EOD"
        if exit_px is None:
            case["sim"] = "no_bars_in_range"
            continue
        counter_net = profit_pct(direction, entry, exit_px) - cush
        case["sim"] = {
            "counter_exit_why": exit_why,
            "counter_net_pct": round(counter_net, 3),
            "diff_pp": round(counter_net - t["net_pct"], 3),
            "partial_day": t["exit_dt"].date() == datetime.now().date(),
        }
        w_pairs.append(case)
    w_out = {
        "n_window_armed": len(w_cases), "n_normal_armed": len(w_normal),
        "summary": agg([c["sim"]["diff_pp"] for c in w_pairs if isinstance(c.get("sim"), dict)],
                       "开盘窗武装案例: gating反事实 vs 真实"),
        "cases": w_cases,
        "caveats": "gating=冷却窗(<09:45)内bar峰值不武装; 窗内SL=冷却放宽4%; 近似同[A]",
    }

    out = {"meta": {"generated": datetime.now().isoformat(), "be_arm_pct": BE_ARM_PCT,
                    "a_arm_pct": A_ARM_PCT, "cooldown_sl_pct": COOLDOWN_SL_PCT,
                    "c_gate_min": 60},
           "B": b_out, "C": c_out, "A": a_out, "W": w_out}
    outp = DATA / "breakeven_shadow_settlement.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    slim = {k: {kk: vv for kk, vv in v.items() if kk not in ("pairs", "cases")}
            for k, v in out.items() if k != "meta"}
    print(json.dumps(slim, ensure_ascii=False, indent=1))
    print(f"\n落盘: {outp}")


if __name__ == "__main__":
    main()
