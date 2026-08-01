#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Trigger Gate Report — 触发门反向选择判分器 (只读)

用途 (2026-07-20 建立):
  回答一个问题: "入场触发门(突破当日高/低+放量确认)是在筛好信号, 还是在反向选择?"

  建立时实证 (近30天, 同口径 fwd_return_intraday, 指令 entry_price 锚定):
    missed(未触发) 93笔 均值 +0.9%, 59% 收盘为正
    executed(成交) 88笔 均值 +0.2%, ~51% 收盘为正
  → 错过的信号平均比成交的好 4.5x, 符合 adverse selection 假说:
    强势票直接飞走不回踩 → miss; 能触发的往往是突破后回落的弱势票。

  本脚本把这个缺口固化成可周度对比的数字。若差距持续且分层后稳健,
  下一个 shadow 变体应该是"触发条件放宽", 而不是继续调退出参数。

口径:
  - 全部基于指令 entry_price 锚定的 fwd_return_intraday (同一把尺子量 missed 和 executed,
    排除退出执行的影响 — 这里只测"触发门选出的信号质量", 不测退出)。
  - 只看终态 outcome; void/holding 不进样本。
  - record-only 分析, 不改任何实单行为。

用法:
  python scripts/trigger_gate_report.py                     # 默认近30天
  python scripts/trigger_gate_report.py --since 2026-07-01  # 指定窗口
  python scripts/trigger_gate_report.py --days 14           # 近14天
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_ROOT)

OUT_LATEST = Path(PROJECT_ROOT) / "data" / "trigger_gate_latest.json"
OUT_HISTORY = Path(PROJECT_ROOT) / "data" / "trigger_gate_history.jsonl"


