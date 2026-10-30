#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DEB-20261031 — chase_cap 改 ATR 相对帽: 同人群双臂回放 (只读, 零生产变更).

预注册: docs/audits/2026-10-31-atr-chase-cap-preregistration.md (读数前写死)
尺子保真: scripts/deb20261031_cap_fidelity_check.py (cap 3316/3316, 违例 3316/3316)

人群   : pullback_limit_shadow.jsonl 中 type=chase_cap_violation 的全部指令 (128 条)
F 臂   : 生产现值 = DA 自带 cap (倒挂时 RFC-120 归一 5%); 结局 = trade_records.pnl_net
A_k 臂 : cap = 生成入场价 ± k×ATR%; 扫该指令的违例轮, 第一个落在帽内的轮入场
退出   : 生产退出栈轮询级重放 (settle_entry_band_widen.settle_widen, 参数运行时读 config)
费用   : estimate_round_trip_fee (同 settled 口径)

用法: env -u PYTHONPATH ./venv/bin/python scripts/deb20261031_atr_chase_cap_replay.py
      [--json-only] [--absolute-sl]
产物: data/reports/deb20261031_atr_chase_cap_latest.json
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date as _date, datetime as _dt, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.playbook.entry_band import CHASE_ZONE_PCT, normalize_chase_cap  # noqa: E402
from app.playbook.entry_routing import chase_cap_violated, parse_raw_semantics  # noqa: E402

SHADOW = ROOT / "data" / "shadow" / "pullback_limit_shadow.jsonl"
DB = ROOT / "data" / "ftseek_us.db"
BARS_PERDAY = ROOT / "data" / "replay_bars"
BARS_MULTI = (ROOT / "data" / "replay_bars_1m", ROOT / "data" / "replay_bars_1m_extra")
OUT = ROOT / "data" / "reports" / "deb20261031_atr_chase_cap_latest.json"

# ── 预注册常量 (改这里 = 改判据 = 须先改预注册文档) ──
K_GRID = (0.15, 0.25, 0.35, 0.50, 0.75)
K_PRIMARY = 0.35
MIN_N = 30
B_BOOT = 10_000
B_PERM = 10_000
ALPHA = 0.05
EFFECT_FLOOR_PER_TRADE = 2.0     # G6: ≥ 往返费一半 (费/笔 ≈ $4)
W1 = ("2026-09-02", "2026-10-01")  # 半开
W2 = ("2026-10-01", "2026-10-31")
HORIZON_DAYS = 2
DEFAULT_NOTIONAL = 5500.0
SEED = 20261031


# ── bar 索引 ────────────────────────────────────────────────────────────
class Bars:
    def __init__(self):
        self.perday = defaultdict(dict)   # sym -> {day: path}
        self.multi = {}                   # sym -> {day: [bars]}
        self._cache = {}
        self.n_multi_parsed = 0
        self.n_multi_bad = 0
        for f in sorted(BARS_PERDAY.iterdir()):
            nm = f.name
            if not nm.endswith(".json") or nm.startswith("daily_"):
                continue
            sym, _, tail = nm[:-5].partition("_2026-")
            if not tail:
                continue
            sym = sym.replace("_", ".", 1)   # 文件名 US_AAPL → US.AAPL (与 DB symbol 同键)
            self.perday[sym]["2026-" + tail[:10]] = f
        for d in BARS_MULTI:
            if not d.exists():
                continue
            for f in sorted(d.iterdir()):
                if not f.name.endswith(".json"):
                    continue
                sym = f.name[:-5].replace("_", ".", 1)
                if sym in self.multi:
                    continue
                try:
                    j = json.loads(f.read_text())
                except Exception as e:  # noqa: BLE001 — 计数留痕, 不静默
                    self.n_multi_bad += 1
                    print(f"[warn] 多日 bar 解析失败 {f}: {e}", file=sys.stderr)
                    continue
                if not j:
                    continue
                days = defaultdict(list)
                for row in j:
                    days[row[0][:10]].append((row[0], float(row[1]), float(row[2]),
                                              float(row[3]), float(row[4])))
                self.multi[sym] = days
                self.n_multi_parsed += 1

    def day_bars(self, sym, day):
        key = (sym, day)
        if key in self._cache:
            return self._cache[key]
        out = None
        p = self.perday.get(sym, {}).get(day)
        if p is not None:
            try:
                j = json.loads(p.read_text())
                out = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in j]
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 日 bar 解析失败 {p}: {e}", file=sys.stderr)
                out = None
        if out is None and sym in self.multi:
            out = self.multi[sym].get(day)
        self._cache[key] = out
        return out

    def window(self, sym, day, horizon_days=HORIZON_DAYS):
        """入场日起连续可用交易日 ≤horizon_days; 遇到缺日即停 (不跨缺口拼接)."""
        d0 = _date.fromisoformat(day)
        bars, days_used = [], []
        for k in range(horizon_days + 1):
            d = (d0 + timedelta(days=k)).isoformat()
            b = self.day_bars(sym, d)
            if not b:
                if k == 0:
                    return None, days_used
                break
            bars.extend(b)
            days_used.append(d)
        return (bars or None), days_used


