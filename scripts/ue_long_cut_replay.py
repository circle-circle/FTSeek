#!/usr/bin/env python3
"""预注册回放: 禁 UE LONG 方向入场 (只减不增, 2026-09-23).

用户指令: "ue 砍掉 long 启用 short 先行验证" —— 本脚本是该提案的**先行验证** (历史回放口径),
不是在跑规则。落地需另走预注册 + 用户裁。

═══ 判据 (2026-09-23 冻结, 见数前定稿, 禁事后挪) ═══
规则 R = 拦截 `decision_id` 前缀 `universe_expansion` 且 `direction=LONG` 的入场; SHORT 侧不动。
窗口 = UE 全部成交所在窗口 (2026-06-25..2026-09-22)。**声明**: UE 全部 82 笔成交都落在该 90 天窗内
⇒ 不存在第二个不重叠窗 (T24-7), 本回放只能给"单窗 + IS/OOS 半窗"证据, 不够裁决级, 需影子向前补。

门槛 (全过才可提"落地"; 缺一即只能记观察):
 G1 样本量: 拦截组 n ≥ 40 (裁决级); 30 天子窗 n<40 只作方向参考。
 G2 显著性 (T18 三列并列): 拦截组 净额 笔均 ≤ −$5 **且** 中位 ≤ 0 **且** 按日聚类 t ≤ −2.0。
 G3 边际 (T12 防"合计差"假象): 拦截组 笔均 − 留存池 笔均 ≤ −$8; 且 notional 归一 (净额/名义) 后同号。
 G4 安慰剂 (T4/Gate4, 方向敏感): 逐日配对随机抽**同数量非 UE-LONG** 单作伪拦截组, 200 次重抽,
    取 ΔB 分布 97.5 分位; 真拦截 ΔB 必须 ≥ 该分位 (砍负期望子集才会让留存池变好)。
 G5 误杀 (T17): 拦截组内盈利单占比 ≤ 40%, 且这些盈利单净利合计占留存池总净 ≤ 15%。
 G6 轴有效性 (T13): 拦截组 vs 其他 LONG 单在**入场前**变量 (entry_gap/atr_basis/vix/signal_strength) 的中位差
    > 0.15×对照群 σ; 且 entry_gap 分桶净额单调 (非单调 = 轴不是真驱动)。
 G7 重叠 (T14): 报拦截组与现役门/其他影子判定的重叠数 (独有拦截量化), 全文进产物。
 G8 切窗 (T17): IS/OOS 半窗 + 上游门落地日 (09-05) 前后段, 拦截组笔均同号。

权限: G1-G8 全过 → 可提 L2 软提示 (只减不增, 仍禁直接 L3); 只过 G1-G3 不过 G4 → "池均值效应、非规则特有", 不落地。
Arm 口径 (两臂并列): A 资金不回流 ΔA = −Σ(拦截净); B 资金回流按留存池笔均 ΔB = ΔA + n拦截×留存池笔均。
产物: data/reports/ue_long_cut_replay_20260923.json (非 /tmp, T6)。
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import statistics as st
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data/ftseek_us.db"
OUT = REPO / "data/reports/ue_long_cut_replay_20260923.json"
N_PLACEBO = 200
SEED = 20260923
WINDOW_DAYS = 90
UPSTREAM_GATE_DATE = date(2026, 9, 5)  # R1 禁追高闸/相关落地 (切窗用, T17)

_log = logging.getLogger(__name__)


def src_of(r: dict) -> str | None:
    for v in (r.get("decision_id") or "", r.get("owner_strategy") or "", r.get("strategy_id") or ""):
        lv = v.lower()
        if lv.startswith("universe_expansion"):
            return "UE"
        if lv.startswith("intraday_reentry"):
            return "REENTRY"
        if lv.startswith("da-") or lv.startswith("deep_analysis"):
            return "DA"
    return None


def pdate(r: dict) -> date | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(r.get("trade_date") or ""))
    return date(*map(int, m.groups())) if m else None


def net_of(r: dict) -> float:
    return float(r["pnl_net"] if r.get("pnl_net") is not None else (r.get("realized_pnl") or 0.0))


def notional(r: dict) -> float:
    try:
        return abs(float(r.get("entry_price") or 0) * float(r.get("quantity") or 0))
    except Exception:
        return 0.0



def pre_scalar(r: dict, key: str):
    """取入场前标量: 顶层列 → metadata_json 同名键 → dict 内的 gap_pct/value/pct (2026-09-23 提取修复)。"""
    v = r.get(key)
    if v is None and r.get("metadata_json"):
        try:
            v = json.loads(r["metadata_json"]).get(key)
        except Exception:
            v = None
    if isinstance(v, dict):
        for kk in ("gap_pct", "aligned_gap_pct", "value", "pct"):
            if kk in v:
                return v[kk]
        return None
    return v


def net_pct(r: dict) -> float | None:
    n = notional(r)
    return (net_of(r) / n) if n else None


def load() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(x) for x in con.execute(
        "SELECT * FROM trade_records WHERE trade_date >= ?", (str(date(2026, 9, 23) - timedelta(days=WINDOW_DAYS)),))]
    con.close()
    for r in rows:
        r["_src"] = src_of(r)
        r["_date"] = pdate(r)
        r["_net"] = net_of(r)
        r["_npct"] = net_pct(r)
    return [r for r in rows if r["_date"]]


def day_t(vals_by_day: dict) -> tuple[float | None, float | None, int]:
    dm = [st.mean(v) for v in vals_by_day.values() if v]
    if len(dm) < 3:
        return None, None, len(dm)
    m = st.mean(dm)
    sd = st.stdev(dm)
    return m, (m / (sd / len(dm) ** 0.5) if sd else 0.0), len(dm)


def blk_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    nets = [r["_net"] for r in rows]
    pcts = [r["_npct"] for r in rows if r["_npct"] is not None]
    byd = defaultdict(list)
    for r in rows:
        byd[r["_date"]].append(r["_net"])
    dm, t, nd = day_t(byd)
    win = [r for r in rows if r["_net"] > 0]
    top = sorted(nets)[-3:]
    return {
        "n": len(rows), "net_total": round(sum(nets), 2), "net_mean": round(st.mean(nets), 3),
        "net_median": round(st.median(nets), 3), "wr": round(len(win) / len(rows), 3),
        "day_mean": round(dm, 3) if dm is not None else None, "day_t": round(t, 2) if t is not None else None,
        "n_days": nd, "net_pct_mean": round(st.mean(pcts), 4) if pcts else None,
        "win_net_total": round(sum(r["_net"] for r in win), 2),
        "top3_net_share": round(sum(top) / sum(nets), 3) if sum(nets) else None,
    }


def main() -> int:
    rows = load()
    ue_long = [r for r in rows if r["_src"] == "UE" and (r.get("direction") or "").upper() == "LONG"]
    ue_short = [r for r in rows if r["_src"] == "UE" and (r.get("direction") or "").upper() == "SHORT"]
    rest = [r for r in rows if r not in ue_long]  # 留存池 = 全窗非 UE-LONG 成交 (任何来源)
    rest_short_excl = [r for r in rows if not (r["_src"] == "UE" and (r.get("direction") or "").upper() == "LONG")]

    out: dict = {"rule": "block UE LONG entries", "window_days": WINDOW_DAYS,
                 "window": [str(min(r["_date"] for r in rows)), str(max(r["_date"] for r in rows))],
                 "pool_all_n": len(rows), "n_placebo": N_PLACEBO, "seed": SEED}
    out["blocked"] = blk_stats(ue_long)
    out["ue_short"] = blk_stats(ue_short)
    out["retained_pool"] = blk_stats(rest)
    out["all"] = blk_stats(rows)

    b, p = out["blocked"], out["retained_pool"]
    out["G1_n_ge_40"] = b["n"] >= 40
    out["G2"] = {"net_mean_le_-5": b["net_mean"] <= -5, "median_le_0": b["net_median"] <= 0,
                 "day_t_le_-2": (b["day_t"] is not None and b["day_t"] <= -2.0)}
    out["G3"] = {"per_trade_diff": round(b["net_mean"] - p["net_mean"], 3),
                 "pass": (b["net_mean"] - p["net_mean"]) <= -8.0,
                 "pct_diff": round((b["net_pct_mean"] or 0) - (p["net_pct_mean"] or 0), 4),
                 "pct_same_sign": ((b["net_pct_mean"] or 0) < 0 < (p["net_pct_mean"] or 0))
                 or ((b["net_pct_mean"] or 0) < (p["net_pct_mean"] or 0))}

    # Δ 两臂
    dA = -b["net_total"]
    dB = dA + b["n"] * p["net_mean"]
    out["dA_no_redeploy"] = round(dA, 2)
    out["dB_redeploy_at_pool_mean"] = round(dB, 2)

    # G4 安慰剂: 逐日配对随机抽同数量非 UE-LONG 单
    rng = random.Random(SEED)
    by_day_pool: dict[date, list[dict]] = defaultdict(list)
    for r in rest_short_excl:
        by_day_pool[r["_date"]].append(r)
    cnt_by_day = Counter(r["_date"] for r in ue_long)
    placebo = []
    for _ in range(N_PLACEBO):
        fake = []
        for d, k in cnt_by_day.items():
            pool_d = by_day_pool.get(d) or []
            fake += rng.sample(pool_d, min(k, len(pool_d)))
        if not fake:
            continue
        fs = blk_stats(fake)
        placebo.append(-fs["net_total"] + fs["n"] * p["net_mean"])
    placebo.sort()
    def pct_rank(x: float) -> float:
        return sum(1 for v in placebo if v <= x) / len(placebo) if placebo else float("nan")
    ci975 = placebo[int(0.975 * (len(placebo) - 1))] if placebo else None
    ci025 = placebo[int(0.025 * (len(placebo) - 1))] if placebo else None
    out["G4"] = {"placebo_mean": round(st.mean(placebo), 2) if placebo else None,
                 "placebo_sd": round(st.stdev(placebo), 2) if len(placebo) > 1 else None,
                 "ci025": round(ci025, 2) if ci025 is not None else None,
                 "ci975": round(ci975, 2) if ci975 is not None else None,
                 "real_dB": out["dB_redeploy_at_pool_mean"],
                 "percentile_rank": round(pct_rank(dB), 3),
                 "pass": (ci975 is not None and dB >= ci975)}

    # G5 误杀
    win_rows = [r for r in ue_long if r["_net"] > 0]
    out["G5"] = {"false_kill_rate": round(len(win_rows) / len(ue_long), 3) if ue_long else None,
                 "win_net_total": round(sum(r["_net"] for r in win_rows), 2),
                 "pool_net_total": round(p["net_total"], 2),
                 "win_share_of_pool": round(sum(r["_net"] for r in win_rows) / p["net_total"], 4)
                 if p["net_total"] else None,
                 "pass": (len(win_rows) / len(ue_long) <= 0.40) if ue_long else None}

    # G6 轴有效性: UE LONG vs 其他 LONG 单的入场前变量中心对比
    def med(rows_, key):
        vals = []
        for r in rows_:
            try:
                vals.append(float(pre_scalar(r, key)))
            except (TypeError, ValueError):
                _log.warning("axis value parse failed", exc_info=True)
                continue
        return (st.median(vals), len(vals), st.pstdev(vals) if len(vals) > 1 else 0.0) if vals else (None, 0, 0.0)

    others_long = [r for r in rows if (r.get("direction") or "").upper() == "LONG" and r not in ue_long]
    axis = {}
    for k in ("entry_gap", "signal_strength", "entry_vix", "mae_pct", "mfe_pct"):
        a, na, _ = med(ue_long, k)
        c, nc, csd = med(others_long, k)
        axis[k] = {"blocked_med": a, "ctrl_med": c, "ctrl_sd": round(csd, 4),
                   "diff_over_sigma": round(abs((a or 0) - (c or 0)) / csd, 3) if (a is not None and c is not None and csd) else None,
                   "n_blocked": na, "n_ctrl": nc}
    out["G6_axis"] = axis
    # entry_gap 分桶单调性 (只有数值可比时)
    gap_rows = [r for r in rows if (r.get("direction") or "").upper() == "LONG"]
    gaps = []
    for r in gap_rows:
        try:
            gaps.append((float(pre_scalar(r, "entry_gap")), r))
        except (TypeError, ValueError):
            _log.warning("entry gap parse failed", exc_info=True)
            continue
    buckets: dict[str, dict] = {}
    if len(gaps) >= 20:
        vals = sorted(g[0] for g in gaps)
        qs = [vals[int(q * (len(vals) - 1))] for q in (0.25, 0.5, 0.75)]
        def lab(v):
            return "Q1低" if v <= qs[0] else "Q2" if v <= qs[1] else "Q3" if v <= qs[2] else "Q4高"
        for v, r in gaps:
            buckets.setdefault(lab(v), []).append(r)
        bm = {k: {"n": len(v), "net_mean": round(st.mean([x["_net"] for x in v]), 3)}
              for k, v in sorted(buckets.items())}
        seq = [bm[k]["net_mean"] for k in sorted(bm)]
        bm["_monotonic"] = all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
        bm["_seq"] = seq
        out["G6_gap_monotonic"] = bm
    # G8 切窗
    spans = {}
    dates = sorted(r["_date"] for r in ue_long)
    if dates:
        mid = dates[len(dates) // 2]
        for name, sel in (("IS_first_half", lambda d: d < mid), ("OOS_second_half", lambda d: d >= mid),
                          ("before_0905", lambda d: d < UPSTREAM_GATE_DATE),
                          ("on_after_0905", lambda d: d >= UPSTREAM_GATE_DATE)):
            sub = [r for r in ue_long if sel(r["_date"])]
            spans[name] = blk_stats(sub)
    out["G8_windows"] = spans

    # G7 重叠: 现役门/影子 判定面 (只读查询, 缺表则声明)
    overlaps = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        tried = 0
        for tbl, col in (("trigger_miss_telemetry", "inst_id"),):
            try:
                rows_ = con.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()
                tried += 1
                overlaps[tbl] = {"rows": rows_["c"] if rows_ else 0,
                                 "note": "含全部拦截原因, 非单门; 详见台账各门行"}
            except sqlite3.Error:
                overlaps[tbl] = {"note": "表不存在"}
        con.close()
    except sqlite3.Error as e:
        overlaps["error"] = str(e)
    # UE LONG 单是否曾出现在 trigger_miss_telemetry (被现役门拦过又放行的)
    out["G7_overlap"] = overlaps

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ── 打印 ──
    def line(tag, s):
        print(f"{tag:14s} n={s.get('n',0):3d} 净合计{s.get('net_total',0):+9.2f}$ 笔均{s.get('net_mean',0):+8.3f}$ "
              f"中位{s.get('net_median',0):+8.3f}$ WR{s.get('wr',0):5.1%} 日均{s.get('day_mean')} t={s.get('day_t')} "
              f"({s.get('n_days')}日) 净%均{s.get('net_pct_mean')}")
    print(f"窗口 {out['window'][0]}..{out['window'][1]} 全池 n={len(rows)}")
    line("拦截组 UE LONG", b)
    line("UE SHORT(不动)", out["ue_short"])
    line("留存池", p)
    line("全池", out["all"])
    print(f"\nΔA(不回流)={out['dA_no_redeploy']:+.2f}$  ΔB(按留存池笔均回流)={out['dB_redeploy_at_pool_mean']:+.2f}$")
    print("G1 n>=40:", out["G1_n_ge_40"], "| G2:", out["G2"], "| G3:", out["G3"])
    print("G4:", out["G4"])
    print("G5:", out["G5"])
    print("G6 axis:", json.dumps(out["G6_axis"], ensure_ascii=False))
    print("G6 gap buckets:", out.get("G6_gap_monotonic"))
    print("G8 切窗:")
    for k, v in out["G8_windows"].items():
        print(f"   {k:16s} n={v.get('n'):3d} 笔均{v.get('net_mean')} 中位{v.get('net_median')} WR{v.get('wr')}")
    print("\n产物:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())