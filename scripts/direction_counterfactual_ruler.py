#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Direction Counterfactual Ruler — 方向反事实尺子 (只读, 白名单·修尺子/攒样本)

立项 (2026-08-20, DEB-20260816 §19 方向偏向证据轮, 用户裁"需要"):
  背景: "批量全部做多"是否偏向, 不靠人感觉、也不靠再叠指标阈值裁决, 而是让一把
  **无方向先验的尺子** 说话。本尺子固化三件方向测量:

  A. 方向 × 周期矩阵 (指令级·已执行, 交易口径)
     BUY / SELL_SHORT × {intraday, T+1, T+3, T+5} 命中率与方向调整均收益。
     fwd_return_* 写入时已方向调整 (ic_tracker.py:210-221 sign*(close-entry)/entry),
     正=方向对; 命中 = fwd>0。回答"哪个方向在哪个周期有 edge"。

  B. 候选级反事实对侧视图 (signal_snapshots, 全候选含未执行/NEUTRAL — 无提名幸存者偏差)
     DA / UE × 提名方向{LONG/SHORT/NEUTRAL} × {T+1,T+5,T+20}:
       - 提名侧方向调整收益 (LONG=+fwd, SHORT=-fwd; fwd 为原始 close-to-close)
       - 反事实对侧 = 提名侧取负 ("若反着做会怎样")
       - 方向调整 alpha (扣 SPY beta; LONG=+alpha, SHORT=-alpha) — 去掉市场方向这一最大混杂
     NEUTRAL 队列作内部控制 (应≈市场漂移, 无方向先验)。

  C. 预注册判读 (读数为尺, 不设门不下单):
     - n<30 → 不下结论 (仅打印, 标 ⏳)
     - 命中落在 50%±5 (45~55) → 视为无边缘 (掷币带)
     - 命中 <45% → 反指 (方向稳定错, 对侧即 edge); >55% → 正指
     - 以上均**只产读数**, 任何据此的方向再权重/闸门都是机制变更, 排队 8/31 终裁后。

口径纪律:
  - 交易结论只引用指令级"已执行(executed=1)"口径; 候选级是信号层, 两者并排防误读。
  - 两个库均 mode=ro 打开, 纯只读, 零行为影响。

用法:
  python scripts/direction_counterfactual_ruler.py                 # 默认 since 2026-07-11
  python scripts/direction_counterfactual_ruler.py --since 2026-08-01
输出:
  data/direction_counterfactual_ruler_latest.json (+ _history.jsonl 追加)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path("/opt/ftseek/FTSeek-US")
DB_MAIN = PROJECT_ROOT / "data" / "ftseek_us.db"
DB_SNAP = PROJECT_ROOT / "data" / "signal_snapshots.db"
OUT_LATEST = PROJECT_ROOT / "data" / "direction_counterfactual_ruler_latest.json"
OUT_HISTORY = PROJECT_ROOT / "data" / "direction_counterfactual_ruler_history.jsonl"

# 预注册判读常数 (改这里=改尺子, 需在 DEB 落档; 不是策略参数)
MIN_N = 30            # n<30 不下结论 (charter)
COIN_LO, COIN_HI = 45.0, 55.0   # 掷币带 50%±5
COST_BPS_ROUNDTRIP = 13.0        # 费用记分牌实测往返 ~12bps, 取 13 保守


def _verdict(hit, n):
    """预注册判读: 只在 n>=MIN_N 时给方向结论, 否则攒样本。仅读数, 不设门。"""
    if n < MIN_N:
        return "insufficient_n"
    if hit is None:
        return "no_data"
    if hit < COIN_LO:
        return "contrarian(方向稳定错, 对侧即edge)"
    if hit > COIN_HI:
        return "directional(正指)"
    return "no_edge(掷币带)"


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# A. 指令级 方向 × 周期 (已执行, 交易口径)
# ─────────────────────────────────────────────────────────────────────────────
INSTR_HORIZONS = [
    ("intraday", "fwd_return_intraday"),
    ("T+1", "fwd_return_t1"),
    ("T+3", "fwd_return_t3"),
    ("T+5", "fwd_return_t5"),
]


