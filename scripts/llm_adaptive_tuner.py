#!/usr/bin/env python3
"""LLM Adaptive Tuner (2026-05-19)

每日 04:30 ET 跑一次 (盘后), 用 LLM 看当日 + 7d trade_records, 给出 yaml 调参建议.

第一性原则: LLM 应做量化做不到的事 — 多源综合 + 因果推理.
取代手工每天改 yaml (Phase 1 5 个改动都是手工).

输出: data/llm_proposals/tuner_<date>.json — 人工每周一 review 后采纳.

用法:
  python scripts/llm_adaptive_tuner.py             # 默认日级 cron
  python scripts/llm_adaptive_tuner.py --dry-run   # 不调 LLM, 仅显示输入数据
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("ftseek.llm_adaptive_tuner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Schema for LLM output
_OUTPUT_SCHEMA_REQUIRED = {
    "summary": str,
    "proposals": list,
}


def _query_today_stats() -> dict:
    """今日交易统计 (按 strategy 分类)"""
    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            owner_strategy, direction, COUNT(*) n,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins,
            ROUND(SUM(realized_pnl), 2) pnl,
            ROUND(AVG(realized_pnl), 2) avg_pnl,
            ROUND(AVG(holding_hours), 2) avg_hours,
            ROUND(MIN(realized_pnl), 2) worst,
            ROUND(MAX(realized_pnl), 2) best
        FROM trade_records
        WHERE trade_date = date('now', 'localtime')
          AND realized_pnl IS NOT NULL
        GROUP BY owner_strategy, direction
        """
    )
    rows = cur.fetchall()
    cols = ["owner_strategy", "direction", "n", "wins", "pnl",
            "avg_pnl", "avg_hours", "worst", "best"]
    today = [dict(zip(cols, r)) for r in rows]

    # 7d 滚动
    cur.execute(
        """
        SELECT direction, COUNT(*) n,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(realized_pnl), 2) pnl
        FROM trade_records
        WHERE date(entry_time) >= date('now', '-7 days')
          AND realized_pnl IS NOT NULL
        GROUP BY direction
        """
    )
    rows_7d = cur.fetchall()
    cols_7d = ["direction", "n", "wins", "pnl"]
    rolling_7d = [dict(zip(cols_7d, r)) for r in rows_7d]

    # 拒绝统计 (今日)
    rej_db = PROJECT_ROOT / "data" / "rejections.db"
    by_reason = []
    try:
        rconn = sqlite3.connect(str(rej_db))
        rcur = rconn.cursor()
        rcur.execute(
            "SELECT reason_category, COUNT(*) FROM rejections "
            "WHERE date(timestamp) = date('now', 'localtime') "
            "GROUP BY reason_category"
        )
        by_reason = [{"reason": r[0], "n": r[1]} for r in rcur.fetchall()]
        rconn.close()
    except Exception:
        pass

    conn.close()
    return {"today": today, "rolling_7d": rolling_7d, "rejections_today": by_reason}


def _query_current_thresholds() -> dict:
    """当前 yaml 关键阈值 (供 LLM 参考)"""
    try:
        import yaml
        cfg_path = PROJECT_ROOT / "config" / "unified_config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        return {
            "scalping_signal_cooldown_sec": cfg.get("scalping", {}).get("signal_cooldown_sec", 300),
            "scalping_loss_cooldown_sec": 90,  # P0-2 加的固定值
            "scalping_breakeven_after_minutes": 6,  # B.1 P1 后
            "main_short_effective_threshold": 65,  # order_executor:807
            "risk_max_position_pct": cfg.get("risk", {}).get("max_position_pct", 0.25),
            "risk_max_total_position_pct": cfg.get("risk", {}).get("max_total_position_pct", 0.80),
            "circuit_breaker_max_consecutive": 3,
            "scalping_min_price": 20.0,  # B.2 后
        }
    except Exception as e:
        logger.warning(f"[Tuner] yaml 阈值读取失败: {e}")
        return {}


