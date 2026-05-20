#!/usr/bin/env python3
"""LLM Failure Mode Miner (2026-05-19)

每周日 04:30 ET 跑一次, 用 LLM 扫前 7 天所有 loss trades, 找出失败模式.

第一性原则: LLM 应做量化做不到的事 — 跨时间模式识别 + 多源因果推理.
取代静态 5 因子归因 (noise_pnl=-$15k 5 天不变, 无诊断价值).

输出: data/llm_proposals_<date>.yaml — 人工 review 后写入 unified_config.yaml

用法:
  python scripts/llm_failure_miner.py                # 默认扫前 7 天
  python scripts/llm_failure_miner.py --days 14      # 扫前 14 天
  python scripts/llm_failure_miner.py --apply        # 自动应用 high confidence 建议 (谨慎!)
  python scripts/llm_failure_miner.py --dry-run      # 不调 LLM, 仅显示输入数据
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("ftseek.llm_failure_miner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Schema for LLM output
_OUTPUT_SCHEMA_REQUIRED = {
    "modes": list,  # 失败模式列表
    "summary": str,  # 整体诊断
}


def _query_loss_trades(days: int) -> list:
    """查 trade_records 前 N 天 loss trades + 关键归因字段."""
    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            symbol, direction, owner_strategy,
            ROUND(realized_pnl, 2), exit_reason,
            ROUND(holding_hours, 2), market_regime, trade_date,
            signal_strength, signal_confidence,
            ROUND(entry_price, 2), ROUND(exit_price, 2)
        FROM trade_records
        WHERE realized_pnl < 0
          AND date(entry_time) >= date('now', ?)
          AND realized_pnl IS NOT NULL
        ORDER BY realized_pnl ASC
        LIMIT 200
        """,
        (f"-{int(days)} days",),
    )
    rows = cur.fetchall()
    conn.close()
    columns = ["symbol", "direction", "owner_strategy", "pnl", "exit_reason",
               "holding_hours", "market_regime", "trade_date",
               "signal_strength", "signal_confidence", "entry_price", "exit_price"]
    return [dict(zip(columns, r)) for r in rows]