def section_instruction_matrix(conn, since):
    """BUY/SELL_SHORT × horizon 命中率+方向调整均收益 (executed=1)。"""
    out = {}
    for direction in ("BUY", "SELL_SHORT"):
        out[direction] = {}
        for hlabel, col in INSTR_HORIZONS:
            r = conn.execute(
                f"""SELECT COUNT(*) n,
                        ROUND(AVG(CASE WHEN {col}>0 THEN 1.0 ELSE 0 END)*100,1) hit,
                        ROUND(AVG({col})*100,3) mean
                    FROM playbook_instructions
                    WHERE date>=? AND executed=1 AND direction=? AND {col} IS NOT NULL""",
                (since, direction),
            ).fetchone()
            n, hit, mean = r["n"], r["hit"], r["mean"]
            out[direction][hlabel] = {
                "n": n,
                "hit_pct": hit,
                "dir_mean_pct": mean,
                "verdict": _verdict(hit, n),
            }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B. 候选级反事实 (signal_snapshots, 全候选)
# ─────────────────────────────────────────────────────────────────────────────
CAND_HORIZONS = [("T+1", "fwd_ret_t1", "alpha_t1"),
                 ("T+5", "fwd_ret_t5", "alpha_t5"),
                 ("T+20", "fwd_ret_t20", "alpha_t20")]
STRATEGIES = [("deep_analysis", "DA"), ("universe_expansion", "UE")]


def section_candidate_counterfactual(snap, since):
    """每个 strategy × 提名方向 × horizon: 提名侧/对侧(反事实)/alpha。

    signal_snapshots.fwd_ret_* 为原始 close-to-close (未方向调整)。
    提名侧方向调整: LONG=+fwd, SHORT=-fwd, NEUTRAL=原始(市场漂移对照)。
    方向调整 alpha: LONG=+alpha, SHORT=-alpha (alpha=fwd-spy)。
    """
    out = {}
    for sid, slabel in STRATEGIES:
        out[slabel] = {}
        for nom in ("LONG", "SHORT", "NEUTRAL"):
            row = {"n_by_horizon": {}, "nominated_side": {}, "counterfactual_flip": {},
                   "dir_adj_alpha": {}}
            for hlabel, fcol, acol in CAND_HORIZONS:
                rs = snap.execute(
                    f"""SELECT COUNT(*) n, ROUND(AVG({fcol})*100,3) mean_raw,
                            ROUND(AVG({acol})*100,3) mean_alpha
                        FROM signal_snapshots
                        WHERE trade_date>=? AND strategy_id=? AND direction=?
                          AND {fcol} IS NOT NULL""",
                    (since, sid, nom),
                ).fetchone()
                n, mean_raw, mean_alpha = rs["n"], rs["mean_raw"], rs["mean_alpha"]
                row["n_by_horizon"][hlabel] = n
                if mean_raw is None:
                    continue
                # NEUTRAL 无方向: 提名侧/对侧无意义, 只记原始市场漂移作对照
                if nom == "NEUTRAL":
                    row["nominated_side"][hlabel] = {"raw_drift_pct": mean_raw,
                                                     "alpha_pct": mean_alpha}
                    continue
                sign = 1.0 if nom == "LONG" else -1.0
                nominated = round(sign * mean_raw, 3)          # 提名侧方向调整
                flip = round(-sign * mean_raw, 3)              # 对侧反事实
                dir_alpha = round(sign * mean_alpha, 3) if mean_alpha is not None else None
                row["nominated_side"][hlabel] = {
                    "dir_adj_pct": nominated,
                    "hit_pct": None,  # 候选级均值优先; 命中率见指令级
                }
                row["counterfactual_flip"][hlabel] = {"dir_adj_pct": flip}
                cost_adj = round(nominated - COST_BPS_ROUNDTRIP / 100.0, 3)
                # 预注册定性 (仅 n>=MIN_N 才给): 费后均值方向即池子真实支持的方向
                if n >= MIN_N:
                    flag = ("pool_supports_this_side" if cost_adj > 0
                            else "pool_does_not_support_this_side")
                else:
                    flag = "insufficient_n"
                row["dir_adj_alpha"][hlabel] = {
                    "dir_adj_alpha_pct": dir_alpha,
                    "cost_adj_pct": cost_adj,
                    "flag": flag,
                }
            out[slabel][nom] = row
    return out


