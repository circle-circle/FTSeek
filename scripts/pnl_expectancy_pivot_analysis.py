#!/usr/bin/env python3
"""净期望/降频 Pivot 只读分析器（DEB-20261114-weekly-pnl-diagnosis §4 方案A 证据面）。

只读承诺：sqlite `mode=ro`，不写库、不下单、不改任何文件、不注册新影子。
默认仅控制台输出；`--json PATH` 时才写一份只读分析产物。

为什么需要它（2026-11-14 诊断）：
  本周 DA 毛期望 −2.51bps、费 9.63bps ⇒ 净 −12.15bps；全书毛 +1.85bps 被费 9.57bps 吞没。
  失血口不是「选股坏」而是「毛边太薄盖不住费」+「入场即错死单（MFE<0.3%）纯烧费」。
  入场/出场门已被 C8/C9/C12/C14/C15/C16/C17 终局关闭；OKR-2026-11 §6 授权的突破路径是
  Pivot（降频/换策略/承认无 edge）。本工具量化「降频」杠杆：按日内执行序取前 N 笔的反事实。

口径：A9 干净（exit_price>0 ∧ pnl_net 非空 ∧ exclusion_reason 空）；bps=pnl_net/(entry_price×qty)×1e4；
  跨日推断按 trade_date 日聚类（宪章第 9 律）。降频反事实=每日按 entry_time 排序取前 N 笔，
  对比「不限频」的笔均净 bps 与日均净 $。**这是探索性读数，不是裁决**：n<30 日不下结论，
  转正须前瞻攒样 + 预注册日聚类 CI（见 DEB §4/§5）。
样本分层（2026-11-14 修尺子）：headline=真实信号期望；收养/补录记账行（MFE/peak/regime
  为收养时近似值）单列 artifact，全样本总账 decomposition_all 保留不抹账。降频只使用成交
  顺序与真实 PnL，故保留恢复补录行，仅排除补偿立即平仓和人工收养，避免删掉真实交易。
  详见 scripts/utils/da_sample.py。

用法：PYTHONPATH=. venv/bin/python scripts/pnl_expectancy_pivot_analysis.py \
        --start 2026-10-06 --source playbook_sim --max-cap 6
"""
import argparse
import json
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

from scripts.utils.da_sample import split_frequency_population, split_signal_artifact

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"


def _connect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn, start, source):
    rows = [dict(r) for r in conn.execute(
        """
        SELECT symbol, trade_date, entry_time, entry_price, quantity, pnl_net, realized_pnl,
               fees_est, mfe_pct, owner_strategy, entry_session, market_regime, exit_reason, entry_reason
          FROM trade_records
         WHERE source_type='PLAYBOOK_SIM' AND owner_strategy=?
           AND trade_date>=? AND exit_price>0
           AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
           AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''
         ORDER BY trade_date, entry_time
        """, (source, start)).fetchall()]
    return rows


def _net(row):
    if row["pnl_net"] is not None:
        return row["pnl_net"]
    # realized_pnl 是毛盈亏；回退必须扣费，不能把毛额当净额（项目净 PnL 约定）。
    return (row["realized_pnl"] or 0.0) - (row.get("fees_est") or 0.0)


def _notional(row):
    return (row["entry_price"] or 0.0) * (row["quantity"] or 0)


def _bps(row):
    denom = _notional(row)
    return _net(row) / denom * 1e4 if denom else 0.0


def _clustered(daily_values):
    """daily_values: list of per-day means. 返回 (mean, sd, days, t)。"""
    if len(daily_values) < 2:
        return (sum(daily_values) / len(daily_values) if daily_values else 0.0, 0.0, len(daily_values), 0.0)
    mean = sum(daily_values) / len(daily_values)
    sd = st.stdev(daily_values)
    t = mean / (sd / len(daily_values) ** 0.5) if sd else 0.0
    return mean, sd, len(daily_values), t


def decompose(rows):
    notional = sum(_notional(r) for r in rows)
    fees = sum(r["fees_est"] or 0.0 for r in rows)
    net = sum(_net(r) for r in rows)
    gross = net + fees
    scale = 1e4 / notional if notional else 0.0
    dead = [r for r in rows if (r["mfe_pct"] or 0.0) < 0.3]
    live = [r for r in rows if (r["mfe_pct"] or 0.0) >= 0.3]
    return {
        "n": len(rows),
        "notional": round(notional, 2),
        "gross_usd": round(gross, 2), "gross_bps": round(gross * scale, 2),
        "fees_usd": round(fees, 2), "fees_bps": round(fees * scale, 2),
        "net_usd": round(net, 2), "net_bps": round(net * scale, 2),
        "dead_n": len(dead), "dead_rate_pct": round(100 * len(dead) / len(rows), 1) if rows else 0.0,
        "dead_net_usd": round(sum(_net(r) for r in dead), 2),
        "live_net_usd": round(sum(_net(r) for r in live), 2),
    }


