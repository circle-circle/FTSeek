#!/usr/bin/env python3
"""入场错（墓地）事前可识别性普查 + entry_confirm 反事实回放。

回答一个问题: **能不能用个股表现/指标/规律，在入场前识别"这单会死（MFE<0.3%）"？**
方法 (DEB-20261106 §30):
  A. 特征普查: 4 族 24 个**事前可得**特征 —— DA 输入 / 个股历史 / 个股当日价格行为 / 大盘同刻,
     逐个算 AUC + 置换检验 p (多重比较用 Bonferroni 门 0.05/家族数);
  B. entry_confirm 反事实: 用真实 1min bar, 在入场后 T∈{5,10,15,30} 分钟复检三类确认
     (hold=仍站在入场价同侧 / vwap=站在 VWAP 同侧 / no_new_low=未破入场价 0.3%),
     不确认则跳过该笔; 确认则以 T 时刻价入场 (同出场价), 逐笔算 Δ;
  C. 重复开仓规则: 同标的上一笔净额<=0 时不开仓.

判读纪律 (预注册, 写在脚本里防事后挪门):
  - AUC 检出下限 ≈ 0.5 + 1.96×sqrt((n+1)/(12·n_dead·n_alive)); 低于此值的差异一律视为噪声;
  - 有经济意义的门需要 AUC ≈ 0.65 以上 (要把 21% 墓地率降到 18% 且误杀可控);
  - 反事实臂: 日聚类 |t|>=2 ∧ 单笔最大贡献 <50% ∧ 双时代同号 才算成立;
  - 禁止用 exit_reason / 结果变量圈人 (选样谬误).

性质: 只读 DB + data/replay_bars; 无写入, 无 cron。
用法: venv/bin/python scripts/dead_cohort_feature_scan.py [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
DEAD_MFE = 0.3
ERA_SPLIT = "2026-09-01"
RNG = random.Random(20261107)


# ── 基础工具 ───────────────────────────────────────────────────────────────
def _json_dict(raw: Any) -> Dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def auc(xs: Sequence[Optional[float]], ys: Sequence[int]) -> Optional[float]:
    pos = [x for x, y in zip(xs, ys) if y == 1 and x is not None]
    neg = [x for x, y in zip(xs, ys) if y == 0 and x is not None]
    if len(pos) < 10 or len(neg) < 10:
        return None
    s = n = 0
    for a in pos:
        for b in neg:
            s += 1 if a > b else (0.5 if a == b else 0)
            n += 1
    return s / n


def perm_p(xs: Sequence[Optional[float]], ys: Sequence[int], obs: float, iters: int = 300) -> float:
    vx = [x for x in xs if x is not None]
    vy = [y for x, y in zip(xs, ys) if x is not None]
    cnt = 0
    for _ in range(iters):
        RNG.shuffle(vy)
        a = auc(vx, vy)
        if a is not None and abs(a - 0.5) >= abs(obs - 0.5):
            cnt += 1
    return cnt / iters


def auc_floor(n_dead: int, n_alive: int, z: float = 1.96) -> Optional[float]:
    """AUC 的 95% 检出下限 (Hanley-McNeil 近似)。"""
    if n_dead < 2 or n_alive < 2:
        return None
    se = math.sqrt((n_dead + n_alive + 1) / (12.0 * n_dead * n_alive))
    return 0.5 + z * se


def load_bars(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        p = d / f"US_{sym}_{day}.json"
        if p.exists():
            return json.loads(p.read_text())
        p = d / f"{sym}_{day}.json"
        if p.exists():
            return json.loads(p.read_text())
    return None


def minute_of_day(ts: str) -> int:
    return int(ts[11:13]) * 60 + int(ts[14:16]) - 570   # 距 09:30 ET 的分钟


# ── 数据装配 ───────────────────────────────────────────────────────────────
def load_trades() -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT t.id, t.symbol, t.trade_date, t.entry_time, t.direction, t.entry_price AS px,
                  t.quantity AS q, t.mfe_pct, t.pnl_net, t.realized_pnl, t.factor_detail,
                  t.metadata_json, t.signal_strength, t.signal_confidence, t.market_regime,
                  i.anchor_dev_pct
           FROM trade_records t
           LEFT JOIN (SELECT decision_id, MAX(anchor_dev_pct) AS anchor_dev_pct
                        FROM playbook_instructions WHERE row_kind='instruction'
                       GROUP BY decision_id) i ON i.decision_id = t.decision_id
           WHERE t.trade_date >= '2026-07-01' AND t.realized_pnl IS NOT NULL
             AND t.decision_id LIKE 'DA-%'""")]
    conn.close()
    # 去重 (JOIN 扇出防护)
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        r["dead"] = 1 if (r["mfe_pct"] or 0) < DEAD_MFE else 0
        ts = _json_dict(r["factor_detail"]).get("ts_ctx") or {}
        r["rsi"] = ts.get("rsi")
        r["atr"] = ts.get("atr_percent")
        r["vr"] = ts.get("volume_ratio")
        r["vwap_dev"] = ((ts.get("price") / ts["vwap"] - 1) * 100) if ts.get("vwap") else None
        eg = _json_dict(r["metadata_json"]).get("entry_gap") or {}
        r["gap"] = eg.get("aligned_gap_pct") if isinstance(eg, dict) else None
        try:
            hh, mm = int(r["entry_time"][11:13]), int(r["entry_time"][14:16])
            r["mao"] = hh * 60 + mm - 570
        except Exception:
            r["mao"] = None
        out.append(r)
    return out


