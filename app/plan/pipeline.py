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
        "model_used": validated.get("model_used", ""),
    }
    return entry


def _generate_position_actions(factor_screen: Dict, validation: Dict, targets: List[Dict]) -> List[Dict]:
    """Stage 4.5: 为现有持仓生成管理建议 (hold/reduce/exit)

    Logic:
    - Position in factor top candidates + VALID → HOLD (or ADD if strong)
    - Position NOT in factor top candidates → check if factor score is low → REDUCE
    - Position in restricted list → EXIT
    - Position with negative factor trend → REDUCE
    """
    actions = []

    # Get current positions from Futu
    positions = []
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        from futu import RET_OK, TrdEnv
        svc = ManualOrderService()
        if svc.connect():
            is_live = svc._is_live()
            trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
            acc_id = int(svc._read_env(
                "REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"
            ))
            ret, data = svc._broker_ctx.position_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            svc.disconnect()
            if ret == RET_OK and not data.empty:
                for _, row in data.iterrows():
                    code = str(row.get("code", ""))
                    if code and code.startswith("US."):
                        positions.append({
                            "symbol": code,
                            "qty": int(float(row.get("qty", 0))),
                            "avg_cost": float(row.get("average_cost", 0)),
                            "market_val": float(row.get("market_val", 0)),
                            "unrealized_pnl": float(row.get("unrealized_pl", 0)),
                            "pnl_pct": float(row.get("pl_ratio_avg_cost", 0)),
                        })
    except Exception as e:
        logger.debug(f"[Pipeline] position fetch error: {e}")

    if not positions:
        return actions

    # Build lookups — use all_scores for factor scores (covers all scored stocks, not just top_n)
    factor_lookup = {c["symbol"]: c for c in factor_screen.get("candidates", [])}
    all_scores_lookup = {s["symbol"]: s.get("composite_score", 0) for s in factor_screen.get("all_scores", [])}
    validated_lookup = {v["symbol"]: v for v in validation.get("validated", [])}
    restricted_set = {r["symbol"] for r in validation.get("validated", [])
                      if r.get("verdict") == "INVALID"}
    target_set = {t["symbol"] for t in targets}

    for pos in positions:
        sym = pos["symbol"]
        bare = sym.replace("US.", "")
        direction = "SHORT" if pos.get("qty", 0) < 0 else "LONG"

        # Check if in restricted list
        if sym in restricted_set:
            actions.append({
                "symbol": sym, "action": "EXIT",
                "reason": f"因子验证INVALID — {'; '.join(validated_lookup.get(sym, {}).get('audit_flags', ['风险过高'])[:2])}",
                "urgency": "HIGH",
                "current_pnl_pct": round(pos.get("pnl_pct", 0), 2),
                "current_price": round(pos.get("avg_cost", 0), 2),
                "qty": pos.get("qty", 0),
                "direction": direction,
                "factor_score": round(all_scores_lookup.get(sym, 0), 3),
            })
            continue

        # Check factor score — use candidates first, fallback to all_scores
        fc = factor_lookup.get(sym, {})
        factor_score = fc.get("composite_score", 0) or all_scores_lookup.get(sym, 0)
        val = validated_lookup.get(sym, {})
        verdict = val.get("verdict", "")
        conf = val.get("confidence", 0)
        pnl = round(pos.get("pnl_pct", 0), 2)

        _base = {
            "symbol": sym, "direction": direction, "qty": pos.get("qty", 0),
            "current_pnl_pct": pnl,
            "current_price": round(pos.get("avg_cost", 0) * (1 + pnl / 100), 2) if pos.get("avg_cost", 0) > 0 else 0,
            "factor_score": round(factor_score, 3),
        }

        if sym in target_set and verdict in ("VALID", "CONDITIONAL") and conf >= 50:
            _base.update({"action": "HOLD", "urgency": "LOW",
                "reason": f"因子{factor_score*100:.0f}分, LLM验证{verdict}(conf={conf}%), 在推荐列表中"})
        elif factor_score < 0.3:
            _base.update({"action": "REDUCE", "urgency": "MEDIUM",
                "reason": f"因子分仅{factor_score*100:.0f}, 不在Top候选中, 动量衰减"})
        elif pnl < -5:
            _base.update({"action": "REDUCE", "urgency": "MEDIUM",
                "reason": f"浮亏{pnl:.1f}%, 因子{factor_score*100:.0f}分, 考虑减仓止损"})
        else:
            _base.update({"action": "HOLD", "urgency": "LOW",
                "reason": f"因子{factor_score*100:.0f}分, {'LLM未验证但因子尚可' if factor_score >= 0.4 else '关注因子变化'}"})

        actions.append(_base)

    return actions


