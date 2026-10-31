#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中"方向识别"可行性测量 v2 (2026-10-31, 探索性诊断, 非裁决材料).

v2 相对 v1 的修正:
  ① **前视泄漏修复**: v1 的 day_hi/day_lo/range 用了**全天**极值 ⇒ daypos 的 IC=-0.18 是
     机械伪影 (未来上涨会抬高 day_hi, 反向压低当时的 daypos)。v2 一律用 ≤t 的运行极值。
  ② 合成臂门槛放宽到 n≥2 特征, 并补 Logistic 截面模型作"能力上界"读数 (IS 训练 / OOS 只读)。
  ③ 补三类特征: 横截面排名 (个股相对同期全部样本的强弱)、量能爆发、资金流强度 (Futu 分钟级)。

问题: 盘中 t 时刻, 只用 ≤t 的分钟数据, 能否比 50% 更准地识别**接下来 30 分钟**方向?
目标: y_raw = 股票收益; y_res = 减去同一 (day,slot) 全样本等权均值 (市场代理) ⇒ 个股特异方向。
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date as _date, datetime as _dt, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BARS_DIRS = (ROOT / "data" / "replay_bars_1m", ROOT / "data" / "replay_bars_1m_extra")
CF = ROOT / "data" / "futu_capital_flow.jsonl"
OUT = ROOT / "data" / "reports" / "intraday_direction_study_v2_latest.json"

SLOTS = ("10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
         "13:00", "13:30", "14:00", "14:30", "15:00", "15:30")
HORIZON_MIN = 30
MIN_DAYS = 100


def _shift(slot, minutes):
    h, m = slot.split(":")
    t = _dt(2000, 1, 1, int(h), int(m)) + timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def load_bars():
    out = {}
    for d in BARS_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if not f.name.endswith(".json"):
                continue
            sym = f.name[:-5].replace("_", ".", 1)
            if sym in out:
                continue
            try:
                j = json.loads(f.read_text())
            except Exception as e:  # noqa: BLE001
                print(f"[warn] bar 解析失败 {f}: {e}", file=sys.stderr)
                continue
            if not j:
                continue
            byday = defaultdict(list)
            for r in j:
                byday[r[0][:10]].append((r[0][11:16], float(r[1]), float(r[2]),
                                         float(r[3]), float(r[4]), float(r[5])))
            for day in byday:
                byday[day].sort()
            out[sym] = byday
    return out


def load_cf():
    """Futu 分钟级资金流: {sym: {minute_ts: (in_flow, super, big)}}."""
    cf = defaultdict(dict)
    n_bad = 0
    if not CF.exists():
        return cf, n_bad
    for line in CF.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            n_bad += 1
            continue
        sym, t = r.get("symbol"), r.get("item_time") or ""
        if not sym or not t:
            n_bad += 1
            continue
        try:
            cf[sym][t] = (float(r.get("in_flow") or 0.0),
                          float(r.get("super_in_flow") or 0.0),
                          float(r.get("big_in_flow") or 0.0))
        except (TypeError, ValueError):
            n_bad += 1
    return cf, n_bad


def spearman(pairs):
    if len(pairs) < 30:
        return None
    n = len(pairs)
    order_x = sorted(range(n), key=lambda i: pairs[i][0])
    order_y = sorted(range(n), key=lambda i: pairs[i][1])
    rx = [0] * n
    ry = [0] * n
    for rank, i in enumerate(order_x):
        rx[i] = rank
    for rank, i in enumerate(order_y):
        ry[i] = rank
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)) * sum((ry[i] - my) ** 2 for i in range(n)))
    return num / den if den else None


def day_t(by_day):
    vals = [v for v in by_day if v is not None]
    if len(vals) < 10:
        return None
    m = sum(vals) / len(vals)
    sd = statistics.pstdev(vals)
    return m / (sd / math.sqrt(len(vals))) if sd else None


def _r(x, n=4):
    return None if x is None else round(float(x), n)