# ── 统计工具 ────────────────────────────────────────────────────────────
def day_cluster_boot(deltas, dates, B=B_BOOT, seed=SEED):
    by = defaultdict(list)
    for d, v in zip(dates, deltas):
        by[d].append(v)
    keys = list(by)
    rnd = random.Random(seed)
    stats = []
    for _ in range(B):
        pick = [keys[rnd.randrange(len(keys))] for _ in keys]
        s = [v for kk in pick for v in by[kk]]
        stats.append(sum(s) / len(s))
    stats.sort()
    return stats[int(0.025 * B)], stats[int(0.975 * B) - 1], len(keys)


def sign_flip_p(deltas, B=B_PERM, seed=SEED):
    if not deltas:
        return None
    obs = sum(deltas)
    rnd = random.Random(seed + 1)
    n_ge = 0
    for _ in range(B):
        s = sum(d if rnd.random() < 0.5 else -d for d in deltas)
        if s >= obs:
            n_ge += 1
    return (n_ge + 1) / (B + 1)


def summarize(vals, dates=None):
    if not vals:
        return {"n": 0}
    out = {
        "n": len(vals),
        "sum": round(sum(vals), 2),
        "mean": round(statistics.mean(vals), 3),
        "median": round(statistics.median(vals), 3),
        "wr": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        "worst": round(min(vals), 2),
        "best": round(max(vals), 2),
    }
    if dates:
        lo, hi, nd = day_cluster_boot(vals, dates)
        out["ci95"] = [round(lo, 3), round(hi, 3)]
        out["n_days"] = nd
        out["perm_p"] = round(sign_flip_p(vals), 5)
        # 剔最强单日 (T18/T34 稳健性)
        by = defaultdict(float)
        for d, v in zip(dates, vals):
            by[d] += v
        if len(by) > 1:
            worst_day = max(by, key=lambda d: by[d])
            out["drop_best_day_mean"] = round(
                statistics.mean([v for d, v in zip(dates, vals) if d != worst_day]), 3)
    return out


def gen_anchor(raw, inst, sem):
    """生产锚优先级逐字复刻 (auto_execute_flow.py:960-1000)."""
    is_short = "SHORT" in str(inst.get("direction") or "").upper()
    j = raw if isinstance(raw, dict) else {}
    g = j.get("planned_entry")
    if not g:
        edge = inst.get("entry_low") if is_short else inst.get("entry_high")
        try:
            g = (float(edge) / (1 - CHASE_ZONE_PCT) if is_short
                 else float(edge) / (1 + CHASE_ZONE_PCT)) if edge else None
        except (TypeError, ValueError):
            g = None
    if not g:
        conf = sem.get("confirmation") or {}
        if isinstance(conf, dict):
            g = conf.get("price_level")
    return g



