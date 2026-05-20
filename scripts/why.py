#!/usr/bin/env python3
"""
LLM 复盘对话 (L10, 2026-05-14)

用法:
  python scripts/why.py NVDA 2026-05-14
  python scripts/why.py NVDA 2026-05-14 --question "为什么这笔亏了"
  python scripts/why.py --recent 5  # 最近 5 笔交易自动复盘

LLM 解读完整交易链路: 入场理由 / 因子分布 / 退出原因 / PnL 归因 / 教训.

可控:
- 默认 enabled (CLI 工具, 无生产副作用)
- 仅读 DB, 不写 DB
- 单次 < 60s
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("why")


def _fetch_trade(symbol: str, date_str: str):
    """读 trade_records + trade_attribution"""
    from app.core.paths import get_trade_records_db
    db = str(get_trade_records_db())
    if not os.path.exists(db):
        print(f"[ERR] DB 不存在: {db}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # trade_records
    rows = conn.execute(
        "SELECT * FROM trade_records WHERE symbol = ? AND trade_date = ? "
        "ORDER BY entry_time", (symbol, date_str)
    ).fetchall()
    if not rows:
        # 找近 1 周
        rows = conn.execute(
            "SELECT * FROM trade_records WHERE symbol = ? AND trade_date >= date(?, '-7 days') "
            "ORDER BY entry_time DESC LIMIT 5", (symbol, date_str)
        ).fetchall()
        if not rows:
            return None
        print(f"[INFO] {symbol} 在 {date_str} 无交易, 显示最近 5 笔")
    trades = [dict(r) for r in rows]

    # 关联 attribution
    for t in trades:
        trace_id = t.get("trace_id")
        if trace_id:
            ar = conn.execute(
                "SELECT * FROM trade_attribution WHERE trace_id = ? LIMIT 1",
                (trace_id,)
            ).fetchone()
            if ar:
                t["attribution"] = dict(ar)
    conn.close()
    return trades


def _fetch_recent(n: int):
    from app.core.paths import get_trade_records_db
    db = str(get_trade_records_db())
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trade_records WHERE exit_time IS NOT NULL "
        "ORDER BY exit_time DESC LIMIT ?", (n,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _build_prompt(trades, question=""):
    summary = []
    for i, t in enumerate(trades[:5], 1):
        attribution = t.get("attribution", {}) or {}
        summary.append(
            f"[{i}] {t.get('symbol')} {t.get('direction')} entry={t.get('entry_time','?')} "
            f"exit={t.get('exit_time','?')}\n"
            f"    Entry: ${t.get('entry_price', 0):.2f}, Exit: ${t.get('exit_price', 0):.2f}\n"
            f"    Qty: {t.get('quantity', 0)}, PnL: ${t.get('realized_pnl', 0):.2f} "
            f"({(t.get('pnl_pct', 0) or 0)*100:+.2f}%)\n"
            f"    Exit Reason: {t.get('exit_reason', '?')}, Holding: {t.get('holding_hours', 0):.1f}h\n"
            f"    Strength: {t.get('signal_strength', '?')}, Confidence: {t.get('signal_confidence', '?')}\n"
            f"    Owner: {t.get('owner_strategy', 'main')}\n"
            f"    Entry Thesis: {attribution.get('entry_thesis', 'unknown')}\n"
            f"    Factors: trend={attribution.get('factor_trend','?')}, momentum={attribution.get('factor_momentum','?')}\n"
        )
    summary_text = "\n".join(summary)

    base_q = "Explain why each trade succeeded or failed. Identify lessons."
    if question:
        base_q = question

    prompt = f"""You are FTSeek's trade post-mortem analyst. Below are recent trades.

{summary_text}

{base_q}

Format your answer:
## 总结 (一句话)
## 逐笔分析
## 关键教训 (≤ 3 条)
## 改进建议 (具体可操作)
"""
    return prompt


def main():
    ap = argparse.ArgumentParser(description="LLM 复盘对话 (L10)")
    ap.add_argument("symbol", nargs="?", help="股票代码 (如 NVDA)")
    ap.add_argument("date", nargs="?", default=None, help="交易日期 YYYY-MM-DD (默认今日)")
    ap.add_argument("--question", "-q", default="", help="自定义复盘问题")
    ap.add_argument("--recent", type=int, help="不指定 symbol/date, 取最近 N 笔")
    args = ap.parse_args()

    if args.recent:
        trades = _fetch_recent(args.recent)
        if not trades:
            print("[ERR] 无最近交易记录", file=sys.stderr)
            sys.exit(2)
    else:
        if not args.symbol:
            ap.print_help()
            sys.exit(1)
        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        trades = _fetch_trade(args.symbol, date_str)
        if not trades:
            print(f"[ERR] 无 {args.symbol} {date_str} 的交易记录", file=sys.stderr)
            sys.exit(2)

    print(f"\n=== 找到 {len(trades)} 笔交易, 调用 LLM 复盘... ===\n")

    from app.llm.features.feature_flags import is_enabled
    if not is_enabled("replay_dialog"):
        print("[INFO] replay_dialog feature 已 disabled, 启用 yaml `llm_features.replay_dialog.enabled=true`")
        sys.exit(0)

    from app.llm.features.llm_helper import call_llm

    prompt = _build_prompt(trades, args.question)
    response = call_llm(
        feature="replay_dialog",
        prompt=prompt,
        system="You are FTSeek's trade post-mortem analyst. Be specific and concise.",
        model="qwen",
        temperature=0.2,
        max_tokens=1500,
        timeout=60,
    )

    if response is None:
        print("[ERR] LLM 调用失败", file=sys.stderr)
        sys.exit(2)
    print(response)


if __name__ == "__main__":
    main()