def _fmt(v, suf="%"):
    return "-" if v is None else f"{v}{suf}"


def main():
    ap = argparse.ArgumentParser(description="方向反事实尺子 (只读)")
    ap.add_argument("--since", default="2026-07-11")
    args = ap.parse_args()

    main_conn = _ro(DB_MAIN)
    snap_conn = _ro(DB_SNAP)

    instr = section_instruction_matrix(main_conn, args.since)
    cand = section_candidate_counterfactual(snap_conn, args.since)
    main_conn.close()
    snap_conn.close()

    rep = {
        "generated_at": datetime.now().isoformat(),
        "since": args.since,
        "calibration": {
            "min_n": MIN_N, "coin_band_pct": [COIN_LO, COIN_HI],
            "cost_bps_roundtrip": COST_BPS_ROUNDTRIP,
            "note": "预注册读数: n<30 攒样本; 45-55 掷币带; <45 反指; >55 正指。"
                    "本尺子只产读数, 方向再权重/闸门=机制变更, 排队 8/31 终裁后。",
        },
        "instruction_matrix_exec": instr,
        "candidate_counterfactual": cand,
    }

    OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
    OUT_LATEST.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUT_HISTORY, "a") as f:
        f.write(json.dumps(rep, ensure_ascii=False) + "\n")

    # ── 人类可读 ──
    print("=" * 80)
    print(f"[direction_counterfactual_ruler] since={args.since}  (只读尺子, 只产读数)")
    print(f"  判读: n<{MIN_N}⏳攒样本 | {COIN_LO:.0f}-{COIN_HI:.0f}%掷币带 | <{COIN_LO:.0f}反指 | >{COIN_HI:.0f}正指")
    print("-" * 80)
    print("A. 方向×周期矩阵 (指令级·已执行, 唯一交易口径)")
    print(f"  {'方向':<11}{'周期':<9}{'n':>5}{'命中%':>8}{'方向均%':>9}  判读")
    for d in ("BUY", "SELL_SHORT"):
        for hlabel, _ in INSTR_HORIZONS:
            c = instr[d][hlabel]
            print(f"  {d:<11}{hlabel:<9}{c['n']:>5}{_fmt(c['hit_pct']):>8}"
                  f"{_fmt(c['dir_mean_pct']):>9}  {c['verdict']}")
    print("-" * 80)
    print("B. 候选级反事实 (signal_snapshots 全候选, 无提名幸存者偏差; alpha=扣SPY beta)")
    for slabel in ("DA", "UE"):
        print(f"  [{slabel}]")
        for nom in ("LONG", "SHORT", "NEUTRAL"):
            row = cand[slabel][nom]
            parts = []
            for hlabel, _, _ in CAND_HORIZONS:
                n = row["n_by_horizon"].get(hlabel, 0)
                if nom == "NEUTRAL":
                    ns = row["nominated_side"].get(hlabel)
                    if ns:
                        parts.append(f"{hlabel}:drift{_fmt(ns.get('raw_drift_pct'))}"
                                     f"/α{_fmt(ns.get('alpha_pct'))} n={n}")
                    continue
                ns = row["nominated_side"].get(hlabel)
                fl = row["counterfactual_flip"].get(hlabel)
                al = row["dir_adj_alpha"].get(hlabel)
                if ns:
                    parts.append(f"{hlabel}:提名{_fmt(ns.get('dir_adj_pct'))}"
                                 f"|对侧{_fmt(fl.get('dir_adj_pct') if fl else None)}"
                                 f"|α{_fmt(al.get('dir_adj_alpha_pct') if al else None)}"
                                 f"|费后{_fmt(al.get('cost_adj_pct') if al else None)} n={n}")
            print(f"    {nom:<8} " + ("  ".join(parts) if parts else "无样本"))
    print("-" * 80)
    print(f"  → {OUT_LATEST}")
    print("=" * 80)


if __name__ == "__main__":
    main()
