#!/usr/bin/env python3
"""S2 窄校准实验 · 期末校准分析 (2026-08-22, 只读).

预注册协议 (handoff.md 85 3k, 窗口 2026-08-04→08-18):
  ① 失真量级: 真值 spread_bps_true / depth_imbalance_10 分布 vs 主链路代理恒 0
     (主链路 push 兜底 bid=ask=last → proxy_spread_bps/proxy_ofi 恒 0)
  ② 真值 IC: depth_imbalance_10@t vs 前瞻收益; IC≈0 → 合法关门 / 显著 → 重开扩面议
     (采集节拍为 cron 每 30min, 预注册的 5/15min 前瞻不可观测 — 实际只能算
      t+30m / t+60m 两档, 属窗口设计的现实约束, 如实标注)

只读: 输入 data/shadow/orderbook_s2.jsonl, 输出 data/s2_calibration_latest.json.
"""
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats

DATA = Path("data/shadow/orderbook_s2.jsonl")
OUT = Path("data/s2_calibration_latest.json")


def load_rows():
    rows = []
    with DATA.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = (r["bid_prices"][0] + r["ask_prices"][0]) / 2.0
            if mid <= 0:
                continue
            r["_mid"] = mid
            r["_dt"] = datetime.fromisoformat(r["ts_et"])
            rows.append(r)
    return rows


def describe(vals):
    a = np.asarray(vals, dtype=float)
    if a.size == 0:
        return {}
    qs = np.percentile(a, [10, 25, 50, 75, 90])
    return {
        "n": int(a.size),
        "mean": round(float(a.mean()), 4),
        "std": round(float(a.std(ddof=1)), 4) if a.size > 1 else 0.0,
        "p10": round(float(qs[0]), 4),
        "p25": round(float(qs[1]), 4),
        "p50": round(float(qs[2]), 4),
        "p75": round(float(qs[3]), 4),
        "p90": round(float(qs[4]), 4),
        "min": round(float(a.min()), 4),
        "max": round(float(a.max()), 4),
    }


def fwd_ic(samples, k):
    """Spearman IC: depth_imbalance_10@t vs mid t+k 档前瞻收益 (k=快照数)."""
    xs, ys = [], []
    for i in range(len(samples) - k):
        t0, t1 = samples[i], samples[i + k]
        gap_min = (t1["_dt"] - t0["_dt"]).total_seconds() / 60.0
        # 相邻快照间隔超出 45min 视为跨日/断档, 跳过 (避免隔夜收益污染日内 IC)
        if gap_min <= 0 or gap_min > 30 * k + 15:
            continue
        ret = t1["_mid"] / t0["_mid"] - 1.0
        if ret == 0:
            continue
        xs.append(t0["depth_imbalance_10"])
        ys.append(ret)
    n = len(xs)
    if n < 10:
        return {"n": n, "ic": None}
    ic, p = stats.spearmanr(xs, ys)
    # MDE: Fisher z 近似, alpha=0.05 双侧, power=0.8
    z_a, z_b = stats.norm.ppf(0.975), stats.norm.ppf(0.8)
    mde = (z_a + z_b) / math.sqrt(max(n - 3, 1))
    return {
        "n": int(n),
        "ic": round(float(ic), 4),
        "p_value": round(float(p), 4),
        "significant_5pct": bool(p < 0.05),
        "mde_ic_at_power80": round(float(mde), 4),
        "horizon": f"~{k*30}min",
    }


