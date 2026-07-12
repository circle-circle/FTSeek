#!/usr/bin/env python3
"""
Backfill: 把 signal_snapshots.db 历史分析灌入 Obsidian per-symbol pages.

从 signal_snapshots 读取 deep_analysis 信号, 对每条调用
ObsidianWriter.write_symbol_analysis, 生成历史 symbol pages。

用法:
  python3 backfill_symbol_pages.py           # 全量回填
  python3 backfill_symbol_pages.py --dry-run # 只看不写
  python3 backfill_symbol_pages.py --symbol US.AAPL  # 单标的

幂等: 已存在的 decision_id 会被跳过 (ObsidianWriter 内部幂等)。
"""
import os
import sys
import json
import sqlite3
from datetime import datetime
from pathlib import Path

FTSEEK = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, FTSEEK)

from app.core.obsidian_writer import get_obsidian_writer

DB_PATH = os.path.join(FTSEEK, "data", "signal_snapshots.db")


def _parse_factors(factors_json: str) -> list:
    """解析 factors_json, 兼容旧格式(字符串数组)和新格式(对象数组)"""
    if not factors_json:
        return []
    try:
        factors = json.loads(factors_json)
    except Exception:
        return []
    result = []
    for f in factors:
        if isinstance(f, str):
            # 旧格式: "above_ma20" → 转为最小 claim 对象
            result.append({"claim_type": f, "thesis": f, "strength": "MEDIUM", "source_type": ""})
        elif isinstance(f, dict):
            result.append(f)
    return result


