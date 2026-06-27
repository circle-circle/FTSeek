"""Deep Research Pipeline — Stage 6-8.5 编排器

对标 deep research 报告的完整分析流程:
  Stage 6:   4 分析师并行 (技术/基本面/新闻/情绪)
  Stage 7:   多空辩论 (bull/bear debate)
  Stage 8:   综合决策 (risk + execution synthesis)
  Stage 8.5: 风险辩论 (aggressive/moderate/conservative 三方并行) ← 2026-06-16 新增

对 top N targets 执行（默认 3 个，控制 LLM 成本）。
每个 target: 4 analyst calls + 1 debate + 1 synthesis + 3 risk debate = 9 LLM calls。

降级策略:
  - 任一分析师失败 → 该维度标注"缺失"，继续后续阶段
  - 辩论 LLM 失败 → 从分析师报告中规则提取 bull/bear 论点
  - 综合 LLM 失败 → 从辩论结果中规则生成执行方案
  - 风险辩论失败 → 保留原 synthesis risk_tiers 不变
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def _run_stage6_analysts(
    target: Dict,
    premarket_data: Optional[Dict] = None,
) -> Dict[str, Dict]:
    """Stage 6: 并行运行 4 个分析师"""
    from app.plan.analysts import (
        run_technical_analysis,
        run_fundamental_analysis,
        run_news_analysis,
        run_sentiment_analysis,
    )

    results = await asyncio.gather(
        run_technical_analysis(target, premarket_data),
        run_fundamental_analysis(target, premarket_data),
        run_news_analysis(target, premarket_data),
        run_sentiment_analysis(target, premarket_data),
        return_exceptions=True,
    )

    def _safe(r, default_status="missing"):
        if isinstance(r, Exception):
            return {"status": default_status, "error": str(r)}
        return r if isinstance(r, dict) else {"status": default_status}

    return {
        "technical": _safe(results[0]),
        "fundamental": _safe(results[1]),
        "news": _safe(results[2]),
        "sentiment": _safe(results[3]),
    }


async def _run_stage7_debate(
    symbol: str,
    target: Dict,
    analyst_reports: Dict[str, Dict],
) -> Optional[Dict]:
    """Stage 7: 多空辩论 (1 LLM call)"""
    from app.llm.prompts.deep_research_prompts import (
        DEBATE_SYSTEM, DEBATE_USER, format_analyst_report,
    )
    from app.llm.model_call import call_llm

    tech_report = format_analyst_report("技术面", analyst_reports.get("technical", {}))
    fund_report = format_analyst_report("基本面", analyst_reports.get("fundamental", {}))
    news_report = format_analyst_report("新闻", analyst_reports.get("news", {}))
    sent_report = format_analyst_report("情绪", analyst_reports.get("sentiment", {}))

    user_prompt = DEBATE_USER.format(
        symbol=symbol,
        current_price=target.get("current_price", target.get("_current_price", "N/A")),
        technical_report=tech_report,
        fundamental_report=fund_report,
        news_report=news_report,
        sentiment_report=sent_report,
    )

    try:
        _loop = asyncio.get_event_loop()
        res = await asyncio.wait_for(
            _loop.run_in_executor(
                None,
                lambda: call_llm(
                    feature="dr_debate",
                    prompt=user_prompt,
                    system=DEBATE_SYSTEM,
                    temperature=0.3,
                    max_tokens=2500,
                    json_mode=True,
                ),
            ),
            timeout=120,
        )

        if res and res.success:
            from app.plan.deep_analysis_engine import _safe_json_loads
            parsed = _safe_json_loads(res.response)
            if parsed:
                parsed["_meta"] = {
                    "model_used": getattr(res, "model_used", ""),
                    "cost_usd": getattr(res, "cost_usd", 0),
                }
                return parsed

    except asyncio.TimeoutError:
        logger.warning(f"[deep_research·{symbol}] Stage 7 debate timeout")
    except Exception as e:
        logger.warning(f"[deep_research·{symbol}] Stage 7 debate error: {e}")

    # 降级: 从分析师报告规则提取
    return _fallback_debate(analyst_reports)


def _fallback_debate(analyst_reports: Dict) -> Dict:
    """降级辩论: 从分析师报告中规则提取 bull/bear 论点"""
    bull = []
    bear = []

    # 技术面
    tech = analyst_reports.get("technical", {})
    verdict = tech.get("overall_verdict", {})
    if verdict.get("direction") == "偏多":
        bull.append({"thesis": verdict.get("summary", "技术面偏多"), "strength": "MEDIUM",
                      "evidence": f"bull_signals={verdict.get('bull_signals', 0)}/5", "source_type": "technical"})
    elif verdict.get("direction") == "偏空":
        bear.append({"thesis": verdict.get("summary", "技术面偏空"), "strength": "MEDIUM",
                      "evidence": f"bear_signals={verdict.get('bear_signals', 0)}/5", "source_type": "technical"})

    # 基本面
    fund = analyst_reports.get("fundamental", {})
    cluster = fund.get("analyst_cluster", {})
    if cluster.get("cluster"):
        pos = cluster.get("position_in_cluster", "")
        if "下沿" in pos or "低于" in pos:
            bull.append({"thesis": f"当前价格在分析师目标价簇{pos}，上行空间大", "strength": "MEDIUM",
                          "evidence": f"目标价簇 ${cluster['cluster']['low']}-${cluster['cluster']['high']}", "source_type": "fundamental"})
        elif "超出" in pos:
            bear.append({"thesis": "当前价格超出分析师目标价簇，估值透支", "strength": "MEDIUM",
                          "evidence": f"目标价簇 ${cluster['cluster']['low']}-${cluster['cluster']['high']}", "source_type": "fundamental"})

    # 情绪
    sent = analyst_reports.get("sentiment", {})
    if sent.get("direction") == "偏多":
        bull.append({"thesis": f"散户情绪{sent.get('direction', '中性')}，{sent.get('crowding', '')}", "strength": "LOW",
                      "evidence": sent.get("conclusion", ""), "source_type": "sentiment"})
    elif sent.get("direction") == "偏空":
        bear.append({"thesis": f"散户情绪{sent.get('direction', '中性')}，可能反向指标", "strength": "LOW",
                      "evidence": sent.get("conclusion", ""), "source_type": "sentiment"})

    # 确保至少有论点
    if not bull:
        bull.append({"thesis": "无明显多头论据", "strength": "LOW", "evidence": "数据有限", "source_type": "technical"})
    if not bear:
        bear.append({"thesis": "无明显空头论据", "strength": "LOW", "evidence": "数据有限", "source_type": "technical"})

    # 独立方向判定: 基于分析师证据，不依赖 factor_engine 预设方向
    bull_strength = sum(1 for b in bull if b.get("strength") in ("HIGH", "MEDIUM"))
    bear_strength = sum(1 for b in bear if b.get("strength") in ("HIGH", "MEDIUM"))
    if bull_strength > bear_strength + 1:
        rec_dir = "LONG"
    elif bear_strength > bull_strength + 1:
        rec_dir = "SHORT"
    else:
        rec_dir = "NEUTRAL"

    return {
        "bull_case": bull,
        "bear_case": bear,
        "verdict": {
            "recommended_direction": rec_dir,
            "direction_confidence": "LOW",
            "key_disagreement": "数据不足以形成强分歧",
            "adopted_from_bear": bear[0]["thesis"] if bear else "",
        },
        "causal_chain": "数据有限，无法构建完整因果链",
        "historical_reference": "N/A",
        "_fallback": True,
    }


async def _run_stage8_synthesis(
    symbol: str,
    target: Dict,
    debate_result: Dict,
    analyst_reports: Dict[str, Dict],
) -> Optional[Dict]:
    """Stage 8: 综合决策 (1 LLM call)"""
    from app.llm.prompts.deep_research_prompts import (
        SYNTHESIS_SYSTEM, SYNTHESIS_USER,
        format_analyst_report, format_indicators_summary,
    )
    from app.llm.model_call import call_llm

    tech = analyst_reports.get("technical", {})
    fund = analyst_reports.get("fundamental", {})
    news = analyst_reports.get("news", {})
    sent = analyst_reports.get("sentiment", {})

    # 格式化辩论结果
    debate_text = json.dumps(debate_result, ensure_ascii=False, indent=2)[:2000]
    
    # 从辩论结果提取独立推荐方向（方向盲审的核心改动）
    debate_direction = debate_result.get("verdict", {}).get("recommended_direction", "NEUTRAL")
    
    # 获取该标的历史交易表现（用于 conviction 校准）
    trade_history_text = "无历史交易数据"
    try:
        from app.plan.llm_validator import _get_trade_history
        hist = _get_trade_history([symbol])
        if hist and symbol in hist:
            h = hist[symbol]
            trade_history_text = (
                f"总交易 {h['count']} 次, 胜率 {h['win_rate']:.0f}%, "
                f"总盈亏 ${h['total_pnl']:+.2f}\n"
            )
            for t in h.get("last_trades", [])[:3]:
                trade_history_text += f"- {t['date']} {t['direction']}: ${t['pnl']:+.2f}\n"
    except Exception:
        pass

    user_prompt = SYNTHESIS_USER.format(
        symbol=symbol,
        debate_direction=debate_direction,
        current_price=target.get("current_price", target.get("_current_price", "N/A")),
        entry_low=target.get("entry_zone", [0, 0])[0] if isinstance(target.get("entry_zone"), list) and len(target.get("entry_zone", [])) >= 1 else 0,
        entry_high=target.get("entry_zone", [0, 0])[-1] if isinstance(target.get("entry_zone"), list) and len(target.get("entry_zone", [])) >= 1 else 0,
        stop_loss=target.get("stop_loss", "N/A"),
        take_profit=target.get("take_profit", "N/A"),
        confidence=target.get("confidence", "N/A"),
        debate_result=debate_text,
        indicators_summary=format_indicators_summary(tech),
        fundamental_summary=format_analyst_report("基本面", fund),
        news_summary=format_analyst_report("新闻", news),
        sentiment_summary=format_analyst_report("情绪", sent),
        tech_quality=tech.get("status", "missing"),
        fund_quality=fund.get("status", "missing"),
        news_quality=news.get("status", "missing"),
        sentiment_quality=sent.get("status", "missing"),
        trade_history=trade_history_text,
    )

    try:
        _loop = asyncio.get_event_loop()
        res = await asyncio.wait_for(
            _loop.run_in_executor(
                None,
                lambda: call_llm(
                    feature="deep_target_analysis",
                    prompt=user_prompt,
                    system=SYNTHESIS_SYSTEM,
                    temperature=0.2,
                    max_tokens=2500,
                    json_mode=True,
                ),
            ),
            timeout=120,
        )

        if res and res.success:
            from app.plan.deep_analysis_engine import _safe_json_loads
            parsed = _safe_json_loads(res.response)
            if parsed:
                parsed["_meta"] = {
                    "model_used": getattr(res, "model_used", ""),
                    "cost_usd": getattr(res, "cost_usd", 0),
                }
                return parsed

    except asyncio.TimeoutError:
        logger.warning(f"[deep_research·{symbol}] Stage 8 synthesis timeout")
    except Exception as e:
        logger.warning(f"[deep_research·{symbol}] Stage 8 synthesis error: {e}")

    # 降级: 规则生成
    return _fallback_synthesis(target, debate_result, analyst_reports)


def _fallback_synthesis(target: Dict, debate: Dict, analyst_reports: Dict) -> Dict:
    """降级综合: 从已有数据规则生成执行方案"""
    entry_zone = target.get("entry_zone", [0, 0])
    sl = target.get("stop_loss", 0)
    tp = target.get("take_profit", 0)
    cur = target.get("current_price", target.get("_current_price", 0))

    # 基本建仓方案
    if isinstance(entry_zone, list) and len(entry_zone) >= 2:
        mid = (entry_zone[0] + entry_zone[1]) / 2
        build_plan = {
            "batch_1": {"pct": "1/3", "zone": f"${entry_zone[0]}-${entry_zone[1]}", "condition": "现价区分批挂单"},
            "batch_2": {"pct": "1/3", "zone": f"${entry_zone[0]}", "condition": "回调至入场下限确认不破"},
            "batch_3": {"pct": "1/3", "zone": "突破确认后", "condition": "放量突破近期高点"},
            "event_buffer": "重大事件前保留≥1/3弹药",
        }
    else:
        build_plan = {
            "batch_1": {"pct": "1/2", "zone": "市价", "condition": "即时建仓"},
            "batch_2": {"pct": "1/2", "zone": "回调后", "condition": "确认支撑"},
            "event_buffer": "注意事件风险",
        }

    # 数据质量
    statuses = {}
    for dim, data in analyst_reports.items():
        statuses[dim] = data.get("status", "missing")
    complete = sum(1 for v in statuses.values() if v == "complete")
    degraded = sum(1 for v in statuses.values() if v == "degraded")
    missing = sum(1 for v in statuses.values() if v == "missing")

    verdict_conf = debate.get("verdict", {}).get("direction_confidence", "LOW")
    debate_dir = debate.get("verdict", {}).get("recommended_direction", "NEUTRAL")
    if missing >= 2:
        verdict_conf = "LOW"
    elif degraded >= 2 and verdict_conf == "HIGH":
        verdict_conf = "MEDIUM"

    # 方向盲审: 基于辩论结果判定 action，不依赖 factor_engine 的预设方向
    if verdict_conf == "LOW" or missing >= 2 or debate_dir == "NEUTRAL":
        action = "WATCH"
        action_detail = "数据不足或方向不明确，加入观察池"
        conviction = max(20, 30 - missing * 5)
        exec_summary = f"⚠️ 加入观察池: 数据不完整({complete}项完整/{missing}项缺失)或辩论方向NEUTRAL，等待更明确信号"
    elif verdict_conf == "MEDIUM" and complete < 3:
        action = "WATCH"
        action_detail = "数据维度不足，持续跟踪"
        conviction = 35
        exec_summary = f"⚠️ 持续跟踪: 仅{complete}维数据完整，需要更多确认信号"
    else:
        # 数据足够 + 方向明确
        if debate_dir == "LONG":
            action = "BUY" if verdict_conf == "HIGH" else "CONDITIONAL"
        elif debate_dir == "SHORT":
            action = "SELL" if verdict_conf == "HIGH" else "CONDITIONAL"
        else:
            action = "WATCH"
        action_detail = "分批建仓(降级方案，辩论推荐方向)" if action in ("BUY", "CONDITIONAL") else "继续观察"
        conviction = 50 if verdict_conf == "MEDIUM" else 70
        exec_summary = f"辩论推荐{'做多' if debate_dir == 'LONG' else '做空'}，但LLM降级为规则方案，建议保守分批操作"

    return {
        "verdict": {
            "action": action,
            "action_detail": action_detail,
            "conviction": conviction,
            "data_quality_score": f"{complete}/{len(statuses)}",
        },
        "executive_summary": exec_summary,
        "build_plan": build_plan,
        "risk_tiers": {
            "aggressive": {"action": "按计划执行", "trigger": "数据确认后", "risk": "数据不完整"},
            "moderate": {"action": "减半仓位", "trigger": "现价区", "risk": "信息不足"},
            "conservative": {"action": "观望", "trigger": "等待数据完善", "risk": "可能错过机会"},
        },
        "risk_triggers": [
            {"condition": f"收盘跌破${sl}", "action": "止损离场"},
            {"condition": "放量突破阻力", "action": "确认加仓"},
        ],
        "key_levels": {
            "current": cur,
            "hard_stop": sl,
            "resistance_1": tp if tp > 0 else "N/A",
            "support_1": entry_zone[0] if isinstance(entry_zone, list) and entry_zone else "N/A",
        },
        "data_quality": {
            "technical": statuses.get("technical", "missing"),
            "fundamental": statuses.get("fundamental", "missing"),
            "news": statuses.get("news", "missing"),
            "sentiment": statuses.get("sentiment", "missing"),
            "overall": f"{complete}项完整/{degraded}项降级/{missing}项缺失",
            "impact_note": "降级方案，建议等待完整分析后决策" if missing >= 2 else "",
        },
        "_fallback": True,
    }


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════
# Stage 8.5: 风险辩论 (Risk Debate) — 2026-06-16 新增
# 对标 Deep Research 案例中三方独立辩论:
#   激进派: 压力测试，论证加大仓位
#   中性派: 校准触发级别，识别结构性缺陷
#   保守派: 尾部风险压力测试，论证减仓
# ═══════════════════════════════════════════════════════════

_RISK_DEBATE_SYSTEM = """你是一位专业的风险分析师。你的角色是 {perspective}。
你将收到一份交易执行方案，你需要从你的角色视角审视它，找出缺陷并提出修改建议。

