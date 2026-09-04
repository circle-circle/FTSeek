#!/usr/bin/env python3
"""[RFC-072 KR3 · 判分报告] 方向×涨幅证伪门 — 均值(预注册) + 稳健性双视角 (M3, 2026-09-04)。

动机: 采集器 kr3_runup_shadow.py 只落逐笔 JSONL, 日报按【均值】口径汇报
  would_block 子集净期望 — 但均值受尾部支配 (9/3: SHORT被拦组中位+13.0 却总亏
  -203, 全靠 HPE -149/AVGO -60 两笔; LONG 被拦组胜率73%中位+14.9, 一刀切会误杀
  追涨多头)。本脚本输出冻结判据的均值视角 + 中位/胜率/尾部分解, 供 DEB 裁决时
  区分「门方向对」与「门只是防尾部」两种完全不同的实装含义:
    - 方向对 (median/胜率也差) → 全拦
    - 仅防尾 (median 正、尾部负) → 只拦尾部形态或降杠杆, 不全拦
零行为变更, 只读; 幂等可重复运行。

运行: ./venv/bin/python scripts/kr3_runup_report.py [--push]
输出: stdout + data/shadow/kr3_runup_verdict_latest.json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "data" / "shadow" / "kr3_direction_runup_gate.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "kr3_runup_verdict_latest.json"
RUNUP_BLOCK_PCT = 1.0  # 与采集器冻结阈值一致 (RFC-072, 期间禁改)


def _load():
    rows = []
    if not SRC.exists():
        return rows
    for line in SRC.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception as e:
            print(f"[kr3-report] JSONL 脏行跳过: {e}", file=sys.stderr)
    return rows


def _sign_test_p(wins: int, n: int) -> float:
    """单侧二项检验 p (H0: 中位≤0, 观测 wins/n 胜). n<=0 或 wins*2<=n 时无证据返回 1.0。"""
    if n <= 0 or wins * 2 <= n:
        return 1.0
    # P(X >= wins) for X~Bin(n, 0.5)
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / (2 ** n)


def _group_stats(rows):
    """单组统计: 均值(预注册口径) + 中位/胜率/尾部(稳健性口径)。"""
    pnl = [float(r["realized_pnl"]) for r in rows if r.get("realized_pnl") is not None]
    if not pnl:
        return {"n_rows": len(rows), "n_eval": 0}
    srt = sorted(pnl)
    wins = sum(1 for p in pnl if p > 0)
    worst2 = sum(srt[:2]) if len(srt) >= 2 else srt[0]
    return {
        "n_rows": len(rows),
        "n_eval": len(pnl),
        "sum": round(sum(pnl), 2),
        "mean": round(sum(pnl) / len(pnl), 2),
        "median": round(statistics.median(pnl), 2),
        "win_rate": round(wins / len(pnl), 3),
        "sign_test_p_median_gt0": round(_sign_test_p(wins, len(pnl)), 4),
        "worst": round(srt[0], 2),
        "worst2_sum": round(worst2, 2),
        "tail_share_pct": round(abs(worst2) / abs(sum(pnl)) * 100, 1) if sum(pnl) != 0 else None,
        "p10": round(srt[max(0, len(srt) // 10)], 2),
    }


def build_report():
    rows = _load()
    lines = ["【KR3 方向×涨幅证伪门 · 判分 (RFC-072, 阈值冻结 %g%%)】" % RUNUP_BLOCK_PCT]
    if not rows:
        lines.append("  暂无采集样本 — 等待 kr3_runup_shadow.py 逐日采集")
        return lines, {"generated_at": datetime.now().isoformat(timespec="seconds"), "n": 0}

    evaluable = [r for r in rows if r.get("realized_pnl") is not None]
    pending = len(rows) - len(evaluable)
    groups = {}
    for d in ("LONG", "SHORT"):
        for blk in (True, False):
            key = f"{d}|{'blocked' if blk else 'pass'}"
            groups[key] = [r for r in evaluable if r.get("direction") == d
                           and bool(r.get("would_block")) == blk]

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_collected": len(rows),
        "n_eval": len(evaluable),
        "n_pending_open": pending,
        "runup_block_pct_frozen": RUNUP_BLOCK_PCT,
        "prereg": "RFC-072: would-block 子集净期望显著更差 (均值口径)",
        "robustness_note": "均值受尾部支配; median/胜率/尾部分解用于区分「方向错」与「仅尾部风险」",
        "groups": {k: _group_stats(v) for k, v in groups.items()},
    }

    lines.append(f"  采集 {len(rows)} 笔 (未平仓待结算 {pending}), 判分 n={len(evaluable)}")
    lines.append("  分组                n   总PnL    均值    中位   胜率  p(中位>0)   最差   最差2和  尾部占比")
    for key in ("LONG|blocked", "LONG|pass", "SHORT|blocked", "SHORT|pass"):
        g = out["groups"][key]
        if not g.get("n_eval"):
            lines.append(f"  {key:<16}{g.get('n_rows', 0):>4}   无已结算样本")
            continue
        tail = f"{g['tail_share_pct']:.0f}%" if g.get("tail_share_pct") is not None else "n/a"
        lines.append(
            f"  {key:<16}{g['n_eval']:>4} {g['sum']:>8.1f} {g['mean']:>7.1f} {g['median']:>7.1f}"
            f" {g['win_rate']*100:>5.0f}% {g['sign_test_p_median_gt0']:>9.4f}"
            f" {g['worst']:>7.1f} {g['worst2_sum']:>8.1f} {tail:>7}"
        )

    # 预注册口径 vs 稳健性口径 结论对照
    verdicts = {}
    for d in ("LONG", "SHORT"):
        b, p = out["groups"].get(f"{d}|blocked", {}), out["groups"].get(f"{d}|pass", {})
        if not b.get("n_eval") or not p.get("n_eval"):
            verdicts[d] = "insufficient"
            continue
        mean_gap = b["mean"] - p["mean"]            # 预注册: blocked 期望更差 → 负
        med_gap = b["median"] - p["median"]          # 稳健: 中位也差 → 负
        if mean_gap < 0 and med_gap < 0:
            verdicts[d] = "DIRECTIONAL (期望与中位均更差 → 支持全拦)"
        elif mean_gap < 0 <= med_gap:
            verdicts[d] = ("TAIL_ONLY (中位不差、尾部拖累 → 全拦会误杀多数, "
                           "考虑尾部形态子规则/降杠杆而非一刀切)")
        else:
            verdicts[d] = "NO_EDGE (被拦组不更差 → 门无效)"
        verdicts[d] = f"{verdicts[d]} [mean_gap={mean_gap:+.1f} median_gap={med_gap:+.1f}]"
    out["verdict_by_direction"] = verdicts
    lines.append("  ── 双视角结论 ──")
    for d, v in verdicts.items():
        lines.append(f"  {d}: {v}")
    lines.append("  注: 均值口径=RFC-072 冻结判据; 中位/尾部分解=稳健性检查, 裁决时二者并呈。")

    OUT_LATEST.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return lines, out


def _push(msg):
    try:
        from app.notifier.feishu import send_text
        send_text(msg)
    except Exception as e:
        # R5 留痕: 推送失败不得静默 (测量证据链断裂风险)
        print(f"[kr3-report] 飞书推送失败 (不影响判分落盘): {e}", file=sys.stderr)


def main():
    lines, _ = build_report()
    print("\n".join(lines))
    print(f"\n→ latest: {OUT_LATEST}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="KR3 方向×涨幅证伪门判分报告 (双口径)")
    ap.add_argument("--push", action="store_true", help="推送飞书摘要")
    a = ap.parse_args()
    main()
    if a.push:
        _push("\n".join(build_report()[0]))
