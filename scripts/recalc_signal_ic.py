#!/usr/bin/env python3
"""
B2: Signal IC 历史重算脚本 (2026-07-19)

背景:
  A2 修复前, signal_ic_tracker 对 SHORT 信号的 strength 取负, 但 fwd_ret 回填时
  已按方向调整 (_calc_fwd: SHORT 用 t0/close_tn-1), 导致双重调整 → SHORT IC 系统性反号.
  修复后新数据正确, 但历史 IC 聚合指标已被污染.

职责:
  1. 从 signal_snapshots.db 读取全量信号 (不修改原始 strength/fwd_ret)
  2. 用 raw strength (LONG/SHORT 均不取负, 仅跳过 NEUTRAL) 重算 IC 聚合
  3. 按 strategy_id 分段产出 (禁混策略 IC 指导单策略调参)
  4. 输出 SHORT 相关因子 IC 差值报告 + 分策略 IC 表
  5. 覆盖 data/factor_ic_latest.json + data/ic_analysis_latest.json

用法:
  python scripts/recalc_signal_ic.py              # 重算并覆盖
  python scripts/recalc_signal_ic.py --dry-run    # 仅输出报告, 不覆盖文件

验收标准 (DEB-20260719 F.2):
  - 重算后全局 IC 与重算前差异应 >0.01 (否则说明 SHORT 污染面不够大)
  - 分策略 IC 表至少覆盖 3 个 strategy_id
  - 脚本幂等 (重复运行结果一致)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 路径设置
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("recalc_ic")

# DB 路径
SNAPSHOTS_DB = PROJECT_ROOT / "data" / "signal_snapshots.db"
FACTOR_IC_OUT = PROJECT_ROOT / "data" / "factor_ic_latest.json"
IC_ANALYSIS_OUT = PROJECT_ROOT / "data" / "ic_analysis_latest.json"


def _rank_values(values: List[float]) -> List[float]:
    """Pure-Python rank (average rank for ties, 1-based)."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(x: List[float], y: List[float]) -> Optional[float]:
    """Pure-Python Spearman rank correlation."""
    if not x or len(x) != len(y) or len(x) < 3:
        return None
    xr = _rank_values(x)
    yr = _rank_values(y)
    n = len(xr)
    mx = sum(xr) / n
    my = sum(yr) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    vx = sum((a - mx) ** 2 for a in xr)
    vy = sum((b - my) ** 2 for b in yr)
    if vx == 0 or vy == 0:
        return None
    return float(cov / math.sqrt(vx * vy))


def load_snapshots(db_path: Path) -> List[Dict]:
    """从 signal_snapshots.db 读取全量信号."""
    if not db_path.exists():
        logger.error(f"DB 不存在: {db_path}")
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_date, symbol, direction, strength, strategy_id, "
        "fwd_ret_t1, fwd_ret_t5, fwd_ret_t20 "
        "FROM signal_snapshots "
        "WHERE direction IS NOT NULL AND direction != 'NEUTRAL' "
        "AND strength IS NOT NULL AND fwd_ret_t1 IS NOT NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calc_ic(rows: List[Dict], forward_col: str = "fwd_ret_t1") -> Optional[float]:
    """计算 IC (Spearman correlation between raw strength and forward return).
    
    A2 修复后逻辑: LONG/SHORT 均用 raw strength, 不取负.
    fwd_ret 本身已按方向调整 (_calc_fwd), 此处不再调整.
    """
    strengths = []
    fwd_rets = []
    for r in rows:
        s = r.get("strength")
        fr = r.get(forward_col)
        if s is None or fr is None:
            continue
        try:
            s = float(s)
            fr = float(fr)
            if math.isnan(s) or math.isnan(fr) or math.isinf(s) or math.isinf(fr):
                continue
        except (ValueError, TypeError):
            continue
        strengths.append(s)
        fwd_rets.append(fr)
    return _spearman(strengths, fwd_rets)


def calc_daily_ic(rows: List[Dict], forward_col: str = "fwd_ret_t1") -> Dict[str, float]:
    """按日计算 IC."""
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["trade_date"]].append(r)
    daily_ic = {}
    for date, day_rows in by_date.items():
        if len(day_rows) < 3:
            continue
        ic = calc_ic(day_rows, forward_col)
        if ic is not None:
            daily_ic[date] = ic
    return daily_ic


def calc_strategy_ic(rows: List[Dict], forward_col: str = "fwd_ret_t1") -> Dict[str, Dict]:
    """按 strategy_id 分段计算 IC."""
    by_strategy = defaultdict(list)
    for r in rows:
        sid = r.get("strategy_id") or "unknown"
        by_strategy[sid].append(r)
    result = {}
    for sid, group in by_strategy.items():
        if len(group) < 5:
            continue
        ic = calc_ic(group, forward_col)
        if ic is not None:
            daily = calc_daily_ic(group, forward_col)
            positive_rate = (
                sum(1 for v in daily.values() if v > 0) / len(daily)
                if daily else 0.0
            )
            result[sid] = {
                "n_signals": len(group),
                "ic": round(ic, 4),
                "ic_positive_rate": round(positive_rate, 4),
                "n_days": len(daily),
            }
    return result