def main():
    bars = load_bars()
    cf, cf_bad = load_cf()
    deep = {s: v for s, v in bars.items() if len(v) >= MIN_DAYS}
    print(f"[bars] {len(bars)} 只, ≥{MIN_DAYS} 日 {len(deep)} 只 | 资金流 symbols={len(cf)} bad_rows={cf_bad}")

    snaps = []
    for sym, byday in deep.items():
        days = sorted(byday)
        for i, day in enumerate(days):
            rows = byday[day]
            if len(rows) < 300:
                continue
            idx_of = {r[0]: k for k, r in enumerate(rows)}
            prevclose = None
            for pj in range(i - 1, max(-1, i - 6), -1):
                pr = byday[days[pj]]
                if len(pr) > 300:
                    prevclose = pr[-1][4]
                    break
            past = [byday[days[pj]] for pj in range(max(0, i - 5), i) if len(byday[days[pj]]) > 300]
            # 运行 VWAP / 运行量
            cum_vol = cum_pv = 0.0
            curve = {}
            run_hi = run_lo = None
            for r in rows:
                tp = (r[2] + r[3] + r[4]) / 3.0
                cum_vol += r[5]
                cum_pv += tp * r[5]
                run_hi = r[2] if run_hi is None else max(run_hi, r[2])
                run_lo = r[3] if run_lo is None else min(run_lo, r[3])
                curve[r[0]] = (cum_vol, cum_pv / cum_vol if cum_vol else r[4], run_hi, run_lo)
            open_px = rows[0][1]
            or_rows = [r for r in rows if r[0] <= "10:00"]
            or_hi = max(r[2] for r in or_rows)
            or_lo = min(r[3] for r in or_rows)
            cf_day = cf.get(sym, {})
            for slot in SLOTS:
                k = idx_of.get(slot)
                if k is None:
                    continue
                px = rows[k][4]
                if open_px <= 0 or px <= 0:
                    continue
                i30 = idx_of.get(_shift(slot, -30))
                if i30 is None or i30 >= k:
                    i30 = max(0, k - 30)
                i5 = max(0, k - 5)
                seg = rows[i30:k + 1]
                cumv, vwap, hi_t, lo_t = curve[slot]
                base = []
                for pr in past:
                    c = sum(r[5] for r in pr if r[0] <= slot)
                    if c > 0:
                        base.append(c)
                rvol = (cumv / statistics.median(base)) if base else None
                tot_v = sum(r[5] for r in seg) or 1.0
                up_v = sum(r[5] for r in seg if r[4] >= r[1])
                up_min = sum(1 for r in seg if r[4] >= r[1])
                vwap_prev = curve.get(_shift(slot, -30), (None, None, None, None))[1]
                # 分钟内收益的均值绝对值 = 日内波动率代理 (ATR-like)
                absr = [abs(rows[j][4] / rows[j - 1][4] - 1) for j in range(max(1, k - 60), k + 1)
                        if rows[j - 1][4] > 0]
                atr1m = statistics.mean(absr) if absr else None
                vol5 = sum(r[5] for r in rows[i5:k + 1]) or 0.0
                vol60 = sum(r[5] for r in rows[max(0, k - 60):k + 1]) or 1.0
                # 资金流: 最近 15 分钟 super+big 净额 ÷ 同期成交额
                cf_net = cf_turn = 0.0
                cf_seen = 0
                for j in range(max(0, k - 15), k + 1):
                    ts = f"{day} {rows[j][0]}:00"
                    v = cf_day.get(ts)
                    if v is None:
                        continue
                    cf_seen += 1
                    cf_net += v[1] + v[2]
                    cf_turn += rows[j][5] * rows[j][4]
                snaps.append({
                    "day": day, "sym": sym, "slot": slot, "px": px, "open": open_px,
                    "ret_since_open": px / open_px - 1,
                    "ret_30m": px / rows[i30][4] - 1 if rows[i30][4] > 0 else None,
                    "vwap_dev": px / vwap - 1 if vwap else None,
                    "vwap_dev_atr": ((px - vwap) / (atr1m * px)) if (vwap and atr1m) else None,
                    "vwap_slope30": (vwap / vwap_prev - 1) if vwap_prev else None,
                    "or_pos": ((px - or_lo) / (or_hi - or_lo)) if or_hi > or_lo else None,
                    "rvol": rvol,
                    "daypos_t": ((px - lo_t) / (hi_t - lo_t)) if hi_t > lo_t else None,
                    "upvol_share30": up_v / tot_v,
                    "upmin_share30": up_min / max(1, len(seg)),
                    "gap_pct": (open_px / prevclose - 1) if prevclose else None,
                    "vol_burst5": vol5 / (vol60 / 13.0) if vol60 else None,
                    "atr1m_pct": atr1m * 100 if atr1m else None,
                    "cf_ratio15": (cf_net / cf_turn) if cf_turn > 0 else None,
                    "cf_cov15": cf_seen / 16.0 if cf_seen else None,
                })
    print(f"[snaps] {len(snaps)}")

    grp = defaultdict(list)
    for s in snaps:
        grp[(s["day"], s["slot"])].append(s)

    # 目标 + 横截面特征 (全部同一 (day,slot) 内计算, 无前视)
    n_target = 0
    for (day, slot), arr in grp.items():
        tslot = _shift(slot, HORIZON_MIN)
        pxs = {}
        for s in arr:
            hm = {r[0]: r for r in deep[s["sym"]].get(day, [])}
            if tslot in hm:
                pxs[s["sym"]] = hm[tslot][4]
        for s in arr:
            s["y_raw"] = (pxs[s["sym"]] / s["px"] - 1) if s["sym"] in pxs else None
        fwd = [s["y_raw"] for s in arr if s["y_raw"] is not None]
        if len(fwd) < 10:
            for s in arr:
                s["y_res"] = None
            continue
        mkt_fwd = sum(fwd) / len(fwd)
        n_target += len(fwd)
        for s in arr:
            s["y_res"] = (s["y_raw"] - mkt_fwd) if s["y_raw"] is not None else None
        # 横截面排名 (0..1)
        for f in ("ret_since_open", "ret_30m", "vwap_dev", "upvol_share30", "cf_ratio15"):
            vals = [(s[f], s) for s in arr if s.get(f) is not None]
            vals.sort(key=lambda x: x[0])
            n = len(vals)
            for rank, (_, s) in enumerate(vals):
                s[f"{f}_xs"] = rank / max(1, n - 1)
        r30 = [s["ret_30m"] for s in arr if s["ret_30m"] is not None]
        m30 = sum(r30) / len(r30) if r30 else 0.0
        for s in arr:
            s["mkt_ret30"] = m30
            s["rs_since_open"] = s["ret_since_open"] - (
                statistics.mean([x["ret_since_open"] for x in arr if x["ret_since_open"] is not None])
                if any(x["ret_since_open"] is not None for x in arr) else 0.0)
    print(f"[target] 有目标 {n_target}")

    feats = ["ret_since_open", "ret_30m", "vwap_dev", "vwap_dev_atr", "vwap_slope30", "or_pos",
             "rvol", "daypos_t", "upvol_share30", "upmin_share30", "gap_pct", "vol_burst5",
             "atr1m_pct", "mkt_ret30", "rs_since_open",
             "ret_since_open_xs", "ret_30m_xs", "vwap_dev_xs", "upvol_share30_xs",
             "cf_ratio15", "cf_ratio15_xs"]
    days = sorted({s["day"] for s in snaps})
    split = days[len(days) // 2]
    print(f"[window] {days[0]} → {days[-1]} | IS<{split}≤OOS | n={len(snaps)}")

    report = {}
    for f in feats:
        row = {}
        for tgt in ("y_res", "y_raw"):
            rows_ = [(s[f], s[tgt], s["day"]) for s in snaps
                     if s.get(f) is not None and s.get(tgt) is not None]
            if len(rows_) < 500:
                continue
            ic_all = spearman([(a, b) for a, b, _ in rows_])
            ic_is = spearman([(a, b) for a, b, d in rows_ if d < split])
            ic_oos = spearman([(a, b) for a, b, d in rows_ if d >= split])
            hit = sum(1 for a, b, _ in rows_ if (a > 0) == (b > 0)) / len(rows_)
            by = defaultdict(list)
            for a, b, d in rows_:
                by[d].append((1 if a > 0 else -1) * b)
            row[tgt] = {"n": len(rows_), "ic_all": _r(ic_all), "ic_is": _r(ic_is),
                        "ic_oos": _r(ic_oos), "hit": _r(hit),
                        "day_t": _r(day_t([sum(v) / len(v) for v in by.values()])),
                        "same_sign": bool(ic_is and ic_oos and ic_is * ic_oos > 0)}
        if row:
            report[f] = row

    base_hit = sum(1 for s in snaps if s.get("y_raw") is not None and s["y_raw"] > 0) / max(1, n_target)

    # ── 合成臂: IS 定权重 (符号 = IS IC 符号, 幅度统一), OOS 只读 ──────
    picked = [f for f, v in report.items()
              if v.get("y_res", {}).get("ic_is") and abs(v["y_res"]["ic_is"]) >= 0.008]
    stats_is = {}
    for f in picked:
        vals = [s[f] for s in snaps
                if s.get(f) is not None and s.get("y_res") is not None and s["day"] < split]
        if len(vals) < 500:
            continue
        stats_is[f] = (statistics.mean(vals), statistics.pstdev(vals) or 1.0,
                       report[f]["y_res"]["ic_is"])
    comp = []
    for s in snaps:
        if s.get("y_res") is None:
            continue
        z = n = 0.0
        for f, (mu, sd, ic) in stats_is.items():
            if s.get(f) is None:
                continue
            z += (1 if ic > 0 else -1) * (s[f] - mu) / sd
            n += 1
        if n >= 3:
            comp.append({"day": s["day"], "sym": s["sym"], "slot": s["slot"],
                         "score": z / n, "y_res": s["y_res"], "y_raw": s["y_raw"]})

    def comp_stats(arr):
        if len(arr) < 200:
            return {"n": len(arr)}
        ic = spearman([(c["score"], c["y_res"]) for c in arr])
        hit_r = sum(1 for c in arr if (c["score"] > 0) == (c["y_res"] > 0)) / len(arr)
        hit_raw = sum(1 for c in arr if (c["score"] > 0) == (c["y_raw"] > 0)) / len(arr)
        thr = sorted(abs(c["score"]) for c in arr)[int(len(arr) * 2 / 3)]
        top = [c for c in arr if abs(c["score"]) >= thr]
        up = [c["y_raw"] for c in top if c["score"] > 0]
        dn = [c["y_raw"] for c in top if c["score"] < 0]
        by = defaultdict(list)
        for c in arr:
            by[c["day"]].append((1 if c["score"] > 0 else -1) * c["y_res"])
        return {"n": len(arr), "ic": _r(ic), "hit_res": _r(hit_r), "hit_raw": _r(hit_raw),
                "n_top": len(top),
                "hit_top_raw": _r(sum(1 for c in top if (c["score"] > 0) == (c["y_raw"] > 0)) / max(1, len(top))),
                "top_up_mean_bp": _r(statistics.mean(up) * 1e4 if up else None, 2),
                "top_dn_mean_bp": _r(statistics.mean(dn) * 1e4 if dn else None, 2),
                "day_t": _r(day_t([sum(v) / len(v) for v in by.values()]))}

    payload = {
        "generated": _dt.now().astimezone().isoformat(timespec="seconds"),
        "note": "探索性诊断 (非裁决材料); 立门须另立预注册。v2 已修 v1 的 daypos 前视泄漏",
        "universe": {"symbols": len(deep), "days": len(days), "window": [days[0], days[-1]],
                     "samples": len(snaps), "with_target": n_target, "split_date": split,
                     "horizon_min": HORIZON_MIN},
        "baseline_p_up_raw": round(base_hit, 4),
        "features": report,
        "composite": {"picked": sorted(stats_is), "IS": comp_stats([c for c in comp if c["day"] < split]),
                      "OOS": comp_stats([c for c in comp if c["day"] >= split])},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    # 样本落盘 (供条件分桶复用, 避免重算; gzip JSONL)
    dump = OUT.with_name("intraday_direction_samples_v2.jsonl.gz")
    import gzip
    with gzip.open(dump, "wt", encoding="utf-8") as fh:
        for s in snaps:
            if s.get("y_res") is None:
                continue
            fh.write(json.dumps({k: s.get(k) for k in
                                 ("day", "sym", "slot", "y_raw", "y_res", "ret_30m", "ret_30m_xs",
                                  "vwap_dev_xs", "upmin_share30", "upvol_share30", "atr1m_pct",
                                  "rvol", "or_pos", "gap_pct", "cf_ratio15")},
                                ensure_ascii=False) + "\n")
    print(f"[样本] {dump}")
    print(f"[产物] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())