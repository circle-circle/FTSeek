#!/usr/bin/env python3
"""RFC-141 附属探索: 2min 入场确认门「路径判据」变体离线回放 (V1-V4) — 纯只读, 零交易行为变更.

════════════════════════════════════════════════════════════════════════
预注册 (2026-10-12, 读数产生前冻结; 改动 = 重新预注册)
════════════════════════════════════════════════════════════════════════
背景: 用户提议 (2026-10-12 讨论) — 现役 2min 门在确认时刻只用单点价 vs 锚 (端点判定),
  能否用当日已过 1min K 线的路径信息 (趋势/均价) 做更抗噪的判定。本脚本在既有影子
  回放人群上离线测 4 个变体腿。现役 live 门 (RFC-141 forward, 10-12 起攒样) 不动;
  本回放产出只用于「是否值得预注册 v2 判据」的裁决材料 (T23-2: 补测腿不得直接替换冻结腿)。

人群 (冻结): data/shadow/entry_delay_confirm.jsonl 中 delay_min=2 ∧ status=ok 的既有行
  (n=580, 2026-07-13..10-09, 64 交易日)。不重新圈人群、不改窗口。
  锚 = trade_records.entry_price (真实成交价, 与原影子同约定)。

bar 标签语义 (2026-10-12 双源实证): Futu K_1M time_key = 分钟**结束**时刻
  (首根标签 09:31 的 open = 当日官方开盘价, 23/23 抽样命中; 全天 390 根 09:31..16:00)。
  ⇒ close(标签 T) = T 时刻价格; open(标签 T) = T−60s 时刻价格。
  ⇒ 原影子 `_price_at`(首根标签≥target 的 open) 实测点 = target−60s = entry_min+1:00,
    有效延迟 = 60−SS 秒 (SS=入场秒数), 均值 ~30s — **不是**名义 2min。此为「已结算腿
    语义漂移」独立发现, 本脚本双口径并列 (V0_settled 复刻 + V0_true2min 修正), 不回头改
    已结算数字 (T23-2), 也不得拿 V0_true 读数给现役 forward 判据补票 (T14: 回放≠前向)。

判定点定义 (冻结):
  t0 = entry HH:MM (分钟截断, 同原影子); tgt = t0+2min; tgt3 = t0+3min
  V0_settled : px = open(首根标签 ≥ tgt)          [= 价格@t0+1:00; 必须逐行复刻 jsonl]
  V0_true2min: px = close(标签 tgt)               [= 价格@t0+2:00; 有效延迟 120−SS ∈(60,120]]
  V0_true3   : px = close(标签 tgt3)              [仅敏感性披露, 不进判据]
  窗口 W = 标签 ∈ (t0, tgt] 的 RTH bar (典型 2 根: t0+1, t0+2; close 即各分钟末价格)
  V1_win_avg : mean(W closes) 对锚同侧 (LONG ≥ / SHORT ≤)      [端点→路径均值, 抗噪]
  V2_unanim  : W 内全部 close 对锚同侧 (n=1 时=单点)            [k-of-n 去抖, 全票制]
  V3_sess_avg: V0_true2min 同侧 AND px 对「会话均价」同侧        [叠加过滤]
               会话均价 = RTH 标签 09:31..tgt 的 typical price (h+l+c)/3 均值。
               ⚠️ 缓存 bar 无 volume 列 ⇒ 这是 TWAP 代理, 非真 VWAP (live vwap_gate 用
               Futu push 的量加权 VWAP) — 读数按「会话均价同侧」解读, 不得外推为 VWAP 结论。
  V4_slope10 : V0_true2min 同侧 AND 最近 ≤10 根 RTH close (标签≤tgt) OLS 斜率
               LONG ≥0 / SHORT ≤0; 可用 bar <5 根 → 回落 V0_true2min 并打 fallback 标。
  缺窗/缺 tgt bar 的行 → 第三桶 (no_confirm_bar) 单列计数, 不静默丢弃 (T14-④)。

价值模型 (与原影子 _agg_block 逐字同式):
  val(臂,笔) = 放弃: −pnl_net | 确认: −(opp_cost_pct/100 × notional)
  opp_cost 一律按 V0_true2min 端点价计 (变体只改判定, 不改成交价; 成交价 = 端点分钟收盘
  的代理 — live 实际在确认后下轮 auto_execute 市价 reprice, +~80s 滑点未建模, 披露)。
  V0_settled 腿维持自身 px 口径 (复刻用)。

判读门 (冻结, 全过才算「值得预注册 v2」):
  G1 保真: V0_settled 复刻 vs jsonl still_triggers 不一致 = 0 (否则停, 先查 bar 读法)
  G2 n 平价: 每臂可评估 n + 第三桶 = 基线 n (无子集偏差, T26-1)
  G3 显著: 配对 Δ=val_V−val_V0true 按日聚类 bootstrap 95% CI 不含 0
     (重抽单位=交易日, 10000 次, seed=20261012; 同日笔不独立)
  G4 IS/OOS: 按日期中位对半切, 两半 Δ 均值同号 (窗口不重叠, T24-7)
  G5 尾部: 剔 Δ 日合计最差 3 日后 Δ 符号不翻 (T18/T39)
  全不过 = 合格交付「无变体与真 2min 端点判定可分辨, 冻结腿维持, forward 照跑」(T25-3)。

分层读数 (冻结): ALL / session(open: entry<09:45 vs later) / direction / SL_INITIAL 族 /
  RFC-081 分支 (B_only vs D_candidate, 判据人群主读 B_only)。分层只呈报不单独裁决。

强制披露 (随读数输出):
  ① 1min 粒度 vs live 10s sweep; 分钟截断 ⇒ 有效延迟分布打印 (SS 从 trade_records 回读)
  ② V3 = TWAP 代理非 VWAP ③ 回放锚=成交价, live 锚=arm 时 reprice 执行锚 (≈成交−滑点)
  ④ T49 地平线: 入口过滤只接得住部分亏损族 (隔夜族 ~52%), 变体继承该上限
  ⑤ 本回放不构成对 RFC-141 forward 判据的任何修改; 两证据源不得混算 (T14)
运行: ./venv/bin/python scripts/entry_confirm_variant_replay.py   (纯缓存, 不触网, 不写生产影子)
输出: data/reports/entry_confirm_variants_20261012.json + stdout 表
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
SRC_JSONL = REPO / "data" / "shadow" / "entry_delay_confirm.jsonl"
OUT_JSON = REPO / "data" / "reports" / "entry_confirm_variants_20261012.json"

SEED = 20261012
N_BOOT = 10000
OPEN_CUTOFF = "09:45"
ARMS = ("V0_settled", "V0_true2min", "V0_true3", "V1_win_avg", "V2_unanim",
        "V3_sess_avg", "V4_slope10")
DECISION_ARMS = ("V1_win_avg", "V2_unanim", "V3_sess_avg", "V4_slope10")
BASELINE = "V0_true2min"


def _log(m):
    print(f"[variant-replay] {m}", file=sys.stderr)


def _hhmm_to_min(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _min_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _same_side(direction: str, anchor: float, px: float) -> bool:
    return px >= anchor if direction == "LONG" else px <= anchor


def _opp(direction: str, anchor: float, px: float) -> float:
    if direction == "LONG":
        return (px - anchor) / anchor * 100
    return (anchor - px) / anchor * 100


def _ols_slope(ys):
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def _day_clustered_t(daily_values):
    n = len(daily_values)
    if n < 3:
        return None
    dm = statistics.mean(daily_values)
    sd = statistics.stdev(daily_values)
    if sd == 0:
        return None
    return {"t": round(dm / (sd / math.sqrt(n)), 2), "n_days": n,
            "day_mean": round(dm, 2),
            "mde_80pct": round(2.802 * sd / math.sqrt(n), 2),
            "distinguishable": bool(abs(dm / (sd / math.sqrt(n))) >= 2.0)}


def _boot_ci_day_clustered(pairs, seed=SEED, n_boot=N_BOOT):
    """pairs: {date: [Δ per trade]} → 95% CI of pooled per-trade mean Δ (day-resampled)."""
    days = sorted(pairs)
    if len(days) < 3:
        return None
    import random
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [pairs[rng.choice(days)] for _ in days]  # resample days w/ replacement
        pooled = [v for day_vals in sample for v in day_vals]
        means.append(sum(pooled) / len(pooled))
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[int(0.975 * n_boot) - 1]
    pos = sum(1 for m in means if m > 0) / n_boot
    return {"ci95_lo": round(lo, 3), "ci95_hi": round(hi, 3),
            "p_delta_gt0": round(pos, 4), "excludes_zero": bool(lo > 0 or hi < 0)}


def _agg(rows, arm):
    """rows: list of per-trade dicts with keys per arm. Mirrors原影子 _agg_block 口径."""
    ok = [r for r in rows if r["status_" + arm] == "ok"]
    av = [r for r in ok if not r["still_" + arm]]
    dl = [r for r in ok if r["still_" + arm]]
    avoided_net = sum(float(r.get("pnl_net") or 0) for r in av)
    opp = [float(r["opp_" + arm]) for r in dl if r.get("opp_" + arm) is not None]
    by_day = {}
    for r in ok:
        v = r["val_" + arm]
        by_day.setdefault(r["date"], []).append(v)
    misk = [r for r in av if float(r.get("pnl_net") or 0) > 0]
    return {
        "n": len(ok),
        "n_third_bucket": len(rows) - len(ok),
        "n_avoided": len(av),
        "avoidance_rate": round(len(av) / len(ok), 4) if ok else None,
        "avoided_value_usd": round(-avoided_net, 2),
        "n_miskilled": len(misk),
        "miskill_rate_cut_pop": round(len(misk) / len(av), 4) if av else None,
        "n_confirmed": len(dl),
        "opp_cost_mean_pct": round(statistics.mean(opp), 4) if opp else None,
        "opp_cost_median_pct": round(statistics.median(opp), 4) if opp else None,
        "net_value_usd": round(sum(sum(v) for v in by_day.values()), 2) if ok else None,
        "day_clustered": _day_clustered_t(
            [sum(v) / len(v) for v in by_day.values()] if by_day else []),
    }


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # entry seconds + mfe for cohort trades (jsonl only has HH:MM)
    tmeta = {}
    for r in con.execute("SELECT id, entry_time, mfe_pct FROM trade_records"):
        tmeta[int(r["id"])] = (str(r["entry_time"]), r["mfe_pct"])
    con.close()

    cohort = []
    for line in SRC_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception as e:
            _log(f"脏行跳过: {e}")
            continue
        if r.get("type") == "entry_delay" and r.get("delay_min") == 2 and r.get("status") == "ok":
            cohort.append(r)
    _log(f"cohort n={len(cohort)}")

    bar_cache = {}

    def bars_for(sym, date):
        key = (sym, date)
        if key not in bar_cache:
            p = BAR_DIR / f"{sym.replace('.', '_')}_{date}.json"
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                assert all(len(b) == 5 for b in raw[:3]), "bar 列数≠5"
                bar_cache[key] = {b[0][11:16]: b for b in raw
                                  if "09:31" <= b[0][11:16] <= "16:00"}
            except Exception as e:
                _log(f"bar 加载失败 {sym} {date}: {e}")
                bar_cache[key] = {}
        return bar_cache[key]

    rows = []
    fidelity_mismatch = []
    fallback_counts = {"V3_no_sess_avg": 0, "V4_slope_fallback": 0}
    delay_eff_settled, delay_eff_true = [], []

    for r in cohort:
        sym, date = r["symbol"], r["date"]
        direction = r["direction"]
        ep = float(r["entry_price"])
        notional = float(r.get("notional") or 0)
        pnl_net = r.get("pnl_net")
        t0 = r["entry_min"]
        t0m = _hhmm_to_min(t0)
        tgt = _min_to_hhmm(t0m + 2)
        tgt3 = _min_to_hhmm(t0m + 3)
        bars = bars_for(sym, date)
        ss = None
        if r["trade_id"] in tmeta:
            et = tmeta[r["trade_id"]][0]
            try:
                # entry_time: 'YYYY-MM-DDTHH:MM:SS...' 或 'YYYY-MM-DD HH:MM:SS' → [17:19]=秒
                ss = int(et[17:19]) if len(et) >= 19 else None
            except Exception:
                ss = None
        if ss is not None:
            delay_eff_settled.append(60 - ss)
            delay_eff_true.append(120 - ss)

        row = {"trade_id": r["trade_id"], "symbol": sym, "date": date,
               "direction": direction, "entry_min": t0, "anchor": ep,
               "notional": notional, "pnl_net": pnl_net,
               "exit_reason": r.get("exit_reason"),
               "source": r.get("source"),
               "routable_candidate": r.get("routable_candidate"),
               "jsonl_still": bool(r["still_triggers"]),
               "jsonl_px": r.get("px_at_target")}

        def set_arm(arm, status, still=None, opp_pct=None, val=None, note=None):
            row["status_" + arm] = status
            row["still_" + arm] = still
            row["opp_" + arm] = round(opp_pct, 4) if opp_pct is not None else None
            row["val_" + arm] = round(val, 4) if val is not None else None
            if note:
                row["note_" + arm] = note

        def val_of(still, opp_pct):
            if still:
                return -(opp_pct / 100.0 * notional)
            return -float(pnl_net or 0)

        # ── V0_settled 复刻: open(首根标签 ≥ tgt) ──
        px_s = None
        for lbl in sorted(bars):
            if lbl >= tgt:
                px_s = float(bars[lbl][1])
                break
        if px_s is None:
            set_arm("V0_settled", "no_confirm_bar")
        else:
            still = _same_side(direction, ep, px_s)
            opp = _opp(direction, ep, px_s)
            set_arm("V0_settled", "ok", still, opp, val_of(still, opp))
            if still != bool(r["still_triggers"]) or abs(px_s - float(r.get("px_at_target") or px_s)) > 1e-9:
                fidelity_mismatch.append(
                    {"trade_id": r["trade_id"], "sym": sym, "date": date,
                     "mine": (still, px_s), "jsonl": (r["still_triggers"], r.get("px_at_target"))})

        # ── V0_true2min: close(标签 tgt); V0_true3: close(标签 tgt3) ──
        px_t = float(bars[tgt][4]) if tgt in bars else None
        px_t3 = float(bars[tgt3][4]) if tgt3 in bars else None
        for arm, px in (("V0_true2min", px_t), ("V0_true3", px_t3)):
            if px is None:
                set_arm(arm, "no_confirm_bar")
            else:
                still = _same_side(direction, ep, px)
                opp = _opp(direction, ep, px)
                set_arm(arm, "ok", still, opp, val_of(still, opp))

        # ── 窗口 W: 标签 ∈ (t0, tgt] ──
        win = [float(bars[l][4]) for l in sorted(bars) if t0 < l <= tgt]
        if px_t is None or not win:
            for arm in ("V1_win_avg", "V2_unanim"):
                set_arm(arm, "no_confirm_bar")
        else:
            avg = statistics.mean(win)
            still = _same_side(direction, ep, avg)
            opp = _opp(direction, ep, px_t)  # 成交价=端点, 判定=路径
            set_arm("V1_win_avg", "ok", still, opp, val_of(still, opp))
            still2 = all(_same_side(direction, ep, c) for c in win)
            set_arm("V2_unanim", "ok", still2, opp, val_of(still2, opp),
                    note=f"win_n={len(win)}")

        # ── V3: V0_true ∧ 端点对会话均价同侧 (TWAP 代理, 无 volume) ──
        if px_t is None:
            set_arm("V3_sess_avg", "no_confirm_bar")
        else:
            sess = [(float(bars[l][2]) + float(bars[l][3]) + float(bars[l][4])) / 3
                    for l in sorted(bars) if "09:31" <= l <= tgt]
            if len(sess) < 5:
                fallback_counts["V3_no_sess_avg"] += 1
                still = _same_side(direction, ep, px_t)
                set_arm("V3_sess_avg", "ok", still, _opp(direction, ep, px_t),
                        val_of(still, _opp(direction, ep, px_t)), note="fallback_V0")
            else:
                twap = statistics.mean(sess)
                still = (_same_side(direction, ep, px_t)
                         and _same_side(direction, twap, px_t))
                opp = _opp(direction, ep, px_t)
                set_arm("V3_sess_avg", "ok", still, opp, val_of(still, opp))

        # ── V4: V0_true ∧ 最近≤10根 close 斜率同向 ──
        if px_t is None:
            set_arm("V4_slope10", "no_confirm_bar")
        else:
            hist = [float(bars[l][4]) for l in sorted(bars) if l <= tgt][-10:]
            if len(hist) < 5:
                fallback_counts["V4_slope_fallback"] += 1
                still = _same_side(direction, ep, px_t)
                set_arm("V4_slope10", "ok", still, _opp(direction, ep, px_t),
                        val_of(still, _opp(direction, ep, px_t)), note="fallback_V0")
            else:
                slope = _ols_slope(hist)
                ok_slope = slope >= 0 if direction == "LONG" else slope <= 0
                still = _same_side(direction, ep, px_t) and ok_slope
                opp = _opp(direction, ep, px_t)
                set_arm("V4_slope10", "ok", still, opp, val_of(still, opp))
        rows.append(row)

    # ══════════ 读数 ══════════
    out = {"pre_registration": "script docstring (frozen 2026-10-12, pre-run)",
           "cohort_n": len(rows),
           "G1_fidelity_mismatches": len(fidelity_mismatch),
           "fidelity_mismatch_sample": fidelity_mismatch[:5],
           "fallback_counts": fallback_counts,
           "effective_delay_sec": {
               "V0_settled(60-SS)": {"mean": round(statistics.mean(delay_eff_settled), 1),
                                     "min": min(delay_eff_settled), "max": max(delay_eff_settled)} if delay_eff_settled else None,
               "V0_true(120-SS)": {"mean": round(statistics.mean(delay_eff_true), 1),
                                   "min": min(delay_eff_true), "max": max(delay_eff_true)} if delay_eff_true else None},
           "arms": {}, "paired_delta_vs_" + BASELINE: {}, "strata": {}}

    for arm in ARMS:
        ok_n = sum(1 for r in rows if r["status_" + arm] == "ok")
        third = len(rows) - ok_n
        out["arms"][arm] = _agg(rows, arm)
        out["arms"][arm]["G2_n_check"] = f"{ok_n}+{third}={ok_n + third} vs cohort {len(rows)}"

    # 配对 Δ (逐笔 val_V − val_base, 双 ok 才配对)
    for arm in DECISION_ARMS + ("V0_settled", "V0_true3"):
        pairs = {}
        flips = {"conf_to_aband": [], "aband_to_conf": []}
        for r in rows:
            if r["status_" + arm] != "ok" or r["status_" + BASELINE] != "ok":
                continue
            d = r["val_" + arm] - r["val_" + BASELINE]
            pairs.setdefault(r["date"], []).append(d)
            if r["still_" + BASELINE] and not r["still_" + arm]:
                flips["conf_to_aband"].append(
                    (r["symbol"], r["date"], round(float(r.get("pnl_net") or 0), 2)))
            if not r["still_" + BASELINE] and r["still_" + arm]:
                flips["aband_to_conf"].append(
                    (r["symbol"], r["date"], round(float(r.get("pnl_net") or 0), 2)))
        pooled = [v for d in pairs.values() for v in d]
        mean_d = statistics.mean(pooled) if pooled else None
        # IS/OOS 对半 (按日期中位)
        dates = sorted(pairs)
        mid = dates[len(dates) // 2] if dates else ""
        is_d = [v for d, vs in pairs.items() if d < mid for v in vs]
        oos_d = [v for d, vs in pairs.items() if d >= mid for v in vs]
        # 尾部: 剔最差 3 日
        day_sums = sorted(((sum(vs), d) for d, vs in pairs.items()))
        worst3 = {d for _, d in day_sums[:3]}
        trimmed = [v for d, vs in pairs.items() if d not in worst3 for v in vs]
        out["paired_delta_vs_" + BASELINE][arm] = {
            "n_pairs": len(pooled),
            "mean_delta_usd_per_trade": round(mean_d, 3) if mean_d is not None else None,
            "sum_delta_usd": round(sum(pooled), 2) if pooled else None,
            "boot_ci95": _boot_ci_day_clustered(pairs) if pairs else None,
            "IS_mean": round(statistics.mean(is_d), 3) if is_d else None,
            "OOS_mean": round(statistics.mean(oos_d), 3) if oos_d else None,
            "split_date": mid,
            "trim3d_mean": round(statistics.mean(trimmed), 3) if trimmed else None,
            "G3_ci_excl0": (out["paired_delta_vs_" + BASELINE].get(arm, {}).get("boot_ci95") or {}).get("excludes_zero")
            if False else None,  # filled below
            "flip_conf_to_aband": {"n": len(flips["conf_to_aband"]),
                                   "pnl_sum_of_flipped": round(sum(x[2] for x in flips["conf_to_aband"]), 2),
                                   "winners_among": sum(1 for x in flips["conf_to_aband"] if x[2] > 0)},
            "flip_aband_to_conf": {"n": len(flips["aband_to_conf"]),
                                   "pnl_sum_of_flipped": round(sum(x[2] for x in flips["aband_to_conf"]), 2),
                                   "winners_among": sum(1 for x in flips["aband_to_conf"] if x[2] > 0)},
        }
        ci = out["paired_delta_vs_" + BASELINE][arm]["boot_ci95"]
        out["paired_delta_vs_" + BASELINE][arm]["G3_ci_excl0"] = bool(ci and ci["excludes_zero"])

    # 分层 (呈报不裁决)
    def sub(pred):
        return [r for r in rows if pred(r)]
    strata_defs = {
        "open_entry_lt_0945": sub(lambda r: r["entry_min"] < OPEN_CUTOFF),
        "later_entry": sub(lambda r: r["entry_min"] >= OPEN_CUTOFF),
        "LONG": sub(lambda r: r["direction"] == "LONG"),
        "SHORT": sub(lambda r: r["direction"] == "SHORT"),
        "SL_INITIAL": sub(lambda r: r["exit_reason"] == "SL_INITIAL"),
        "B_only": sub(lambda r: r.get("routable_candidate") is False),
        "D_candidate": sub(lambda r: r.get("routable_candidate") is True),
    }
    for name, srows in strata_defs.items():
        out["strata"][name] = {"n": len(srows),
                               **{arm: _agg(srows, arm) for arm in ARMS}}

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # ══════════ stdout 摘要表 ══════════
    print("=" * 100)
    print(f"G1 保真复刻: V0_settled vs jsonl 不一致 = {out['G1_fidelity_mismatches']} (门槛=0)")
    print(f"有效延迟分布: settled {out['effective_delay_sec']['V0_settled(60-SS)']} | true2min {out['effective_delay_sec']['V0_true(120-SS)']}")
    print(f"fallback: {fallback_counts}")
    print("=" * 100)
    hdr = f"{'arm':<14}{'n':>5}{'3rd':>4}{'avoid%':>8}{'miskill%':>9}{'oppMean%':>9}{'oppMed%':>8}{'netVal$':>10}{'dayT':>7}"
    print(hdr)
    for arm in ARMS:
        a = out["arms"][arm]
        dc = a["day_clustered"] or {}
        print(f"{arm:<14}{a['n']:>5}{a['n_third_bucket']:>4}"
              f"{(a['avoidance_rate'] or 0) * 100:>7.1f}%"
              f"{(a['miskill_rate_cut_pop'] or 0) * 100 if a['miskill_rate_cut_pop'] is not None else -1:>8.1f}%"
              f"{a['opp_cost_mean_pct'] if a['opp_cost_mean_pct'] is not None else float('nan'):>9.3f}"
              f"{a['opp_cost_median_pct'] if a['opp_cost_median_pct'] is not None else float('nan'):>8.3f}"
              f"{a['net_value_usd'] if a['net_value_usd'] is not None else float('nan'):>10.0f}"
              f"{dc.get('t', '')!s:>7}")
    print("=" * 100)
    print(f"配对 Δ vs {BASELINE} ($/笔, 正=变体更优):")
    for arm, d in out["paired_delta_vs_" + BASELINE].items():
        ci = d["boot_ci95"] or {}
        print(f"  {arm:<12} n={d['n_pairs']:<4} meanΔ={d['mean_delta_usd_per_trade']!s:>8} "
              f"Σ={d['sum_delta_usd']!s:>9} CI95=[{ci.get('ci95_lo')},{ci.get('ci95_hi')}] "
              f"excl0={d['G3_ci_excl0']} IS={d['IS_mean']} OOS={d['OOS_mean']} trim3={d['trim3d_mean']} "
              f"| conf→aband {d['flip_conf_to_aband']['n']}笔(赢{d['flip_conf_to_aband']['winners_among']}) "
              f"aband→conf {d['flip_aband_to_conf']['n']}笔(赢{d['flip_aband_to_conf']['winners_among']})")
    print("=" * 100)
    print(f"产物: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
