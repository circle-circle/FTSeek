#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[RFC-072 §三/§六.3] KR3 期末裁决批 — 方向×涨幅证伪门 五条判据机械判分 (2026-10-04).

定位: RFC-072 §六.3「期末裁决为独立批次 (含按日聚类统计脚本), 判分口径在裁决批
现写现验」——本脚本即该独立批次。与日常稳健性报告 scripts/kr3_runup_report.py
(行级均值+中位/尾部分解, 只印不判) 分工不同: 本脚本逐条复现 §三 冻结判据并给出
verdict, 是裁决证据载体, 不改动任何冻结阈值 (期间禁改条款仍有效)。

判据 (RFC-072 §三 原文, 冻结):
  0. 窗口: 预注册时刻 → 2026-09-30 全部 PLAYBOOK_SIM 已执行入场, 且已结算可判分。
  1. 主判据: would-block 子集 vs 其余人群单均净期望差异, **按日聚类**双样本 t > 2
     (pooled 不得单独采信, 宪章第 9 律; t 必须标注聚类口径)。假设方向 = blocked 更差
     ⇒ 采信需 t(blk−pass) 显著为负 (|t|>2 且 gap<0)。
  2. MDE 覆盖: 记分输出必带 MDE 列; 声称效应 < MDE → 判「不可测」, 不判确认。
     MDE80 = (z0.975 + z0.80) × se = 2.802 × se (双样本 Welch, 日聚类均值口径)。
  3. 样本外: 窗口最后 2 周 (≥2026-09-17) vs 之前, gap 方向一致性。
  4. 次级端点: would-block 子集 MFE=0 率 (参考, 不作主判据)。
  5. 样本量诚实: would-block 子集 n < 30 → 不裁决, 延长观察。

verdict 机械映射 (现写现验, 判前落档):
  n_blk < 30                        → INSUFFICIENT_SAMPLE_DO_NOT_ADJUDICATE (判据5)
  n_blk ≥ 30 且 1+2+3 全过           → CONFIRMED (→ 10 月立案, 新 DEB + 用户裁)
  n_blk ≥ 30 且 三要素未全过          → FALSIFIED (如实记录, 不立即换假设 — KR3 明令)
零行为变更, 只读生产 jsonl; 幂等可重跑。产物 data/shadow/kr3_final_adjudication.json。

运行: venv/bin/python scripts/kr3_final_adjudication.py
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "data" / "shadow" / "kr3_direction_runup_gate.jsonl"
OUT = REPO / "data" / "shadow" / "kr3_final_adjudication.json"

WINDOW_END = "2026-09-30"      # §三.0 冻结窗尾 (entry_time 口径)
OOS_SPLIT = "2026-09-17"       # §三.3 窗口最后 2 周分界
MIN_N_BLOCK = 30               # §三.5 样本门
MDE_Z = 2.802                  # z0.975(1.960) + z0.80(0.842)
RUNUP_BLOCK_PCT_FROZEN = 1.0   # 采集器冻结阈值核对用 (§二)


def _load_rows():
    rows = []
    for line in SRC.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _day_clustered(rows, flag):
    """按 ET 日期 (exit_time 优先, 缺则 entry_time) 聚类, 返回 [簇均值]。"""
    dm = defaultdict(list)
    for r in rows:
        if bool(r.get("would_block")) != flag:
            continue
        day = (r.get("exit_time") or r.get("entry_time") or "")[:10]
        dm[day].append(float(r["realized_pnl"]))
    return [sum(v) / len(v) for v in dm.values()], len(dm)


def _welch(a, b):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return None
    df = (va / na + vb / nb) ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    return {"t": (ma - mb) / se, "df": df, "se": se,
            "mean_blk_clusters": ma, "mean_pass_clusters": mb}


