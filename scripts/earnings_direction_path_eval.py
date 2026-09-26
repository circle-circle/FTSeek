#!/usr/bin/env python3
"""eth_earnings_direction_path 判据评估器 (只读, 零下单, 零写盘).

## 为什么单独一个 (2026-09-26, RFC-109④-D)
原 eval_script 指向 `scripts/earnings_direction_check.py` — 那是 RFC-099 的**一次性判据实证**
脚本 (2026-09-23 为回答用户「财报日如何选多空」而写), 它读的是
`/root/.hermes/cache/scratch/replay_earnings_criterion_final.json` (118 条回放产物)。
两个结构性问题:
  ① 该 scratch 目录 **72h 自动清理** ⇒ 产物已不存在 (实测 FileNotFoundError) ⇒ eval 结构性不可重跑;
  ② 脚本**无 main() 保护** (顶层直接执行) ⇒ import 即跑, 不属可复用判据评估器;
  ③ 它读的 118 条回放与本影子的入库产物 (`data/earnings_direction_path_shadow.jsonl`)
     **不是同一个数据源** ⇒ 就算能跑, 也不是在评这条影子。
本器改读**入库产物** (cron `20 4 * * 2-6` 每日攒样), 可无限重跑。

## 判据 (预注册, 逐字抄录, 读数前不得改)
攒样器 `scripts/earnings_direction_path_shadow.py` 头部预注册:
  · 计划方向分档 (RFC-099 §4): gap ≥ +3.0% ⇒ SHORT / +0.5% ≤ gap ≤ +2.0% ⇒ LONG / 其它 ⇒ NONE
  · **判据读数门槛 (转正需另立 RFC): 每方向 ≥30 笔 且 每方向净/笔 > 0**
注册表 graduation_criteria: 相位标定覆盖率稳定 + 分档方向一致性成立 ⇒ 提案转正;
  与 RFC-099 §5 判据逐条对账。

## ⚠️ 计数轴错配 (诚实披露, 不擅改)
注册表 `min_n=30` 数的是 **jsonl 总行数**, 而预注册门槛是 **每方向 ≥30 笔** ⇒ 两个不同轴。
实测 (2026-09-26): 总行数 32 (≥30 ⇒ 被 refresh_counts 机械提升 READY_TO_EVALUATE),
但按方向拆: SHORT 10 笔 / LONG 2 笔 ⇒ **两方向都远未达 30**。
本器如实分方向报 n, 并明确标注「总行数达标 ≠ 判据达标」; 不改 min_n (改判据门槛=重裁, 须用户裁)。

## 副作用
无。只读 jsonl, 只打印到 stdout: **不推送 / 不写库 / 不下单 / 不写任何文件 / 不改 crontab**。
挂载方式: eval_on_demand=True (按需评估, 不挂 cron) — 同 sltp_verify_shadow / iv_hv 先例。

用法: PYTHONPATH=. venv/bin/python scripts/earnings_direction_path_eval.py
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "earnings_direction_path_shadow.jsonl"

FEE_PCT_FALLBACK = 0.53  # 攒样器预注册: 每 $5,000 费 0.53% (产物内有 fee_pct 则用产物值)

# RFC-099 §4 预注册分档 (读数前写死, 不得改)
def band_of(gap_pct):
    """按 RFC-099 §4 预注册分档返回 (应标定方向, 档名)。"""
    if not isinstance(gap_pct, (int, float)):
        return (None, "无gap")
    if gap_pct >= 3.0:
        return ("SHORT", "gap>=+3%")
    if 0.5 <= gap_pct <= 2.0:
        return ("LONG", "+0.5~+2%")
    return (None, "其它(NONE)")

# 预注册门槛 (逐字: 每方向 >=30 笔 且 每方向净/笔 > 0)
MIN_N_PER_DIR = 30


def main(argv=None) -> int:
    if not DATA.exists():
        print(f"[ern_dir_eval] 产物不存在: {DATA}")
        print("  ⇒ 无法评估。注意: 产物缺失不等于断供 — 先查 cron `20 4 * * 2-6` 与攒样器日志。")
        return 1

    rows = []
    for ln in DATA.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue

    if not rows:
        print(f"[ern_dir_eval] 产物为空: {DATA}")
        return 1

    dates = sorted({r.get("date") for r in rows if r.get("date")})
    print(f"[ern_dir_eval] n={len(rows)} 行 | 日期 {dates[0]}~{dates[-1]} ({len(dates)} 个 distinct 日) "
          f"| symbol {len({r.get('symbol') for r in rows})}")

    for r in rows:
        want, label = band_of(r.get("gap_pct"))
        r["_want"], r["_band"] = want, label

    # ── ① 相位标定覆盖率 (plan_dir 是否与 §4 预注册分档一致) ──
    print("\n── ① 相位标定覆盖率 (plan_dir vs RFC-099 §4 预注册分档) ──")
    total_should = total_hit = 0
    for label in ("gap>=+3%", "+0.5~+2%", "其它(NONE)", "无gap"):
        g = [r for r in rows if r["_band"] == label]
        if not g:
            continue
        want = g[0]["_want"] if g[0]["_want"] else "NONE"
        got = Counter(r.get("plan_dir") for r in g)
        hit = got.get(want, 0)
        total_should += len(g)
        total_hit += hit
        print(f"  {label:12s} n={len(g):3d} 应={want:5s} 实际={dict(got)} ⇒ 标定正确 {hit}/{len(g)}")
    cov = (100 * total_hit / total_should) if total_should else 0.0
    print(f"  合计标定正确率 = {total_hit}/{total_should} = {cov:.1f}%")
    print(f"  「覆盖率稳定」判定: distinct 日期 {len(dates)} 天 "
          f"⇒ {'✗ 天数不足 15, 无法判「稳定」(只是单批一致)' if len(dates) < 15 else '✓ 天数够'}")

    # ── ② 分档方向一致性 (每方向 n + 扣费后净/笔) ──
    print("\n── ② 分档方向一致性 (预注册门槛: 每方向 >=30 笔 且 每方向净/笔 > 0) ──")
    print(f"  {'方向':6s} {'n':>4s} {'门槛':>5s} {'n达标':>6s} {'gross中位':>10s} {'净中位':>8s} "
          f"{'净均值':>8s} {'净>0':>5s} {'胜率':>5s}")
    dir_verdicts = {}
    for d in ("SHORT", "LONG"):
        g = [r for r in rows if r["_want"] == d]
        if not g:
            print(f"  {d:6s} n=0 ⇒ 无样本")
            dir_verdicts[d] = (False, 0, None)
            continue
        key = "short_ret_pct" if d == "SHORT" else "long_ret_pct"
        fee = next((r.get("fee_pct") for r in g if isinstance(r.get("fee_pct"), (int, float))),
                   FEE_PCT_FALLBACK)
        gross = [r[key] for r in g if isinstance(r.get(key), (int, float))]
        nets = [x - fee for x in gross]
        if not nets:
            print(f"  {d:6s} 无可结算收益")
            dir_verdicts[d] = (False, 0, None)
            continue
        wins = sum(1 for x in nets if x > 0)
        n_ok = len(nets) >= MIN_N_PER_DIR
        net_ok = mean(nets) > 0
        dir_verdicts[d] = (n_ok and net_ok, len(nets), mean(nets))
        print(f"  {d:6s} {len(nets):4d} {MIN_N_PER_DIR:5d} {'✓' if n_ok else '✗':>6s} "
              f"{median(gross):+10.2f} {median(nets):+8.2f} {mean(nets):+8.2f} "
              f"{'✓' if net_ok else '✗':>5s} {100*wins/len(nets):4.0f}%")

    # ── ③ 计数轴错配披露 ──
    print("\n── ③ 计数轴披露 (不擅改, 如实呈) ──")
    n_rows = len(rows)
    print(f"  注册表 min_n=30 数的是 jsonl **总行数** = {n_rows} ⇒ {'达标' if n_rows >= 30 else '未达标'}")
    print(f"  预注册门槛数的是 **每方向笔数**: "
          f"SHORT={dir_verdicts.get('SHORT', (None, 0))[1]} / LONG={dir_verdicts.get('LONG', (None, 0))[1]}")
    if n_rows >= MIN_N_PER_DIR and not all(v[0] for v in dir_verdicts.values()):
        print(f"  ⚠️ 总行数达标但**每方向均未达 {MIN_N_PER_DIR}** ⇒ READY_TO_EVALUATE 是机械提升"
              f" (refresh_counts 按总行数), **不代表判据达标**。")

    # ── ④ 判决 ──
    print("\n── ④ 判决 ──")
    both_pass = all(v[0] for v in dir_verdicts.values())
    if both_pass and len(dates) >= 15:
        print("  ✅ 判据全过 (每方向 n>=30 ∧ 净/笔>0 ∧ 覆盖率天数足) ⇒ 可另立 RFC 提请转正。")
        print("     注意: 转正须另立 RFC (预注册原文要求), 本器只出读数不改任何生产参数。")
    else:
        why = []
        for d, (ok, n, mn) in dir_verdicts.items():
            if not ok:
                if n < MIN_N_PER_DIR:
                    why.append(f"{d} n={n}<{MIN_N_PER_DIR}")
                elif mn is not None and mn <= 0:
                    why.append(f"{d} 净/笔={mn:+.2f}<=0")
        if len(dates) < 15:
            why.append(f"覆盖率天数={len(dates)}<15")
        print("  ✗ 判据未过 ⇒ **维持影子攒样, 不提案转正, 生产参数不动**。未过项: " + "; ".join(why))
    print("\n  (本器 record-only: 不推送/不写库/不写盘/不下单/不改 crontab)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