def frequency_cap(rows, max_cap):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["trade_date"]].append(row)
    uncapped_day_bps = [sum(_bps(r) for r in v) / len(v) for v in by_day.values()]
    uncapped_day_usd = [sum(_net(r) for r in v) for v in by_day.values()]
    um, us, ud, ut = _clustered(uncapped_day_bps)
    out = {"uncapped": {"day_mean_bps": round(um, 2), "day_sd": round(us, 2),
                        "days": ud, "t": round(ut, 2),
                        "day_mean_usd": round(sum(uncapped_day_usd) / len(uncapped_day_usd), 2)}}
    caps = []
    for cap in range(1, max_cap + 1):
        diff_bps, cap_day_usd, kept = [], [], 0
        for day, trades in by_day.items():
            taken = trades[:cap]
            kept += len(taken)
            cap_mean = sum(_bps(r) for r in taken) / len(taken)
            unc_mean = sum(_bps(r) for r in trades) / len(trades)
            diff_bps.append(cap_mean - unc_mean)
            cap_day_usd.append(sum(_net(r) for r in taken))
        dm, ds, dd, dt = _clustered(diff_bps)
        caps.append({
            "cap": cap, "kept_trades": kept,
            "cap_day_mean_bps": round(um + dm, 2),
            "delta_day_mean_bps": round(dm, 2), "delta_sd": round(ds, 2),
            "days": dd, "delta_t": round(dt, 2),
            "cap_day_mean_usd": round(sum(cap_day_usd) / len(cap_day_usd), 2),
        })
    out["caps"] = caps
    return out


def build_report(rows, max_cap):
    """headline=真实信号；artifact 单列；decomposition_all 保留全样本总账（分层≠抹账）。"""
    signal, artifact = split_signal_artifact(rows)
    frequency_rows, frequency_excluded = split_frequency_population(rows)
    return {
        "decomposition": decompose(signal),
        "decomposition_all": decompose(rows),
        "artifact": decompose(artifact),
        "frequency_cap": frequency_cap(frequency_rows, max_cap) if frequency_rows else None,
        "n_artifact_excluded": len(artifact),
        "n_frequency_excluded": len(frequency_excluded),
        "n_frequency_recovery_included": sum(
            (r.get("entry_reason") or "") == "sync_recovery_backfill" for r in frequency_rows),
    }


def main():
    parser = argparse.ArgumentParser(description="净期望/降频 Pivot 只读分析器")
    parser.add_argument("--start", default="2026-10-06")
    parser.add_argument("--source", default="playbook_sim",
                        choices=["playbook_sim", "extended_hours_entry"])
    parser.add_argument("--max-cap", type=int, default=6)
    parser.add_argument("--json", default=None, help="可选：写一份只读分析产物到该路径")
    args = parser.parse_args()

    conn = _connect()
    rows = _load(conn, args.start, args.source)
    if not rows:
        print("no rows for source=%s start=%s" % (args.source, args.start))
        return
    report = {
        "source": args.source, "start": args.start,
        **build_report(rows, args.max_cap),
        "note": "探索性读数，非裁决；n<30 日不下结论；转正须前瞻预注册日聚类 CI（DEB-20261114 §4/§5）。headline=真实信号，收养/补录单列；降频保留恢复补录真实PnL，仅排除补偿/人工收养。",
    }
    dec = report["decomposition"]
    print("== %s since %s | 真实信号 n=%d (剔除收养/补录 %d) ==" % (
        args.source, args.start, dec["n"], report["n_artifact_excluded"]))
    print("  GROSS %.2f bps | FEES %.2f bps | NET %.2f bps (net $%.2f)" % (
        dec["gross_bps"], dec["fees_bps"], dec["net_bps"], dec["net_usd"]))
    print("  dead-on-arrival(MFE<0.3%%): %d/%d=%.1f%% net $%.2f | live net $%.2f" % (
        dec["dead_n"], dec["n"], dec["dead_rate_pct"], dec["dead_net_usd"], dec["live_net_usd"]))
    art, alla = report["artifact"], report["decomposition_all"]
    print("  [分层] 收养/补录记账: n=%d net=$%.2f (%.2fbps) | 全样本总账 NET %.2fbps net=$%.2f" % (
        art["n"], art["net_usd"], art["net_bps"], alla["net_bps"], alla["net_usd"]))
    fc = report["frequency_cap"]
    if fc:
        print("  uncapped day-mean bps=%.2f t=%.2f days=%d | day-mean $=%.2f" % (
            fc["uncapped"]["day_mean_bps"], fc["uncapped"]["t"], fc["uncapped"]["days"],
            fc["uncapped"]["day_mean_usd"]))
        print("  cap | kept | capped bps | Δday-mean bps | Δt | cap day-mean $")
        for c in fc["caps"]:
            print("   %2d | %4d | %10.2f | %13.2f | %5.2f | %14.2f" % (
                c["cap"], c["kept_trades"], c["cap_day_mean_bps"],
                c["delta_day_mean_bps"], c["delta_t"], c["cap_day_mean_usd"]))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("wrote", args.json)


if __name__ == "__main__":
    main()
