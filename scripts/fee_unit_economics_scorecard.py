#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Fee Unit-Economics Scorecard — 费用单位经济学记分牌 (只读)

立项 (2026-08-20, DEB-20260820-architecture-quant-dual-audit P0-3, 白名单·修尺子):
  干净窗口实测 (n=257): 毛 -1210.92 / 费 -1142.71 / 净 -2353.63,
  平均名义 $3,717 → 费用 ≈ 12.0bps 往返, 占净亏损 49%。
  结论: **毛 edge 即使修到 0, 仍净亏 ~12bps** — 费用结构是最大确定性亏损源。
  本记分牌把单位经济学固化成每周可对比的数字, 为终裁后"名义下限/降频"决策攒尺子。

回答的问题:
  1. 每笔费用占名义多少 bps? 分布如何?
  2. 名义分位与单位经济学的关系 (客单价越小 fee_bps 越高)?
  3. 有多少笔的毛利 bps 跑不过费用 bps (单笔层面即注定亏)?
  4. 费用占净亏损的比例趋势 (本周 vs 窗口基线 49%)?
  5. 个股盘感台账 (DEB-20260820 第 7 轮): 每标的净口径 n/胜率/费用强度,
     红线 = 全部 n<30 只观察不结论 (个股级样本天然不足, 算术盘感可读,
     统计盘感必须攒到 n≥30 才允许进门柱)。

口径 (与 exit_scorecard.py 对齐):
  - source_type 白名单 IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE','PLAYBOOK_SHADOW','MANUAL_PLAYBOOK')
  - 已平仓 only (exit_time 非空); $ SSOT = trade_records (不用 instructions.pnl)
  - fee = realized_pnl - pnl_net; bps 基准 = entry_price * quantity

用法:
  python scripts/fee_unit_economics_scorecard.py                  # 默认 since 2026-07-11
  python scripts/fee_unit_economics_scorecard.py --since 2026-08-01
输出:
  data/fee_unit_economics_latest.json (+ _history.jsonl 追加)
零行为影响: 纯只读 (DB 以 mode=ro 打开)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_ROOT)

DB_PATH = f"{PROJECT_ROOT}/data/ftseek_us.db"
OUT_LATEST = f"{PROJECT_ROOT}/data/fee_unit_economics_latest.json"
OUT_HISTORY = f"{PROJECT_ROOT}/data/fee_unit_economics_history.jsonl"

# 与 exit_scorecard.py / okr_status.py 同口径
PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')