def _build_prompt(stats: dict, thresholds: dict) -> tuple[str, str]:
    system = (
        "You are a quantitative trading parameter tuning analyst. Your job: based on today's "
        "trading data + 7d rolling baseline, suggest 3-7 SPECIFIC yaml parameter adjustments.\n\n"
        "Decision principles:\n"
        "- Each suggestion must specify exact yaml_path, current_value, suggested_value.\n"
        "- Quantify expected_impact_usd_per_day (positive = improvement).\n"
        "- Confidence level: high (clear signal in data), medium (plausible), low (speculative).\n"
        "- Do NOT suggest changes if data sample is too small (<5 trades).\n"
        "- Conservative: prefer small adjustments (e.g., +/- 20%) over drastic changes.\n\n"
        "Output strict JSON: \n"
        "{\n"
        '  "summary": "<= 200 chars overall trading day diagnosis",\n'
        '  "proposals": [\n'
        '    {\n'
        '      "param_name": "...", "yaml_path": "scalping.xxx",\n'
        '      "current_value": any, "suggested_value": any,\n'
        '      "reason": "<= 150 chars data-driven justification",\n'
        '      "expected_impact_usd_per_day": float,\n'
        '      "confidence": "high|medium|low"\n'
        '    }\n'
        '  ]\n'
        "}\n"
    )

    user = (
        f"## Today's trading stats (by strategy x direction):\n"
        + json.dumps(stats["today"], ensure_ascii=False, indent=2)
        + f"\n\n## 7d rolling baseline:\n"
        + json.dumps(stats["rolling_7d"], ensure_ascii=False, indent=2)
        + f"\n\n## Today's rejections:\n"
        + json.dumps(stats["rejections_today"], ensure_ascii=False, indent=2)
        + f"\n\n## Current yaml thresholds:\n"
        + json.dumps(thresholds, ensure_ascii=False, indent=2)
        + "\n\nGenerate 3-7 specific tuning proposals."
    )
    return system, user


def adaptive_tune(dry_run: bool = False) -> dict:
    """主入口: 每日跑, 输出 yaml 调参建议"""
    logger.info(f"[AdaptiveTuner] 开始数据收集")

    stats = _query_today_stats()
    thresholds = _query_current_thresholds()
    today_n = sum(s["n"] for s in stats["today"])
    logger.info(f"[AdaptiveTuner] 今日 {today_n} 笔交易, 7d 累积 "
                f"{sum(s['n'] for s in stats['rolling_7d'])} 笔")

    if today_n == 0:
        logger.warning("[AdaptiveTuner] 今日 0 笔交易, 跳过 (no data)")
        return {}

    if dry_run:
        print("=== Today's stats ===")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        print("\n=== Current thresholds ===")
        print(json.dumps(thresholds, ensure_ascii=False, indent=2))
        return {}

    system, user = _build_prompt(stats, thresholds)
    logger.info(f"[AdaptiveTuner] 调用 LLM (prompt={len(system)+len(user)} chars)")

    try:
        from app.llm.model_call import call_llm
        res = call_llm(
            "adaptive_tuner",
            user, system=system,
            temperature=0.0, max_tokens=1500, timeout=60,
            schema_required=_OUTPUT_SCHEMA_REQUIRED,
        )
        if not res or not res.success or not isinstance(res.response, dict):
            logger.error(f"[AdaptiveTuner] LLM 失败: {res.error if res else 'no result'}")
            return {}
        result = res.response
        result["_meta"] = {
            "model": res.model_used, "latency_ms": int(res.latency_ms),
            "cost_usd": round(res.cost_usd, 4),
            "today_trades": today_n,
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.exception(f"[AdaptiveTuner] LLM 异常: {e}")
        return {}

    # 写 proposal 文件
    output_dir = PROJECT_ROOT / "data" / "llm_proposals"
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"tuner_{date_str}.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[AdaptiveTuner] ✅ 已写: {output_path}")

    # 摘要打印
    proposals = result.get("proposals", [])
    print(f"\n=== Adaptive Tuner Result ({date_str}) ===")
    print(f"Summary: {result.get('summary', '')}\n")
    for p in proposals:
        cf = p.get("confidence", "?")
        impact = p.get("expected_impact_usd_per_day", 0)
        print(f"  [{cf}] {p.get('param_name')}: "
              f"{p.get('current_value')} → {p.get('suggested_value')} "
              f"(~${impact}/day)")
        print(f"    {p.get('reason')}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Adaptive Tuner")
    parser.add_argument("--dry-run", action="store_true", help="不调 LLM, 仅显示输入数据")
    args = parser.parse_args()
    adaptive_tune(dry_run=args.dry_run)
