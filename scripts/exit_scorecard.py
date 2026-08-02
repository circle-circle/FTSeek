#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Exit Scorecard — 退出纪律周度判分器 (只读)

用途 (2026-07-12 建立):
  DA-FTSeek 链路的迭代仪表。回答一个问题:
    "信号本身(持到收盘)是赚的, 但退出机制把它做亏了多少?"

  核心发现 (建立时): 同批成交单里, 收盘方向为正的占比 ≈ 51%,
  但实际成交胜率只有 41% —— 退出机制把本该赢的单子做成了亏损。
  漏血在执行/退出纪律, 不在预测。本脚本把这个缺口固化成每周可对比的数字。

每周五盘后跑一次, 只改一个退出参数, 下周同一张表对比: 变好→保留, 变差→回滚。

口径 (与 scripts/okr_status.py 对齐):
  - $ 层面 (R:R / expectancy): trade_records.realized_pnl 为 SSOT
    (playbook_instructions.pnl 会被 ORPHAN_CLEANUP 幽灵单污染, 不用)。
  - 混淆矩阵 (收盘方向 vs 实际结局): playbook_instructions
    (outcome=win/loss 是分类, 不涉 $, 不受幽灵单影响) join fwd_return_intraday。

用法:
  python scripts/exit_scorecard.py                 # 默认近30天
  python scripts/exit_scorecard.py --since 2026-07-11   # 只看断点后干净窗口
  python scripts/exit_scorecard.py --days 14       # 近14天
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_ROOT)

# 与 okr_status.py 同口径: OKR 目标是 Playbook 系统
PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')