def attach_symbol_priors(rows: List[dict], min_prior: int = 5) -> None:
    """个股历史 (严格早于当日成交), 防前视。"""
    hist: Dict[str, List[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda x: (x["trade_date"], x["entry_time"] or "")):
        h = hist[r["symbol"]]
        r["sym_n_prior"] = len(h)
        if len(h) >= min_prior:
            r["sym_dead_prior"] = sum(x["dead"] for x in h) / len(h)
            r["sym_net_prior"] = statistics.mean(x["pnl_net"] or 0 for x in h)
        else:
            r["sym_dead_prior"] = None
            r["sym_net_prior"] = None
        r["prev_net"] = (h[-1]["pnl_net"] or 0) if h else None
        h.append(r)


def attach_intraday(rows: List[dict]) -> List[dict]:
    """个股当日价格行为 (入场前可得) + 大盘同刻。"""
    qqq_cache: Dict[str, Optional[List[list]]] = {}
    out = []
    for r in rows:
        bars = load_bars(r["symbol"], r["trade_date"])
        if not bars or len(bars) < 40:
            continue
        st = [b for b in bars if minute_of_day(b[0]) < (r["mao"] if r["mao"] is not None else -1)]
        if len(st) < 6:
            continue
        seg = st[: min(len(st), 15)]
        hi, lo = max(b[2] for b in seg), min(b[3] for b in seg)
        vwap = sum((b[2] + b[3] + b[4]) / 3 for b in seg) / len(seg)
        moves = [abs(seg[i][4] - seg[i - 1][4]) for i in range(1, len(seg))]
        day_open = bars[0][1]
        prev_close = (_json_dict(r["metadata_json"]).get("entry_gap") or {}).get("prev_close")
        d = r["trade_date"]
        if d not in qqq_cache:
            qqq_cache[d] = load_bars("US.QQQ", d)
        qb = qqq_cache[d]
        r["open_to_entry_bps"] = (r["px"] / day_open - 1) * 1e4
        r["entry_in_range"] = ((r["px"] - lo) / (hi - lo)) if hi > lo else None
        r["entry_vs_vwap_bps"] = (r["px"] / vwap - 1) * 1e4
        r["early_range_pct"] = (hi - lo) / day_open * 100
        r["early_vol_pct"] = (statistics.pstdev(moves) / day_open * 1e4) if len(moves) > 1 else None
        r["first5_ret_bps"] = (bars[4][4] / bars[0][1] - 1) * 1e4 if len(bars) > 5 else None
        r["day_open_gap_bps"] = (day_open / prev_close - 1) * 1e4 if prev_close else None
        if qb and len(qb) > 5:
            qs = [x for x in qb if minute_of_day(x[0]) < (r["mao"] or -1)]
            r["qqq_since_open"] = (qs[-1][4] / qb[0][1] - 1) * 1e4 if len(qs) >= 3 else None
        else:
            r["qqq_since_open"] = None
        out.append(r)
    return out


# ── A. 特征普查 ────────────────────────────────────────────────────────────
FEATURES = {
    "DA输入": ["gap", "mao", "rsi", "atr", "vr", "vwap_dev", "signal_strength",
               "signal_confidence", "anchor_dev_pct"],
    "个股历史": ["sym_dead_prior", "sym_net_prior", "sym_n_prior"],
    "个股当日行为": ["open_to_entry_bps", "entry_in_range", "entry_vs_vwap_bps",
                     "early_range_pct", "early_vol_pct", "first5_ret_bps", "day_open_gap_bps"],
    "大盘同刻": ["qqq_since_open"],
}


def scan_features(rows: List[dict]) -> Dict[str, List[dict]]:
    ys = [r["dead"] for r in rows]
    n_dead = sum(ys)
    floor = auc_floor(n_dead, len(ys) - n_dead)
    res: Dict[str, List[dict]] = {}
    for family, feats in FEATURES.items():
        res[family] = []
        for f in feats:
            xs = [r.get(f) for r in rows]
            a = auc(xs, ys)
            if a is None:
                res[family].append({"feature": f, "auc": None, "n": sum(1 for x in xs if x is not None)})
                continue
            e1 = [(r.get(f), r["dead"]) for r in rows if r["trade_date"] < ERA_SPLIT and r.get(f) is not None]
            e2 = [(r.get(f), r["dead"]) for r in rows if r["trade_date"] >= ERA_SPLIT and r.get(f) is not None]
            a1 = auc([p[0] for p in e1], [p[1] for p in e1])
            a2 = auc([p[0] for p in e2], [p[1] for p in e2])
            res[family].append({
                "feature": f, "auc": round(a, 3), "n": sum(1 for x in xs if x is not None),
                "perm_p": round(perm_p(xs, ys, a), 3),
                "era1_auc": round(a1, 3) if a1 else None, "era2_auc": round(a2, 3) if a2 else None,
                "above_floor": bool(floor and abs(a - 0.5) > (floor - 0.5)),
            })
    res["_meta"] = [{"n": len(rows), "n_dead": n_dead, "auc_floor_95": round(floor, 3) if floor else None}]
    return res


# ── B. entry_confirm 反事实 ────────────────────────────────────────────────
def confirm_replay(rows: List[dict]) -> List[dict]:
    combos = [(t, c) for t in (5, 10, 15, 30) for c in ("hold", "vwap", "no_new_low")]
    acc: Dict[Tuple[int, str], List[dict]] = {k: [] for k in combos}
    for r in rows:
        bars = load_bars(r["symbol"], r["trade_date"])
        if not bars:
            continue
        start = sum(1 for b in bars if minute_of_day(b[0]) < (r["mao"] if r["mao"] is not None else -1))
        if start >= len(bars):
            continue
        sign = 1 if r["direction"] == "LONG" else -1
        q = r["q"] or 0
        for t, cond in combos:
            j = start + t - 1
            if j >= len(bars):
                continue
            seg = bars[start:j + 1]
            p_t = seg[-1][4]
            vwap = sum((b[2] + b[3] + b[4]) / 3 for b in seg) / len(seg)
            if cond == "hold":
                ok = p_t >= r["px"] if sign > 0 else p_t <= r["px"]
            elif cond == "vwap":
                ok = p_t >= vwap if sign > 0 else p_t <= vwap
            else:
                ok = (min(b[3] for b in seg) > r["px"] * 0.997) if sign > 0 \
                    else (max(b[2] for b in seg) < r["px"] * 1.003)
            delta = sign * (r["px"] - p_t) * q if ok else -(r["pnl_net"] or 0)
            acc[(t, cond)].append({"day": r["trade_date"], "ok": ok, "delta": delta,
                                   "net": r["pnl_net"] or 0, "dead": r["dead"]})
    out = []
    for (t, cond), v in acc.items():
        if not v:
            continue
        by_day: Dict[str, float] = defaultdict(float)
        for x in v:
            by_day[x["day"]] += x["delta"]
        days = list(by_day.values())
        mean, sd = statistics.mean(days), statistics.pstdev(days)
        n = len(days)
        sv = sum(x["delta"] for x in v)
        out.append({
            "T": t, "cond": cond, "n": len(v),
            "pass": sum(1 for x in v if x["ok"]), "skip": sum(1 for x in v if not x["ok"]),
            "dead_avoided": sum(1 for x in v if (not x["ok"]) and x["dead"]),
            "winners_lost": sum(1 for x in v if (not x["ok"]) and (not x["dead"]) and x["net"] > 0),
            "sum_delta": round(sv, 2),
            "t_day": round(mean / (sd / math.sqrt(n)), 2) if sd else 0.0,
            "mde_day": round(2.802 * sd / math.sqrt(n), 1) if sd else None,
            "median_delta": round(statistics.median(x["delta"] for x in v), 2),
            "max_day_share": round(100 * max(days, key=abs) / sv, 0) if sv else None,
            "era_consistent": (sum(x["delta"] for x in v if x["day"] < ERA_SPLIT) > 0) ==
                              (sum(x["delta"] for x in v if x["day"] >= ERA_SPLIT) > 0),
        })
    return sorted(out, key=lambda x: -x["sum_delta"])


# ── C. 重复开仓规则 ────────────────────────────────────────────────────────
def reentry_rule(rows: List[dict]) -> dict:
    skip = [r for r in rows if r.get("prev_net") is not None and r["prev_net"] <= 0]
    keep = [r for r in rows if r.get("prev_net") is not None and r["prev_net"] > 0]
    if not skip:
        return {}
    by_day: Dict[str, float] = defaultdict(float)
    for r in skip:
        by_day[r["trade_date"]] += -(r["pnl_net"] or 0)
    days = list(by_day.values())
    mean, sd = statistics.mean(days), statistics.pstdev(days)
    total = sum(r["pnl_net"] or 0 for r in skip)
    return {
        "n_skip": len(skip), "n_keep": len(keep),
        "skip_net": round(total, 2), "keep_net": round(sum(r["pnl_net"] or 0 for r in keep), 2),
        "skip_dead_rate": round(sum(r["dead"] for r in skip) / len(skip), 3),
        "keep_dead_rate": round(sum(r["dead"] for r in keep) / len(keep), 3),
        "sum_delta": round(-total, 2),
        "t_day": round(mean / (sd / math.sqrt(len(days))), 2) if sd else 0.0,
        "max_trade_share": round(100 * abs(min(r["pnl_net"] or 0 for r in skip)) / abs(total), 0) if total else None,
        "era_sign_consistent": (sum(r["pnl_net"] or 0 for r in skip if r["trade_date"] < ERA_SPLIT) < 0) ==
                               (sum(r["pnl_net"] or 0 for r in skip if r["trade_date"] >= ERA_SPLIT) < 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = load_trades()
    attach_symbol_priors(rows)
    intraday = attach_intraday(rows)

    feats = scan_features(rows)
    conf = confirm_replay(intraday)
    reentry = reentry_rule(rows)
    payload = {"features": feats, "confirm": conf, "reentry": reentry}

    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    meta = feats["_meta"][0]
    print("== A. 特征普查 (n=%d, 墓地=%d, AUC 检出下限 %.3f) ==" % (
        meta["n"], meta["n_dead"], meta["auc_floor_95"]))
    for fam, items in feats.items():
        if fam == "_meta":
            continue
        print(" [%s]" % fam)
        for x in items:
            if x.get("auc") is None:
                print("   %-20s 样本不足 n=%d" % (x["feature"], x["n"]))
                continue
            print("   %-20s AUC %.3f  n=%-3d  置换p %.2f  7-8月 %.3f / 9-11月 %.3f %s" % (
                x["feature"], x["auc"], x["n"], x["perm_p"],
                x["era1_auc"] or 0, x["era2_auc"] or 0, "✓超下限" if x["above_floor"] else ""))
    print("\n== B. entry_confirm 反事实 (1min bar) ==")
    print("  %-3s %-11s %5s %5s %7s %7s %10s %7s %8s %7s" % (
        "T", "确认条件", "通过", "跳过", "避死单", "误杀赢", "ΣΔ", "日t", "MDE", "单日占比"))
    for c in conf:
        print("  %-3d %-11s %5d %5d %7d %7d %10.2f %7.2f %8s %6s%%" % (
            c["T"], c["cond"], c["pass"], c["skip"], c["dead_avoided"], c["winners_lost"],
            c["sum_delta"], c["t_day"], c["mde_day"], c["max_day_share"]))
    print("\n== C. 重复开仓规则 (同标的上一笔净额<=0 不开仓) ==")
    print("  跳过 %d 笔 (墓地率 %.0f%%) 净 %+.2f | 保留 %d 笔 (墓地率 %.0f%%) 净 %+.2f" % (
        reentry["n_skip"], reentry["skip_dead_rate"] * 100, reentry["skip_net"],
        reentry["n_keep"], reentry["keep_dead_rate"] * 100, reentry["keep_net"]))
    print("  Δ %+.2f | 日聚类 t %+.2f | 单笔最大占比 %s%% | 双时代同号 %s" % (
        reentry["sum_delta"], reentry["t_day"], reentry["max_trade_share"], reentry["era_sign_consistent"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())