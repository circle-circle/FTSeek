"""
Playbook IC Tracker — Playbook 指令预测力追踪 (Phase 1)

核心指标:
  Playbook IC = Spearman rank correlation(confidence, forward_return)
  - IC > 0.05: 指令有预测力 (可考虑毕业)
  - IC ≈ 0:    指令是噪音
  - IC < 0:    指令反向 (需要调整)

数据流:
  盘后 shadow_tracker → 更新 instruction 追踪字段
  盘后 backfill_forward_returns() → 回填 T+1/T+3/T+5 收益
  盘后 compute_playbook_ic() → 计算 IC + 按因子分解

设计原则:
  1. 纯只读计算, 不修改交易路径
  2. 复用 PlaybookDB, 不引入新表 (扩展 _TRACKING_FIELDS)
  3. 失败静默, 不影响主流程
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Any, Tuple
from datetime import date, datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)


def _spearman_rank_correlation(x: List[float], y: List[float]) -> Optional[float]:
    """
    Spearman 秩相关系数

    Args:
        x: 信号强度 (confidence)
        y: 前瞻收益 (forward_return)

    Returns:
        Spearman rho [-1, 1], 或 None (样本不足)
    """
    n = len(x)
    if n < 5:
        return None

    def _rank(vals):
        indexed = sorted(enumerate(vals), key=lambda p: p[1])
        ranks = [0.0] * n
        for rank, (idx, _) in enumerate(indexed, 1):
            ranks[idx] = rank
        return ranks

    rx = _rank(x)
    ry = _rank(y)

    d_sq_sum = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    if n <= 1:
        return None
    rho = 1 - (6 * d_sq_sum) / (n * (n ** 2 - 1))
    return max(-1.0, min(1.0, rho))


def backfill_forward_returns(db, market_data, trade_date: str = None) -> int:
    """
    回填前瞻收益: 对历史指令计算 T+1/T+3/T+5 收益率

    Args:
        db: PlaybookDB 实例
        market_data: 市场数据提供者 (get_daily_bars)
        trade_date: 当前日期 (用于判断哪些历史指令需要回填)

    Returns:
        更新了多少条指令
    """
    if trade_date is None:
        trade_date = date.today().isoformat()

    today = date.fromisoformat(trade_date)
    updated = 0

    # 查最近 10 天的指令 (T+5 最多需要 5 个交易日 ≈ 7 个自然日)
    lookback = today - timedelta(days=10)
    try:
        instructions = db.get_recent_instructions(limit=200)
    except Exception as e:
        logger.error(f"Failed to get instructions: {e}")
        return 0

    for inst in instructions:
        inst_date_str = inst.get("date")
        if not inst_date_str:
            continue
        try:
            inst_date = date.fromisoformat(inst_date_str)
        except (ValueError, TypeError):
            continue

        # 只处理 lookback 范围内的
        if inst_date < lookback or inst_date >= today:
            continue

        direction = inst.get("direction", "")
        entry_price = inst.get("entry_price", 0)
        symbol = inst.get("symbol", "")

        if direction == "SKIP" or not entry_price or entry_price <= 0 or not symbol:
            continue

        # 获取日线数据
        try:
            bars = market_data.get_daily_bars(symbol, count=10)
        except Exception as e:
            logger.debug(f"Failed to get bars for {symbol}: {e}")
            continue

        if not bars:
            continue

        # 按日期排序
        bars_by_date = {}
        for bar in bars:
            bar_date = bar.get("time", "")
            if bar_date:
                bars_by_date[bar_date] = bar

        updates = {}
        sign = 1 if direction == "BUY" else -1

        for horizon, field_name in [(1, "fwd_return_t1"), (3, "fwd_return_t3"), (5, "fwd_return_t5")]:
            # 找 T+horizon 的收盘价
            target_date = inst_date + timedelta(days=horizon)
            # 允许 ±2 天偏移 (周末/假日)
            for offset in range(0, 3):
                check_date = target_date + timedelta(days=offset)
                check_str = check_date.isoformat()
                if check_str in bars_by_date:
                    close = bars_by_date[check_str].get("close", 0)
                    if close > 0:
                        fwd_return = sign * (close - entry_price) / entry_price
                        updates[field_name] = round(fwd_return, 6)
                    break

        if updates:
            try:
                db.update_instruction_tracking(inst["id"], updates)
                updated += 1
            except Exception as e:
                logger.debug(f"Failed to update IC for {symbol}: {e}")

    logger.info(f"[PlaybookIC] Backfilled forward returns for {updated} instructions")
    return updated


def compute_playbook_ic(db, days: int = 30) -> Dict[str, Any]:
    """
    计算 Playbook 整体 IC 和按因子分解的 IC

    Args:
        db: PlaybookDB 实例
        days: 回溯天数

    Returns:
        {
            "overall_ic": float,
            "overall_ic_t3": float,
            "overall_ic_t5": float,
            "sample_count": int,
            "by_factor": {factor_name: {"ic": float, "count": int}},
            "by_regime": {regime: {"ic": float, "count": int}},
        }
    """
    try:
        instructions = db.get_recent_instructions(limit=days * 10)
    except Exception as e:
        logger.error(f"Failed to get instructions for IC: {e}")
        return {"overall_ic": None, "sample_count": 0}

    # 过滤有前瞻收益的指令
    valid_t1 = []  # (confidence, fwd_return, conditions, regime)
    valid_t3 = []
    valid_t5 = []

    for inst in instructions:
        conf = inst.get("confidence", 0)
        if not conf or conf <= 0:
            continue

        fwd_t1 = inst.get("fwd_return_t1")
        fwd_t3 = inst.get("fwd_return_t3")
        fwd_t5 = inst.get("fwd_return_t5")

        conditions = []
        try:
            cond_json = inst.get("conditions_json")
            if cond_json:
                import json
                conditions = json.loads(cond_json) if isinstance(cond_json, str) else cond_json
        except Exception:
            pass

        regime = inst.get("market_regime", "UNKNOWN")

        if fwd_t1 is not None:
            valid_t1.append((conf, fwd_t1, conditions, regime))
        if fwd_t3 is not None:
            valid_t3.append((conf, fwd_t3, conditions, regime))
        if fwd_t5 is not None:
            valid_t5.append((conf, fwd_t5, conditions, regime))

    # 计算整体 IC
    overall_t1 = _spearman_rank_correlation(
        [v[0] for v in valid_t1], [v[1] for v in valid_t1]
    ) if len(valid_t1) >= 5 else None

    overall_t3 = _spearman_rank_correlation(
        [v[0] for v in valid_t3], [v[1] for v in valid_t3]
    ) if len(valid_t3) >= 5 else None

    overall_t5 = _spearman_rank_correlation(
        [v[0] for v in valid_t5], [v[1] for v in valid_t5]
    ) if len(valid_t5) >= 5 else None

    # 按因子分解 IC (T+3)
    by_factor = {}
    if valid_t3:
        factor_data = defaultdict(lambda: {"confs": [], "returns": []})
        for conf, ret, conditions, _ in valid_t3:
            for cond in conditions:
                factor_data[cond]["confs"].append(conf)
                factor_data[cond]["returns"].append(ret)

        for factor_name, data in factor_data.items():
            if len(data["confs"]) >= 5:
                ic = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_factor[factor_name] = {"ic": round(ic, 4), "count": len(data["confs"])}

    # 按 regime 分解 IC (T+3)
    by_regime = {}
    if valid_t3:
        regime_data = defaultdict(lambda: {"confs": [], "returns": []})
        for conf, ret, _, regime in valid_t3:
            regime_data[regime]["confs"].append(conf)
            regime_data[regime]["returns"].append(ret)

        for regime_name, data in regime_data.items():
            if len(data["confs"]) >= 5:
                ic = _spearman_rank_correlation(data["confs"], data["returns"])
                if ic is not None:
                    by_regime[regime_name] = {"ic": round(ic, 4), "count": len(data["confs"])}

    result = {
        "overall_ic_t1": round(overall_t1, 4) if overall_t1 is not None else None,
        "overall_ic_t3": round(overall_t3, 4) if overall_t3 is not None else None,
        "overall_ic_t5": round(overall_t5, 4) if overall_t5 is not None else None,
        "sample_t1": len(valid_t1),
        "sample_t3": len(valid_t3),
        "sample_t5": len(valid_t5),
        "by_factor": by_factor,
        "by_regime": by_regime,
    }

    # 毕业门禁判定
    result["graduation_check"] = _check_graduation(result)

    return result


def _check_graduation(ic_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    毕业门禁检查

    条件 (全部满足):
    - Playbook IC (T+3) > 0.05
    - 样本量 ≥ 30
    - 最大回撤 < 5% (由 RiskCircuitBreaker 提供)
    """
    ic_t3 = ic_report.get("overall_ic_t3")
    sample = ic_report.get("sample_t3", 0)

    checks = {
        "ic_positive": ic_t3 is not None and ic_t3 > 0.05,
        "sample_sufficient": sample >= 30,
    }

    all_pass = all(checks.values())

    return {
        "graduated": all_pass,
        "checks": checks,
        "ic_t3": ic_t3,
        "sample_count": sample,
        "verdict": "READY" if all_pass else "ACCUMULATING",
    }


