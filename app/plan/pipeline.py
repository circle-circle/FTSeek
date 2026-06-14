"""
Plan Pipeline — 完整作战计划生成管线

Stage 1: factor_engine.run_factor_screen()  → Top 8 candidates (量化预筛)
Stage 2: llm_validator.validate_candidates() → LLM验证 (并行8次)
Stage 3: 确定性排序 → Top 5 VALID/CONDITIONAL
Stage 4: narrative生成 → regime_reasoning + risk_warnings (1次LLM)
Output:  daily_battle_plan_us.json (兼容现有格式)

替换原有的 debate_planner (Hermes↔Claude 3轮辩论) 路径。
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek_us.plan_pipeline")

PLAN_OUTPUT = Path(__file__).parent.parent.parent / "data" / "daily_battle_plan_us.json"
ET = timezone(timedelta(hours=-4))


def _composite_rank(factor_score: float, llm_confidence: float, verdict: str) -> float:
    """Stage 3: 确定性排序分数"""
    # VALID gets full weight, CONDITIONAL gets 70%, INVALID gets 0
    verdict_mult = {"VALID": 1.0, "CONDITIONAL": 0.7, "INVALID": 0.0}
    mult = verdict_mult.get(verdict, 0.0)
    # 60% factor score + 40% LLM confidence (normalized to 0-1)
    return (factor_score * 0.6 + (llm_confidence / 100) * 0.4) * mult


def _build_target_entry(validated: Dict, factor_data: Dict) -> Dict:
    """将LLM验证结果转换为plan target格式 (兼容现有daily_battle_plan_us.json)"""
    sym = validated.get("symbol", "")
    factors = factor_data.get("factors", {})
    tech = factor_data.get("technicals", {})

    # Determine urgency from confidence + factor score
    conf = validated.get("confidence", 0)
    fs = validated.get("factor_score", 0)
    if conf >= 70 and fs >= 0.7:
        urgency = "HIGH"
    elif conf >= 50 and fs >= 0.5:
        urgency = "MEDIUM"
    else:
        urgency = "LOW"

    # Build rationale from factor details + audit flags
    factor_details = []
    for fname, fdata in factors.items():
        detail = fdata.get("detail", "")
        score = fdata.get("score", 0)
        status = "✅" if score >= 0.7 else "⚠️" if score >= 0.4 else "❌"
        factor_details.append(f"{status} {fname}: {detail}")

    fv = validated.get("factor_validation", {})
    fv_details = []
    for fname, verdict in fv.items():
        if verdict in ("CONFIRMED", "VALID"):
            fv_details.append(f"{fname}=confirmed")
        elif verdict in ("WEAK", "FALSE_POSITIVE", "INVALID"):
            fv_details.append(f"{fname}={verdict.lower()}")

    audit_flags = validated.get("audit_flags", [])
    rationale_parts = [
        " | ".join(factor_details[:3]),
    ]
    if fv_details:
        rationale_parts.append("验证: " + ", ".join(fv_details))
    if audit_flags:
        rationale_parts.append("风险: " + "; ".join(audit_flags[:2]))
    rationale = "\n".join(rationale_parts)

    # Entry zone from LLM or derive from technicals
    entry_zone = validated.get("entry_zone", [])
    if not entry_zone or len(entry_zone) < 2:
        cur = tech.get("cur_price", 0)
        if cur > 0:
            atr = tech.get("atr14", cur * 0.02)
            entry_zone = [round(cur - atr * 0.5, 1), round(cur + atr * 0.5, 1)]
        else:
            entry_zone = [0, 0]

    entry = {
        "symbol": sym,
        "direction": validated.get("direction", factor_data.get("direction_hint", "LONG")),
        "entry_zone": entry_zone,
        "stop_loss": validated.get("stop_loss", 0),
        "take_profit": validated.get("take_profit", 0),
        "urgency": urgency,
        "rationale": rationale,
        # New fields (additive, backward-compatible)
        "trigger_condition": validated.get("trigger_condition", ""),
        "risk_reward": validated.get("risk_reward", 0),
        "position_size_pct": validated.get("position_size_pct", 5),
        "invalidation": validated.get("invalidation", ""),
        "confidence": conf,
        "factor_score": round(fs, 3),
        "composite_rank": round(_composite_rank(fs, conf, validated.get("verdict", "INVALID")), 3),
        "verdict": validated.get("verdict", "INVALID"),
        "execution_rules": validated.get("execution_rules", {}),
        "factor_validation": fv,
        "data_cited": [f.get("detail", "") for f in factors.values() if f.get("score", 0) >= 0.6],
    }
    return entry


def _generate_narrative(factor_screen: Dict, validation_result: Dict, targets: List[Dict]) -> Dict:
    """Stage 4: 1次LLM调用生成人类可读摘要"""
    regime = factor_screen.get("regime", "NEUTRAL")
    spy_chg = factor_screen.get("spy_change_20d", 0)
    sector_data = factor_screen.get("sector_momentum", {})

    try:
        from app.llm.unified_service import UnifiedLLMService

        target_summary = "\n".join(
            f"- {t['symbol']} {t['direction']} entry={t['entry_zone']} SL={t['stop_loss']} TP={t['take_profit']} "
            f"(confidence={t.get('confidence',0)}%, verdict={t.get('verdict','')})"
            for t in targets
        )

        sector_summary = ", ".join(f"{s}: {c:+.1f}%" for s, c in sorted(sector_data.items(), key=lambda x: -x[1])[:5])

        prompt = f"""## 今日市场
