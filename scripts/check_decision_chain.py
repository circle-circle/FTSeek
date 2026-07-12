#!/usr/bin/env python3
"""决策链路核对 — 双脑清理配套 (2026-07-11).

判别键 = strategy_id (执行/决策通道), 这是"并行入场泄漏"的真实信号。
decision_id 前缀仅作 playbook 内部溯源 (DA bridge / DEC 旧命名 / TFB 兜底)。

分桶:
  ✅ 双脑正路   : strategy_id=playbook_sim  → 再按 decision_id 拆 DA/DEC/TFB
  ⛔ 独立入场泄漏: strategy_id ∈ {orb_engine, scalping, voting_llm, technical,
                   insider, earnings_revision, momentum} (盘中独立脑; 应恒为 0)
  ○ 运维/手动  : manual*, auto_main, *_sync, *reconcile*, shadow, None

用法:
  ./venv/bin/python scripts/check_decision_chain.py            # 近 7 天
  ./venv/bin/python scripts/check_decision_chain.py 2026-07-13   # 指定单日
  ./venv/bin/python scripts/check_decision_chain.py --days 30    # 近 N 天
  ./venv/bin/python scripts/check_decision_chain.py --invalidation  # DA 失效条件覆盖率 (Stage 1)

判读: 干净单链路 = "独立入场泄漏"恒为 0; playbook_sim 内 DA/TFB 占比越高越好
      (DEC- 为 bridge 命名前的旧 playbook 决策, 会随时间被 DA- 取代, 非泄漏)。
"""
import sqlite3
import sys
from collections import defaultdict

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"

# 盘中独立入场脑 (双脑门应关掉的路径; 出现即泄漏)
LEAK_STRATEGIES = {
    "orb_engine", "orb", "scalping", "voting_llm", "technical",
    "insider", "earnings_revision", "erev", "momentum",
}
# 运维/对账/手动 (非决策脑)
HOUSEKEEPING = {
    "manual", "manual_sync", "auto_main", "futu_sync", "broker_reconcile",
    "reconciler", "playbook_shadow", None,
}


def classify(strategy_id, decision_id):
    sid = strategy_id
    if sid == "playbook_sim":
        d = (decision_id or "").upper()
        if d.startswith("DA-") or d.startswith("DA2"):
            return "✅ 双脑正路 · DA bridge"
        if d.startswith("TFB-"):
            return "✅ 双脑正路 · TFB 兜底"
        if d.startswith("DEC"):
            return "✅ 双脑正路 · DEC 旧命名"
        return "✅ 双脑正路 · 无溯源"
    if sid in LEAK_STRATEGIES:
        return f"⛔ 独立入场泄漏 · {sid}"
    if sid in HOUSEKEEPING:
        return f"○ 运维/手动 · {sid}"
    return f"? 未分类 · {sid}"