def _adjudicate_leg(rows, direction):
    sub = [r for r in rows if r["direction"] == direction]
    blk = [r for r in sub if r.get("would_block")]
    ps = [r for r in sub if not r.get("would_block")]
    leg = {"direction": direction, "n_blocked": len(blk), "n_pass": len(ps)}

    # 判据5: 样本门 (先判 — 不裁决时其余判据只作观测记录, 不构成 verdict)
    if len(blk) < MIN_N_BLOCK:
        leg["verdict"] = "INSUFFICIENT_SAMPLE_DO_NOT_ADJUDICATE"
        leg["verdict_basis"] = f"§三.5: would-block n={len(blk)} < {MIN_N_BLOCK} → 不裁决, 延长观察"
    # 判据1: 按日聚类 Welch t
    bm, bm_days = _day_clustered(sub, True)
    pm, pm_days = _day_clustered(sub, False)
    w = _welch(bm, pm)
    leg["day_clusters"] = {"blocked": bm_days, "pass": pm_days}
    if w:
        leg["criterion_1_day_clustered_t"] = {
            "t": round(w["t"], 4), "df": round(w["df"], 1), "se": round(w["se"], 2),
            "pass_|t|>2_and_gap<0": bool(abs(w["t"]) > 2 and w["t"] < 0),
            "note": "聚类口径=ET日期簇均值, Welch; pooled 不采信 (宪章第9律)",
        }
    else:
        leg["criterion_1_day_clustered_t"] = {"t": None, "pass_|t|>2_and_gap<0": False,
                                              "note": "簇数不足, Welch 不可算"}
    # 判据2: MDE 覆盖 (声称效应 = 行级均值 gap, 与 §三.1 单均净期望差异同义)
    row_gap = None
    if blk and ps:
        row_gap = (sum(float(r["realized_pnl"]) for r in blk) / len(blk)
                   - sum(float(r["realized_pnl"]) for r in ps) / len(ps))
    if w and row_gap is not None:
        mde80 = MDE_Z * w["se"]
        leg["criterion_2_mde"] = {
            "claimed_effect_row_gap": round(row_gap, 2),
            "mde80_day_clustered": round(mde80, 2),
            "pass_claimed>=mde": bool(abs(row_gap) >= mde80),
            "note": "MDE80=2.802×se; 声称效应<MDE → 不可测, 不判确认 (§三.2)",
        }
    else:
        leg["criterion_2_mde"] = {"pass_claimed>=mde": False, "note": "se 不可算"}
    # 判据3: OOS 最后2周 vs 之前, gap 方向一致
    def _gap(sel):
        b2 = [float(r["realized_pnl"]) for r in blk if sel(r)]
        p2 = [float(r["realized_pnl"]) for r in ps if sel(r)]
        if not b2 or not p2:
            return None, len(b2), len(p2)
        return sum(b2) / len(b2) - sum(p2) / len(p2), len(b2), len(p2)
    d = lambda r: (r.get("exit_time") or r.get("entry_time") or "")[:10]
    g_early, nb_e, np_e = _gap(lambda r: d(r) < OOS_SPLIT)
    g_late, nb_l, np_l = _gap(lambda r: d(r) >= OOS_SPLIT)
    leg["criterion_3_oos"] = {
        "split": OOS_SPLIT, "gap_before": round(g_early, 2) if g_early is not None else None,
        "gap_last2w": round(g_late, 2) if g_late is not None else None,
        "n": {"before_blk": nb_e, "before_pass": np_e, "late_blk": nb_l, "late_pass": np_l},
        "pass_same_sign": bool(g_early is not None and g_late is not None
                               and g_early * g_late > 0),
    }
    # 判据4: 次级端点 MFE=0 率 (参考不判)
    if blk and ps:
        leg["criterion_4_mfe0_secondary"] = {
            "blk_mfe0_rate": round(sum(1 for r in blk if (r.get("mfe_pct") or 0) == 0) / len(blk), 3),
            "pass_mfe0_rate": round(sum(1 for r in ps if (r.get("mfe_pct") or 0) == 0) / len(ps), 3),
            "note": "参考项, 不作主判据 (§三.4); mfe_pct None 按 or-0 会低估, 双列并呈",
        }
    # verdict (样本门已过时)
    if "verdict" not in leg:
        c1 = leg["criterion_1_day_clustered_t"].get("pass_|t|>2_and_gap<0", False)
        c2 = leg["criterion_2_mde"].get("pass_claimed>=mde", False)
        c3 = leg["criterion_3_oos"].get("pass_same_sign", False)
        leg["three_elements"] = {"c1_t": c1, "c2_mde": c2, "c3_oos": c3}
        if c1 and c2 and c3:
            leg["verdict"] = "CONFIRMED"
            leg["verdict_basis"] = "§三.1+2+3 全过 → 10 月立案为入场门 (新 DEB + 用户裁)"
        else:
            leg["verdict"] = "FALSIFIED"
            leg["verdict_basis"] = ("§三 三要素未全过 (c1=%s c2=%s c3=%s) → 干净证伪, 如实记录; "
                                    "不立即换假设 (KR3 明令, 防链式 fishing)" % (c1, c2, c3))
    # 稳健性附注 (中位/尾部, 只呈不判 — 同 kr3_runup_report 双视角纪律)
    if blk:
        srt = sorted(float(r["realized_pnl"]) for r in blk)
        leg["robustness_blocked"] = {
            "median": round(statistics.median(srt), 2),
            "worst2_sum": round(sum(srt[:2]), 2),
            "tail_share_pct": round(abs(sum(srt[:2])) / max(abs(sum(srt)), 1e-9) * 100, 1),
        }
    return leg