def _percentile(sorted_xs, p):
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_xs) - 1)
    frac = k - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(description="费用单位经济学记分牌 (只读)")
    ap.add_argument("--since", default="2026-07-11", help="窗口起始 trade_date")
    args = ap.parse_args()

    ph = ",".join("?" * len(PLAYBOOK_SOURCE_TYPES))
    uri = Path(DB_PATH).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT symbol, realized_pnl, pnl_net, entry_price, quantity
            FROM trade_records
            WHERE source_type IN ({ph})
              AND trade_date >= ?
              AND exit_time IS NOT NULL AND exit_time != ''
              AND entry_price > 0 AND quantity > 0""",
        (*PLAYBOOK_SOURCE_TYPES, args.since),
    ).fetchall()
    # 未平仓单单独计数 (realized_pnl=0 不入单位经济学, 但需可解释 n 差异)
    n_open = conn.execute(
        f"""SELECT COUNT(*) FROM trade_records
            WHERE source_type IN ({ph}) AND trade_date >= ?
              AND (exit_time IS NULL OR exit_time = '')""",
        (*PLAYBOOK_SOURCE_TYPES, args.since),
    ).fetchone()[0]
    conn.close()

    if not rows:
        print("[fee_unit_economics] 窗口内无样本, 不产出。")
        return

    trades = []
    for r in rows:
        notional = float(r["entry_price"]) * float(r["quantity"])
        gross = float(r["realized_pnl"] or 0.0)
        net = float(r["pnl_net"] if r["pnl_net"] is not None else r["realized_pnl"])
        fee = gross - net
        trades.append({
            "symbol": r["symbol"],
            "notional": notional,
            "gross": gross,
            "net": net,
            "fee": fee,
            "gross_bps": gross / notional * 10000.0,
            "fee_bps": fee / notional * 10000.0,
        })

    n = len(trades)
    tot_gross = sum(t["gross"] for t in trades)
    tot_fee = sum(t["fee"] for t in trades)
    tot_net = sum(t["net"] for t in trades)
    tot_notional = sum(t["notional"] for t in trades)

    # 单笔层面: 毛利跑不赢费用的占比 (结构性注定亏的单)
    n_fee_dominant = sum(1 for t in trades if t["fee"] > 0 and t["gross"] < t["fee"])
    # 费用强度分布
    fee_bps_sorted = sorted(t["fee_bps"] for t in trades)
    notional_sorted = sorted(t["notional"] for t in trades)

    # 名义分位 × 单位经济学 (证明客单价-费用关系)
    quintiles = []
    for qi in range(5):
        lo = _percentile(notional_sorted, qi / 5.0)
        hi = _percentile(notional_sorted, (qi + 1) / 5.0)
        if qi < 4:
            seg = [t for t in trades if lo <= t["notional"] < hi] if qi else \
                  [t for t in trades if t["notional"] < hi]
        else:
            seg = [t for t in trades if t["notional"] >= lo]
        if not seg:
            continue
        seg_net = sum(t["net"] for t in seg)
        quintiles.append({
            "q": qi + 1,
            "n": len(seg),
            "notional_range": [round(lo, 0), round(hi, 0)],
            "avg_notional": round(_mean([t["notional"] for t in seg]), 0),
            "avg_fee_bps": round(_mean([t["fee_bps"] for t in seg]), 1),
            "avg_gross_bps": round(_mean([t["gross_bps"] for t in seg]), 1),
            "avg_net_per_trade": round(_mean([t["net"] for t in seg]), 2),
            "net_total": round(seg_net, 2),
            # n<30 机器断言 (DEB-20260820 Qoder攻击点3, 只读打标不拦行为):
            # 七律⑤ 子格 n<30 的读数只能观察、不得驱动参数变更或写进结论。
            "n_lt_30_flag": len(seg) < 30,
            # 单笔稳健性 (DEB-20260820 复查新发现): 去掉最大单笔后的净额,
            # 防止 "Q5 唯一毛利正分位" 类结论被单笔驱动、终裁时被误引。
            "net_total_excl_max_trade": round(seg_net - max(t["net"] for t in seg), 2),
        })

    # 费用强度分档
    bands = {"<8bps": [], "8-15bps": [], ">=15bps": []}
    for t in trades:
        if t["fee_bps"] < 8:
            bands["<8bps"].append(t)
        elif t["fee_bps"] < 15:
            bands["8-15bps"].append(t)
        else:
            bands[">=15bps"].append(t)
    band_rows = {
        k: {
            "n": len(v),
            "share_pct": round(100.0 * len(v) / n, 1),
            "net_total": round(sum(t["net"] for t in v), 2),
            "avg_gross_bps": round(_mean([t["gross_bps"] for t in v]), 1) if v else None,
            "n_lt_30_flag": len(v) < 30,
        }
        for k, v in bands.items()
    }

    # 个股盘感台账 (DEB-20260820 第 7 轮立项): 每标的净口径单位经济学。
    # 红线: 个股级样本天然稀薄 (87 标的/268 笔, 均 3.1 笔), 全部 n_lt_30_flag=true,
    # 读数只作观察与攒样本, 不得驱动任何参数变更 (七律⑤)。
    by_symbol_map = {}
    for t in trades:
        by_symbol_map.setdefault(t["symbol"], []).append(t)
    symbol_rows = []
    for sym, seg in by_symbol_map.items():
        seg_net = sum(t["net"] for t in seg)
        wins = sum(1 for t in seg if t["net"] > 0)
        symbol_rows.append({
            "symbol": sym,
            "n": len(seg),
            "win_rate_net_pct": round(100.0 * wins / len(seg), 1),
            "avg_notional": round(_mean([t["notional"] for t in seg]), 0),
            "avg_fee_bps": round(_mean([t["fee_bps"] for t in seg]), 1),
            "avg_net_per_trade": round(_mean([t["net"] for t in seg]), 2),
            "net_total": round(seg_net, 2),
            "net_total_excl_max_trade": round(seg_net - max(t["net"] for t in seg), 2),
            "n_lt_30_flag": len(seg) < 30,
        })
    symbol_rows.sort(key=lambda r: (-r["n"], r["net_total"]))

    rep = {
        "window_since": args.since,
        "n": n,
        "open_holdings_excluded": n_open,
        "total_notional": round(tot_notional, 0),
        "avg_notional": round(tot_notional / n, 0),
        "gross_total": round(tot_gross, 2),
        "fee_total": round(tot_fee, 2),
        "net_total": round(tot_net, 2),
        "gross_bps_avg": round(tot_gross / tot_notional * 10000.0, 1),
        "fee_bps_avg": round(tot_fee / tot_notional * 10000.0, 1),
        "net_bps_avg": round(tot_net / tot_notional * 10000.0, 1),
        "fee_share_of_net_loss_pct": round(100.0 * tot_fee / abs(tot_net), 1) if tot_net < 0 else None,
        "fee_dominant_trades": {"n": n_fee_dominant, "share_pct": round(100.0 * n_fee_dominant / n, 1)},
        "fee_bps_percentiles": {
            "p25": round(_percentile(fee_bps_sorted, 0.25), 1),
            "p50": round(_percentile(fee_bps_sorted, 0.50), 1),
            "p75": round(_percentile(fee_bps_sorted, 0.75), 1),
        },
        "by_notional_quintile": quintiles,
        "by_fee_band": band_rows,
        "by_symbol": {
            "symbols_total": len(symbol_rows),
            "trades_per_symbol_avg": round(n / len(symbol_rows), 1),
            "top_by_n": symbol_rows,
            "interpretation": (
                "个股盘感台账: 全部标的 n<30, 读数只观察不结论 (七律⑤); "
                "win_rate_net_pct 为净口径; avg_fee_bps 是该标的的算术盘感 "
                "(确定性费用结构, 可直接读); net_total_excl_max_trade 防单笔驱动的伪盘感。"
            ),
        },
        "interpretation": (
            "fee_bps_avg = 每笔往返费用占名义 bps; 若 gross_bps_avg 的绝对值 < fee_bps_avg, "
            "则即使方向预测打平也必亏。名义分位表展示客单价与费用强度的反比关系, "
            "为终裁后名义下限决策提供尺子。"
            "纪律标: n_lt_30_flag=true 的分位只作观察不作结论 (七律⑤); "
            "net_total_excl_max_trade 用于识别单笔驱动的伪正分位。"
        ),
    }

    Path(OUT_LATEST).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_LATEST, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    with open(OUT_HISTORY, "a") as f:
        f.write(json.dumps(rep, ensure_ascii=False) + "\n")

    # 人类可读输出
    print("=" * 72)
    print(f"[fee_unit_economics] 窗口 since={args.since}  n={n}  总名义=${tot_notional:,.0f}")
    print(f"  毛: ${tot_gross:>9,.2f} ({rep['gross_bps_avg']:>6.1f} bps)")
    print(f"  费: ${-tot_fee:>9,.2f} ({rep['fee_bps_avg']:>6.1f} bps)")
    print(f"  净: ${tot_net:>9,.2f} ({rep['net_bps_avg']:>6.1f} bps)")
    if rep["fee_share_of_net_loss_pct"]:
        print(f"  费用占净亏损: {rep['fee_share_of_net_loss_pct']}%")
    print(f"  毛利<费用 的单: {n_fee_dominant}/{n} ({rep['fee_dominant_trades']['share_pct']}%)")
    print(f"  fee_bps p25/p50/p75: "
          f"{rep['fee_bps_percentiles']['p25']}/{rep['fee_bps_percentiles']['p50']}/{rep['fee_bps_percentiles']['p75']}")
    print("-" * 72)
    print("  名义分位 × 单位经济学:")
    for q in quintiles:
        flag = " ⚠️n<30只观察" if q.get("n_lt_30_flag") else ""
        excl = q.get("net_total_excl_max_trade")
        excl_s = f" [去最大单笔 ${excl:>9.2f}]" if excl is not None else ""
        print(f"    Q{q['q']} n={q['n']:>3} 名义~${q['avg_notional']:>7,.0f} "
              f"fee {q['avg_fee_bps']:>5.1f}bps gross {q['avg_gross_bps']:>6.1f}bps "
              f"净/笔 ${q['avg_net_per_trade']:>7.2f} (合计 ${q['net_total']:>9.2f}){excl_s}{flag}")
    print("-" * 72)
    print("  费用强度分档:")
    for k, v in band_rows.items():
        print(f"    {k:>8}: n={v['n']:>3} ({v['share_pct']:>5.1f}%) 净 ${v['net_total']:>9.2f} "
              f"均毛利 {v['avg_gross_bps']}bps")
    print("-" * 72)
    print(f"  个股盘感台账 ({len(symbol_rows)} 标的, 均 {n / len(symbol_rows):.1f} 笔/标的, "
          f"全部 n<30 只观察 — 按 n 排序前 15):")
    for s in symbol_rows[:15]:
        flag = " ⚠️n<30只观察" if s["n_lt_30_flag"] else ""
        excl_s = f" [去最大单笔 ${s['net_total_excl_max_trade']:>8.2f}]"
        print(f"    {s['symbol']:>8} n={s['n']:>3} 胜率(净) {s['win_rate_net_pct']:>5.1f}% "
              f"名义~${s['avg_notional']:>7,.0f} fee {s['avg_fee_bps']:>5.1f}bps "
              f"净/笔 ${s['avg_net_per_trade']:>7.2f} (合计 ${s['net_total']:>9.2f}){excl_s}{flag}")
    print(f"  → {OUT_LATEST}")
    print("=" * 72)


if __name__ == "__main__":
    main()
