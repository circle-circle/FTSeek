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


def _assign_primary_target(targets: list) -> list:
    """Task #19: 为 targets 打上 rank_position / is_primary / why_primary 标记。

    假设 targets 已按优先级降序 (composite_rank 高 -> 低)。
    """
    if not targets:
        return targets

    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            continue
        t["rank_position"] = i + 1
        t["is_primary"] = (i == 0)

    first = targets[0] if isinstance(targets[0], dict) else None
    if first is not None:
        why_parts = ["综合排名#1"]
        try:
            rr = float(first.get("risk_reward", 0) or 0)
            if rr >= 1.5:
                why_parts.append(f"R:R {rr:.1f}:1")
        except (TypeError, ValueError):
            pass
        try:
            conf = int(first.get("confidence", 0) or 0)
            if conf >= 70:
                why_parts.append(f"置信度{conf}%")
        except (TypeError, ValueError):
            pass
        first["why_primary"] = " · ".join(why_parts)

    return targets


def _build_target_entry(validated: Dict, factor_data: Dict) -> Dict:
    """将LLM验证结果转换为plan target格式 (兼容现有daily_battle_plan_us.json)"""
    sym = validated.get("symbol", "")
    factors = factor_data.get("factors", {})
    tech = factor_data.get("technicals", {})

    # Determine urgency from confidence + factor score
    conf = validated.get("confidence", 0)
    fs = factor_data.get("composite_score", 0)  # FIX: 从 factor_engine 读, 不是 LLM validator
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
            direction = validated.get("direction", factor_data.get("direction_hint", "LONG"))
            if direction == "SHORT":
                entry_zone = [round(cur - atr * 0.3, 1), round(cur + atr * 0.5, 1)]
            else:
                entry_zone = [round(cur - atr * 0.5, 1), round(cur + atr * 0.3, 1)]
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
        # Catalyst & discovery (from Stage 1.5 / factor_engine)
        "catalyst": factor_data.get("catalyst", {"type": "NONE", "strength": "NONE", "headline": ""}),
        "discovery_source": factor_data.get("discovery_source", "core_pool"),
        # 实时价格注入 (2026-06-21 修复: portfolio_synth需要计算目标价空间)
        "current_price": round(tech.get("cur_price", 0), 2),
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
                    if not code or not code.startswith("US."):
                        continue
                    # Filter out options: codes like US.SNDK260620C75000 have digits after ticker
                    bare = code[3:]  # strip "US."
                    if any(c.isdigit() for c in bare):
                        continue  # option code, skip
                    positions.append({
                        "symbol": code,
                        "qty": int(float(row.get("qty", 0))),
                        "avg_cost": float(row.get("average_cost", 0)),
                        "market_val": float(row.get("market_val", 0)),
                        "unrealized_pnl": float(row.get("unrealized_pl", 0)),
                        # Calculate P&L% from prices (more reliable than pl_ratio_avg_cost)
                        "pnl_pct": (
                            ((float(row.get("nominal_price", 0)) - float(row.get("average_cost", 0)))
                             / float(row.get("average_cost", 1)) * 100)
                            if float(row.get("average_cost", 0)) > 0 and float(row.get("nominal_price", 0)) > 0
                            else float(row.get("pl_ratio_avg_cost", 0))
                        ),
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
        # Sanity: cap unreasonable P&L values (simulate env / options artifacts)
        if abs(pnl) > 200:
            pnl = 0

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
    """Stage 5: 模板化叙事生成 — 零 LLM 调用 (P0 2026-06-17)

    原 LLM 调用替换为确定性模板引擎:
    - regime_reasoning: 基于 regime + SPY + 板块数据的规则生成
    - risk_warnings: 基于 VIX/regime/持仓/验证结果的规则生成
    - strategy_adjustments: 基于 regime + 目标特征的规则生成

    收益: 0 LLM 调用 (省 ~$0.04 + ~5s), 零幻觉, 100% 一致格式。
    """
    regime = factor_screen.get("regime", "NEUTRAL")
    spy_chg = factor_screen.get("spy_change_20d", 0)
    sector_data = factor_screen.get("sector_momentum", {})
    vix = factor_screen.get("vix", 0)

    valid_count = validation_result.get("valid_count", 0)
    conditional_count = validation_result.get("conditional_count", 0)
    invalid_count = validation_result.get("invalid_count", 0)

    # ── regime_reasoning ──
    regime_map = {
        "STRONG_BULL": "市场强势上行",
        "BULL": "市场温和上行",
        "NEUTRAL": "市场方向不明",
        "CAUTION": "市场谨慎,波动加大",
        "BEAR": "市场下行趋势",
        "CRISIS": "市场恐慌,极端波动",
        "RISK_ON": "风险偏好模式",
        "RISK_OFF": "避险模式",
        "ROTATION": "板块轮动模式",
        "HIGH_VOL": "高波动模式",
    }
    regime_label = regime_map.get(regime, regime)

    # 板块排名
    sorted_sectors = sorted(sector_data.items(), key=lambda x: -x[1]) if sector_data else []
    top_sectors = sorted_sectors[:3]
    bottom_sectors = sorted_sectors[-2:] if len(sorted_sectors) >= 2 else []

    sector_leader = f"领涨板块: {top_sectors[0][0]}({top_sectors[0][1]:+.1f}%)" if top_sectors else ""
    sector_laggard = f"领跌板块: {bottom_sectors[0][0]}({bottom_sectors[0][1]:+.1f}%)" if bottom_sectors and bottom_sectors[0][1] < 0 else ""

    # 构建 regime_reasoning
    reasoning_parts = [f"当前市场处于{regime_label}。"]
    if abs(spy_chg) > 1:
        direction = "上涨" if spy_chg > 0 else "下跌"
        reasoning_parts.append(f"SPY 近20日{direction} {abs(spy_chg):.1f}%,")
    else:
        reasoning_parts.append("SPY 近20日窄幅震荡,")

    if sector_leader:
        reasoning_parts.append(sector_leader)
    if sector_laggard:
        reasoning_parts.append(sector_laggard)

    reasoning_parts.append(f"因子筛选出 {valid_count} 只 VALID、{conditional_count} 只 CONDITIONAL、{invalid_count} 只 INVALID。")
    regime_reasoning = " ".join(reasoning_parts)

    # ── risk_warnings ──
    risk_warnings = []
    if regime in ("RISK_OFF", "HIGH_VOL", "CRISIS"):
        risk_warnings.append(f"市场处于{regime_label},建议缩小仓位、加宽止损")
    if regime in ("CAUTION",) or (isinstance(vix, (int, float)) and vix > 22):
        risk_warnings.append("VIX 偏高,入场门槛已收紧,避免追高")
    if spy_chg < -3:
        risk_warnings.append(f"SPY 20日跌幅 {spy_chg:+.1f}%,LONG 方向需额外确认")
    if spy_chg > 5:
        risk_warnings.append(f"SPY 20日涨幅 {spy_chg:+.1f}%,警惕获利回吐")

    # 检查 top target 的风险
    for t in targets[:2]:
        conf = t.get("confidence", 0)
        rr = t.get("risk_reward", 0)
        fv = t.get("factor_validation", {})
        fp_count = sum(1 for v in fv.values() if v in ("FALSE_POSITIVE", "WEAK"))
        if conf < 60:
            risk_warnings.append(f"{t['symbol']} 置信度仅 {conf}%,建议观望或缩小仓位")
        if fp_count >= 2:
            risk_warnings.append(f"{t['symbol']} 有 {fp_count} 个因子 WEAK/FP,setup 可靠性存疑")
        if isinstance(rr, (int, float)) and rr > 0 and rr < 1.5:
            risk_warnings.append(f"{t['symbol']} 风险回报比 {rr:.1f}:1,低于 1.5:1 最低标准")

    if not risk_warnings:
        risk_warnings.append("当前无明显系统性风险,正常执行计划")

    # ── strategy_adjustments ──
    strategy_adjustments = []
    if regime in ("STRONG_BULL", "BULL", "RISK_ON"):
        strategy_adjustments.append("趋势环境,可适度加仓,止损可设在 2x ATR")
        if valid_count >= 3:
            strategy_adjustments.append("多标的 VALID,优先选择 R:R 最高的 1-2 只")
    elif regime in ("NEUTRAL", "ROTATION"):
        strategy_adjustments.append("震荡环境,仓位控制在 50% 以内,优先板块动量最强的标的")
    elif regime in ("CAUTION", "BEAR", "RISK_OFF", "HIGH_VOL", "CRISIS"):
        strategy_adjustments.append("防御环境,仓位控制在 30% 以内,止损加宽到 2.5x ATR")
        strategy_adjustments.append("优先等待回调到支撑位再入场,不追突破")

    if conditional_count > valid_count:
        strategy_adjustments.append(f"CONDITIONAL({conditional_count})多于VALID({valid_count}),整体质量偏低,精选执行")

    return {
        "regime_reasoning": regime_reasoning,
        "risk_warnings": risk_warnings,
        "strategy_adjustments": strategy_adjustments,
    }


# ── Catalyst Detection (v2: 10分制 + company_news + 价格确认) ──
_CATALYST_RULES = [
    # (keywords_list, type, base_score, direction_hint)
    # base_score: 基础强度 1-10, 会被价格确认和多源叠加调整
    (["merger", "acquire", "acquisition", "m&a", "buyout", "takeover", "bid"], "MA", 8, None),
    (["short report", "short seller", "fraud", "hindenburg", "muddy waters", "citron", "wolfpack"], "SHORT_ATTACK", 8, "SHORT"),
    (["fda approval", "fda clear", "phase 3 success", "drug approval", "breakthrough therapy"], "REGULATORY", 8, "LONG"),
    (["index inclusion", "index rebalance", "nasdaq-100", "s&p 500 add", "russell reconstitution"], "INDEX", 7, "LONG"),
    (["spin-off", "spinoff", "split-off", "divestiture"], "CORPORATE_ACTION", 7, None),
    (["earnings beat", "revenue beat", "guidance raise", "raised outlook", "blowout quarter"], "EARNINGS_BEAT", 6, "LONG"),
    (["earnings miss", "revenue miss", "guidance cut", "lowered outlook", "disappointing"], "EARNINGS_MISS", 6, "SHORT"),
    (["ipo", "direct listing", "spac merger", "debut"], "IPO", 5, None),
    (["upgrade", "overweight", "outperform", "buy rating", "raises target"], "UPGRADE", 4, "LONG"),
    (["downgrade", "underweight", "underperform", "sell rating", "cuts target"], "DOWNGRADE", 4, "SHORT"),
    (["insider buy", "insider purchase", "10b5-1"], "INSIDER", 3, "LONG"),
    (["sector rotation", "fund flow", "etf rebalance"], "FLOW", 2, None),
]

# 价格确认乘数 — 已有价格动量确认催化剂已生效
_PRICE_CONFIRM_MULTIPLIER = {
    # abs(change_pct) range → multiplier
    (10, 999): 1.3,   # >10% 移动 = 强确认
    (5, 10): 1.15,    # 5-10% = 中确认
    (3, 5): 1.0,      # 3-5% = 轻微确认
    (0, 3): 0.85,     # <3% = 未确认/延迟反应
}


def _get_price_confirm_mult(change_pct: float) -> float:
    """根据价格变动幅度返回确认乘数"""
    abs_chg = abs(change_pct)
    for (lo, hi), mult in _PRICE_CONFIRM_MULTIPLIER.items():
        if lo <= abs_chg < hi:
            return mult
    return 1.0


def _strength_label(score: float) -> str:
    """将10分制数值转为标签"""
    if score >= 7:
        return "HIGH"
    elif score >= 4:
        return "MEDIUM"
    return "LOW"


def _tag_catalysts(candidates: List[Dict]) -> Dict[str, Dict]:
    """为候选标的标注催化剂类型和强度 (v2: 10分制 + 多源)
    
    改进:
      1. 使用 company_news(symbol) 替代 general_news — 针对性更强
      2. 10分制强度 (base_score * price_confirm * multi_source_bonus)
      3. 价格动量确认 — 已移动>5%说明催化剂已生效
      4. 多新闻命中同一规则 → 强度+1 (多源确认)
    
    API消耗: candidates数 * 1 (company_news) + candidates数 * 1 (quote) ≤ 20次
    降级: 失败返回空 dict
    """
    result = {}
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        client = _get_finnhub_client()
        if not client:
            return result
        
        t0 = time.time()
        from datetime import date, timedelta
        today = date.today()
        yesterday = today - timedelta(days=2)  # 取2天内新闻
        
        for c in candidates:
            if time.time() - t0 > 12:  # 总超时12s
                break
            
            symbol = c.get("symbol", "")
            bare = symbol.replace("US.", "")
            if not bare:
                continue
            
            # 1. 获取该股票专属新闻 (company_news API)
            articles = []
            try:
                articles = client.company_news(
                    bare, 
                    _from=yesterday.strftime("%Y-%m-%d"),
                    to=today.strftime("%Y-%m-%d")
                ) or []
            except Exception:
                # 降级: company_news 失败则跳过该标的
                continue
            
            time.sleep(0.08)  # rate limit
            
            # 2. 获取当前价格变动 (用于确认)
            change_pct = 0.0
            try:
                q = client.quote(bare)
                pc = q.get("pc", 0)
                cur = q.get("c", 0)
                if pc and cur and pc > 0:
                    change_pct = (cur - pc) / pc * 100
            except Exception:
                pass
            
            time.sleep(0.08)  # rate limit
            
            # 3. 匹配催化剂规则
            best_score = 0
            best_type = "NONE"
            best_headline = ""
            best_direction = None
            hit_count = 0  # 多条新闻命中同一规则的次数
            
            for article in articles[:20]:  # 每只股票最多看20条新闻
                headline = (article.get("headline", "") or "").lower()
                summary = (article.get("summary", "") or "").lower()
                text = headline + " " + summary
                
                for keywords, cat_type, base_score, direction in _CATALYST_RULES:
                    if any(kw in text for kw in keywords):
                        if cat_type == best_type:
                            hit_count += 1  # 同类催化剂多源确认
                        elif base_score > best_score:
                            best_score = base_score
                            best_type = cat_type
                            best_headline = (article.get("headline", "") or "")[:120]
                            best_direction = direction
                            hit_count = 1
                        break  # 一条新闻只匹配第一个命中规则
            
            if best_score > 0:
                # 4. 计算最终强度 = base * 价格确认 * 多源加成
                price_mult = _get_price_confirm_mult(change_pct)
                multi_source_bonus = min(hit_count - 1, 2) * 0.5  # 每多一条确认+0.5, 上限+1
                final_score = min(10, round(best_score * price_mult + multi_source_bonus, 1))
                
                result[symbol] = {
                    "type": best_type,
                    "score": final_score,
                    "strength": _strength_label(final_score),
                    "headline": best_headline,
                    "price_change_pct": round(change_pct, 1),
                    "source_count": hit_count,
                    "direction_hint": best_direction,
                }
        
        logger.info(f"[Pipeline] 催化剂标注(v2): {len(result)}个标的有催化剂 ({time.time()-t0:.1f}s)")
    except Exception as e:
        logger.debug(f"[Pipeline] catalyst tagging error: {e}")
    
    return result


def _fetch_plan_vix() -> float:
    """获取 plan 生成时刻的 VIX 基准值 (供 ConvictionTracker 计算 VIX 变化)"""
    try:
        import requests
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            lines = resp.text.strip().split('\n')
            if len(lines) > 1:
                parts = lines[-1].split(',')
                if len(parts) >= 2 and parts[1]:
                    return float(parts[1])
    except Exception:
        pass
    return 20.0  # fallback


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

    # ── Stage 1.5: Catalyst Tagging (规则引擎, 无LLM) ──
    logger.info("[Pipeline] 🏷️ Stage 1.5: 催化剂标注...")
    catalyst_tags = _tag_catalysts(candidates)
    # 将 catalyst 信息注入每个 candidate，供 Stage 2 LLM Validator 使用
    for c in candidates:
        c["catalyst"] = catalyst_tags.get(c["symbol"], {"type": "NONE", "strength": "NONE", "headline": ""})
    logger.info(f"[Pipeline] Stage 1.5 ✅ {len(catalyst_tags)} catalysts detected")

    # ── Stage 2: LLM Validation ──
    logger.info("[Pipeline] 🧠 Stage 2: LLM验证 (8 candidates)...")
    from app.plan.llm_validator import validate_candidates
    validation = validate_candidates(factor_screen)

    validated = validation.get("validated", [])

    # P0 修复 (2026-06-21): 过滤掉NEUTRAL方向的candidates
    # factor_engine方向冲突仲裁会产出direction_hint="NEUTRAL"的candidates
    # 这些candidates方向不明确, 不应进入LLM验证和后续流程
    factor_lookup_pre = {c["symbol"]: c for c in candidates}
    pre_count = len(validated)
    validated = [
        v for v in validated
        if v.get("direction", factor_lookup_pre.get(v.get("symbol", ""), {}).get("direction_hint", "")) != "NEUTRAL"
    ]
    neutral_filtered = pre_count - len(validated)
    if neutral_filtered > 0:
        logger.info(f"[Pipeline] 过滤 {neutral_filtered} 个 NEUTRAL 方向 candidates (冲突仲裁)")

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
    # P0 修复 (2026-06-21): 去掉方向强制补充 — 由信号质量决定方向, 不人为偏置
    ranked.sort(key=lambda r: r["rank_score"], reverse=True)
    top_ranked = [r for r in ranked if r["rank_score"] > 0]

    # 取 top 5, 纯按质量排序, 不强制方向多样性
    top_targets = top_ranked[:5]

    # Build target entries
    targets = []
    for r in top_targets:
        entry = _build_target_entry(r["validated"], r["factor_data"])
        targets.append(entry)

    # Task #19: 标记 Primary Target (rank_position / is_primary / why_primary)
    try:
        targets = _assign_primary_target(targets)
    except Exception as _pe:
        logger.debug(f"[Pipeline] _assign_primary_target 异常(忽略): {_pe}")

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

    # ── Stage 4.7: Deep Research (对标 deep research 报告的完整分析) ──
    # 对 top 3 targets 运行 4 分析师 → 多空辩论 → 综合决策
    # 每个 target 6 次 LLM 调用，总计 18 次 ≈ 60-90s
    logger.info("[Pipeline] 🔬 Stage 4.7: Deep Research (top 3 targets)...")
    deep_research_results = []
    try:
        import asyncio
        from app.plan.deep_research_pipeline import run_deep_research_batch

        # 获取盘前数据 (如果可用)
        premarket_data = None
        try:
            _premarket_path = _PLAN_PATH.parent / "premarket_enrichment.json"
            if _premarket_path.exists():
                premarket_data = json.loads(_premarket_path.read_text())
        except Exception:
            pass

        # 运行 deep research
        _loop = asyncio.new_event_loop()
        try:
            deep_research_results = _loop.run_until_complete(
                run_deep_research_batch(targets, premarket_data, max_targets=3)
            )
        finally:
            _loop.close()

        # 将结果注入 targets
        dr_lookup = {r["symbol"]: r.get("deep_research") for r in deep_research_results}
        for t in targets:
            sym = t.get("symbol", "")
            dr = dr_lookup.get(sym)
            if dr:
                t["deep_research"] = dr
                # 如果 deep research 给出了更好的 executive_summary，更新 rationale
                exec_summary = dr.get("executive_summary", "")
                if exec_summary and len(exec_summary) > 10:
                    t["executive_summary"] = exec_summary

                # P1 修复 (2026-06-21): deep_research方向冲突回传标注
                # 不翻转方向(太激进), 但标注冲突让用户知道
                factor_dir = t.get("direction", "")
                dr_action = str(dr.get("action", "")).upper()
                conflict = None
                if ("BUY" in dr_action or "LONG" in dr_action) and factor_dir == "SHORT":
                    conflict = "factor_engine推荐SHORT, 但deep_research建议做多"
                elif ("SELL" in dr_action or "SHORT" in dr_action) and factor_dir == "LONG":
                    conflict = "factor_engine推荐LONG, 但deep_research建议做空"

                if conflict:
                    t["conflict_warning"] = conflict
                    # 降级VALID→CONDITIONAL (不翻转方向, 但提醒风险)
                    if t.get("verdict") == "VALID":
                        t["verdict"] = "CONDITIONAL"
                    logger.info(
                        f"[Pipeline] ⚠️ 方向冲突 {sym}: {conflict}"
                    )

        success_count = sum(1 for r in deep_research_results if r.get("deep_research"))
        logger.info(
            f"[Pipeline] Stage 4.7 ✅ {success_count}/{len(deep_research_results)} deep research completed"
        )
    except Exception as e:
        logger.warning(f"[Pipeline] Stage 4.7 deep research failed (non-fatal): {e}")

    # ── Stage 5: Narrative ──
    logger.info("[Pipeline] ✍️ Stage 5: 生成摘要...")
    narrative = _generate_narrative(factor_screen, validation, targets)

    # ── Assemble final plan ──
    plan = {
        "date": today,
        "market_regime": factor_screen.get("regime", "NEUTRAL"),
        "vix": _fetch_plan_vix(),  # P0 修复 (2026-06-21): ConvictionTracker 需要 plan 时刻 VIX 基准
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
            "deep_research_count": len([r for r in deep_research_results if r.get("deep_research")]),
            "total_sec": round(time.time() - t0, 1),
        },
        "discovery_meta": factor_screen.get("pool_meta", {}),
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

    # ── 飞书推送 Top 机会 ──
    try:
        from app.notifier.feishu import notify_top_opportunities
        ok = notify_top_opportunities(targets, factor_screen.get("pool_meta"))
        if ok:
            logger.info(f"[Pipeline] 📢 Feishu push: {len(targets)} targets")
        else:
            logger.warning("[Pipeline] Feishu push failed (non-critical)")
    except Exception as e:
        logger.warning(f"[Pipeline] Feishu push error: {e}")

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
