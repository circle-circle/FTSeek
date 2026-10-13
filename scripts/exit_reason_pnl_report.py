#!/usr/bin/env python3
"""
F13/F22 (2026-05-14): Exit Reason × Net PnL 分布报告

按 exit_reason × session × direction 分组的净 PnL 分布。
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

from app.risk.exit_reasons import normalize_exit_reason  # noqa: E402


def _load_trades(db_path: str, days: int) -> List[Dict]:
    """读取最近 N 天有 exit_reason 的成交记录"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # 2026-08-28 口径统一: 窗起止一律 ET (原 datetime.now()=服务器 CST 日),
    # 归集用 substr(exit_time,1,10) 而非 date(exit_time) — 后者把行内 -04:00
    # 归一到 UTC (+4h), 与本脚本后文按 ET 墙钟划 session 相互矛盾。
    from app.utils.timezone import now_et
    cutoff = (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")

    metric_columns = [
        name for name in ("pnl_net", "fees_est", "net_pnl_pct")
        if name in {row[1] for row in conn.execute("PRAGMA table_info(trade_records)")}
    ]
    metric_select = f", {', '.join(metric_columns)}" if metric_columns else ""
    rows = conn.execute(
        f"""
        SELECT exit_reason, direction, realized_pnl, pnl_pct, entry_price, quantity,
               entry_time, exit_time, holding_hours, symbol, source_type, trade_date
               {metric_select}
        FROM trade_records
        WHERE exit_reason IS NOT NULL
          AND exit_time IS NOT NULL
          AND substr(exit_time, 1, 10) >= ?
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


def _compute_gross_pnl_pct(t: Dict) -> Optional[float]:
    """返回毛收益率 (0.01 = 1%)"""
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


def _compute_net_pnl_usd(t: Dict) -> Optional[float]:
    net = t.get("pnl_net")
    if net is not None:
        return float(net)
    gross = t.get("realized_pnl")
    fees = t.get("fees_est")
    if gross is not None and fees is not None:
        return float(gross) - float(fees)
    return None


def _compute_net_pnl_pct(t: Dict) -> Optional[float]:
    """返回净收益率 (0.01 = 1%); 没有净额/费用证据时返回 None."""
    stored = t.get("net_pnl_pct")
    if stored is not None:
        return float(stored)
    net = _compute_net_pnl_usd(t)
    qty = float(t.get("quantity") or 0)
    entry = float(t.get("entry_price") or 0)
    if net is not None and qty > 0 and entry > 0:
        return net / (qty * entry)
    return None


def _compute_pnl_pct(t: Dict) -> Optional[float]:
    """兼容旧函数名: 主聚合口径为净收益率."""
    return _compute_net_pnl_pct(t)


def _aggregate(trades: List[Dict]) -> Dict[str, Dict]:
    """按 exit_reason 聚合; avg_pnl_pct 为净口径, avg_gross_pnl_pct 为毛口径."""
    groups: Dict[str, List[Dict]] = {}
    sessions: Dict[str, Dict[str, List[float]]] = {}
    directions: Dict[str, Dict[str, List[float]]] = {}

    for t in trades:
        r = normalize_exit_reason(t.get("exit_reason") or "UNKNOWN")
        pct = _compute_net_pnl_pct(t)
        if pct is None:
            continue
        gross_pct = _compute_gross_pnl_pct(t)
        net_usd = _compute_net_pnl_usd(t)
        gross_usd = t.get("realized_pnl")
        fees_usd = t.get("fees_est")
        groups.setdefault(r, []).append({
            "net_pct": pct,
            "gross_pct": gross_pct,
            "net_usd": net_usd,
            "gross_usd": float(gross_usd) if gross_usd is not None else None,
            "fees_usd": float(fees_usd) if fees_usd is not None else None,
        })
        sess = _classify_session(t.get("exit_time"))
        sessions.setdefault(r, {}).setdefault(sess, []).append(pct)
        dir_ = (t.get("direction") or "UNKNOWN").upper()
        directions.setdefault(r, {}).setdefault(dir_, []).append(pct)

    out: Dict[str, Dict] = {}
    for r, records in groups.items():
        vals = [record["net_pct"] for record in records]
        gross_vals = [record["gross_pct"] for record in records if record["gross_pct"] is not None]
        gross_usd_vals = [record["gross_usd"] for record in records if record["gross_usd"] is not None]
        fees_usd_vals = [record["fees_usd"] for record in records if record["fees_usd"] is not None]
        net_usd_vals = [record["net_usd"] for record in records if record["net_usd"] is not None]
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
            "avg_gross_pnl_pct": statistics.mean(gross_vals) if gross_vals else None,
            "median_pnl_pct": med,
            "p25_pnl_pct": p25,
            "p75_pnl_pct": p75,
            "stdev_pnl_pct": std,
            "sharpe_proxy": sharpe,
            "min_pnl_pct": min(vals) if vals else 0.0,
            "max_pnl_pct": max(vals) if vals else 0.0,
            "gross_pnl_usd": sum(gross_usd_vals) if gross_usd_vals else None,
            "fees_usd": sum(fees_usd_vals) if fees_usd_vals else None,
            "net_pnl_usd": sum(net_usd_vals) if net_usd_vals else None,
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
    out.append(f"# Exit Reason × Net PnL Distribution Report")
    out.append("")
    out.append(f"- 周期: 最近 **{days}** 天")
    out.append(f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}")
    out.append(f"- 总规则数: {len(stats)}")
    out.append("- 口径: `avg_pnl_pct`/`win_rate`/建议=净额；`avg_gross_pnl_pct`=毛价差；金额单位=USD")
    out.append("")
    out.append("## 主表 (按 avg_pnl_pct 降序)")
    out.append("")
    out.append("| exit_reason | n | win_rate | avg_gross% | avg_net% | median_net% | gross$ | fees$ | net$ | min/max_net% | recommendation |")
    out.append("|-------------|---:|---------:|-----------:|---------:|------------:|-------:|------:|-----:|-------------:|----------------|")
    for r, s in rows:
        out.append(
            f"| `{r}` | {s['n']} | {s['win_rate']:.0%} | "
            f"{(s['avg_gross_pnl_pct'] or 0)*100:+.2f} | "
            f"{s['avg_pnl_pct']*100:+.2f} | {s['median_pnl_pct']*100:+.2f} | "
            f"{(s['gross_pnl_usd'] or 0):+.2f} | "
            f"{(s['fees_usd'] or 0):.2f} | "
            f"{(s['net_pnl_usd'] or 0):+.2f} | "
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