# 生产 exit_reason → 回放引擎桶 的显式映射 (口径声明, 非"抹平差异"):
#   回放引擎只产出 SL_TIGHT / BE_TRAIL / DEAD_SIGNAL / TP / EOD 五桶。
#   生产把"保本武装后的锁利"细分为 SL_BREAKEVEN / SL_TRAIL_LOCK / SL_CERT_LOCK,
#   这三个在引擎里同属 BE_TRAIL (锁利栈), SL_INITIAL 才对应 SL_TIGHT。
_REASON_FAMILY = {
    "SL_TIGHT": "SL_TIGHT",
    "SL_INITIAL": "SL_TIGHT",
    "BE_TRAIL": "BE_TRAIL",
    "SL_BREAKEVEN": "BE_TRAIL",
    "SL_TRAIL_LOCK": "BE_TRAIL",
    "SL_CERT_LOCK": "BE_TRAIL",
    "DEAD_SIGNAL": "DEAD_SIGNAL",
    "DEAD_SIGNAL_EXIT": "DEAD_SIGNAL",
    "TP": "TP",
    "TAKE_PROFIT": "TP",
    "EOD": "EOD",
    "EOD_EXIT": "EOD",
    "PRE_CLOSE_EXIT": "EOD",
    "OVERNIGHT_GAP_STOP": "GAP_STOP",
}


