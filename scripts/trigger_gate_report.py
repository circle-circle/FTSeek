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

# 同口径时序锚点 (2026-09-06 修尺子)。
#   病灶: history 每行的 since 随 `--days 30` 滚动 — 实测 10 行里 since 分别是
#   6/19,6/19,7/27,7/1,7/8,7/15,7/22,7/29,8/5,8/7 → render 里写的"缺口>0 且持续 ≥2 周"
#   这条判据**无从审计**: 看到的"缺口 4.955→0.703 收窄"其实是窗口起点在动, 不是同口径时序。
#   治法: 锚点冻结在本脚本既有的三门基线 (BASELINE), 每次运行额外落一份 fixed_anchor 块,
#   滚动窗口照旧输出 (头条不变, 下游不破), 时序可比性由锚点块提供。
#   改锚点 = 改判据, 须先落档 ADR/DEB (承判据字典纪律), 不许静默改写。
ANCHOR_SINCE = "2026-07-24"


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


def _window_stats(conn, since: str) -> dict:
    """单个窗口的反向选择画像 (纯取样+统计, 不落 timestamp/锚点, 便于同口径复用)。"""
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
    return result


def _fixed_anchor_streak(current_gap, current_robust, at=None) -> dict:
    """给 render 里"持续 ≥2 周"这条判据补上可审计留痕。

    只统计带 fixed_anchor 块的 history 行 — 滚动 since 的行彼此不可比, 计入即造假时序。
    单位: gap 为百分点 (pp), 与 adverse_selection_gap_pct 同量纲。
    **观测点按日去重**: 同一天跑 N 次只算 1 个点 (否则一天内手动重跑两次就能假满足
      "持续≥2周" — 判据要的是时间跨度, 不是运行次数)。同日保留最后一次。
    诚实披露: 2026-09-06 之前的 10 行 history 无 fixed_anchor 块, 且其缺口是"当时窗口"的
      点值, 无法按时点回填 (playbook_instructions 持续在长) → 同口径时序自本次起算, 不追溯。
    异常处置 (R5 模式 / `tests/test_except_pass_ratchet.py` 棘轮): 坏行与读失败**不静默吞**,
      以计数/错误串显性透出 — "零观测点"与"history 读炸了"必须可区分。
    """
    seq = []
    bad_json_rows = 0
    history_error = None
    try:
        if OUT_HISTORY.exists():
            for line in OUT_HISTORY.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except Exception as _je:
                    bad_json_rows += 1
                    continue
                fa = doc.get("fixed_anchor")
                if not fa or fa.get("adverse_selection_gap_pct") is None:
                    continue
                seq.append((doc.get("generated_at") or doc.get("timestamp"),
                            fa["adverse_selection_gap_pct"],
                            bool(fa.get("robust_both_directions"))))
    except Exception as _he:
        history_error = f"{type(_he).__name__}: {_he}"
    if current_gap is not None:
        seq.append((at or "current", current_gap, bool(current_robust)))

    # 按日折叠 (history 追加即时间序, 故只需折叠相邻同日项)
    collapsed = []
    for _at, gap, robust in seq:
        day = (_at or "")[:10] or "unknown"
        if collapsed and collapsed[-1][0] == day:
            collapsed[-1] = (day, _at, gap, robust)
        else:
            collapsed.append((day, _at, gap, robust))

    run = 0
    for _, _, gap, robust in reversed(collapsed):
        if gap > 0 and robust:
            run += 1
        else:
            break
    return {
        "n_points": len(collapsed),
        "n_points_dedup_by": "ET/本地日历日 (同日多次运行折叠为 1 点)",
        "consecutive_positive_robust": run,
        "criterion_ge2_met": bool(run >= 2),
        "history_rows_skipped_bad_json": bad_json_rows,
        "history_error": history_error,
        "points": [{"day": day, "at": at, "gap_pct": gap, "robust_both_directions": rob}
                   for day, at, gap, rob in collapsed[-8:]],
        "note": ("同口径时序自 2026-09-06 起算 (此前 history 行 since 滚动且无 fixed_anchor, "
                 "点值不可按时点回填); '持续≥2周'需 ≥2 个**不同日**的锚点观测点"),
    }


def _generated_at() -> str:
    """带时区的生成时刻。

    病灶: history 10 行全部只有 `timestamp` (本地裸时间, 无时区) 而无 generated_at →
      留痕无法与 ET 交易日 / cron 时刻对齐取证, 跨机对时更是无解。
    `timestamp` 保留不动 (下游兼容); 本值另作 `generated_at`, 并被 streak 用于**按日去重**。
    """
    try:
        from app.utils.timezone import now_et
        return now_et().isoformat()
    except Exception:
        return datetime.now().astimezone().isoformat()


def compute(since: str) -> dict:
    from app.storage.db_pool import get_db
    conn = get_db("ftseek_us").connection()

    result = _window_stats(conn, since)
    anchor = _window_stats(conn, ANCHOR_SINCE)
    generated_at = _generated_at()
    result["fixed_anchor"] = {
        "since": ANCHOR_SINCE,
        "executed": anchor["executed"],
        "missed": anchor["missed"],
        "missed_opportunity": anchor["missed_opportunity"],
        "adverse_selection_gap_pct": anchor["adverse_selection_gap_pct"],
        "gap_by_direction": anchor["gap_by_direction"],
        "robust_both_directions": anchor["robust_both_directions"],
    }
    result["fixed_anchor_streak"] = _fixed_anchor_streak(
        anchor["adverse_selection_gap_pct"], anchor["robust_both_directions"], at=generated_at)
    result["timestamp"] = datetime.now().isoformat()
    result["generated_at"] = generated_at
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
    ]
    fa = r.get("fixed_anchor") or {}
    if fa:
        lines += [
            "",
            f"同口径锚点 (since {fa['since']} 冻结, 不随 --days 滚动 → 时序可比):",
            f"  缺口 {fa['adverse_selection_gap_pct']} pp"
            f" | executed {fa['executed']['avg_fwd_pct']}% (n={fa['executed']['n']})"
            f" vs missed {fa['missed']['avg_fwd_pct']}% (n={fa['missed']['n']})"
            f" | 双向同号稳健: {'✅' if fa['robust_both_directions'] else '❌'}",
            f"  机会成本 ${fa['missed_opportunity']['total_shadow_pnl']}"
            f" / {fa['missed_opportunity']['n_with_shadow_pnl']} 笔",
        ]
    st = r.get("fixed_anchor_streak") or {}
    if st:
        lines += [
            f"  '持续≥2周'判据留痕: 锚点观测 {st['n_points']} 个, 连续为正且稳健 "
            f"{st['consecutive_positive_robust']} 个 → "
            f"{'✅ 满足' if st['criterion_ge2_met'] else '⏳ 未满足 (需 ≥2)'}",
            f"  ({st.get('note', '')})",
        ]
    lines += [
        "",
        "判读: 缺口>0 且双向稳健 且持续 ≥2 周 → 触发门在反向选择,",
        "      下一个 shadow 变体应为'触发条件放宽', 而非继续调退出参数。",
        "      ⚠️ 三条都得看**同口径锚点**那几行 — 滚动窗口的缺口跨行不可比。",
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
    BASELINE = ANCHOR_SINCE   # 单一真源: 基线警告与同口径锚点共用一个日期
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