你的输出必须是 JSON，包含:
{{
  "structural_flaws": ["发现的结构性缺陷，如批次间距过小、止损与入场重叠等"],
  "modifications": [
    {{"field": "修改的字段名", "current": "当前值", "proposed": "建议值", "reason": "原因"}}
  ],
  "risk_assessment": "整体风险评估 (1-2句话)",
  "adopted_priority": "HIGH/MEDIUM/LOW"
}}"""

_RISK_DEBATE_USER = """审视以下 {symbol} {direction} 交易方案:

## 建仓方案
{build_plan}

## 风控三层方案
{risk_tiers}

## 止损/止盈
入场区间: {entry_zone}
止损: ${stop_loss}
止盈: ${take_profit}
当前价: ${current_price}
ATR: {atr}

## 辩论裁决
{debate_verdict}

从{perspective_cn}视角，找出结构性缺陷并提出修改。特别注意:
1. 批次间距是否合理 (至少 > 0.5 ATR)?
2. 止损与入场区间是否重叠 (重叠 = 建仓即止损)?
3. 触发条件是否可观测、可执行?
4. 仓位上限是否在重大事件前适当收紧?"""


async def _run_risk_debate(
    symbol: str,
    target: Dict,
    synthesis: Dict,
    debate: Dict,
) -> Optional[Dict]:
    """Stage 8.5: 三方风险辩论 (3 LLM calls 并行)
    
    Returns: {
        "perspectives": {"aggressive": {...}, "moderate": {...}, "conservative": {...}},
        "adopted_modifications": [...],
        "structural_flaws": [...],
        "adjudication": "裁决摘要",
    }
    """
    from app.llm.model_call import call_llm

    build_plan = synthesis.get("build_plan", {})
    risk_tiers = synthesis.get("risk_tiers", {})
    debate_verdict = debate.get("verdict", {})
    entry_zone = target.get("entry_zone", [0, 0])
    sl = target.get("stop_loss", 0)
    tp = target.get("take_profit", 0)
    cur = target.get("current_price", target.get("_current_price", 0))
    # 方向盲审: 使用辩论推荐方向，不用 factor_engine 预设方向
    direction = debate.get("verdict", {}).get("recommended_direction", target.get("direction", "LONG"))
    
    # ATR 估算
    atr = target.get("atr", 0)
    if not atr and cur > 0:
        atr = round(cur * 0.03, 2)  # 3% 默认估算

    perspectives = {
        "aggressive": {
            "perspective": "激进派风控分析师",
            "perspective_cn": "激进派 (倾向更大仓位、更紧止损)",
            "focus": "你倾向于在有利条件下加大仓位。审视方案是否过于保守，但同时检查止损是否太紧导致频繁触发。",
        },
        "moderate": {
            "perspective": "中性派风控分析师",
            "perspective_cn": "中性派 (校准触发级别、识别结构性缺陷)",
            "focus": "你是最注重方案结构合理性的分析师。特别检查: 批次间距、止损与入场重叠、ATR 合理性、触发条件可执行性。你的核心价值是发现其他人忽略的结构性缺陷。",
        },
        "conservative": {
            "perspective": "保守派风控分析师",
            "perspective_cn": "保守派 (尾部风险压力测试、倾向更小仓位)",
            "focus": "你关注极端情景 (gap-down、黑天鹅)。检查方案在极端波动下是否仍安全，事件前仓位上限是否足够低。",
        },
    }

    async def _run_perspective(name: str, config: dict) -> dict:
        system = _RISK_DEBATE_SYSTEM.format(perspective=config["perspective"])
        user = _RISK_DEBATE_USER.format(
            symbol=symbol,
            direction=direction,
            build_plan=json.dumps(build_plan, ensure_ascii=False, indent=2)[:1500],
            risk_tiers=json.dumps(risk_tiers, ensure_ascii=False, indent=2)[:1000],
            entry_zone=f"${entry_zone[0]}-${entry_zone[1]}" if isinstance(entry_zone, list) and len(entry_zone) >= 2 else "N/A",
            stop_loss=sl,
            take_profit=tp,
            current_price=cur,
            atr=atr,
            debate_verdict=json.dumps(debate_verdict, ensure_ascii=False)[:500],
            perspective_cn=config["perspective_cn"],
        )
        user += f"\n\n你的关注点: {config['focus']}"

        try:
            _loop = asyncio.get_event_loop()
            res = await asyncio.wait_for(
                _loop.run_in_executor(
                    None,
                    lambda: call_llm(
                        feature="dr_risk_debate",
                        prompt=user,
                        system=system,
                        temperature=0.4,
                        max_tokens=800,
                        json_mode=True,
                    ),
                ),
                timeout=60,
            )
            if res and res.success:
                from app.plan.deep_analysis_engine import _safe_json_loads
                parsed = _safe_json_loads(res.response)
                if parsed:
                    return parsed
        except Exception as e:
            logger.debug(f"[deep_research·{symbol}] risk_debate {name} error: {e}")
        
        return {}

    # 3 perspectives in parallel
    tasks = {name: _run_perspective(name, cfg) for name, cfg in perspectives.items()}
    results = {}
    try:
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.debug(f"[deep_research·{symbol}] risk_debate {name} exception: {result}")
                results[name] = {}
            else:
                results[name] = result or {}
    except Exception as e:
        logger.warning(f"[deep_research·{symbol}] risk_debate gather error: {e}")
        results = {name: {} for name in perspectives}

    # 合并: 收集所有 structural_flaws 和 modifications
    all_flaws = []
    all_modifications = []
    for name, res in results.items():
        flaws = res.get("structural_flaws", [])
        if isinstance(flaws, list):
            all_flaws.extend([f"[{name}] {f}" for f in flaws])
        mods = res.get("modifications", [])
        if isinstance(mods, list):
            for m in mods:
                m["_source"] = name
                all_modifications.append(m)

    # 裁决: 中性派发现的结构性缺陷优先级最高
    adopted = []
    for m in all_modifications:
        source = m.get("_source", "")
        priority = m.get("adopted_priority", "LOW") if isinstance(m, dict) else "LOW"
        # 中性派的修改默认采纳，激进/保守派的需要 HIGH 优先级
        if source == "moderate" or priority == "HIGH":
            adopted.append({
                "field": m.get("field", ""),
                "current": m.get("current", ""),
                "proposed": m.get("proposed", ""),
                "reason": m.get("reason", ""),
                "source": source,
            })

    result = {
        "perspectives": results,
        "adopted_modifications": adopted,
        "structural_flaws": all_flaws,
        "adjudication": f"三方辩论完成: {len(all_flaws)} 个缺陷, {len(adopted)} 项采纳",
    }

    if all_flaws:
        logger.info(
            f"[deep_research·{symbol}] 风险辩论发现 {len(all_flaws)} 个结构性缺陷: "
            + "; ".join(all_flaws[:3])
        )

    return result


def _apply_risk_debate_to_synthesis(synthesis: Dict, risk_debate: Dict) -> Dict:
    """将风险辩论的采纳修改应用到 synthesis 的 risk_tiers 和 build_plan"""
    adopted = risk_debate.get("adopted_modifications", [])
    if not adopted:
        return synthesis

    modified = synthesis.copy()
    risk_tiers = modified.get("risk_tiers", {})
    build_plan = modified.get("build_plan", {})

    for m in adopted:
        field = m.get("field", "")
        proposed = m.get("proposed", "")
        # 尝试匹配到 risk_tiers 或 build_plan 的具体字段
        for tier_name in ["aggressive", "moderate", "conservative"]:
            tier = risk_tiers.get(tier_name, {})
            if isinstance(tier, dict) and field in tier:
                tier[field] = proposed
                logger.info(
                    f"[risk_debate·apply] {tier_name}.{field}: "
                    f"{m.get('current', '')} → {proposed} ({m.get('reason', '')})"
                )
        for batch_name in ["batch_1", "batch_2", "batch_3", "event_buffer"]:
            batch = build_plan.get(batch_name, {})
            if isinstance(batch, dict) and field in batch:
                batch[field] = proposed

    # 添加风险辩论元数据
    modified["_risk_debate"] = {
        "structural_flaws": risk_debate.get("structural_flaws", []),
        "adopted_count": len(adopted),
        "adjudication": risk_debate.get("adjudication", ""),
    }

    return modified


# ═══════════════════════════════════════════════════════════

async def run_deep_research_for_target(
    target: Dict,
    premarket_data: Optional[Dict] = None,
) -> Optional[Dict]:
    """为单个 target 运行完整 deep research (Stage 6-8)。

    Args:
        target: plan target dict
        premarket_data: 盘前数据

    Returns:
        deep_research dict (含 verdict, executive_summary, build_plan, risk_tiers, etc.)
        失败返回 None
    """
    symbol = target.get("symbol", "")
    t0 = time.time()

    try:
        # Stage 6: 4 分析师并行
        logger.info(f"[deep_research·{symbol}] Stage 6: 4 分析师并行...")
        analyst_reports = await _run_stage6_analysts(target, premarket_data)
        logger.info(
            f"[deep_research·{symbol}] Stage 6 完成: "
            + ", ".join(f"{k}={v.get('status', '?')}" for k, v in analyst_reports.items())
        )

        # Stage 7: 多空辩论
        logger.info(f"[deep_research·{symbol}] Stage 7: 多空辩论...")
        debate = await _run_stage7_debate(symbol, target, analyst_reports)
        if not debate:
            debate = _fallback_debate(analyst_reports)
        logger.info(
            f"[deep_research·{symbol}] Stage 7 完成: "
            f"bull={len(debate.get('bull_case', []))} bear={len(debate.get('bear_case', []))} "
            f"fallback={debate.get('_fallback', False)}"
        )

        # Stage 8: 综合决策
        logger.info(f"[deep_research·{symbol}] Stage 8: 综合决策...")
        synthesis = await _run_stage8_synthesis(symbol, target, debate, analyst_reports)
        if not synthesis:
            synthesis = _fallback_synthesis(target, debate, analyst_reports)
        logger.info(
            f"[deep_research·{symbol}] Stage 8 完成: "
            f"action={synthesis.get('verdict', {}).get('action', '?')} "
            f"fallback={synthesis.get('_fallback', False)}"
        )

        # Stage 8.5: 风险辩论 (2026-06-16 新增)
        logger.info(f"[deep_research·{symbol}] Stage 8.5: 风险辩论 (3方并行)...")
        try:
            risk_debate = await asyncio.wait_for(
                _run_risk_debate(symbol, target, synthesis, debate),
                timeout=90,
            )
            if risk_debate:
                synthesis = _apply_risk_debate_to_synthesis(synthesis, risk_debate)
                logger.info(
                    f"[deep_research·{symbol}] Stage 8.5 完成: "
                    f"flaws={len(risk_debate.get('structural_flaws', []))} "
                    f"adopted={len(risk_debate.get('adopted_modifications', []))}"
                )
            else:
                logger.info(f"[deep_research·{symbol}] Stage 8.5: 风险辩论无结果，保留原方案")
                risk_debate = {}
        except asyncio.TimeoutError:
            logger.warning(f"[deep_research·{symbol}] Stage 8.5 风险辩论超时 (90s)")
            risk_debate = {}
        except Exception as e:
            logger.warning(f"[deep_research·{symbol}] Stage 8.5 风险辩论异常: {e}")
            risk_debate = {}

        # 组装最终 deep_research 对象
        # 注入 fetched_at + data_sources 到 data_quality (LLM 不会输出这些)
        synth_dq = synthesis.get("data_quality", {})
        fund_fin = analyst_reports.get("fundamental", {}).get("financials", {})
        synth_dq["fetched_at"] = fund_fin.get("fetched_at", "")
        synth_dq["data_sources"] = {
            "financials": "ok" if fund_fin.get("source") else "failed",
            "analyst": "ok" if analyst_reports.get("fundamental", {}).get("analyst_cluster", {}).get("total_analysts", 0) > 0 else "failed",
            "news": "ok" if analyst_reports.get("news", {}).get("news_timeline") else "failed",
            "catalyst": "ok" if analyst_reports.get("fundamental", {}).get("next_catalyst") else "failed",
        }
        deep_research = {
            "verdict": synthesis.get("verdict", {}),
            "executive_summary": synthesis.get("executive_summary", ""),
            "build_plan": synthesis.get("build_plan", {}),
            "risk_tiers": synthesis.get("risk_tiers", {}),
            "risk_triggers": synthesis.get("risk_triggers", []),
            "key_levels": synthesis.get("key_levels", {}),
            "data_quality": synth_dq,
            # 辩论结果
            "bull_case": debate.get("bull_case", []),
            "bear_case": debate.get("bear_case", []),
            "debate_verdict": debate.get("verdict", {}),
            "causal_chain": debate.get("causal_chain", ""),
            "historical_reference": debate.get("historical_reference", ""),
            # 分析师原始报告 (供 UI 展开使用)
            "analyst_reports": {
                "technical": {
                    "status": analyst_reports["technical"].get("status"),
                    "indicators_table": analyst_reports["technical"].get("indicators_table", []),
                    "trend": analyst_reports["technical"].get("trend", {}),
                    "momentum": analyst_reports["technical"].get("momentum", {}),
                    "volatility": analyst_reports["technical"].get("volatility", {}),
                    "volume": analyst_reports["technical"].get("volume", {}),
                    "key_levels": analyst_reports["technical"].get("key_levels", {}),
                    "overall_verdict": analyst_reports["technical"].get("overall_verdict", {}),
                    "conclusion": analyst_reports["technical"].get("conclusion", ""),
                },
                "fundamental": {
                    "status": analyst_reports["fundamental"].get("status"),
                    "financials": analyst_reports["fundamental"].get("financials", {}),
                    "valuation": analyst_reports["fundamental"].get("valuation", {}),
                    "analyst_cluster": analyst_reports["fundamental"].get("analyst_cluster", {}),
                    "conclusion": analyst_reports["fundamental"].get("conclusion", ""),
                },
                "news": {
                    "status": analyst_reports["news"].get("status"),
                    "news_timeline": analyst_reports["news"].get("news_timeline", []),
                    "catalysts": analyst_reports["news"].get("catalysts", []),
                    "macro_calendar": analyst_reports["news"].get("macro_calendar", []),
                    "sector_context": analyst_reports["news"].get("sector_context", ""),
                    "conclusion": analyst_reports["news"].get("conclusion", ""),
                },
                "sentiment": {
                    "status": analyst_reports["sentiment"].get("status"),
                    "stocktwits": analyst_reports["sentiment"].get("stocktwits", {}),
                    "reddit": analyst_reports["sentiment"].get("reddit", {}),
                    "crowding": analyst_reports["sentiment"].get("crowding", ""),
                    "direction": analyst_reports["sentiment"].get("direction", ""),
                    "conclusion": analyst_reports["sentiment"].get("conclusion", ""),
                },
            },
            # 指标摘要 (从技术分析中提取)
            "indicators_summary": {
                "trend": analyst_reports["technical"].get("trend", {}).get("alignment", "N/A"),
                "momentum": analyst_reports["technical"].get("momentum", {}).get("summary", "N/A"),
                "volatility": analyst_reports["technical"].get("volatility", {}).get("summary", "N/A"),
                "volume": analyst_reports["technical"].get("volume", {}).get("summary", "N/A"),
            },
            # 元信息
            "_meta": {
                "symbol": symbol,
                "total_time_sec": round(time.time() - t0, 1),
                "analyst_statuses": {k: v.get("status", "missing") for k, v in analyst_reports.items()},
                "debate_fallback": debate.get("_fallback", False),
                "synthesis_fallback": synthesis.get("_fallback", False),
                "risk_debate": synthesis.get("_risk_debate", {}),
            },
        }

        logger.info(
            f"[deep_research·{symbol}] ✅ 完成: "
            f"action={deep_research['verdict'].get('action', '?')} "
            f"conviction={deep_research['verdict'].get('conviction', '?')}% "
            f"summary={deep_research['executive_summary'][:60]}... "
            f"({deep_research['_meta']['total_time_sec']}s)"
        )
        return deep_research

    except Exception as e:
        logger.exception(f"[deep_research·{symbol}] 未预期异常: {e}")
        return None


async def run_deep_research_batch(
    targets: List[Dict],
    premarket_data: Optional[Dict] = None,
    max_targets: int = 3,
) -> List[Dict]:
    """批量为多个 targets 运行 deep research。

    Args:
        targets: target dict 列表
        premarket_data: 公共盘前数据
        max_targets: 最多处理几个 (控制 LLM 成本)

    Returns:
        列表，每个元素 {"symbol": "...", "deep_research": {...} | None}
    """
    to_process = targets[:max_targets]
    logger.info(f"[deep_research] 批量启动: {len(to_process)} targets (max={max_targets})")

    t0 = time.time()

    # 串行处理每个 target（避免 LLM 并发过载）
    results = []
    for target in to_process:
        sym = target.get("symbol", "")

        # 缓存检查: 跳过已有有效 deep_research 的 target
        existing_dr = target.get("deep_research")
        if existing_dr and isinstance(existing_dr, dict):
            is_fallback = existing_dr.get("_meta", {}).get("synthesis_fallback", False)
            has_verdict = bool(existing_dr.get("verdict", {}).get("action"))
            if has_verdict and not is_fallback:
                logger.info(f"[deep_research] {sym} 已有有效 deep_research, 跳过")
                results.append({"symbol": sym, "deep_research": existing_dr, "cached": True})
                continue

        try:
            dr = await run_deep_research_for_target(target, premarket_data)
            results.append({"symbol": sym, "deep_research": dr})
        except Exception as e:
            logger.error(f"[deep_research] {sym} batch error: {e}")
            results.append({"symbol": sym, "deep_research": None, "error": str(e)})

    elapsed = round(time.time() - t0, 1)
    success = sum(1 for r in results if r.get("deep_research"))
    logger.info(f"[deep_research] 批量完成: {success}/{len(to_process)} 成功, {elapsed}s")

    return results


__all__ = [
    "run_deep_research_for_target",
    "run_deep_research_batch",
]