def _build_analysis_from_snapshot(row: sqlite3.Row) -> dict:
    """从 signal_snapshots 行构建 analysis dict (ObsidianWriter 期望的格式)"""
    symbol = row["symbol"]
    direction = row["direction"]  # LONG/SHORT/NEUTRAL
    strength = row["strength"] or 50
    confidence = row["confidence"] or 0.5
    trade_date = row["trade_date"]
    decision_id = row["decision_id"]
    price = row["price_at_signal"] or 0

    # 方向映射
    action = {"LONG": "BUY", "SHORT": "SELL_SHORT"}.get(direction, "SKIP")
    rating = {"LONG": "看多", "SHORT": "看空"}.get(direction, "观望")
    score = strength / 10.0

    # factors → claim 对象
    factors = _parse_factors(row["factors_json"])
    bull_cases = [f for f in factors if f.get("side") == "bull"]
    bear_cases = [f for f in factors if f.get("side") == "bear"]
    # 旧格式没有 side, 按方向猜
    if not bull_cases and not bear_cases and factors:
        if direction == "LONG":
            bull_cases = factors
        elif direction == "SHORT":
            bear_cases = factors

    # IC 数据
    fwd_ret_t1 = row["fwd_ret_t1"]
    alpha_t1 = row["alpha_t1"]
    fwd_ret_t5 = row["fwd_ret_t5"]
    alpha_t5 = row["alpha_t5"]

    # IC 验证结果
    ic_verdict = ""
    if fwd_ret_t1 is not None:
        ic_verdict = f"T+1: fwd={fwd_ret_t1:+.4f}"
        if alpha_t1 is not None:
            ic_verdict += f" alpha={alpha_t1:+.4f}"
    if fwd_ret_t5 is not None:
        ic_verdict += f" | T+5: fwd={fwd_ret_t5:+.4f}"
        if alpha_t5 is not None:
            ic_verdict += f" alpha={alpha_t5:+.4f}"

    return {
        "symbol": symbol,
        "name": "",  # 历史数据可能没有name
        "rating": rating,
        "confidence": f"{confidence:.1f}",
        "score": score,
        "action": action,
        "conclusion": f"历史回填信号 {trade_date} {direction} strength={strength}",
        "_ic_decision_id": decision_id,
        "snapshot": {"price": price},
        "plates": [],
        "data_quality": {"ok": 0, "total": 0, "coverage_pct": 0},
        "_llm_output": {
            "bull_case": bull_cases,
            "bear_case": bear_cases,
            "execution_plan": {},
            "causal_chain": "",
            "risks": [],
        },
        # 用 trade_date 覆盖 datetime.now()
        "_backfill_date": trade_date,
        "_backfill_ic_verdict": ic_verdict,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    target_symbol = None
    for i, arg in enumerate(sys.argv):
        if arg == "--symbol" and i + 1 < len(sys.argv):
            target_symbol = sys.argv[i + 1]

    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    where_clause = "WHERE strategy_id = 'deep_analysis'"
    if target_symbol:
        where_clause += f" AND symbol = '{target_symbol}'"
    where_clause += " ORDER BY trade_date ASC"

    rows = conn.execute(f"""
        SELECT decision_id, symbol, direction, strength, confidence,
               trade_date, price_at_signal, factors_json,
               fwd_ret_t1, alpha_t1, fwd_ret_t5, alpha_t5
        FROM signal_snapshots
        {where_clause}
    """).fetchall()
    conn.close()

    print(f"Found {len(rows)} historical signals to backfill")
    if dry_run:
        for r in rows[:10]:
            print(f"  {r['trade_date']} {r['symbol']} {r['direction']} str={r['strength']} id={r['decision_id']}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    ow = get_obsidian_writer()
    written = 0
    skipped = 0

    for row in rows:
        analysis = _build_analysis_from_snapshot(row)

        # 检查是否已存在 (幂等)
        safe_name = analysis["symbol"].replace(".", "_")
        filepath = os.path.join(ow.vault, "symbols", f"{safe_name}.md")
        decision_id = analysis["_ic_decision_id"]
        id_suffix = decision_id.split("-")[-1] if decision_id else ""
        section_anchor = f"{row['trade_date']}-{id_suffix}"

        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                existing = f.read()
            # 检查同 trade_date+id 是否已存在
            # ObsidianWriter 用 datetime.now() 生成 date_str, 回填需覆盖
            # 我们检查 decision_id 是否已在文件中
            if decision_id and decision_id in existing:
                skipped += 1
                continue

        # 临时 hack: 覆盖 date_str (ObsidianWriter 内部用 datetime.now())
        # 通过 monkey-patch datetime 来实现太重, 改为直接写
        _write_backfill_section(ow, analysis, row, section_anchor)
        written += 1

    print(f"Backfill done: {written} written, {skipped} skipped (already existed)")


def _write_backfill_section(ow, analysis: dict, row: sqlite3.Row, section_anchor: str):
    """直接写 backfill section (复用 ObsidianWriter 的逻辑但用 trade_date 而非 now)"""
    try:
        import json as _json

        symbol = analysis["symbol"]
        safe_name = symbol.replace(".", "_")
        filename = f"{safe_name}.md"
        date_str = row["trade_date"]
        decision_id = analysis["_ic_decision_id"]

        llm_out = analysis["_llm_output"]
        bull_cases = llm_out.get("bull_case", [])
        bear_cases = llm_out.get("bear_case", [])

        claim_ic = ow._load_claim_ic()

        bull_lines = []
        for i, r in enumerate(bull_cases[:5], 1):
            thesis = r.get("thesis", "")[:120]
            st = r.get("strength", "MEDIUM")
            ct = r.get("claim_type", "")
            ic_tag = ow._claim_ic_tag(ct, claim_ic)
            bull_lines.append(f"  {i}. [{st}]{ic_tag} {thesis}")
        bear_lines = []
        for i, r in enumerate(bear_cases[:5], 1):
            thesis = r.get("thesis", "")[:120]
            st = r.get("strength", "MEDIUM")
            ct = r.get("claim_type", "")
            ic_tag = ow._claim_ic_tag(ct, claim_ic)
            bear_lines.append(f"  {i}. [{st}]{ic_tag} {thesis}")

        ic_summary = ow._build_ic_summary(bull_cases + bear_cases, claim_ic)
        ic_verdict = analysis.get("_backfill_ic_verdict", "")

        stats = ow._read_symbol_stats(symbol)
        score = analysis["score"]
        rating = analysis["rating"]
        action = analysis["action"]
        price = analysis["snapshot"]["price"]
        strength = row["strength"]

        section = f"""
## {date_str} | {rating} | str={strength} | {action}
<!-- id:{section_anchor} -->

> 历史回填信号 {date_str} {row['direction']} strength={strength}

- **标的**: {symbol} | 现价: {price} | 评分: {score}/10
- **IC ID**: `{decision_id}`
- **IC验证**: {ic_verdict or '待回填'}

### 看多论据
{chr(10).join(bull_lines) if bull_lines else '- 无'}

### 看空论据
{chr(10).join(bear_lines) if bear_lines else '- 无'}

### 论据IC验证
{ic_summary}
---
"""

        filepath = os.path.join(ow.vault, "symbols", filename)
        os.makedirs(os.path.join(ow.vault, "symbols"), exist_ok=True)

        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing = f.read()
            if section_anchor in existing or (decision_id and decision_id in existing):
                return  # skip
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(section)
            # update frontmatter
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            content = ow._replace_frontmatter(content, {
                "latest_date": date_str,
                "latest_direction": action,
                "latest_score": score,
                "latest_rating": rating,
                "total_trades": stats.get("total_trades", 0),
                "win_rate": stats.get("win_rate", 0),
            })
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            fm = ow._fm(
                type="symbol", symbol=symbol, name=analysis.get("name", ""),
                latest_date=date_str, latest_direction=action,
                latest_score=score, latest_rating=rating,
                total_trades=stats.get("total_trades", 0),
                win_rate=stats.get("win_rate", 0),
                tags=["symbol", "backfill"],
            )
            content = f"""{fm}

# {symbol}

> 本页由 backfill 脚本从 signal_snapshots.db 历史数据回填生成。
> 后续新分析会自动追加。

{section}"""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

    except Exception as e:
        print(f"  backfill write failed: {symbol} {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
