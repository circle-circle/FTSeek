#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DEB-20260928-averaging-down-separability · Phase 0 可分离性读出 (只读, 一次性).

裁决: 2026-09-28 用户批准 Phase 0。判据 (§六 + §8.2 分桶冻结补写, 读出前钉死):
  k=2.0% 单值冻结; 结局=虚拟腿净额>0 (口径=§二: 等量补仓, 滑点0.10%方向感知+费0.15%往返,
  退出时机不变假设); 分桶冻结=连续特征中位数二分 (禁三分位/自定义切点=阈值购物),
  离散特征固定语义边界; 判据=桶间腿胜率差≥20pp ∧ Newcombe-Wilson 95%CI 不跨0
  ∧ 较优桶 n≥15 ∧ 特征触发时刻在线可得。不设 Fisher 死门 (DEB③ 教训)。

已知偏差 (印在输出里): ①9月上行期顺风 ②"退出时机不变"假设失真 ③mae_pct 20s轮询采样+
  57/207 整笔缺失 ④F3 当日方向含前视 ⇒ 仅讨论, 判据用前一日方向 (触发时刻可得)。

用法: PYTHONPATH=. venv/bin/python scripts/averaging_down_separability.py
产物: data/averaging_down_phase0_<ET日期>.json (gitignored)
"""
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
TODAY = datetime.now(ET).date().isoformat()

K = 2.0                 # 冻结单值 (知情选择已自曝登记)
SLIP, FEE_RT = 0.001, 0.0015
Z = 1.96
SENTINELS: dict = defaultdict(list)
ELIGIBLE_FEATURES = {
    "F1_known_event",
    "F2_holding",
    "F2_time_to_mae_median_split",
    "F3_qqq_prevday_up",
    "F4_vix_median_split",
    "F5_price_tier",
    "F6_session",
    "F7_source_type",
}


def wilson(w: int, n: int):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = w / n
    d = 1 + Z * Z / n
    c = p + Z * Z / (2 * n)
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return p, max(0.0, (c - half) / d), min(1.0, (c + half) / d)


def newcombe_diff_ci(w1, n1, w2, n2):
    """较优桶1 − 较差桶2 的胜率差 95%CI (Newcombe 平方法)。"""
    p1, l1, u1 = wilson(w1, n1)
    p2, l2, u2 = wilson(w2, n2)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return d, lo, hi


def load_rows(con):
    rows = con.execute("""
        SELECT id, symbol, direction, entry_price, exit_price, quantity, pnl_net,
               entry_time, exit_time, entry_vix, source_type, signal_strength,
               json_extract(metadata_json,'$.mae_pct') mae,
               json_extract(metadata_json,'$.time_to_mae') time_to_mae
        FROM trade_records WHERE date(entry_time)>='2026-08-25' AND exit_time IS NOT NULL
        AND pnl_net IS NOT NULL AND exit_price > 0""").fetchall()
    coverage = {
        "n_total": len(rows),
        "mae_missing": sum(1 for r in rows if r["mae"] is None),
        "mae_zero": sum(1 for r in rows if r["mae"] == 0),
        "mae_positive": sum(1 for r in rows if r["mae"] is not None and r["mae"] > 0),
        "invalid_direction": 0,
    }
    out = []
    for r in rows:
        if r["mae"] is None or r["mae"] == 0 or r["mae"] < K:
            continue
        if r["direction"] not in ("LONG", "SHORT"):
            coverage["invalid_direction"] += 1
            continue
        sgn = 1.0 if r["direction"] == "LONG" else -1.0
        add_px = r["entry_price"] * (1 - sgn * K / 100) * (1 + sgn * SLIP)
        notional = add_px * r["quantity"]
        leg_net = (sgn * (r["exit_price"] / add_px - 1) - FEE_RT) * notional
        out.append({**dict(r), "leg_net": leg_net, "win": leg_net > 0})
    coverage["k_triggered"] = len(out)
    coverage["time_to_mae_valid"] = sum(
        1 for r in out if r["time_to_mae"] is not None)
    return out, coverage


def load_event_flags():
    """overnight_known_event_flags 的 (date, US symbol) → flag；覆盖不足会自然成死轴。"""
    fp = ROOT / "data" / "shadow" / "overnight_known_event_flags.jsonl"
    flags = {}
    if not fp.exists():
        return flags
    for line in fp.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            SENTINELS["bad_event_flag"].append(line[:120])
            continue
        symbol = row.get("symbol")
        if not symbol:
            continue
        if not str(symbol).startswith("US."):
            symbol = f"US.{symbol}"
        flags[(row.get("et_date"), symbol)] = row.get("flag")
    return flags


def qqq_dirs():
    """QQQ 日K: {date: (当日涨跌符号, 前一日涨跌符号)}; 缓存 data/replay_bars/qqq_daily_cache.json"""
    fp = ROOT / "data" / "replay_bars" / "qqq_daily_cache.json"
    bars = None
    if fp.exists():
        try:
            bars = json.loads(fp.read_text())
        except Exception:
            SENTINELS["bad_cache"].append(str(fp))
    if not bars or bars[-1][0] < "2026-09-25":
        try:
            from futu import AuType, KLType, RET_OK, OpenQuoteContext
            q = OpenQuoteContext(host="127.0.0.1", port=11111)
            try:
                ret, df, _ = q.request_history_kline(
                    "US.QQQ", start="2026-08-20", end=TODAY, ktype=KLType.K_DAY,
                    autype=AuType.NONE, max_count=60)
                if ret == RET_OK and df is not None and len(df):
                    bars = [[r["time_key"][:10], float(r["close"])] for _, r in df.iterrows()]
                    fp.write_text(json.dumps(bars))
            finally:
                q.close()
                time.sleep(1.2)
        except Exception as e:
            SENTINELS["qqq_fetch"].append(type(e).__name__)
    if not bars:
        return {}
    res = {}
    for i in range(1, len(bars)):
        prev = bars[i][1] > bars[i - 1][1]
        prevprev = bars[i - 1][1] > bars[i - 2][1] if i >= 2 else None
        res[bars[i][0]] = (prev, prevprev)
    return res


def feature_of(r, qdir, event_flags):
    """返回 {特征: 桶}；None=该特征对此笔不可评估。分桶全部冻结语义。"""
    f = {}
    et = datetime.fromisoformat(str(r["entry_time"])).astimezone(ET)
    f["direction_diag"] = r["direction"] if r["direction"] in ("LONG", "SHORT") else None
    p = r["entry_price"]
    f["F5_price_tier"] = "<$20" if p < 20 else ("$20-100" if p < 100 else ">=$100")
    hh = et.hour + et.minute / 60
    if 9.5 <= hh < 11:
        f["F6_session"] = "open_0930_1100"
    elif 11 <= hh < 14:
        f["F6_session"] = "mid_1100_1400"
    elif 14 <= hh <= 16:
        f["F6_session"] = "late_1400_1600"
    else:
        f["F6_session"] = None
    vix = r["entry_vix"]
    f["F4_vix_pos"] = float(vix) if (vix and vix > 0) else None  # 中位二分在聚合层做
    # F2 持仓龄: 用 time_to_mae 回推触发时刻，不得用退出日冒充触发日。
    if r["time_to_mae"] is None:
        f["F2_holding"] = None
        f["F2_time_to_mae_pos"] = None
    else:
        trigger_et = et + timedelta(minutes=float(r["time_to_mae"]))
        f["F2_holding"] = "overnight" if trigger_et.date() > et.date() else "intraday"
        f["F2_time_to_mae_pos"] = float(r["time_to_mae"])
    d = str(r["entry_time"])[:10]
    f["F1_known_event"] = event_flags.get((d, r["symbol"]))
    if qdir and d in qdir:
        f["F3_qqq_prevday_up"] = {True: "up", False: "down", None: None}[qdir[d][1]]
        f["F3_qqq_sameday_up_LOOKAHEAD"] = {True: "up", False: "down"}[qdir[d][0]]
    else:
        f["F3_qqq_prevday_up"] = None
        f["F3_qqq_sameday_up_LOOKAHEAD"] = None
    f["F7_source_type"] = r["source_type"] or None
    return f


def eval_feature(name, pairs):
    """pairs=[(bucket, win)]; 冻结判据: 差≥20pp ∧ Newcombe CI 不跨0 ∧ 较优桶n≥15。"""
    buck = defaultdict(lambda: [0, 0])
    for b, w in pairs:
        if b is None:
            continue
        buck[b][0] += int(w)
        buck[b][1] += 1
    if len(buck) < 2:
        return {"feature": name,
                "eligible": name in ELIGIBLE_FEATURES and "LOOKAHEAD" not in name,
                "verdict": "DEAD_AXIS", "buckets":
                {b: {"n": v[1], "winrate": round(v[0] / v[1], 3)} for b, v in buck.items()}}
    stats = {b: (v[0], v[1], v[0] / v[1]) for b, v in buck.items()}
    best = max(stats, key=lambda b: stats[b][2])
    worst = min(stats, key=lambda b: stats[b][2])
    w1, n1, p1 = stats[best]
    w2, n2, p2 = stats[worst]
    diff, lo, hi = newcombe_diff_ci(w1, n1, w2, n2)
    gates = {"diff>=20pp": diff >= 0.20, "ci_excludes_0": lo > 0 or hi < 0,
             "better_bucket_n>=15": n1 >= 15}
    return {"feature": name,
            "eligible": name in ELIGIBLE_FEATURES and "LOOKAHEAD" not in name,
            "best": best, "worst": worst,
            "buckets": {b: {"n": v[1], "wins": v[0], "winrate": round(v[2], 3)}
                        for b, v in stats.items()},
            "diff_pp": round(diff * 100, 1), "ci95": [round(lo, 3), round(hi, 3)],
            "gates": gates, "verdict": "PASS" if all(gates.values()) else "FAIL"}


def main():
    con = sqlite3.connect("file:" + str(ROOT / "data" / "ftseek_us.db") + "?mode=ro",
                          uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    rows, coverage = load_rows(con)
    qdir = qqq_dirs()
    event_flags = load_event_flags()
    feats = defaultdict(list)
    n_vix = 0
    vix_vals = []
    n_time_to_mae = 0
    time_to_mae_vals = []
    n_event_covered = 0
    for r in rows:
        f = feature_of(r, qdir, event_flags)
        for k, b in f.items():
            if k in ("F4_vix_pos", "F2_time_to_mae_pos"):
                continue
            feats[k].append((b, r["win"]))
        if f["F4_vix_pos"] is not None:
            vix_vals.append((f["F4_vix_pos"], r["win"]))
            n_vix += 1
        if f["F2_time_to_mae_pos"] is not None:
            time_to_mae_vals.append((f["F2_time_to_mae_pos"], r["win"]))
            n_time_to_mae += 1
        if f["F1_known_event"] is not None:
            n_event_covered += 1
    # F4 中位数二分 (冻结切法, 连续特征唯一允许)
    if vix_vals:
        med = sorted(v[0] for v in vix_vals)[len(vix_vals) // 2]
        feats["F4_vix_median_split"] = [("high" if v > med else "low", w)
                                        for v, w in vix_vals]
    if time_to_mae_vals:
        med = sorted(v[0] for v in time_to_mae_vals)[len(time_to_mae_vals) // 2]
        feats["F2_time_to_mae_median_split"] = [
            ("slow" if v > med else "fast", w) for v, w in time_to_mae_vals]
    results = [eval_feature(k, v) for k, v in sorted(feats.items())]
    passed = [r["feature"] for r in results
              if r.get("eligible") and r.get("verdict") == "PASS"]
    verdict = "SEPARABLE→另立Phase1影子DEB" if passed else "NOT_SEPARABLE→结题(两类不可分⇒不补)"
    out = {"script": "averaging_down_separability", "date": TODAY,
           "deb": "DEB-20260928-averaging-down-separability", "k_pct": K,
           "n_triggered": len(rows), "n_vix_valid": n_vix,
           "coverage": {**coverage, "n_vix_valid": n_vix,
                        "n_time_to_mae_valid": n_time_to_mae,
                        "n_event_flag_covered": n_event_covered},
           "criteria": "diff≥20pp ∧ Newcombe95%CI不跨0 ∧ 较优桶n≥15 ∧ 在线可得; "
                       "分桶冻结: 连续=中位二分, 离散=固定语义边界 (§8.2)",
           "results": results, "features_passed": passed, "verdict": verdict,
           "notes": ["F1事件日历: 影子9/24起, 触发集覆盖不足 ⇒ DEAD_AXIS, 留前向诊断",
                     "F3当日方向含前视 ⇒ 仅讨论; 判据用前一日方向",
                     "57/207整笔缺mae_pct不可判定 + 14笔mae=0未浮亏 ⇒ 触发集外",
                     "F7 signal_strength 档位边界未预注册 ⇒ 不读数后补切点, 本轮不进判据",
                     "direction/F3当日方向仅诊断轴, 不属于F1–F7判据清单",
                     "退出时机不变假设/9月顺风/20s采样低估: 见DEB §五"],
           "sentinels": dict(SENTINELS)}
    fp = ROOT / "data" / f"averaging_down_phase0_{TODAY}.json"
    fp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"[phase0] k={K}% 触发 n={len(rows)} (vix有效 {n_vix})")
    for r in results:
        if r["verdict"] == "DEAD_AXIS":
            print(f"  {r['feature']:<28} DEAD_AXIS {r['buckets']}")
        else:
            print(f"  {r['feature']:<28} {r['best']}({r['buckets'][r['best']]['winrate']:.0%},n={r['buckets'][r['best']]['n']}) vs "
                  f"{r['worst']}({r['buckets'][r['worst']]['winrate']:.0%},n={r['buckets'][r['worst']]['n']}) "
                  f"差{r['diff_pp']}pp CI{r['ci95']} → {r['verdict']}")
    print(f"[phase0] verdict = {verdict}")
    print(f"[saved] {fp}")


if __name__ == "__main__":
    main()
