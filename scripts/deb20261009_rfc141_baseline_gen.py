#!/usr/bin/env python3
"""RFC-141 判据基线生成器 (只读 DB) — 产出 BASELINE 的 Python 字面量。

为什么需要它: 基线是判据①②④ 的全部依据, 手写/口算 = 自证陷阱。
本脚本用**结算器自己的** `_pnl_block` / `_day_clustered_t` / `fee_bps` /
`is_fee_anomaly` 计算 ⇒ 「生成基线的尺子」与「裁决用的尺子」是同一把
(两把尺子 = 错相位对比 = 死门形态; DEB-20261009 §33.4 已踩过 pstdev/stdev 坑)。

用法:
    PYTHONPATH=. ./venv/bin/python scripts/deb20261009_rfc141_baseline_gen.py \
        [--since 2026-08-01] [--until 2026-10-10]

窗口: trade_date ∈ [since, until) —— until 默认今天, 即「开门前」。
只读: DB 以 mode=ro 打开, 不写任何文件 (字面量走 stdout, 由人粘贴进结算器)。
"""
from __future__ import annotations
import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import settle_entry_confirm_forward as st  # noqa: E402

ANOMALY_PRINT_LIMIT = 8


def _blk(rows, pred=None):
    """用结算器的 _pnl_block (同一把尺子), 返回可直接进 BASELINE 的 dict。"""
    b = st._pnl_block(rows, pred)
    return {k: b[k] for k in ("n", "net_sum", "net_mean", "days", "t", "win_rate")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01",
                    help="基线窗起点 (用户 2026-10-10 裁: 从 8 月起统计, 早期策略不成熟)")
    ap.add_argument("--until", default=None,
                    help="基线窗终点 (不含), 默认今天 = 开门前")
    args = ap.parse_args()

    until = args.until or datetime.date.today().isoformat()
    rows = [r for r in st.load_sim_pnl(args.since) if r["trade_date"] < until]
    if not rows:
        print(f"❌ 窗口 [{args.since}, {until}) 无样本", file=sys.stderr)
        return 1

    is_sl = lambda r: (r.get("exit_reason") or "") == "SL_INITIAL"      # noqa: E731
    not_sl = lambda r: (r.get("exit_reason") or "") != "SL_INITIAL"     # noqa: E731
    clean = lambda r: not_sl(r) and not st.is_fee_anomaly(r)            # noqa: E731
    in_win = lambda r: st._in_open_win(r.get("entry_time"))             # noqa: E731

    b_all = _blk(rows)
    b_sl = _blk(rows, is_sl)
    b_rest = _blk(rows, not_sl)
    b_rest_clean = _blk(rows, clean)
    b_win = _blk(rows, in_win)
    dropped = [r for r in rows if not_sl(r) and st.is_fee_anomaly(r)]

    baseline = {
        "all": b_all,
        "open_win": b_win,
        "sl_initial": b_sl,
        "rest": b_rest,
        "rest_clean": {
            **{k: b_rest_clean[k] for k in ("n", "net_sum", "net_mean")},
            "fee_bps_anomaly_dropped": len(dropped),
            "fee_bps_threshold": st.FEE_BPS_ANOMALY,
        },
    }

    sl_mean = b_sl["net_mean"] or 0.0
    ratio = (b_sl["net_sum"] / b_all["net_sum"]) if b_all["net_sum"] else None

    print(f"# 窗口 [{args.since}, {until})  n={len(rows)}  "
          f"distinct日={b_all['days']}")
    print(f"# 生成于 {datetime.datetime.now().isoformat(timespec='seconds')}")
    print("# 尺子 = settle_entry_confirm_forward._pnl_block / _day_clustered_t "
          "(statistics.stdev, ddof=1)")
    print(f"# 剔费笔数 = {len(dropped)} (fee_bps > {st.FEE_BPS_ANOMALY})")
    print("# 键序注释: all=总面 / open_win=判据② / sl_initial=判据① / "
          "rest=判据④全量 / rest_clean=判据④裁决口径")
    print("BASELINE = " + json.dumps(baseline, indent=4, ensure_ascii=False))
    print()
    print("# ── 派生阈值 (供人工核对; 改判据=重新裁决, 不得静默改) ──")
    print(f"# 判据① SL_INIT_MAX_LOSS_PER_TRADE 现值 = {st.SL_INIT_MAX_LOSS_PER_TRADE}")
    print(f"#        本窗 sl_initial.net_mean × 25% = {round(sl_mean * 0.25, 2)}")
    print(f"# 判据④ 全量地板 = {round((b_rest['net_mean'] or 0) * 0.8, 2)}   "
          f"(rest.net_mean {b_rest['net_mean']} × 0.8)")
    print(f"#        剔费地板 = {round((b_rest_clean['net_mean'] or 0) * 0.8, 2)}   "
          f"(rest_clean.net_mean {b_rest_clean['net_mean']} × 0.8) ← 裁决口径")
    print(f"# 判据② open_win: net_mean {b_win['net_mean']} / t {b_win['t']}")
    print()
    print(f"# 病灶倍数 sl_initial.net_sum / all.net_sum = "
          f"{round(ratio, 4) if ratio is not None else 'NA (all.net_sum≈0)'}")
    print(f"# SL_INITIAL 胜率 = {b_sl['win_rate']} (n={b_sl['n']})")
    print()
    print("# 被剔的费用异常单 (逐笔, 防「剔多了/剔错了」):")
    for r in sorted(dropped, key=lambda x: -(st.fee_bps(x) or 0))[:ANOMALY_PRINT_LIMIT]:
        print(f"#   {r['trade_date']} {str(r['symbol']):12s} "
              f"fee_bps={st.fee_bps(r):7.1f} net={r['net']:+9.2f} "
              f"exit={r.get('exit_reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
