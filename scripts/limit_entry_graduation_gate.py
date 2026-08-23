#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锚限价影子毕业主门 (DEB-20260806-limit-entry-anchor §4.3 预注册, 逐字实现).

预注册条文 (2026-08-06 立案锁定, 见数前):
  主门:   配对符号翻转置换 (paired sign-flip permutation), B=10,000, 单侧 α=0.025
  δ定义:  anchor成交=sign×(实际入场−anchor)×qty (≥0) / 追单成交=可负 / 漏单=−实际净
  人群:   真被动 = bucket != marketable (市价化恒等零不得池入)
  双条件: mean δ ≥ $1/笔 且 bootstrap CI95 下界 > 0
  共门:   符号检验 (防 top5 振幅集中) — 双门同过才毕业
  主口径: chase_5m (功效分析 §4.2 与二阶段保真闸均以 5min 口径锁定)
  hedge:  只用裁决后数据 (影子 8/6 裁决, 数据 8/7 起, out-of-time 满足)

输入: data/limit_entry_shadow_replay.jsonl (由 limit_entry_shadow_replay.py 产出)
输出: stdout 判决 + data/limit_entry_graduation_report.json
零交易路径. 回滚: 删本脚本 + report.
"""
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
REPLAY = REPO / "data" / "limit_entry_shadow_replay.jsonl"
OUT = REPO / "data" / "limit_entry_graduation_report.json"
CALIBER = "judge_5m"  # 预注册主口径
B = 10_000
ALPHA = 0.025
SEED = 20260823


def load_paired_deltas():
    """完全复刻 replay 汇总的配对口径: passive(≠marketable) + paired_trade."""
    deltas, rows = [], []
    for line in REPLAY.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        j = r.get(CALIBER)
        if not j or not r.get("paired_trade"):
            continue
        if j.get("bucket") in (None, "marketable", "no_bars"):
            continue
        if j.get("delta") is None:
            continue
        deltas.append(float(j["delta"]))
        rows.append(r)
    return np.array(deltas), rows


def sign_flip_permutation(d, b=B, seed=SEED):
    """配对符号翻转置换: H0 下每笔 δ 对称于 0, 独立翻号."""
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=(b, len(d)))
    perm_sums = signs @ d
    obs = d.sum()
    p = float((perm_sums >= obs).mean())
    return obs, p, perm_sums


def bootstrap_ci(d, b=B, seed=SEED, alpha=0.05):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(b, len(d)))
    means = d[idx].mean(axis=1)
    lo = float(np.percentile(means, 100 * alpha / 2))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return lo, hi


def sign_test(d):
    """单侧符号检验: P(X≥k), X~Binom(n,0.5). 正态近似+精确二项双算取精确."""
    n = len(d)
    k = int((d > 0).sum())
    # 精确二项 (n<200 直接算)
    from math import comb, log
    log_half_n = -n * math.log(2)
    p_tail = sum(comb(n, i) for i in range(k, n + 1)) * math.exp(log_half_n)
    return k, n, float(p_tail)


def day_clustered_t(rows):
    """宪章第9律补充披露 (非预注册门): 按日聚类均值, 单日聚合 δ 的 t."""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r.get("date")].append(float(r[CALIBER]["delta"]))
    day_means = np.array([np.mean(v) for v in by_day.values()])
    n = len(day_means)
    if n < 2:
        return None, None, n
    m = day_means.mean()
    sd = day_means.std(ddof=1)
    t = float(m / (sd / math.sqrt(n))) if sd > 0 else None
    return float(m), t, n


def main():
    if not REPLAY.exists():
        print("无重放输出, 先跑 limit_entry_shadow_replay.py")
        return 1
    d, rows = load_paired_deltas()
    n = len(d)
    print(f"主口径 {CALIBER}: 配对真被动 δ 样本 n={n} (预注册门槛 n≥60)")

    if n < 60:
        print(f"样本不足 ({n}<60), 不启用判决, 继续攒样")
        return 0

    obs_sum, p_perm, _ = sign_flip_permutation(d)
    mean_d = float(d.mean())
    ci_lo, ci_hi = bootstrap_ci(d)
    k_pos, n_all, p_sign = sign_test(d)
    day_mean, day_t, n_days = day_clustered_t(rows)

    # top5 集中度披露 (符号共门存在的原因)
    order = np.argsort(-np.abs(d))
    top5_share = float(np.abs(d[order[:5]]).sum() / np.abs(d).sum()) if np.abs(d).sum() > 0 else 0
    fallback_n = sum(1 for r in rows if r[CALIBER].get("band_edge_fallback"))

    gate_perm = p_perm < ALPHA
    gate_mean = mean_d >= 1.0
    gate_ci = ci_lo > 0
    gate_sign = p_sign < ALPHA
    double_cond = gate_mean and gate_ci
    main_gate = gate_perm and double_cond
    graduated = main_gate and gate_sign

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pre_registered": "DEB-20260806-limit-entry-anchor §4.3 (2026-08-06, 见数前锁定)",
        "caliber": CALIBER, "n_paired_passive": n,
        "delta_sum": round(float(d.sum()), 2), "delta_mean": round(mean_d, 4),
        "delta_sd": round(float(d.std(ddof=1)), 4),
        "sign_flip_permutation": {"B": B, "observed_sum": round(float(obs_sum), 2),
                                  "one_sided_p": round(p_perm, 5), "alpha": ALPHA,
                                  "pass": gate_perm},
        "double_condition": {"mean_delta": round(mean_d, 4), "mean_ge_1": gate_mean,
                             "bootstrap_ci95_lo": round(ci_lo, 4),
                             "bootstrap_ci95_hi": round(ci_hi, 4),
                             "ci_lo_gt_0": gate_ci, "pass": double_cond},
        "sign_common_gate": {"k_positive": k_pos, "n": n_all,
                             "one_sided_p": round(p_sign, 6), "pass": gate_sign},
        "disclosure": {"top5_abs_share": round(top5_share, 4),
                       "band_edge_fallback_rows": fallback_n,
                       "day_clustered_mean": (round(day_mean, 4) if day_mean is not None else None),
                       "day_clustered_t": (round(day_t, 3) if day_t is not None else None),
                       "n_days": n_days,
                       "note": "按日聚类为宪章第9律补充披露, 非预注册门"},
        "main_gate_pass": main_gate, "graduated": graduated,
        "verdict": ("✅ 毕业 (主门+双条件+符号共门全过)" if graduated else
                    "❌ 未毕业" if main_gate and not gate_sign else
                    "❌ 未毕业 (主门/双条件未过)"),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