def _generate_orb_status(factor_screen: Dict) -> List[Dict]:
    """Stage 4.6: Check ORB ETF breakout signals (lightweight)

    For each of the 10 ORB ETFs, check if there's a potential breakout setup.
    Returns a brief status list, not full factor scoring.
    """
    orb_etfs = [
        "US.SPY", "US.QQQ", "US.IWM", "US.DIA",
        "US.XLE", "US.XLF", "US.XLK", "US.XLV", "US.XLY", "US.XLI",
    ]

    status_list = []
    sector_momentum = factor_screen.get("sector_momentum", {})

    # Map ORB ETFs to sector names for context
    etf_sector = {
        "US.XLK": "Technology", "US.XLF": "Financials", "US.XLE": "Energy",
        "US.XLV": "Healthcare", "US.XLI": "Industrials", "US.XLY": "Consumer",
    }

    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        svc = ManualOrderService()
        if not svc.connect():
            return status_list

        from futu import RET_OK, SubType
        for etf in orb_etfs:
            try:
                svc._quote_ctx.subscribe([etf], [SubType.K_DAY])
            except Exception:
                pass

        import time; time.sleep(0.5)

        for etf in orb_etfs:
            try:
                ret, data = svc._quote_ctx.get_cur_kline(etf, 5, ktype="K_DAY")
                if ret != RET_OK or data.empty or len(data) < 3:
                    continue

                closes = [float(r["close"]) for _, r in data.iterrows()]
                highs = [float(r["high"]) for _, r in data.iterrows()]
                lows = [float(r["low"]) for _, r in data.iterrows()]

                cur = closes[-1]
                prev_high = max(highs[:-1]) if len(highs) > 1 else highs[0]
                prev_low = min(lows[:-1]) if len(lows) > 1 else lows[0]

                # Simple breakout check: price near 5-day high or low
                range_5d = prev_high - prev_low
                if range_5d <= 0:
                    continue

                pos_in_range = (cur - prev_low) / range_5d if range_5d > 0 else 0.5
                chg_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0

                signal = ""
                if pos_in_range > 0.9 and chg_1d > 0.3:
                    signal = "BREAKOUT_UP"
                elif pos_in_range < 0.1 and chg_1d < -0.3:
                    signal = "BREAKOUT_DOWN"
                elif abs(chg_1d) < 0.2:
                    signal = "CONSOLIDATION"
                else:
                    signal = "NEUTRAL"

                sector = etf_sector.get(etf, "")
                sector_chg = sector_momentum.get(sector, 0)

                status_list.append({
                    "symbol": etf,
                    "signal": signal,
                    "price": round(cur, 2),
                    "chg_1d": round(chg_1d, 2),
                    "pos_in_range": round(pos_in_range, 2),
                    "sector_chg": sector_chg,
                })
            except Exception:
                pass

        svc.disconnect()
    except Exception as e:
        logger.debug(f"[Pipeline] ORB status error: {e}")

    # Sort: breakouts first
    priority = {"BREAKOUT_UP": 0, "BREAKOUT_DOWN": 1, "CONSOLIDATION": 2, "NEUTRAL": 3}
    status_list.sort(key=lambda x: priority.get(x.get("signal", "NEUTRAL"), 3))

    return status_list


def _generate_narrative(factor_screen: Dict, validation_result: Dict, targets: List[Dict]) -> Dict:
    """Stage 5: 1次LLM调用生成人类可读摘要"""
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
            timeout=300,
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

    # ── Stage 4.5: Position Actions (现有持仓管理) ──
    logger.info("[Pipeline] 📊 Stage 4.5: 生成持仓管理建议...")
    position_actions = _generate_position_actions(factor_screen, validation, targets)
    logger.info(f"[Pipeline] Stage 4.5 ✅ {len(position_actions)} position actions")

    # ── Stage 4.6: ORB ETF Status ──
    logger.info("[Pipeline] 📈 Stage 4.6: ORB ETF状态...")
    orb_status = _generate_orb_status(factor_screen)
    logger.info(f"[Pipeline] Stage 4.6 ✅ {len(orb_status)} ORB ETFs checked")

    # ── Stage 5: Narrative ──
    logger.info("[Pipeline] ✍️ Stage 5: 生成摘要...")
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
        "position_actions": position_actions,
        "orb_status": orb_status,
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