def reason_family(x):
    return _REASON_FAMILY.get(str(x or ""), str(x or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--absolute-sl", action="store_true",
                    help="稳健性旁证臂: SL 用指令绝对价而非按回放入场价重推")
    args = ap.parse_args()

    from scripts.settle_entry_band_widen import _prod_exit_params, settle_widen
    from app.playbook.admission import estimate_round_trip_fee

    P = _prod_exit_params()
    bars = Bars()
    print(f"[bars] 多日档解析 {bars.n_multi_parsed} 只, 失败 {bars.n_multi_bad}"
          f" | 单日档 {len(bars.perday)} 只")

    rows = [json.loads(l) for l in SHADOW.open(encoding="utf-8")]
    rows = [r for r in rows if r.get("type") == "chase_cap_violation"]
    by_did = defaultdict(list)
    for r in rows:
        by_did[r["decision_id"]].append(r)
    for v in by_did.values():
        v.sort(key=lambda x: x["ts"])

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    dids = sorted(by_did)
    q = ",".join("?" * len(dids))
    inst = {r["decision_id"]: dict(r) for r in con.execute(
        f"SELECT decision_id, date, symbol, direction, entry_price, entry_low, entry_high, "
        f"raw_instruction_json, atr_pct, stop_loss, take_profit, position_size "
        f"FROM playbook_instructions WHERE decision_id IN ({q})", dids)}
    trades = {r["decision_id"]: dict(r) for r in con.execute(
        f"SELECT decision_id, direction, entry_time, entry_price, quantity, exit_price, "
        f"exit_reason, pnl_net, mfe_pct FROM trade_records WHERE decision_id IN ({q})", dids)}
    con.close()

    # ── 1. 尺子保真 (预注册 §4.1) ──
    n_cap_ok = n_cap_bad = n_vio_ok = n_vio_bad = 0
    for r in rows:
        i = inst.get(r["decision_id"])
        if not i:
            continue
        raw = i["raw_instruction_json"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                raw = {}
        sem = parse_raw_semantics(i["raw_instruction_json"])
        cap0 = sem["chase_cap_price"]
        g = gen_anchor(raw, i, sem)
        if r.get("cap_inverted") is not None and cap0 is not None and g:
            cap_eff, _ = normalize_chase_cap(r["direction"], g, cap0)
        else:
            cap_eff = cap0
        if cap_eff is not None and abs(float(cap_eff) - float(r["chase_cap_price"])) < 1e-6:
            n_cap_ok += 1
        else:
            n_cap_bad += 1
        if chase_cap_violated(r["direction"], r["repriced_entry"], cap_eff):
            n_vio_ok += 1
        else:
            n_vio_bad += 1
    if n_cap_bad or n_vio_bad:
        print(f"[FATAL] 尺子保真未过: cap {n_cap_ok}/{n_cap_ok + n_cap_bad}, "
              f"违例 {n_vio_ok}/{n_vio_ok + n_vio_bad} ⇒ 按预注册 §4.1 停止, 不产生读数")
        return 2
    print(f"[尺子] cap 重建 {n_cap_ok}/{n_cap_ok} | 违例再现 {n_vio_ok}/{n_vio_ok} ✓")

    # ── 2. 逐指令构建双臂 ──
    events, skipped = [], []
    for did, rs in sorted(by_did.items()):
        i = inst.get(did)
        if not i:
            skipped.append((did, "no_instruction"))
            continue
        raw = i["raw_instruction_json"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                raw = {}
        sem = parse_raw_semantics(i["raw_instruction_json"])
        direction = "LONG" if rs[0]["direction"] in ("BUY", "LONG") else "SHORT"
        gen = gen_anchor(raw, i, sem)
        atr = i.get("atr_pct")
        if not gen or not atr or float(atr) <= 0:
            skipped.append((did, "no_gen_or_atr"))
            continue
        gen, atr = float(gen), float(atr)
        day = rs[0]["date"]
        bar_list, days_used = bars.window(i["symbol"], day)
        if not bar_list:
            skipped.append((did, f"no_bars:{i['symbol']} {day}"))
            continue
        qty = int(i.get("position_size") or 0)
        tp = float(i.get("take_profit") or 0) or None
        sl_abs = float(i.get("stop_loss") or 0) or None
        anchor_db = float(i.get("entry_price") or 0) or None

        def exit_net(entry_px, entry_ts_str, entry_day):
            bl, _ = bars.window(i["symbol"], entry_day)
            if not bl:
                return None, "NO_BARS", 0.0, 0.0
            if args.absolute_sl and sl_abs and anchor_db:
                # 旁证臂: 直接用指令绝对 SL (生产下单形态), 距离由回放入场价反推
                sl_pct = abs(entry_px - sl_abs) / entry_px * 100
            else:
                sl_pct = (abs(anchor_db - sl_abs) / anchor_db * 100
                          if (anchor_db and sl_abs) else 3.0)
            sl_dist = max(P["sl_beta"] * sl_pct, P["sl_floor"])
            ex, reason, mfe = settle_widen(direction, entry_px, bl, sl_dist, tp,
                                           entry_ts_str[11:16], P)
            if ex is None:
                return None, reason, 0.0, 0.0
            g_pct = ((ex - entry_px) / entry_px * 100 if direction == "LONG"
                     else (entry_px - ex) / entry_px * 100)
            q_use = qty if qty > 0 else max(1, int(DEFAULT_NOTIONAL / entry_px))
            fee = estimate_round_trip_fee(q_use, entry_px, direction, entry_day)
            return g_pct / 100 * entry_px * q_use - fee, reason, mfe, q_use

        # F 臂 (生产实际)
        t = trades.get(did)
        f_net = float(t["pnl_net"] or 0.0) if t else 0.0
        f_entry = float(t["entry_price"]) if t else None
        f_time = t["entry_time"] if t else None
        f_qty = int(t["quantity"]) if t and t.get("quantity") else qty
        # F′ 臂 (同引擎重放, 尺子对照)
        f_replay, f_reason, f_mfe = (None, None, None)
        if t and f_time:
            f_replay, f_reason, f_mfe, _q = exit_net(f_entry, f_time, f_time[:10])

        arm = {}
        for k in K_GRID:
            cap_k = gen * (1 + k * atr) if direction == "LONG" else gen * (1 - k * atr)
            hit = None
            for r in rs:
                if not chase_cap_violated(rs[0]["direction"], r["repriced_entry"], cap_k):
                    hit = r
                    break
            if hit is None:
                arm[k] = {"fill": False, "net": 0.0}
                continue
            px = float(hit["repriced_entry"])
            net, reason, mfe, q_use = exit_net(px, hit["ts"], hit["date"])
            arm[k] = {"fill": True, "net": (net if net is not None else None),
                      "entry": px, "ts": hit["ts"], "reason": reason,
                      "mfe": round(mfe, 3), "qty": q_use,
                      "sl_dist_pct": round(max(P["sl_beta"] * (
                          abs(anchor_db - sl_abs) / anchor_db * 100
                          if (args.absolute_sl and anchor_db and sl_abs)
                          else (abs(anchor_db - sl_abs) / anchor_db * 100
                                if (anchor_db and sl_abs) else 3.0)), P["sl_floor"]), 3)}

        events.append({
            "did": did, "date": day, "symbol": i["symbol"], "direction": direction,
            "gen": round(gen, 4), "atr_pct": round(atr, 4),
            "cap_prod": float(rs[0]["chase_cap_price"]),
            "rounds": len(rs), "days_used": days_used,
            "F": {"filled": bool(t), "net": round(f_net, 2), "entry": f_entry,
                  "ts": f_time, "reason": (t or {}).get("exit_reason"),
                  "replay_net": round(f_replay, 2) if f_replay is not None else None,
                  "replay_reason": f_reason,
                  "started_before_window": bool(t and f_time and day != f_time[:10])},
            "arms": {str(k): v for k, v in arm.items()},
            "qty": qty,
        })

    n_skipped = len(skipped)
    print(f"[人群] 指令 {len(by_did)} | 可用 {len(events)} | 剔除 {n_skipped}")
    for s in skipped:
        print(f"  剔除: {s[0]} {s[1]}")

    # ── 3. F′ 对账 (预注册 §4.2) ──
    fids = [e for e in events if e["F"]["filled"] and e["F"]["replay_net"] is not None]
    match = sum(1 for e in fids if reason_family(e["F"]["reason"]) == e["F"]["replay_reason"])
    dnet = [e["F"]["replay_net"] - e["F"]["net"] for e in fids]
    fidelity = {
        "n_filled": len(fids),
        "reason_match_pct": round(match / len(fids), 3) if fids else None,
        "abs_net_delta_median": round(statistics.median([abs(x) for x in dnet]), 2) if dnet else None,
        "net_delta_median": round(statistics.median(dnet), 2) if dnet else None,
    }
    print(f"[对账] F′ vs 实际: n={fidelity['n_filled']} "
          f"reason_match={fidelity['reason_match_pct']} "
          f"|Δ净|中位=${fidelity['abs_net_delta_median']} "
          f"Δ净中位=${fidelity['net_delta_median']}")
    absolute_levels_admissible = bool(
        fidelity["reason_match_pct"] is not None
        and fidelity["reason_match_pct"] >= 0.70
        and abs(fidelity["abs_net_delta_median"] or 99) <= 5.0)

    # ── 4. 逐 k 读数 ──
    result = {}
    for k in K_GRID:
        per = []
        cls = {"NEW": [], "REPLACED": [], "SAME_ROUND": [], "LOST": [], "NEITHER": []}
        for e in events:
            a = e["arms"][str(k)]
            f_filled = e["F"]["filled"]
            a_filled = a["fill"] and a.get("net") is not None
            f_net = e["F"]["net"]
            a_net = a["net"] if a_filled else 0.0
            if a_filled and not f_filled:
                kind = "NEW"
            elif a_filled and f_filled:
                kind = ("SAME_ROUND" if abs(a_net - f_net) < 0.01 and
                        e["F"]["entry"] and abs(a["entry"] - e["F"]["entry"]) < 1e-6
                        else "REPLACED")
            elif f_filled and not a_filled:
                kind = "LOST"
            else:
                kind = "NEITHER"
            cls[kind].append({"did": e["did"], "date": e["date"], "symbol": e["symbol"],
                              "direction": e["direction"],
                              "f_net": round(f_net, 2), "a_net": round(a_net, 2),
                              "delta": round(a_net - f_net, 2),
                              "f_entry": e["F"]["entry"], "a_entry": a.get("entry"),
                              "a_reason": a.get("reason")})
            if kind != "NEITHER":
                per.append({"did": e["did"], "date": e["date"], "delta": round(a_net - f_net, 2),
                            "a_net": round(a_net, 2), "f_net": round(f_net, 2)})
        deltas = [p["delta"] for p in per]
        dates = [p["date"] for p in per]
        all_deltas = [round((e["arms"][str(k)]["net"] or 0.0) - e["F"]["net"], 2) for e in events]
        all_dates = [e["date"] for e in events]
        st = summarize(deltas, dates)
        st_all = summarize(all_deltas, all_dates)
        w1 = summarize([p["delta"] for p in per if W1[0] <= p["date"] < W1[1]],
                       [p["date"] for p in per if W1[0] <= p["date"] < W1[1]])
        w2 = summarize([p["delta"] for p in per if W2[0] <= p["date"] < W2[1]],
                       [p["date"] for p in per if W2[0] <= p["date"] < W2[1]])
        a_fills = [e["arms"][str(k)]["net"] for e in events
                   if e["arms"][str(k)]["fill"] and e["arms"][str(k)].get("net") is not None]
        result[str(k)] = {
            "cap_pct_median": round(statistics.median([
                (k * e["atr_pct"] * 100) for e in events]), 3),
            "n_fill": len(a_fills),
            "n_fill_F": sum(1 for e in events if e["F"]["filled"]),
            "arms_stats": summarize(a_fills, [e["date"] for e in events
                                              if e["arms"][str(k)]["fill"]
                                              and e["arms"][str(k)].get("net") is not None]),
            "paired": st,
            "all_instr": st_all,
            "W1": w1,
            "W2": w2,
            "classes": {kk: {"n": len(v), "sum_delta": round(sum(x["delta"] for x in v), 2),
                             "rows": v} for kk, v in cls.items()},
        }

    # ── 5. 判据 ──
    kp = result[str(K_PRIMARY)]
    gate = {}
    gate["G1_n"] = bool(kp["n_fill"] >= MIN_N and kp["paired"].get("n", 0) >= MIN_N)
    ci = kp["paired"].get("ci95")
    gate["G2_sig"] = bool(ci and ci[0] > 0 and kp["paired"]["mean"] > 0)
    w1m, w2m = kp["W1"].get("mean"), kp["W2"].get("mean")
    gate["G3_two_windows_same_sign"] = bool(
        w1m is not None and w2m is not None and w1m > 0 and w2m > 0)
    n_pos_k = sum(1 for k in K_GRID if result[str(k)]["paired"].get("mean", 0) > 0)
    gate["G4_grid_robust"] = bool(n_pos_k >= 3)
    gate["G4_n_pos_k"] = n_pos_k
    gate["G5_classes_reported"] = True
    gate["G6_effect_floor"] = bool(kp["paired"].get("mean", 0) >= EFFECT_FLOOR_PER_TRADE)
    verdict = "GRADUATE_FOR_DEB" if all(v is True for k, v in gate.items()
                                        if k.startswith("G") and isinstance(v, bool)) else "FAIL"

    payload = {
        "generated": _dt.now().astimezone().isoformat(timespec="seconds"),
        "prereg": "docs/audits/2026-10-31-atr-chase-cap-preregistration.md",
        "params": {kk: vv for kk, vv in P.items() if not kk.startswith("_")},
        "absolute_sl_variant": bool(args.absolute_sl),
        "population": {"instructions": len(by_did), "usable": len(events),
                       "skipped": n_skipped, "skipped_detail": skipped,
                       "rows": len(rows), "n_fill_F": sum(1 for e in events if e["F"]["filled"])},
        "fidelity": fidelity,
        "absolute_levels_admissible": absolute_levels_admissible,
        "K_GRID": list(K_GRID), "K_PRIMARY": K_PRIMARY,
        "results": result,
        "gates": gate,
        "verdict": verdict,
        "events": events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"[产物] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())