- Regime: {regime}
- SPY 20日涨跌: {spy_chg:+.1f}%
- 板块排名: {sector_summary}

## 选中Targets ({len(targets)}只)
{target_summary}

请用2-3句话解释:
1. 为什么判断为{regime} regime
2. 今天的主要风险是什么
3. 最应该关注的1-2个target及原因

输出JSON:
{{
  "regime_reasoning": "2-3句话解释regime判断",
  "risk_warnings": ["风险1", "风险2"],
  "strategy_adjustments": ["策略建议1"]
}}"""

        result = UnifiedLLMService.call(
            "plan_validator",  # reuse same feature
            prompt,
            system="你是一个简洁的交易策略师。用数据和事实说话，不要废话。只输出JSON。",
            temperature=0,
            max_tokens=300,
            json_mode=True,
            timeout=60,
        )

        if result.success:
            raw = result.response
            if isinstance(raw, str):
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass
            elif isinstance(raw, dict):
                return raw
    except Exception as e:
        logger.debug(f"[Pipeline] narrative error: {e}")

    # Fallback
    return {
        "regime_reasoning": f"Regime={regime}, SPY 20d={spy_chg:+.1f}%",
        "risk_warnings": [],
        "strategy_adjustments": [],
    }


def run_pipeline() -> Optional[Dict]:
    """
    执行完整作战计划生成管线。
    
    Returns: plan dict (also saved to daily_battle_plan_us.json)
    """
    t0 = time.time()
    today = datetime.now(ET).strftime("%Y-%m-%d")

    logger.info("[Pipeline] 🚀 Stage 1: 量化预筛...")

    # ── Stage 1: Factor Screen ──
    from app.plan.factor_engine import run_factor_screen
    factor_screen = run_factor_screen(top_n=8)
    if "error" in factor_screen:
        logger.error(f"[Pipeline] Stage 1 failed: {factor_screen['error']}")
        return None

    candidates = factor_screen.get("candidates", [])
    logger.info(
        f"[Pipeline] Stage 1 ✅ {factor_screen.get('scored_count',0)}只评分 → "
        f"Top {len(candidates)} | Regime={factor_screen.get('regime')} | "
        f"{factor_screen.get('elapsed_sec',0)}s"
    )

    if not candidates:
        logger.warning("[Pipeline] No candidates from factor screen")
        return None

    # ── Stage 2: LLM Validation ──
    logger.info("[Pipeline] 🧠 Stage 2: LLM验证 (8 candidates)...")
    from app.plan.llm_validator import validate_candidates
    validation = validate_candidates(factor_screen)

    validated = validation.get("validated", [])
    logger.info(
        f"[Pipeline] Stage 2 ✅ VALID={validation.get('valid_count',0)} "
        f"CONDITIONAL={validation.get('conditional_count',0)} "
        f"INVALID={validation.get('invalid_count',0)} | "
        f"{validation.get('elapsed_sec',0)}s"
    )

    # ── Stage 3: Deterministic Ranking ──
    logger.info("[Pipeline] 📊 Stage 3: 确定性排序...")

    # Build lookup from factor_screen candidates
    factor_lookup = {c["symbol"]: c for c in candidates}

    # Score and rank
    ranked = []
    for v in validated:
        sym = v.get("symbol", "")
        factor_data = factor_lookup.get(sym, {})
        fs = v.get("factor_score", factor_data.get("composite_score", 0))
        conf = v.get("confidence", 0)
        verdict = v.get("verdict", "INVALID")
        rank_score = _composite_rank(fs, conf, verdict)
        ranked.append({
            "validated": v,
            "factor_data": factor_data,
            "rank_score": rank_score,
        })

    # Sort by rank_score descending, take top 5 non-zero
    ranked.sort(key=lambda r: r["rank_score"], reverse=True)
    top_targets = [r for r in ranked if r["rank_score"] > 0][:5]

    # Build target entries
    targets = []
    for r in top_targets:
        entry = _build_target_entry(r["validated"], r["factor_data"])
        targets.append(entry)

    logger.info(f"[Pipeline] Stage 3 ✅ {len(targets)} targets selected")

    # If no targets passed validation, still generate plan with empty targets
    # (restricted list and regime info still valuable)
    if not targets:
        logger.warning("[Pipeline] No targets passed validation — generating empty plan")

    # Build restricted list (INVALID candidates)
    restricted = [
        r["validated"]["symbol"]
        for r in ranked
        if r["validated"].get("verdict") == "INVALID"
    ]

    # ── Stage 4: Narrative ──
    logger.info("[Pipeline] ✍️ Stage 4: 生成摘要...")
    narrative = _generate_narrative(factor_screen, validation, targets)

    # ── Assemble final plan ──
    plan = {
        "date": today,
        "market_regime": factor_screen.get("regime", "NEUTRAL"),
        "regime_reasoning": narrative.get("regime_reasoning", ""),
        "targets": targets,
        "restricted": restricted,
        "restricted_reasons": {
            r["validated"]["symbol"]: "; ".join(r["validated"].get("audit_flags", [])[:2])
            for r in ranked if r["validated"].get("verdict") == "INVALID"
        },
        "position_actions": [],  # TODO: derive from existing positions + factor signals
        "strategy_adjustments": narrative.get("strategy_adjustments", []),
        "risk_warnings": narrative.get("risk_warnings", []),
        "confidence": round(
            sum(t.get("confidence", 0) for t in targets) / max(len(targets), 1) / 100, 2
        ),
        # Metadata (additive)
        "arbiter_mode": "factor_pipeline",
        "pipeline_stats": {
            "pool_size": factor_screen.get("pool_size", 0),
            "scored_count": factor_screen.get("scored_count", 0),
            "factor_screen_sec": factor_screen.get("elapsed_sec", 0),
            "validation_sec": validation.get("elapsed_sec", 0),
            "valid_count": validation.get("valid_count", 0),
            "conditional_count": validation.get("conditional_count", 0),
            "total_sec": round(time.time() - t0, 1),
        },
        "sector_momentum": factor_screen.get("sector_momentum", {}),
        "spy_change_20d": factor_screen.get("spy_change_20d", 0),
        # Compatibility fields
        "_paused": False,
        "_paused_at": None,
        "_resumed_at": None,
        "debate_summary": f"Factor pipeline: {len(targets)} targets from {factor_screen.get('pool_size',0)} pool",
        "claude_audit": None,  # No Claude in this pipeline
    }

    # ── Save ──
    try:
        PLAN_OUTPUT.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
        logger.info(
            f"[Pipeline] ✅ Plan saved: {len(targets)} targets, "
            f"{len(restricted)} restricted, "
            f"regime={plan['market_regime']}, "
            f"total={plan['pipeline_stats']['total_sec']}s"
        )
    except Exception as e:
        logger.error(f"[Pipeline] save error: {e}")

    return plan


if __name__ == "__main__":
    """CLI: python -m app.plan.pipeline"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    plan = run_pipeline()
    if plan:
        print(f"\n{'='*60}")
        print(f"Plan: {plan['date']} | Regime: {plan['market_regime']}")
        print(f"Targets: {len(plan['targets'])} | Restricted: {len(plan['restricted'])}")
        print(f"Time: {plan['pipeline_stats']['total_sec']}s")
        print(f"{'='*60}")
        for t in plan['targets']:
            print(f"  {t['symbol']} {t['direction']} | "
                  f"Entry={t['entry_zone']} SL={t['stop_loss']} TP={t['take_profit']} | "
                  f"Conf={t.get('confidence',0)}% | Rank={t.get('composite_rank',0):.3f}")