def load_old_ic(path: Path) -> Dict:
    """加载重算前的 factor_ic_latest.json (用于差值对比)."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser(description="B2: Signal IC 历史重算")
    parser.add_argument("--dry-run", action="store_true", help="仅输出报告, 不覆盖文件")
    parser.add_argument("--forward", default="fwd_ret_t1",
                        choices=["fwd_ret_t1", "fwd_ret_t5", "fwd_ret_t20"],
                        help="前瞻收益列 (默认 fwd_ret_t1)")
    args = parser.parse_args()

    logger.info(f"=== B2 IC 重算开始 (forward={args.forward}, dry_run={args.dry_run}) ===")

    # 1. 加载数据
    rows = load_snapshots(SNAPSHOTS_DB)
    if not rows:
        logger.error("无数据, 退出")
        return
    logger.info(f"加载 {len(rows)} 条信号 (已排除 NEUTRAL 和 NULL)")

    # 统计 SHORT 数量
    short_count = sum(1 for r in rows if r.get("direction") == "SHORT")
    long_count = sum(1 for r in rows if r.get("direction") == "LONG")
    logger.info(f"  LONG: {long_count}, SHORT: {short_count}")

    # 2. 加载旧 IC (用于差值对比)
    old_factor_ic = load_old_ic(FACTOR_IC_OUT)

    # 3. 重算全局 IC
    global_ic = calc_ic(rows, args.forward)
    daily_ic = calc_daily_ic(rows, args.forward)
    ic_positive_rate = (
        sum(1 for v in daily_ic.values() if v > 0) / len(daily_ic)
        if daily_ic else 0.0
    )
    logger.info(f"全局 IC ({args.forward}): {global_ic:.4f}" if global_ic else "全局 IC: N/A")
    logger.info(f"IC Positive Rate: {ic_positive_rate:.2%} ({len(daily_ic)} 天)")

    # 4. 分策略 IC
    strategy_ic = calc_strategy_ic(rows, args.forward)
    logger.info(f"\n=== 分策略 IC 表 ({len(strategy_ic)} 个策略) ===")
    logger.info(f"{'strategy_id':<30} {'n_signals':>10} {'IC':>10} {'IC+Rate':>10} {'n_days':>8}")
    logger.info("-" * 70)
    for sid, info in sorted(strategy_ic.items(), key=lambda x: -x[1]["n_signals"]):
        logger.info(
            f"{sid:<30} {info['n_signals']:>10} {info['ic']:>10.4f} "
            f"{info['ic_positive_rate']:>9.2%} {info['n_days']:>8}"
        )

    # 5. SHORT 相关因子 IC 差值报告
    # 注: factor_ic_latest.json 的 key 是因子名, 不是 strategy_id
    # 这里输出全局 IC 差值作为替代
    logger.info(f"\n=== SHORT IC 差值报告 ===")
    if old_factor_ic:
        old_global = old_factor_ic.get("_global_ic", old_factor_ic.get("global_ic"))
        if old_global is not None and global_ic is not None:
            delta = global_ic - float(old_global)
            logger.info(f"全局 IC: 旧={old_global:.4f} → 新={global_ic:.4f} (Δ={delta:+.4f})")
            if abs(delta) < 0.01:
                logger.warning("⚠️ 差值 <0.01, SHORT 污染面可能不够大, 需排查")
        else:
            logger.info(f"旧全局 IC 未找到, 新值={global_ic:.4f}" if global_ic else "N/A")
    else:
        logger.info("无旧 factor_ic_latest.json, 跳过差值对比")

    # 6. 输出文件
    if args.dry_run:
        logger.info("\n[dry-run] 不覆盖文件")
        return

    # factor_ic_latest.json: 保持原有结构, 更新全局 IC
    new_factor_ic = dict(old_factor_ic)  # 保留原有因子 IC
    new_factor_ic["_global_ic"] = round(global_ic, 4) if global_ic else None
    new_factor_ic["_ic_positive_rate"] = round(ic_positive_rate, 4)
    new_factor_ic["_recalc_timestamp"] = datetime.now().isoformat()
    new_factor_ic["_recalc_note"] = "B2: A2 修复后重算, SHORT 不再双重调整"

    with open(FACTOR_IC_OUT, "w", encoding="utf-8") as f:
        json.dump(new_factor_ic, f, indent=2, ensure_ascii=False)
    logger.info(f"已覆盖 {FACTOR_IC_OUT}")

    # ic_analysis_latest.json: 完整分析结果
    analysis = {
        "global_ic": round(global_ic, 4) if global_ic else None,
        "ic_positive_rate": round(ic_positive_rate, 4),
        "n_signals": len(rows),
        "n_days": len(daily_ic),
        "forward_col": args.forward,
        "strategy_ic": strategy_ic,
        "daily_ic": {k: round(v, 4) for k, v in sorted(daily_ic.items())},
        "direction_breakdown": {
            "LONG": long_count,
            "SHORT": short_count,
        },
        "recalc_timestamp": datetime.now().isoformat(),
        "recalc_note": "B2: A2 修复后重算 (raw strength, 不取负), 按 strategy_id 分段",
    }
    with open(IC_ANALYSIS_OUT, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)
    logger.info(f"已覆盖 {IC_ANALYSIS_OUT}")

    logger.info("\n=== B2 IC 重算完成 ===")


if __name__ == "__main__":
    from datetime import datetime
    main()