def check_invalidation_coverage(days: int = 7) -> None:
    """Stage 1 核对: DA 入场指令中带 invalidation 失效条件的覆盖率。

    干净目标: DA bridge 指令应 ~100% 带 invalidation (DA 缺席的 TFB 兜底除外)。
    invalidation 列若未迁移 (周一盘前首个 ensure_tables 前), 优雅提示而非报错。
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(playbook_instructions)")}
    print(f"\n{'='*66}\nDA 失效条件覆盖率 · 近 {days} 天 · playbook_instructions\n{'='*66}")
    if "invalidation" not in cols:
        print("invalidation 列尚未迁移 (下次盘前 ensure_tables 自动补) — 暂无数据。\n")
        return
    rows = conn.execute(
        "SELECT date, "
        "SUM(CASE WHEN direction!='SKIP' THEN 1 ELSE 0 END) AS entries, "
        "SUM(CASE WHEN direction!='SKIP' AND COALESCE(invalidation,'')!='' THEN 1 ELSE 0 END) AS with_inval "
        "FROM playbook_instructions WHERE date >= date('now', ?) "
        "GROUP BY date ORDER BY date",
        (f"-{days} day",),
    ).fetchall()
    if not rows:
        print("(近窗口无指令记录)\n")
        return
    tot_e = tot_i = 0
    for r in rows:
        e, i = int(r["entries"] or 0), int(r["with_inval"] or 0)
        tot_e += e
        tot_i += i
        pct = 100.0 * i / e if e else 0
        print(f"  {r['date']} | 入场指令 {e:3d} | 带 invalidation {i:3d} ({pct:4.0f}%)")
    pct = 100.0 * tot_i / tot_e if tot_e else 0
    print(f"{'-'*66}\n  合计: 入场 {tot_e} | 带失效条件 {tot_i} ({pct:.0f}%)")
    if tot_e and tot_i == 0:
        print("  ⚠ 覆盖率 0% — Stage 1 管道可能未生效, 或当日全走 TFB/Null 兜底。")
    print()


def check_calibration(owner: str = "playbook_sim") -> None:
    """校准闭环: DA 路径 confidence 分桶 vs 实际胜率/PnL。

    前提: signal_confidence 已量纲统一到 0-1 (update_signal_lifecycle._norm_conf)。
    健康的校准 = confidence 越高胜率越高 (单调); 走平/倒挂 = confidence 无预测力。
    度量用 pnl_pct (收益率%) 而非绝对 $, 避免不同仓位规模制度混淆小样本。
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print(f"\n{'='*66}\n校准闭环 · owner={owner} · signal_lifecycle\n{'='*66}")
    rows = conn.execute(
        "SELECT CASE WHEN signal_confidence<0.5 THEN '1 <50%' "
        "WHEN signal_confidence<0.65 THEN '2 50-65%' "
        "WHEN signal_confidence<0.8 THEN '3 65-80%' ELSE '4 80%+' END bucket, "
        "count(*) n, round(avg(trade_win)*100,1) win, round(avg(pnl_pct)*100,2) ret, "
        "round(avg(signal_confidence)*100,1) avg_conf "
        "FROM signal_lifecycle WHERE owner_strategy=? AND signal_confidence IS NOT NULL "
        "AND trade_win IS NOT NULL GROUP BY bucket ORDER BY bucket",
        (owner,),
    ).fetchall()
    if not rows:
        print("(无带结果的样本)\n")
        return
    tot = 0
    for r in rows:
        tot += r["n"]
        print(f"  conf {r['bucket']:9s} | 样本 {r['n']:3d} | 胜率 {r['win'] or 0:5.1f}% | 均收益 {r['ret'] or 0:+6.2f}% | 均conf {r['avg_conf'] or 0:.0f}%")
    ov = conn.execute(
        "SELECT round(avg(trade_win)*100,1) w, round(avg(pnl_pct)*100,2) r "
        "FROM signal_lifecycle WHERE owner_strategy=? AND trade_win IS NOT NULL", (owner,)
    ).fetchone()
    print(f"{'-'*66}\n  整体: {tot} 笔 | 胜率 {ov['w'] or 0}% | 均收益 {ov['r'] or 0:+.2f}%")
    if tot < 50:
        print(f"  ⚠ 样本 {tot}<50, 只看结构不下结论; 累积到 ≥50 再判 confidence 是否可信。")
    print()


