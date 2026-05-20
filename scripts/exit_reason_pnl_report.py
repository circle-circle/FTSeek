#!/usr/bin/env python3
"""
F13/F22 (2026-05-14): Exit Reason × PnL 分布报告

按 exit_reason × session × direction 分组的 PnL 分布。
让"哪条退出规则真正赚钱 / 哪条该禁用"可量化。

用法:
  python scripts/exit_reason_pnl_report.py --days 30
  python scripts/exit_reason_pnl_report.py --days 7 --output reports/weekly_exit_quality.md
  python scripts/exit_reason_pnl_report.py --days 30 --json   # 同时输出 JSON 给 daily_review

输出 markdown 表 + 可选 JSON. 当任何 reason avg_pnl_pct < -0.5% 且 n>=5 时给 ⚠ 标记;
连续 4 周都为负则建议禁用 (写入 review_loop 的 lessons 待确认).
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 让脚本可以独立 -m 运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_trades(db_path: str, days: int) -> List[Dict]:
    """读取最近 N 天有 exit_reason 的成交记录"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute(
        """
        SELECT exit_reason, direction, realized_pnl, pnl_pct, entry_price, quantity,
               entry_time, exit_time, holding_hours, symbol, source_type, trade_date
        FROM trade_records
        WHERE exit_reason IS NOT NULL
          AND exit_time IS NOT NULL
          AND date(exit_time) >= ?
        """,
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# session 划分 (US ET)
def _classify_session(exit_time_str: str) -> str:
    if not exit_time_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(exit_time_str.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(exit_time_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return "unknown"

    h, m = dt.hour, dt.minute
    minutes = h * 60 + m
    if minutes < 9 * 60 + 30:
        return "premarket"
    if minutes < 9 * 60 + 50:
        return "opening_drive"
    if minutes < 11 * 60 + 30:
        return "morning"
    if minutes < 14 * 60:
        return "midday"
    if minutes < 15 * 60 + 30:
        return "afternoon"
    if minutes < 16 * 60:
        return "closing_drive"
    return "post_close"


def _compute_pnl_pct(t: Dict) -> Optional[float]:
    """统一返回百分比 (0.01 = 1%)"""
    p = t.get("pnl_pct")
    if p is not None:
        return float(p)
    pnl = t.get("realized_pnl")
    qty = t.get("quantity") or 0
    entry = t.get("entry_price") or 0
    if pnl is not None and qty and entry:
        try:
            return float(pnl) / (float(qty) * float(entry))
        except Exception:
            return None
    return None


def _aggregate(trades: List[Dict]) -> Dict[str, Dict]:
    """按 exit_reason 聚合"""
    groups: Dict[str, List[float]] = {}
    sessions: Dict[str, Dict[str, List[float]]] = {}
    directions: Dict[str, Dict[str, List[float]]] = {}

    for t in trades:
        r = t.get("exit_reason") or "UNKNOWN"
        pct = _compute_pnl_pct(t)
        if pct is None:
            continue
        groups.setdefault(r, []).append(pct)
        sess = _classify_session(t.get("exit_time"))
        sessions.setdefault(r, {}).setdefault(sess, []).append(pct)
        dir_ = (t.get("direction") or "UNKNOWN").upper()
        directions.setdefault(r, {}).setdefault(dir_, []).append(pct)

    out: Dict[str, Dict] = {}
    for r, vals in groups.items():
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        avg = statistics.mean(vals) if vals else 0.0
        med = statistics.median(vals) if vals else 0.0
        std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        sharpe = (avg / std) if std > 0 else 0.0
        sorted_v = sorted(vals)
        p25 = sorted_v[int(n * 0.25)] if n else 0.0
        p75 = sorted_v[int(n * 0.75)] if n else 0.0
        out[r] = {
            "n": n,
            "win_rate": wins / n if n else 0.0,
            "avg_pnl_pct": avg,
            "median_pnl_pct": med,
            "p25_pnl_pct": p25,
            "p75_pnl_pct": p75,
            "stdev_pnl_pct": std,
            "sharpe_proxy": sharpe,
            "min_pnl_pct": min(vals) if vals else 0.0,
            "max_pnl_pct": max(vals) if vals else 0.0,
            "by_session": {
                s: {
                    "n": len(svs),
                    "avg_pnl_pct": statistics.mean(svs) if svs else 0.0,
                }
                for s, svs in sessions.get(r, {}).items()
            },
            "by_direction": {
                d: {
                    "n": len(dvs),
                    "win_rate": sum(1 for v in dvs if v > 0) / len(dvs) if dvs else 0.0,
                    "avg_pnl_pct": statistics.mean(dvs) if dvs else 0.0,
                }
                for d, dvs in directions.get(r, {}).items()
            },
        }
    return out


def _recommendation(stats: Dict) -> str:
    if stats["n"] < 5:
        return "🔍 insufficient"  # 样本不足
    a = stats["avg_pnl_pct"]
    if a >= 0.005:
        return "✅ keep"
    if a >= -0.001:
        return "⚠ marginal"
    if a >= -0.005:
        return "⚠ review"
    return "🚨 disable"


def _format_markdown(stats: Dict[str, Dict], days: int) -> str:
    rows = sorted(stats.items(), key=lambda kv: kv[1]["avg_pnl_pct"], reverse=True)
    out = []
    out.append(f"# Exit Reason × PnL Distribution Report")
    out.append("")
    out.append(f"- 周期: 最近 **{days}** 天")
    out.append(f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}")
    out.append(f"- 总规则数: {len(stats)}")
    out.append("")
    out.append("## 主表 (按 avg_pnl_pct 降序)")
    out.append("")
    out.append("| exit_reason | n | win_rate | avg_pnl% | median% | p25/p75% | sharpe | min/max% | recommendation |")
    out.append("|-------------|---:|---------:|---------:|--------:|---------:|-------:|---------:|----------------|")
    for r, s in rows:
        out.append(
            f"| `{r}` | {s['n']} | {s['win_rate']:.0%} | "
            f"{s['avg_pnl_pct']*100:+.2f} | {s['median_pnl_pct']*100:+.2f} | "
            f"{s['p25_pnl_pct']*100:+.2f} / {s['p75_pnl_pct']*100:+.2f} | "
            f"{s['sharpe_proxy']:+.2f} | "
            f"{s['min_pnl_pct']*100:+.1f} / {s['max_pnl_pct']*100:+.1f} | "
            f"{_recommendation(s)} |"
        )
    out.append("")

    # by direction
    out.append("## 按方向分解 (LONG vs SHORT)")
    out.append("")
    out.append("| exit_reason | direction | n | win_rate | avg_pnl% |")
    out.append("|-------------|-----------|---:|---------:|---------:|")
    for r, s in rows:
        for d, ds in sorted(s["by_direction"].items()):
            out.append(
                f"| `{r}` | {d} | {ds['n']} | {ds['win_rate']:.0%} | {ds['avg_pnl_pct']*100:+.2f} |"
            )
    out.append("")

    # by session
    out.append("## 按 session 分解")
    out.append("")
    out.append("| exit_reason | session | n | avg_pnl% |")
    out.append("|-------------|---------|---:|---------:|")
    for r, s in rows:
        for sess, ss in sorted(s["by_session"].items()):
            out.append(
                f"| `{r}` | {sess} | {ss['n']} | {ss['avg_pnl_pct']*100:+.2f} |"
            )
    out.append("")

    # 总结建议
    disable = [r for r, s in stats.items() if _recommendation(s) == "🚨 disable"]
    review = [r for r, s in stats.items() if _recommendation(s) == "⚠ review"]
    if disable:
        out.append("## 🚨 建议禁用 (avg_pnl < -0.5%, n>=5)")
        out.append("")
        for r in disable:
            out.append(f"- `{r}`: avg={stats[r]['avg_pnl_pct']*100:+.2f}%, n={stats[r]['n']}")
        out.append("")
    if review:
        out.append("## ⚠ 建议审查 (avg_pnl < -0.1%)")
        out.append("")
        for r in review:
            out.append(f"- `{r}`: avg={stats[r]['avg_pnl_pct']*100:+.2f}%, n={stats[r]['n']}")
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Exit Reason × PnL 分布报告 (F13/F22)")
    ap.add_argument("--days", type=int, default=30, help="回看天数 (默认 30)")
    ap.add_argument("--db", type=str, default=None, help="trade_records.db 路径 (默认走 paths.py)")
    ap.add_argument("--output", "-o", type=str, default=None, help="markdown 输出文件 (默认 stdout)")
    ap.add_argument("--json", dest="json_out", type=str, default=None, help="同时输出 JSON 文件")
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    else:
        from app.core.paths import get_trade_records_db
        db_path = str(get_trade_records_db())

    if not os.path.exists(db_path):
        print(f"[ERR] DB 不存在: {db_path}", file=sys.stderr)
        sys.exit(2)

    trades = _load_trades(db_path, args.days)
    if not trades:
        print(f"[WARN] 最近 {args.days} 天无成交记录 (db={db_path})", file=sys.stderr)
        sys.exit(0)

    stats = _aggregate(trades)
    md = _format_markdown(stats, args.days)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        print(f"[OK] markdown 写入 {out_path}", file=sys.stderr)
    else:
        print(md)

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
        print(f"[OK] JSON 写入 {json_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