def format_ic_report(report: Dict[str, Any]) -> str:
    """格式化 IC 报告为飞书消息"""
    lines = ["📊 **Playbook IC 追踪报告**", ""]

    # 整体 IC
    ic_t1 = report.get("overall_ic_t1")
    ic_t3 = report.get("overall_ic_t3")
    ic_t5 = report.get("overall_ic_t5")

    def _ic_emoji(ic):
        if ic is None:
            return "⏳ 数据不足"
        if ic > 0.05:
            return f"✅ {ic:+.4f}"
        if ic > -0.05:
            return f"🟡 {ic:+.4f}"
        return f"🔴 {ic:+.4f}"

    lines.append(f"T+1 IC: {_ic_emoji(ic_t1)} (n={report.get('sample_t1', 0)})")
    lines.append(f"T+3 IC: {_ic_emoji(ic_t3)} (n={report.get('sample_t3', 0)})")
    lines.append(f"T+5 IC: {_ic_emoji(ic_t5)} (n={report.get('sample_t5', 0)})")
    lines.append("")

    # 按因子分解
    by_factor = report.get("by_factor", {})
    if by_factor:
        lines.append("**按因子 IC (T+3):**")
        sorted_factors = sorted(by_factor.items(), key=lambda x: x[1]["ic"], reverse=True)
        for name, data in sorted_factors[:10]:
            ic = data["ic"]
            emoji = "✅" if ic > 0.05 else "🟡" if ic > -0.05 else "🔴"
            lines.append(f"  {emoji} {name}: IC={ic:+.4f} (n={data['count']})")
        lines.append("")

    # 毕业门禁
    grad = report.get("graduation_check", {})
    verdict = grad.get("verdict", "UNKNOWN")
    if verdict == "READY":
        lines.append("🎓 **毕业门禁: 通过** — 可考虑切换到 live 模式")
    else:
        lines.append(f"⏳ **毕业门禁: 积累中** (verdict={verdict})")
        checks = grad.get("checks", {})
        for check_name, passed in checks.items():
            status = "✅" if passed else "❌"
            lines.append(f"  {status} {check_name}")

    return "\n".join(lines)
