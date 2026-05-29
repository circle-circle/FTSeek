"""
LiveBacktestComparator — 回测/实盘差距对比工具
================================================

任务: Task #16 (2026-05-27)

目的
----
- 从实盘交易记录 DB 中读取已平仓成交
- 用相同时段、相同参数对相同股票池做"理想化回测重放"
- 对比胜率/平均盈亏/总收益等关键指标
- 估算滑点 (实盘 entry/exit vs 信号价/midpoint) 与执行延迟影响

仅作为诊断工具, 不修改任何配置, 不下单。
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.backtest.weekly_optimizer import (
    LIVE_CURRENT_PARAMS,
    TradeRow,
    load_trade_rows,
    replay_pnl_series,
    _stats,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_REPORT_DIR = _PROJECT_ROOT / "data" / "optimization_reports"
_REPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ComparisonReport:
    generated_at: str
    window: Tuple[str, str]
    params: Dict[str, float]
    n_live: int
    n_backtest: int
    live_stats: Dict[str, float]
    backtest_stats: Dict[str, float]
    diffs: Dict[str, float]
    slippage_estimate: Dict[str, float]
    latency_impact: Dict[str, float]
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["window"] = list(self.window)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# 滑点 / 延迟估算
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_slippage(rows: List[TradeRow]) -> Dict[str, float]:
    """
    粗估滑点: 用 (entry_price - 信号参考价) / 信号参考价 的绝对值平均。
    若无 signal price 字段, 使用历史 max_drawdown_during_trade 作为下行滑点上界代理。
    """
    if not rows:
        return {"avg_slippage_pct": 0.0, "p95_slippage_pct": 0.0, "samples": 0}
    # 没有信号价字段 → 用持仓最大回撤的 5% 作为滑点上限代理 (保守估计)
    proxies = [r.max_drawdown_during_trade * 0.05 for r in rows
               if r.max_drawdown_during_trade > 0]
    if not proxies:
        return {"avg_slippage_pct": 0.0, "p95_slippage_pct": 0.0, "samples": 0}
    proxies.sort()
    avg = sum(proxies) / len(proxies)
    p95 = proxies[int(0.95 * (len(proxies) - 1))]
    return {
        "avg_slippage_pct": round(avg, 6),
        "p95_slippage_pct": round(p95, 6),
        "samples": len(proxies),
    }


def _estimate_latency_impact(rows: List[TradeRow]) -> Dict[str, float]:
    """
    用 holding_hours 的离散度作为执行延迟敏感性的代理:
    标准差越大, 表明执行时机越不一致 (可能含较多延迟成交)。
    """
    if not rows:
        return {"holding_hours_mean": 0.0, "holding_hours_std": 0.0, "n": 0}
    hours = [r.holding_hours for r in rows if r.holding_hours > 0]
    if len(hours) < 2:
        return {
            "holding_hours_mean": round(sum(hours) / max(1, len(hours)), 2),
            "holding_hours_std": 0.0,
            "n": len(hours),
        }
    return {
        "holding_hours_mean": round(statistics.mean(hours), 2),
        "holding_hours_std": round(statistics.stdev(hours), 2),
        "n": len(hours),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主类
# ─────────────────────────────────────────────────────────────────────────────

class LiveBacktestComparator:
    """对比实盘成交 vs 同参数同时段回测重放"""

    def __init__(
        self,
        params: Optional[Dict[str, float]] = None,
        days: int = 30,
    ):
        self.params = dict(params or LIVE_CURRENT_PARAMS)
        self.days = days

    def run(self) -> ComparisonReport:
        end_dt = datetime.utcnow().date()
        start_dt = end_dt - timedelta(days=self.days)
        rows = load_trade_rows(start_date=start_dt.isoformat(), end_date=end_dt.isoformat())
        notes: List[str] = []

        if not rows:
            notes.append("窗口内无已平仓成交, 报告为空。")
            return ComparisonReport(
                generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
                window=(start_dt.isoformat(), end_dt.isoformat()),
                params=self.params,
                n_live=0, n_backtest=0,
                live_stats=_stats([]), backtest_stats=_stats([]),
                diffs={},
                slippage_estimate=_estimate_slippage([]),
                latency_impact=_estimate_latency_impact([]),
                notes=notes,
            )

        # ── 实盘真实 PnL 序列 ──
        live_pnls = [r.pnl_pct for r in rows]
        live_stats = _stats(live_pnls)

        # ── 回测重放 (相同参数下的理想 PnL) ──
        bt_pnls = replay_pnl_series(rows, self.params)
        bt_stats = _stats(bt_pnls)

        # ── 差异 ──
        diffs = {
            "win_rate_diff": round(bt_stats["win_rate"] - live_stats["win_rate"], 4),
            "avg_pnl_diff": round(bt_stats["avg_pnl"] - live_stats["avg_pnl"], 6),
            "sharpe_diff": round(bt_stats["sharpe"] - live_stats["sharpe"], 4),
            "total_return_diff": round(
                bt_stats["total_return"] - live_stats["total_return"], 4
            ),
            "max_drawdown_diff": round(
                bt_stats["max_drawdown"] - live_stats["max_drawdown"], 4
            ),
        }

        slip = _estimate_slippage(rows)
        latency = _estimate_latency_impact(rows)

        # ── 启发式备注 ──
        if diffs["avg_pnl_diff"] > 0.005:
            notes.append(
                f"回测 avg_pnl 高出实盘 {diffs['avg_pnl_diff']*100:.2f}%, "
                "差距主要可能来自滑点 + 执行延迟 + 部分成交。"
            )
        if diffs["win_rate_diff"] > 0.05:
            notes.append(
                f"回测胜率比实盘高 {diffs['win_rate_diff']*100:.1f}个百分点, "
                "建议检查信号到下单的延迟与成交回报机制。"
            )
        if slip["avg_slippage_pct"] > 0.002:
            notes.append(
                f"估算平均滑点 {slip['avg_slippage_pct']*100:.2f}% (P95 {slip['p95_slippage_pct']*100:.2f}%), "
                "可能侵蚀短周期收益。"
            )

        return ComparisonReport(
            generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            window=(start_dt.isoformat(), end_dt.isoformat()),
            params=self.params,
            n_live=len(rows),
            n_backtest=len(bt_pnls),
            live_stats=live_stats,
            backtest_stats=bt_stats,
            diffs=diffs,
            slippage_estimate=slip,
            latency_impact=latency,
            notes=notes,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 报告输出
# ─────────────────────────────────────────────────────────────────────────────

def render_markdown(report: ComparisonReport) -> str:
    lines: List[str] = []
    lines.append("# Live vs Backtest Gap Report")
    lines.append(f"_Generated: {report.generated_at}_")
    lines.append("")
    lines.append(f"窗口: `{report.window[0]}` → `{report.window[1]}`")
    lines.append(f"参数: `{json.dumps(report.params, ensure_ascii=False)}`")
    lines.append(f"实盘成交: {report.n_live} 笔 | 回测重放: {report.n_backtest} 笔")
    lines.append("")
    lines.append("## 关键指标对比")
    lines.append("| 指标 | 实盘 | 回测 | 差异 |")
    lines.append("|------|------|------|------|")
    keys = ["n_trades", "win_rate", "avg_pnl", "sharpe", "total_return", "max_drawdown"]
    for k in keys:
        live_v = report.live_stats.get(k)
        bt_v = report.backtest_stats.get(k)
        diff = report.diffs.get(f"{k}_diff", "—")
        lines.append(f"| {k} | {live_v} | {bt_v} | {diff} |")
    lines.append("")
    lines.append("## 滑点估算")
    for k, v in report.slippage_estimate.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 执行延迟代理")
    for k, v in report.latency_impact.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    if report.notes:
        lines.append("## 备注")
        for n in report.notes:
            lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


def write_report(report: ComparisonReport, out_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    out_dir = out_dir or _REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"live_vs_backtest_{ts}.json"
    md_path = out_dir / f"live_vs_backtest_{ts}.md"
    json_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def run_comparison(
    params: Optional[Dict[str, float]] = None,
    days: int = 30,
    notifier: Any = None,
) -> ComparisonReport:
    cmp = LiveBacktestComparator(params=params, days=days)
    report = cmp.run()
    write_report(report)
    summary = _format_summary(report)
    logger.info(summary)
    if notifier and hasattr(notifier, "send_text"):
        try:
            notifier.send_text(summary)
        except Exception as e:
            logger.warning(f"[LiveBacktestCompare] 通知失败: {e}")
    return report


def _format_summary(report: ComparisonReport) -> str:
    return (
        f"📈 [LiveBacktestCompare] {report.window[0]}→{report.window[1]} "
        f"实盘 {report.n_live}笔 | 回测 {report.n_backtest}笔\n"
        f"胜率差: {report.diffs.get('win_rate_diff')} | "
        f"avg_pnl差: {report.diffs.get('avg_pnl_diff')} | "
        f"sharpe差: {report.diffs.get('sharpe_diff')}\n"
        f"滑点: avg={report.slippage_estimate.get('avg_slippage_pct')} "
        f"p95={report.slippage_estimate.get('p95_slippage_pct')}"
    )


def _cli() -> int:
    import os
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    report = run_comparison()
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