def main():
    rows = load_rows()
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    for s in by_sym.values():
        s.sort(key=lambda r: r["_dt"])

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": "2026-08-04 → 2026-08-18",
        "rows_total": len(rows),
        "symbols": {},
        "part1_distortion": {},
        "part2_true_ic": {},
        "verdict": "",
    }

    # ── Part ① 失真量级 ──
    pooled_spread, pooled_imb = [], []
    for sym in sorted(by_sym):
        ss = by_sym[sym]
        spreads = [r["spread_bps_true"] for r in ss]
        imbs = [r["depth_imbalance_10"] for r in ss]
        pooled_spread.extend(spreads)
        pooled_imb.extend(imbs)
        proxies = {r["proxy_spread_bps"] for r in ss}
        result["symbols"][sym] = {
            "n": len(ss),
            "spread_bps_true": describe(spreads),
            "depth_imbalance_10": describe(imbs),
            "proxy_spread_bps_values": sorted(proxies)[:5],
            # 快照 last vs 盘口中价的偏离: 代理口径陈旧度的直接量级
            "last_vs_mid_dev_bps": describe(
                [
                    abs(float(r.get("last", 0) or 0) - r["_mid"]) / r["_mid"] * 1e4
                    for r in ss
                    if float(r.get("last", 0) or 0) > 0
                ]
            ),
        }
    result["part1_distortion"] = {
        "spread_bps_true_pooled": describe(pooled_spread),
        "depth_imbalance_10_pooled": describe(pooled_imb),
        "proxy_constant": 0,
        "note": "主链路代理 bid=ask=last → spread/OFI 恒 0; 真值分布即失真量级",
    }

    # ── Part ② 真值 IC ──
    for sym in sorted(by_sym):
        ss = by_sym[sym]
        result["part2_true_ic"][sym] = {f"k{k}": fwd_ic(ss, k) for k in (1, 2)}
    # 合并池化 (z-transform 平均)
    pooled = defaultdict(list)
    for sym in sorted(by_sym):
        for k, res in result["part2_true_ic"][sym].items():
            if res.get("ic") is not None and res["n"] >= 10:
                z = math.atanh(max(min(res["ic"], 0.999), -0.999))
                pooled[k].append((z, res["n"]))
    result["part2_true_ic"]["pooled_fisher_z"] = {
        k: {
            "symbols": len(v),
            "ic_combined": round(float(math.tanh(sum(z for z, _ in v) / len(v))), 4),
            "median_n": int(sorted(n for _, n in v)[len(v) // 2]),
        }
        for k, v in pooled.items()
    }

    # ── 裁决 (预注册: IC≈0 → 合法关门 / 显著 → 重开议) ──
    sig_flags = [
        res.get("significant_5pct")
        for sym in result["part2_true_ic"]
        if sym != "pooled_fisher_z"
        for res in result["part2_true_ic"][sym].values()
        if isinstance(res, dict)
    ]
    n_sig = sum(1 for f in sig_flags if f)
    mdes = [
        res.get("mde_ic_at_power80")
        for sym in result["part2_true_ic"]
        if sym != "pooled_fisher_z"
        for res in result["part2_true_ic"][sym].values()
        if isinstance(res, dict) and res.get("mde_ic_at_power80")
    ]
    result["verdict"] = (
        f"{n_sig}/{len(sig_flags)} 个 IC 检验在 5% 水平显著; "
        f"本样本量下 MDE≈{min(mdes):.2f}–{max(mdes):.2f} "
        f"(|IC| 低于该值的'不显著'只是功效不足, 不是关门证据)"
        if mdes
        else "样本不足以计算 MDE"
    )

    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    # 控制台摘要
    p1 = result["part1_distortion"]
    print(f"═══ S2 窄校准期末分析 ({result['rows_total']} rows) ═══")
    print(f"\n① 失真量级 (真值 vs 代理恒0):")
    sp = p1["spread_bps_true_pooled"]
    print(
        f"   spread_bps_true: p50={sp['p50']:.1f} p75={sp['p75']:.1f} "
        f"p90={sp['p90']:.1f} max={sp['max']:.1f}"
    )
    im = p1["depth_imbalance_10_pooled"]
    print(
        f"   depth_imbalance_10: std={im['std']:.3f} p10={im['p10']:.3f} p90={im['p90']:.3f}"
    )
    print(f"\n② 真值 IC (depth_imbalance_10 vs 前瞻收益):")
    for sym in sorted(by_sym):
        cells = []
        for k in ("k1", "k2"):
            r = result["part2_true_ic"][sym][k]
            if r.get("ic") is None:
                cells.append(f"{r['horizon']}: n<10")
            else:
                star = "*" if r["significant_5pct"] else ""
                cells.append(
                    f"{r['horizon']}: IC={r['ic']:+.3f}{star} (n={r['n']}, p={r['p_value']:.3f})"
                )
        print(f"   {sym:10s} {' | '.join(cells)}")
    pz = result["part2_true_ic"]["pooled_fisher_z"]
    for k in sorted(pz):
        print(f"   pooled({pz[k]['symbols']}标的): IC={pz[k]['ic_combined']:+.4f}")
    print(f"\n裁决: {result['verdict']}")
    print(f"\n输出: {OUT}")


if __name__ == "__main__":
    main()