def _query_winning_patterns(days: int) -> list:
    """查 winning trades — 让 LLM 也看成功模式."""
    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, direction, owner_strategy,
               ROUND(realized_pnl, 2), exit_reason,
               ROUND(holding_hours, 2), market_regime
        FROM trade_records
        WHERE realized_pnl > 50
          AND date(entry_time) >= date('now', ?)
        ORDER BY realized_pnl DESC LIMIT 20
        """,
        (f"-{int(days)} days",),
    )
    rows = cur.fetchall()
    conn.close()
    cols = ["symbol", "direction", "owner_strategy", "pnl", "exit_reason",
            "holding_hours", "market_regime"]
    return [dict(zip(cols, r)) for r in rows]


def _aggregate_by_pattern(trades: list) -> list:
    """按 (symbol, direction) 聚合, 找重复亏损模式."""
    from collections import defaultdict
    groups = defaultdict(list)
    for t in trades:
        key = (t["symbol"], t["direction"])
        groups[key].append(t)
    aggregated = []
    for (symbol, direction), group in groups.items():
        if len(group) < 2:
            continue
        total_pnl = sum(t["pnl"] for t in group)
        aggregated.append({
            "symbol": symbol,
            "direction": direction,
            "occurrences": len(group),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(group), 2),
            "common_exit_reason": max(
                set(t["exit_reason"] for t in group if t["exit_reason"]),
                key=lambda x: sum(1 for t in group if t["exit_reason"] == x),
                default=""
            ),
            "owner_strategies": list(set(t["owner_strategy"] for t in group if t["owner_strategy"])),
        })
    aggregated.sort(key=lambda x: x["total_pnl"])  # 最亏的先排
    return aggregated[:15]  # 取最差 15 个


def _build_prompt(loss_aggregates: list, winners: list, days: int) -> tuple[str, str]:
    """构造 LLM prompt — 给 LLM 失败聚合 + 成功样本, 让它找模式."""
    system = (
        "You are a quantitative trading post-mortem analyst. Your job: identify ACTIONABLE failure "
        "patterns from recent loss trades, and suggest concrete configuration changes (NOT generic advice).\n\n"
        "Decision principles:\n"
        "- Focus on patterns with >=3 occurrences AND total_pnl < -$100 (signal vs noise).\n"
        "- Compare to winning patterns — what's structurally different?\n"
        "- Suggested actions must be EXECUTABLE (yaml param/value), not vague advice.\n"
        "- Confidence: high if pattern is clear (>=5 occurrences, consistent), medium if 3-4.\n\n"
        "Output strict JSON: \n"
        "{\n"
        '  "summary": "<= 200 chars overall diagnosis",\n'
        '  "modes": [\n'
        '    {\n'
        '      "id": "M1", "pattern": "<= 100 chars description",\n'
        '      "occurrences": int, "total_pnl": float,\n'
        '      "action": {\n'
        '        "type": "blacklist|cooldown|amplify|threshold_change",\n'
        '        "target": "symbol or strategy",\n'
        '        "yaml_path": "e.g., scalping.signal_cooldown_sec",\n'
        '        "current_value": any,\n'
        '        "suggested_value": any,\n'
        '        "expected_impact_usd_per_week": float\n'
        '      },\n'
        '      "confidence": "high|medium|low",\n'
        '      "rationale": "<= 100 chars why"\n'
        '    }\n'
        '  ]\n'
        "}\n"
    )

    user = (
        f"Analyze last {days} days of trading.\n\n"
        f"## Aggregated loss patterns (top 15 by total_pnl):\n"
        + json.dumps(loss_aggregates, ensure_ascii=False, indent=2)
        + f"\n\n## Winning patterns ({len(winners)} samples for contrast):\n"
        + json.dumps(winners, ensure_ascii=False, indent=2)
        + "\n\nIdentify 3-7 actionable failure modes with executable config changes."
    )
    return system, user


def mine_failure_modes(days: int = 7, apply: bool = False, dry_run: bool = False) -> dict:
    """主入口: 挖失败模式, 输出 yaml 建议."""
    logger.info(f"[FailureMiner] 开始挖掘前 {days} 天失败模式")

    # 1. 查数据
    losses = _query_loss_trades(days)
    if not losses:
        logger.warning("[FailureMiner] 无 loss trades, 退出")
        return {}
    aggregates = _aggregate_by_pattern(losses)
    winners = _query_winning_patterns(days)
    logger.info(f"[FailureMiner] 数据: {len(losses)} losses → {len(aggregates)} 聚合模式, {len(winners)} winners")

    # 2. dry-run: 仅显示输入数据
    if dry_run:
        print("=== Loss aggregates (top 15) ===")
        print(json.dumps(aggregates, ensure_ascii=False, indent=2))
        print("\n=== Winners ===")
        print(json.dumps(winners, ensure_ascii=False, indent=2))
        return {}

    # 3. LLM 调用
    system, user = _build_prompt(aggregates, winners, days)
    logger.info(f"[FailureMiner] 调用 LLM (prompt={len(system)+len(user)} chars)")

    try:
        from app.llm.model_call import call_llm
        res = call_llm(
            "failure_mode_miner",
            user, system=system,
            temperature=0.0, max_tokens=2000, timeout=60,
            schema_required=_OUTPUT_SCHEMA_REQUIRED,
        )
        if not res or not res.success or not isinstance(res.response, dict):
            logger.error(f"[FailureMiner] LLM 失败: {res.error if res else 'no result'}")
            return {}
        result = res.response
        result["_meta"] = {
            "model": res.model_used, "latency_ms": int(res.latency_ms),
            "cost_usd": round(res.cost_usd, 4),
            "data_window_days": days,
            "loss_count": len(losses),
            "pattern_count": len(aggregates),
            "winner_count": len(winners),
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.exception(f"[FailureMiner] LLM 异常: {e}")
        return {}

    # 4. 写 yaml
    output_dir = PROJECT_ROOT / "data" / "llm_proposals"
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"failure_modes_{date_str}.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[FailureMiner] ✅ 已写: {output_path}")

    # 5. 摘要打印
    modes = result.get("modes", [])
    summary = result.get("summary", "")
    print(f"\n=== Failure Mode Miner Result ({date_str}) ===")
    print(f"Summary: {summary}\n")
    for m in modes:
        cf = m.get("confidence", "?")
        impact = m.get("action", {}).get("expected_impact_usd_per_week", 0)
        print(f"  [{cf}] {m.get('id')}: {m.get('pattern')} (impact ~${impact}/week)")
        action = m.get("action", {})
        if action:
            print(f"    → {action.get('type')}: {action.get('yaml_path')} "
                  f"{action.get('current_value')} → {action.get('suggested_value')}")

    if apply:
        logger.warning("[FailureMiner] --apply 启用: 高 confidence 建议将自动写入 unified_config.yaml")
        # TODO: 实施自动应用 (需谨慎设计 yaml 回滚机制)
        logger.warning("[FailureMiner] --apply 自动应用待 Phase 2 实施, 当前仅生成 proposal 文件")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Failure Mode Miner")
    parser.add_argument("--days", type=int, default=7, help="天数窗口 (默认 7)")
    parser.add_argument("--apply", action="store_true", help="自动应用高 confidence 建议 (谨慎)")
    parser.add_argument("--dry-run", action="store_true", help="不调 LLM, 仅显示输入数据")
    args = parser.parse_args()
    mine_failure_modes(days=args.days, apply=args.apply, dry_run=args.dry_run)