def _et_cutoff(days: int) -> str:
    """ET 口径的 N 天前日期 (date('now') 是 UTC, 会差一天)。"""
    try:
        from app.utils.timezone import now_et
        return (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def compute(since: str) -> dict:
    from app.storage.db_pool import get_db
    conn = get_db("ftseek_us").connection()  # row_factory=Row 由 pool 设置

    # ── 1. 混淆矩阵: 收盘方向(持到收盘) vs 实际成交结局 ──
    #    fwd_return_intraday = sign*(收盘-entry)/entry, >0 表示"若持到收盘方向是对的"
    ci_rows = conn.execute(
        """
        SELECT outcome, fwd_return_intraday AS fwd, direction
        FROM playbook_instructions
        WHERE outcome IN ('win','loss')
          AND fwd_return_intraday IS NOT NULL
          AND direction != 'SKIP'
          AND date >= ?
        """,
        (since,),
    ).fetchall()

    def confusion(rows):
        n = len(rows)
        a = sum(1 for r in rows if r["fwd"] > 0 and r["outcome"] == "win")   # 收盘正→赢 (忠实)
        b = sum(1 for r in rows if r["fwd"] > 0 and r["outcome"] == "loss")  # 收盘正→亏 (杀赢家) ★
        d = sum(1 for r in rows if r["fwd"] <= 0 and r["outcome"] == "win")  # 收盘负→赢 (救回)
        e = sum(1 for r in rows if r["fwd"] <= 0 and r["outcome"] == "loss")
        killed = [r["fwd"] for r in rows if r["fwd"] > 0 and r["outcome"] == "loss"]
        held_wr = (a + b) / n if n else 0.0
        exec_wr = (a + d) / n if n else 0.0
        return {
            "n": n,
            "held_to_close_wr": round(held_wr * 100, 1),   # 信号命中(持到收盘)
            "executed_wr": round(exec_wr * 100, 1),          # 实际成交胜率
            "edge_leak_pts": round((held_wr - exec_wr) * 100, 1),  # 退出漏掉的百分点
            "killed_winners": b,                              # ★ 被退出杀死的赢家
            "saved_losers": d,                                # 退出救回的输家
            "net_flipped": b - d,                             # 净被翻成亏的笔数
            "killed_avg_ret_pct": round(_mean(killed) * 100, 2) if killed else 0.0,
            "cm": {"pos_win": a, "pos_loss": b, "neg_win": d, "neg_loss": e},
        }

    overall_cm = confusion(ci_rows)
    buy_cm = confusion([r for r in ci_rows if r["direction"] == "BUY"])
    short_cm = confusion([r for r in ci_rows if ("SHORT" in (r["direction"] or "") or r["direction"] == "SELL")])

    # ── 2. R:R & 期望值: trade_records pnl_net (SSOT, DEB-20260730 D1) ──
    #   旧库无 pnl_net 列 → COALESCE 回退 realized_pnl (毛口径, 仅兼容)
    src_filter = ", ".join(f"'{s}'" for s in PLAYBOOK_SOURCE_TYPES)
    tr_rows = conn.execute(
        f"""
        SELECT COALESCE(pnl_net, realized_pnl) AS pnl, entry_price * quantity AS notional
        FROM trade_records
        WHERE trade_date >= ?
          AND source_type IN ({src_filter})
          AND trade_mode != 'live'
          AND exit_reason IS NOT NULL AND exit_reason != ''
        """,
        (since,),
    ).fetchall()
    pnls = [r["pnl"] or 0 for r in tr_rows]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]  # 存正数, 便于算 R:R
    nwl = len(wins) + len(losses)
    wr = len(wins) / nwl if nwl else 0.0
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    rr = (avg_win / avg_loss) if avg_loss else float("inf")
    breakeven_rr = ((1 - wr) / wr) if wr else float("inf")   # 当前胜率的盈亏平衡 R:R
    expectancy = wr * avg_win - (1 - wr) * avg_loss           # 每笔期望值 ($)
    gross_win = sum(wins)
    gross_loss = sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # RFC-062 G1-1 (2026-08-02): 滑点折算后期望 — round-trip = 2×haircut, 按 avg 名义本金折算
    _notionals = [r["notional"] for r in tr_rows if r["notional"] and r["notional"] > 0]
    avg_notional = _mean(_notionals)
    try:
        from app.core.config_container import get_config as _gc
        _hc_bps = float(_gc().get_dotted("live_readiness.slippage_haircut_bps", 15))
    except Exception:
        _hc_bps = 15.0
    expectancy_adj = expectancy - avg_notional * (2 * _hc_bps / 10000.0)

    payoff = {
        "n": nwl,
        "win_rate": round(wr * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "rr": round(rr, 2) if rr != float("inf") else None,
        "breakeven_rr": round(breakeven_rr, 2) if breakeven_rr != float("inf") else None,
        "rr_gap": round(rr - breakeven_rr, 2) if (rr != float("inf") and breakeven_rr != float("inf")) else None,
        "expectancy_per_trade": round(expectancy, 2),
        "avg_notional": round(avg_notional, 2),
        "slip_haircut_bps": _hc_bps,
        "expectancy_slip_adj": round(expectancy_adj, 2),
        "total_pnl": round(sum(pnls), 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
    }

    # ── 3. G4 存活判据 (2026-08-01 补落盘: 原终裁 G4 无任何判分产物) ──
    #    阈值按冻结框架: 最大回撤 <5% 且 无连续 5 笔亏损 (窗口 = since, 净口径)
    #    回撤自算: capital_state.drawdown_pct 列语义不可信 (实测全 ≤0 的分数), 用权益曲线峰谷算
    eq_rows = conn.execute(
        "SELECT current_equity FROM capital_state WHERE date(last_update) >= ? "
        "AND current_equity > 0 ORDER BY last_update",
        (since,),
    ).fetchall()
    max_dd = None
    if eq_rows:
        _peak = 0.0
        _mdd = 0.0
        for r in eq_rows:
            eq = r["current_equity"]
            _peak = max(_peak, eq)
            if _peak > 0:
                _mdd = max(_mdd, (_peak - eq) / _peak * 100.0)
        max_dd = _mdd

    seq_rows = conn.execute(
        f"""
        SELECT COALESCE(pnl_net, realized_pnl) AS pnl
        FROM trade_records
        WHERE trade_date >= ?
          AND source_type IN ({src_filter})
          AND trade_mode != 'live'
          AND exit_reason IS NOT NULL AND exit_reason != ''
        ORDER BY exit_time
        """,
        (since,),
    ).fetchall()
    max_consec_loss = 0
    _cur = 0
    for r in seq_rows:
        if (r["pnl"] or 0) < 0:
            _cur += 1
            max_consec_loss = max(max_consec_loss, _cur)
        else:
            _cur = 0
    g4 = {
        "max_drawdown_pct": round(max_dd, 2) if max_dd is not None else None,
        "max_drawdown_ok": (max_dd < 5.0) if max_dd is not None else None,
        "max_consecutive_losses": max_consec_loss,
        "consecutive_losses_ok": max_consec_loss < 5,
        "verdict": None,
    }
    if g4["max_drawdown_ok"] is not None:
        g4["verdict"] = "PASS" if (g4["max_drawdown_ok"] and g4["consecutive_losses_ok"]) else "FAIL"

    return {"since": since, "pnl_basis": "net: COALESCE(pnl_net, realized_pnl) (DEB-20260730 D1)",
            "confusion": overall_cm, "by_buy": buy_cm, "by_short": short_cm, "payoff": payoff,
            "g4_survival": g4}


def render(rep: dict) -> None:
    cm = rep["confusion"]
    pf = rep["payoff"]
    print("=" * 66)
    print("  EXIT SCORECARD — " + datetime.now().strftime("%Y-%m-%d %H:%M") + f"   (since {rep['since']})")
    print("=" * 66)

    print("\n🩸 EDGE LEAK — 信号 vs 执行 (源: playbook_instructions)")
    if cm["n"] == 0:
        print("  ⚠️ 窗口内无成交样本 (需 date≥since 的 win/loss 且 fwd_return_intraday 已回填)")
    else:
        print(f"  样本 n={cm['n']}")
        print(f"  信号命中(持到收盘): {cm['held_to_close_wr']}%")
        print(f"  实际成交胜率:       {cm['executed_wr']}%")
        print(f"  ── 退出漏掉: {cm['edge_leak_pts']} 个百分点 ──")
        print(f"  收盘正→成交亏 (杀赢家 ★): {cm['killed_winners']}   本可 +{cm['killed_avg_ret_pct']}%/笔")
        print(f"  收盘负→成交赢 (救回):     {cm['saved_losers']}")
        print(f"  净被翻成亏损: {cm['net_flipped']} 笔")

    print("\n📐 PAYOFF — R:R & 期望值 (源: trade_records SSOT)")
    if pf["n"] == 0:
        print("  ⚠️ 窗口内无已平仓成交")
    else:
        print(f"  样本 n={pf['n']}  胜率 {pf['win_rate']}%")
        print(f"  avg赢 +${pf['avg_win']}   avg亏 ${pf['avg_loss']}")
        _rr = pf["rr"] if pf["rr"] is not None else "∞"
        _be = pf["breakeven_rr"] if pf["breakeven_rr"] is not None else "∞"
        print(f"  R:R = {_rr}   盈亏平衡需 ≥ {_be}   缺口 {pf['rr_gap']}")
        print(f"  每笔期望值: ${pf['expectancy_per_trade']}   总盈亏: ${pf['total_pnl']}")
        print(f"  每笔期望值(折滑点{pf['slip_haircut_bps']:g}bps×2): ${pf['expectancy_slip_adj']}  "
              f"(avg名义 ${pf['avg_notional']:.0f}, RFC-062 门1口径)")
        _pfv = pf["profit_factor"] if pf["profit_factor"] is not None else "∞"
        print(f"  Profit Factor: {_pfv}   (G2 裁决线 ≥1.3, 终裁窗整体)")
        if pf["n"] < 30:
            print(f"  ⚠️ n={pf['n']} <30 — 本窗统计功效不足, 期望值/PF 仅供方向参考")

    g4 = rep.get("g4_survival") or {}
    if g4:
        print("\n🛡️ G4 SURVIVAL — 存活判据 (裁决线: 回撤<5% 且 连亏<5笔)")
        _dd = g4.get("max_drawdown_pct")
        print(f"  最大回撤: {('%s%%' % _dd) if _dd is not None else 'N/A (capital_state 无数据)'}   "
              f"连亏峰值: {g4.get('max_consecutive_losses')} 笔   裁决: {g4.get('verdict') or 'N/A'}")

    print("\n🧭 BY DIRECTION — 命中率 (持到收盘 / 实际成交)")
    for label, key in (("BUY  ", "by_buy"), ("SHORT", "by_short")):
        c = rep[key]
        if c["n"] == 0:
            print(f"  {label}: 无样本")
        else:
            print(f"  {label}: n={c['n']:3d}  持到收盘 {c['held_to_close_wr']}%  →  成交 {c['executed_wr']}%  (杀赢家 {c['killed_winners']})")

    # ── 一句话判词 ──
    print("\n📝 判词")
    if cm["n"] and pf["n"]:
        leak = cm["edge_leak_pts"]
        gap = pf["rr_gap"]
        exp = pf["expectancy_per_trade"]
        verdict = "✅ 正期望" if exp > 0 else "🔴 负期望"
        print(f"  {verdict} (${exp}/笔). 退出漏 {leak}pts, 杀赢家净 {cm['net_flipped']} 笔; "
              f"R:R 距盈亏平衡 {'+' if (gap or 0) >= 0 else ''}{gap}。")
        if exp <= 0:
            print("  → 本周只改一个退出参数(优先: 让赢家别被砍 / 拉高 R:R), 下周对比此表。")
            print("  → ⚠️ 期望值转正前, 不要靠加仓/放大 R:R 去追收益 —— 那是加速亏损。")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description="退出纪律周度判分器 (只读)")
    ap.add_argument("--since", type=str, default=None, help="起始日期 YYYY-MM-DD (优先于 --days)")
    ap.add_argument("--days", type=int, default=30, help="近 N 天 (默认30)")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    since = args.since or _et_cutoff(args.days)

    # 基线警告: 07-24 三门上线 = 退出参数 config_version 断点, 跨窗口混合无意义
    BASELINE = "2026-07-24"
    if since < BASELINE:
        print(f"⚠️  警告: since={since} 早于三门基线 {BASELINE}, 窗口混入旧退出参数 regime。"
              f" 周度对比/裁决判据请用 --since {BASELINE} 或更晚。", file=sys.stderr)

    rep = compute(since)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        render(rep)

    # 落盘供每周复盘对比
    data_dir = Path(PROJECT_ROOT) / "data"
    data_dir.mkdir(exist_ok=True)
    rep_out = dict(rep, timestamp=datetime.now().isoformat())
    with open(data_dir / "exit_scorecard_latest.json", "w") as f:
        json.dump(rep_out, f, ensure_ascii=False, indent=2)
    with open(data_dir / "exit_scorecard_history.jsonl", "a") as f:
        f.write(json.dumps(rep_out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