def check_mfe_health(days: int = 60) -> None:
    """MFE/MAE 数据健康 + giveback 简析 (退出优化 §工作块A 验证探针)。

    量纲: mfe_pct/mae_pct 列存"百分比数值"(2.79=2.79%), pnl_pct 存"分数"(0.0279)。
    giveback = mfe% - pnl_pct*100 (盘中峰值与最终落袋之差, 越大=回吐越多)。
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print(f"\n{'='*66}\nMFE/MAE 数据健康 · 近 {days} 天 · trade_records\n{'='*66}")
    r = conn.execute(
        "SELECT COUNT(*) tot, "
        "SUM(CASE WHEN mfe_pct IS NULL OR mfe_pct=0 THEN 1 ELSE 0 END) zero, "
        "SUM(CASE WHEN mfe_pct>0 AND mfe_pct<=50 THEN 1 ELSE 0 END) sane, "
        "SUM(CASE WHEN mfe_pct>50 THEN 1 ELSE 0 END) dirty "
        "FROM trade_records WHERE exit_price>0 AND trade_date>=date('now', ?) "
        "AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING')",
        (f"-{days} day",),
    ).fetchone()
    tot = r["tot"] or 0
    if not tot:
        print("(无样本)\n")
        return
    cov = r["sane"] / tot * 100
    print(f"  样本 {tot} | 缺失(0/NULL) {r['zero']} | 合理 {r['sane']} ({cov:.0f}%) | 脏(>50) {r['dirty']}")
    if r["dirty"]:
        print(f"  ⚠ {r['dirty']} 笔 mfe>50 脏值; go-forward 夹取应拦截, 存量跑 backfill_mfe_mae.py。")
    if cov < 40:
        print(f"  ⚠ 合理覆盖 {cov:.0f}%<40%, MFE 反馈不足以支撑 trailing 调参; 先回填/累积。")
    g = conn.execute(
        "SELECT exit_reason, COUNT(*) n, round(avg(mfe_pct),2) amfe, "
        "round(avg(pnl_pct*100),2) apnl, round(avg(mfe_pct - pnl_pct*100),2) gb "
        "FROM trade_records WHERE exit_price>0 AND trade_date>=date('now', ?) "
        "AND mfe_pct>0 AND mfe_pct<=50 AND pnl_pct>0 "
        "AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') "
        "GROUP BY exit_reason ORDER BY gb DESC",
        (f"-{days} day",),
    ).fetchall()
    if g:
        print(f"{'-'*66}\n  giveback (盈利单, 峰值% - 落袋%; 越大回吐越多):")
        for row in g:
            lbl = row["exit_reason"] or "(空)"
            print(f"    {lbl:18s} n={row['n']:2d} 峰值{row['amfe'] or 0:5.2f}% 落袋{row['apnl'] or 0:5.2f}% 回吐{row['gb'] or 0:5.2f}%")
    print()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--calibration":
        check_calibration(args[1] if len(args) > 1 else "playbook_sim")
        return
    if args and args[0] == "--invalidation":
        days = int(args[2]) if len(args) > 2 and args[1] == "--days" else 7
        check_invalidation_coverage(days)
        return
    if args and args[0] == "--mfe-health":
        days = int(args[2]) if len(args) > 2 and args[1] == "--days" else 60
        check_mfe_health(days)
        return
    single_date, days = None, 7
    if args:
        if args[0] == "--days" and len(args) > 1:
            days = int(args[1])
        elif args[0].count("-") == 2:
            single_date = args[0]

    where = "WHERE trade_date = ?" if single_date else "WHERE trade_date >= date('now', ?)"
    param = single_date if single_date else f"-{days} day"

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT trade_date, strategy_id, decision_id, "
        f"COALESCE(realized_pnl, 0) AS pnl FROM trade_records {where} "
        f"ORDER BY trade_date, entry_time",
        (param,),
    ).fetchall()

    scope = f"日期 {single_date}" if single_date else f"近 {days} 天"
    print(f"\n{'='*66}\n决策链路核对 · {scope} · trade_records\n{'='*66}")
    if not rows:
        print("(无成交记录)\n")
        return

    totals = defaultdict(lambda: [0, 0.0])
    leak_n = 0
    for r in rows:
        b = classify(r["strategy_id"], r["decision_id"])
        totals[b][0] += 1
        totals[b][1] += float(r["pnl"] or 0)
        if b.startswith("⛔"):
            leak_n += 1

    total_n = sum(v[0] for v in totals.values())
    for b, v in sorted(totals.items(), key=lambda x: -x[1][0]):
        pct = 100.0 * v[0] / total_n if total_n else 0
        print(f"  {b:30s} {v[0]:3d} 笔 ({pct:4.0f}%)  PnL ${v[1]:+.0f}")

    print(f"{'-'*66}")
    if leak_n == 0:
        print(f"✅ 无并行泄漏: 全部 {total_n} 笔来自 playbook_sim / 运维, 盘中独立入场 = 0。")
    else:
        print(f"⛔ 检出 {leak_n} 笔盘中独立入场泄漏! 双脑门未真正关闭, 立即排查 "
              f"signal_stage.independent_entry_enabled。")
    print()


if __name__ == "__main__":
    main()