def main():
    rows = _load_rows()
    in_win = [r for r in rows
              if r.get("realized_pnl") is not None
              and (r.get("entry_time") or "")[:10] <= WINDOW_END]
    thr = {r.get("runup_block_pct_frozen") for r in rows if r.get("runup_block_pct_frozen") is not None}
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "RFC-072 §三 (判据冻结) / §六.3 (裁决批现写现验)",
        "window": {"start": "预注册采集起点 (jsonl 首行)", "end": WINDOW_END,
                   "caliber": "entry_time ≤ 窗尾 且 realized_pnl 非空 (已结算可判分)"},
        "threshold_guard": {"runup_block_pct_frozen_values": sorted(thr),
                            "expected": RUNUP_BLOCK_PCT_FROZEN,
                            "ok": thr == {RUNUP_BLOCK_PCT_FROZEN}},
        "n_rows_total": len(rows), "n_eval_in_window": len(in_win),
        "legs": [_adjudicate_leg(in_win, "LONG"), _adjudicate_leg(in_win, "SHORT")],
        "overall": ("分腿裁决, 无合并 verdict — KR3 假设是方向×涨幅联合门, "
                    "两腿 verdict 独立呈用户 (开跑会裁决材料)"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # stdout 摘要
    print(f"═══ RFC-072 KR3 期末裁决批 ({out['generated_at']}) ═══")
    print(f"窗口: {out['window']['caliber']}, ≤{WINDOW_END} | 台账 {len(rows)} 行 → 判分 {len(in_win)}")
    print(f"阈值守卫: runup_block_pct_frozen={sorted(thr)} (期望 {RUNUP_BLOCK_PCT_FROZEN}) "
          f"{'✅' if out['threshold_guard']['ok'] else '🔴 漂移'}")
    for leg in out["legs"]:
        c1 = leg.get("criterion_1_day_clustered_t", {})
        c2 = leg.get("criterion_2_mde", {})
        c3 = leg.get("criterion_3_oos", {})
        print(f"\n[{leg['direction']}] blk n={leg['n_blocked']} pass n={leg['n_pass']} "
              f"日簇 {leg['day_clusters']}")
        print(f"  判据1 日聚类t: {c1.get('t')} (df={c1.get('df')}) 过门={c1.get('pass_|t|>2_and_gap<0')}")
        print(f"  判据2 MDE: 声称={c2.get('claimed_effect_row_gap')} vs MDE80={c2.get('mde80_day_clustered')} "
              f"覆盖={c2.get('pass_claimed>=mde')}")
        print(f"  判据3 OOS: 前段={c3.get('gap_before')} 后2周={c3.get('gap_last2w')} 同向={c3.get('pass_same_sign')}")
        c4 = leg.get("criterion_4_mfe0_secondary", {})
        if c4:
            print(f"  判据4(参考) MFE=0率: blk={c4['blk_mfe0_rate']} pass={c4['pass_mfe0_rate']}")
        rb = leg.get("robustness_blocked", {})
        if rb:
            print(f"  稳健性(blocked): median={rb['median']} worst2={rb['worst2_sum']} 尾部占比={rb['tail_share_pct']}%")
        print(f"  ➤ verdict: {leg['verdict']} — {leg['verdict_basis']}")
    print(f"\n产物: {OUT}")
    print("处置: 本批=裁决证据载体; CONFIRMED/FALSIFIED/INSUFFICIENT 的最终处置 (立案/记录/延长/停采)"
          " 属项目级, 10 月开跑会用户裁决。")


if __name__ == "__main__":
    main()