def _et_today_minus(days: int) -> str:
    """ET 口径的 N 天前日期 (与 exit_scorecard 一致)。"""
    try:
        from app.utils.timezone import now_et
        return (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _group_stats(rows) -> dict:
    """一组指令的信号质量画像 (fwd_return_intraday 锚定)。"""
    fwds = [r["fwd"] for r in rows if r["fwd"] is not None]
    n = len(fwds)
    pos = sum(1 for f in fwds if f > 0)
    return {
        "n": n,
        "avg_fwd_pct": round(_mean(fwds) * 100, 3),
        "median_fwd_pct": round(_median(fwds) * 100, 3),
        "pos_rate": round(pos / n * 100, 1) if n else 0.0,
        "avg_win_fwd_pct": round(_mean([f for f in fwds if f > 0]) * 100, 3),
        "avg_loss_fwd_pct": round(_mean([f for f in fwds if f <= 0]) * 100, 3),
    }


def compute(since: str) -> dict:
    from app.storage.db_pool import get_db
    conn = get_db("ftseek_us").connection()

    rows = conn.execute(
        """
        SELECT outcome, direction, skip_reason, triggered,
               fwd_return_intraday AS fwd, position_size, entry_price, shadow_pnl
        FROM playbook_instructions
        WHERE date >= ?
          AND direction != 'SKIP'
          AND outcome IN ('win', 'loss', 'missed')
          AND fwd_return_intraday IS NOT NULL
        """,
        (since,),
    ).fetchall()

    executed = [r for r in rows if r["outcome"] in ("win", "loss")]
    missed = [r for r in rows if r["outcome"] == "missed"]
    # missed 细分: 从未触发 vs 触发了没执行 (归因来自 #1 skip_reason 补齐)
    missed_no_trigger = [r for r in missed if not r["triggered"]]
    missed_no_exec = [r for r in missed if r["triggered"]]

    result: dict = {
        "since": since,
        "executed": _group_stats(executed),
        "missed": _group_stats(missed),
        "missed_no_trigger": _group_stats(missed_no_trigger),
        "missed_triggered_no_exec": _group_stats(missed_no_exec),
    }

    # 方向分层 (排除"missed 恰好集中在单边趋势行情"的混杂因素)
    for tag, grp in (("executed", executed), ("missed", missed)):
        for d in ("BUY", "SELL_SHORT"):
            result[f"{tag}_{d.lower()}"] = _group_stats([r for r in grp if r["direction"] == d])

    # 错过机会成本: shadow_pnl 是"若按指令价成交持到收盘"的假设 PnL
    missed_shadow = [r["shadow_pnl"] for r in missed if r["shadow_pnl"] is not None]
    result["missed_opportunity"] = {
        "n_with_shadow_pnl": len(missed_shadow),
        "total_shadow_pnl": round(sum(missed_shadow), 2),
        "avg_shadow_pnl": round(_mean(missed_shadow), 2),
    }

    # 核心判分: adverse selection gap (missed 均值 - executed 均值, 百分点)
    gap = result["missed"]["avg_fwd_pct"] - result["executed"]["avg_fwd_pct"]
    result["adverse_selection_gap_pct"] = round(gap, 3)
    # 稳健性: 两个方向分层是否同号 (单边行情混杂时会破)
    gap_buy = (result["missed_buy"]["avg_fwd_pct"] - result["executed_buy"]["avg_fwd_pct"]
               if result["missed_buy"]["n"] and result["executed_buy"]["n"] else None)
    gap_short = (result["missed_sell_short"]["avg_fwd_pct"] - result["executed_sell_short"]["avg_fwd_pct"]
                 if result["missed_sell_short"]["n"] and result["executed_sell_short"]["n"] else None)
    result["gap_by_direction"] = {"buy": gap_buy and round(gap_buy, 3),
                                  "short": gap_short and round(gap_short, 3)}
    result["robust_both_directions"] = bool(
        gap_buy is not None and gap_short is not None
        and (gap_buy > 0) == (gap_short > 0) == (gap > 0)
    )
    result["timestamp"] = datetime.now().isoformat()
    return result


def render(r: dict) -> str:
    lines = [
        f"═══ Trigger Gate Report (since {r['since']}) ═══",
        "",
        f"{'组':<28}{'n':>5}{'均值fwd%':>10}{'中位%':>8}{'收盘为正%':>10}",
    ]
    for key, label in (
        ("executed", "executed (成交)"),
        ("missed", "missed (全部错过)"),
        ("missed_no_trigger", "  └ 未触发"),
        ("missed_triggered_no_exec", "  └ 触发未执行"),
        ("executed_buy", "executed BUY"),
        ("missed_buy", "missed BUY"),
        ("executed_sell_short", "executed SHORT"),
        ("missed_sell_short", "missed SHORT"),
    ):
        g = r[key]
        lines.append(f"{label:<26}{g['n']:>5}{g['avg_fwd_pct']:>10}{g['median_fwd_pct']:>8}{g['pos_rate']:>10}")
    mo = r["missed_opportunity"]
    lines += [
        "",
        f"错过机会成本 (shadow_pnl): {mo['n_with_shadow_pnl']} 笔, "
        f"合计 ${mo['total_shadow_pnl']}, 均值 ${mo['avg_shadow_pnl']}/笔",
        f"反向选择缺口 (missed−executed): {r['adverse_selection_gap_pct']} 个百分点"
        f" | 分方向 BUY={r['gap_by_direction']['buy']} SHORT={r['gap_by_direction']['short']}"
        f" | 双向同号稳健: {'✅' if r['robust_both_directions'] else '❌ (可能是单边行情混杂)'}",
        "",
        "判读: 缺口>0 且双向稳健 且持续 ≥2 周 → 触发门在反向选择,",
        "      下一个 shadow 变体应为'触发条件放宽', 而非继续调退出参数。",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=30, help="近 N 天 (默认30)")
    args = ap.parse_args()
    since = args.since or _et_today_minus(args.days)

    # 基线警告: 07-24 三门上线 = 退出参数 config_version 断点
    # 注: playbook_instructions 无 source_type 列, 本脚本度量指令层 (非 trade_records 层),
    #     executed=1 的 win/loss 与 SIM/LIVE 成交一一对应 (实测无 SHADOW 混入)
    BASELINE = "2026-07-24"
    if since < BASELINE:
        import sys
        print(f"⚠️  警告: since={since} 早于三门基线 {BASELINE}, 窗口混入旧退出参数 regime。"
              f" 裁决判据请用 --since {BASELINE} 或更晚。", file=sys.stderr)

    result = compute(since)
    print(render(result))

    OUT_LATEST.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    with OUT_HISTORY.open("a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"\n→ 已落 {OUT_LATEST.name} + history")


if __name__ == "__main__":
    main()
