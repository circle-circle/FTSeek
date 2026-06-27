"""Portfolio Synthesis — 跨标的组合级多模型综合分析

在所有target的deep_research完成后，增加一个"组合经理"阶段：
  - 输入: 全部target的deep_research结果 + 市况 + 持仓
  - 用2个模型(qwen-max + deepseek-pro)独立生成组合级分析
  - 合并输出: 排序后的股票名单 + 买卖方向 + 一句话理由

设计原则:
  1. 不替用户做最终决策——给方向+理由，用户决定下不下单
  2. 多模型交叉验证——两个模型都推荐的方向才标为"高确信"
  3. 借鉴deepsearch模式——核心理由3条+风控触发+分批方案
  4. 输出绝对化——每个标的明确BUY/SELL/CONDITIONAL/WATCH，不给中性选项
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


PORTFOLIO_SYNTH_SYSTEM = """你是一位资深投资组合经理，负责在盘前对所有候选标的做最终排序和方向推荐。

【你的角色】
你不是分析师——分析已经由4分析师+多空辩论完成。你是做最终决策的组合经理。
你需要综合所有标的的分析结果，在当前市况下给出明确的操作优先级和方向。

【原则】
1. 每个标的必须给出明确方向: BUY / SELL / CONDITIONAL / WATCH，不允许"建议观望"这种废话
2. 必须给出排序——在当前市况下最值得操作的标的排在前面
3. 每个标的给出一句话核心理由(≤30字)，包含最关键的1个做多理由+1个风险
4. BUY/SELL的标的必须附带建议仓位(1%-6%)和止损位
5. CONDITIONAL的标的必须明确"需要什么条件才能升级为BUY"
6. WATCH的标的必须明确"在什么条件下值得重新关注"
7. 所有数据来自前置分析师的真实API数据，标注来源和时间。禁止生成模板废话
8. 不受历史胜率单独影响——小样本历史胜率不具统计显著性
9. 关注市况适配——震荡市偏均值回归，趋势市偏动量

【方向决策规则——充分利用扩展数据】
- 空头数据(short%/days_to_cover): SHORT方向必须检查！short%>5%且days_to_cover>5=空头拥挤，SHORT风险高；short%<2%=做空安全
- 分析师共识(rating/target_avg): rating≥4且buy%>70%=机构看多共识强；rating≤2=机构看空
- 内部人交易(signal): strong_buy信号=管理层看好，做多加分；sell信号=管理层减持，做空加分
- 目标价 vs 当前价: 目标价高于当前价20%+ = 上行空间大，做多加分；低于当前价 = 下行风险
- LONG和SHORT都可以选——根据数据做方向决策，不要默认偏多
- 如果空头拥挤(short%高)但分析师看多+内部人买入 → 不要做空，可能逼空
- 如果分析师看空+内部人卖出+空头不拥挤 → SHORT信号确认

【输出格式】严格JSON，不要markdown包裹"""

PORTFOLIO_SYNTH_USER = """## 市况
- Regime: {regime}
- 信心度: {confidence}%
- 市况研判: {reasoning}

## 当前持仓
{positions}

## 候选标的 Deep Research 汇总
{targets_summary}

## 经济事件
{economic_events}

---

请输出以下JSON结构:
{{
    "market_assessment": "1-2句话市况判断，明确今天偏多/偏空/震荡",
    "recommended_direction": "今日整体操作方向: 偏多/偏空/防御",
    "risk_level": "LOW/MEDIUM/HIGH — 今日整体风险等级",
    "action_list": [
        {{
            "symbol": "标的代码",
            "rank": 1,
            "action": "BUY/SELL/CONDITIONAL/WATCH",
            "direction": "LONG/SHORT",
            "conviction": 0-100,
            "position_pct": "建议仓位%(如3%)",
            "entry": "入场价或区间",
            "stop_loss": "止损价",
            "take_profit": "止盈价",
            "core_reason": "核心理由(≤30字: 1个做点+1个风险)",
            "upgrade_condition": "升级条件(CONDITIONAL/WATCH必填)",
            "key_risk": "最大风险(1句话)"
        }}
    ],
    "portfolio_summary": "组合级总结(2-3句话: 今日该做什么不该做什么)",
    "feishu_brief": "飞书推送摘要(1句话, ≤80字, 直接送飞书)"
}}"""


def _format_targets_for_synthesis(targets: List[Dict]) -> str:
    """将所有target的deep_research + Futu扩展数据精简为合成器输入

    2026-06-21: 新增空头/分析师/内部人数据注入，让LLM看到完整画面
    """
    # 懒加载 FutuExtendedProvider (共享实例)
    futu_ext = None
    try:
        from app.data.providers.futu_extended_provider import FutuExtendedProvider
        futu_ext = FutuExtendedProvider()
    except Exception:
        pass

    lines = []
    for i, t in enumerate(targets, 1):
        sym = t.get("symbol", "")
        direction = t.get("direction", "?")
        verdict = t.get("verdict", "")
        conf = t.get("confidence", 0)
        entry = t.get("entry_zone", [0, 0])
        sl = t.get("stop_loss", 0)
        tp = t.get("take_profit", 0)
        cur = t.get("current_price", 0) or t.get("_current_price", 0)
        fs = t.get("factor_score", 0)

        dr = t.get("deep_research", {})
        dr_verdict = {}
        dr_summary = ""
        bull_top = ""
        bear_top = ""
        if dr:
            dr_verdict = dr.get("verdict", {})
            dr_summary = dr.get("executive_summary", "")
            bulls = dr.get("bull_case", [])
            bears = dr.get("bear_case", [])
            if bulls:
                bull_top = bulls[0].get("thesis", "")
            if bears:
                bear_top = bears[0].get("thesis", "")

        # ── 注入 Futu 扩展数据 (2026-06-21) ──
        ext_lines = []
        if futu_ext:
            code = sym if sym.startswith("US.") else f"US.{sym}"

            # 空头数据 — SHORT方向决策关键
            si = futu_ext.get_short_interest(code)
            if si.get("status") == "ok":
                d = si["data"]
                if d.get("short_percent") is not None:
                    ext_lines.append(
                        f"  空头: short%={d['short_percent']}%, "
                        f"days_to_cover={d['days_to_cover']}, "
                        f"shares_short={d['shares_short']:,}"
                    )

            # 分析师共识 — 目标价和评级
            ac = futu_ext.get_analyst_consensus(code)
            if ac.get("status") == "ok":
                d = ac["data"]
                if d.get("rating") is not None:
                    ext_lines.append(
                        f"  分析师: {d['rating_label']}({d['rating']}/5), "
                        f"{d['total_analysts']}人, "
                        f"目标价${d['target_avg']}({d['target_low']}-{d['target_high']}), "
                        f"buy%={d['buy_pct']}"
                    )

            # 内部人交易 — 信号方向
            it = futu_ext.get_insider_trades(code)
            if it.get("status") == "ok":
                d = it["data"]
                if d.get("buy_count", 0) + d.get("sell_count", 0) > 0:
                    ext_lines.append(
                        f"  内部人: buy={d['buy_count']} sell={d['sell_count']} "
                        f"net={d['net_shares']:,} → {d['signal']}"
                    )

        ext_text = "\n".join(ext_lines) if ext_lines else "  (无扩展数据)"

        lines.append(
            f"### {i}. {sym} | {direction} | verdict={verdict} conf={conf}% fs={fs:.0f}\n"
            f"  当前价: ${cur} | 入场: ${entry[0] if isinstance(entry, list) and entry else 0}-${entry[-1] if isinstance(entry, list) and entry else 0}\n"
            f"  止损: ${sl} | 止盈: ${tp}\n"
            f"  DR裁决: {dr_verdict.get('action', 'N/A')} (确信{dr_verdict.get('conviction', '?')}%)\n"
            f"  DR摘要: {dr_summary[:100]}\n"
            f"  最强多头: {bull_top[:80]}\n"
            f"  最强空头: {bear_top[:80]}\n"
            f"{ext_text}"
        )

    if futu_ext:
        futu_ext.close()

    return "\n\n".join(lines)


def _format_positions(position_actions: List[Dict]) -> str:
    """格式化当前持仓"""
    if not position_actions:
        return "(无持仓)"
    lines = []
    for p in position_actions:
        sym = p.get("symbol", "")
        direction = p.get("direction", "")
        pnl = p.get("current_pnl_pct", 0)
        action = p.get("action", "")
        lines.append(f"  {sym} {direction} pnl={pnl:+.1f}% action={action}")
    return "\n".join(lines)


async def _call_model_for_synthesis(
    system: str,
    user: str,
    feature: str,
) -> Optional[Dict]:
    """调用单个模型生成组合级分析"""
    from app.llm.model_call import call_llm

    try:
        _loop = asyncio.get_event_loop()
        res = await asyncio.wait_for(
            _loop.run_in_executor(
                None,
                lambda: call_llm(
                    feature=feature,
                    prompt=user,
                    system=system,
                    temperature=0.2,
                    max_tokens=4000,
                    json_mode=True,
                ),
            ),
            timeout=300,
        )
        if res and res.success:
            from app.plan.deep_analysis_engine import _safe_json_loads
            parsed = _safe_json_loads(res.response)
            if parsed:
                parsed["_model"] = getattr(res, "model_used", "")
                return parsed
    except asyncio.TimeoutError:
        logger.warning(f"[portfolio_synth] {feature} timeout")
    except Exception as e:
        logger.warning(f"[portfolio_synth] {feature} error: {e}")

    return None


async def run_portfolio_synthesis(
    plan: Dict,
) -> Optional[Dict]:
    """运行组合级综合分析

    用2个模型独立分析，合并结果:
      - 两个模型都推荐BUY/SELL → 高确信
      - 一个推荐一个观望 → 中确信
      - 都观望 → 低确信

    Args:
        plan: 完整的battle_plan dict

    Returns:
        {
            "market_assessment": "...",
            "recommended_direction": "...",
            "risk_level": "...",
            "action_list": [...],
            "portfolio_summary": "...",
            "feishu_brief": "...",
            "_meta": {
                "models": ["qwen-max", "deepseek-pro"],
                "agreement": "HIGH/MEDIUM/LOW",
                "generated_at": "...",
            }
        }
    """
    targets = plan.get("targets", [])
    if not targets:
        logger.warning("[portfolio_synth] 无targets，跳过")
        return None

    regime = plan.get("market_regime", "UNKNOWN")
    confidence = plan.get("confidence", 0)
    reasoning = plan.get("regime_reasoning", "")
    position_actions = plan.get("position_actions", [])
    economic_events = plan.get("_economic_events", [])

    targets_summary = _format_targets_for_synthesis(targets)
    positions_text = _format_positions(position_actions)
    events_text = json.dumps(economic_events, ensure_ascii=False, indent=2)[:800] if economic_events else "(无)"

    user_prompt = PORTFOLIO_SYNTH_USER.format(
        regime=regime,
        confidence=confidence,
        reasoning=reasoning[:200],
        positions=positions_text,
        targets_summary=targets_summary,
        economic_events=events_text,
    )

    # 3个模型并行调用 (2026-06-21: qwen3.7-max + glm-5.2 + deepseek-v4-pro)
    logger.info("[portfolio_synth] 启动3模型并行分析...")
    t0 = time.time()

    results = await asyncio.gather(
        _call_model_for_synthesis(PORTFOLIO_SYNTH_SYSTEM, user_prompt, "deep_target_analysis"),
        _call_model_for_synthesis(PORTFOLIO_SYNTH_SYSTEM, user_prompt, "dr_debate"),
        _call_model_for_synthesis(PORTFOLIO_SYNTH_SYSTEM, user_prompt, "stock_analyst"),
        return_exceptions=True,
    )

    r1_raw, r2_raw, r3_raw = results
    r1: Optional[Dict] = r1_raw if isinstance(r1_raw, dict) else None
    r2: Optional[Dict] = r2_raw if isinstance(r2_raw, dict) else None
    r3: Optional[Dict] = r3_raw if isinstance(r3_raw, dict) else None

    if not r1 and not r2 and not r3:
        logger.warning("[portfolio_synth] 三个模型都失败")
        return None

    # 合并结果: 取第一个成功的结果作为primary
    primary = r1 or r2 or r3
    secondary_models = [m for m in [r2, r3, r1] if m and m is not primary]

    if len(secondary_models) >= 1:
        # 比较多模型 action_list 一致性
        p_actions = {a.get("symbol", ""): a.get("action", "") for a in primary.get("action_list", [])}
        agree_count = 0
        total = len(p_actions)
        for sec in secondary_models:
            s_actions = {a.get("symbol", ""): a.get("action", "") for a in sec.get("action_list", [])}
            agree_count += sum(1 for sym, act in p_actions.items() if s_actions.get(sym) == act)

        total_comparisons = total * len(secondary_models)
        agreement = "HIGH" if total_comparisons > 0 and agree_count >= total_comparisons * 0.8 else "MEDIUM" if total_comparisons > 0 and agree_count >= total_comparisons * 0.5 else "LOW"

        if agreement == "LOW":
            primary["divergence_note"] = "⚠ 多模型分歧较大，建议人工验证"
    else:
        agreement = "SINGLE"

    model_names = []
    for r, default_name in [(r1, "qwen3.7-max"), (r2, "deepseek-v4-pro"), (r3, "glm-5.2")]:
        model_names.append(r.get("_model", default_name) if r else "failed")

    primary["_meta"] = {
        "models": model_names,
        "agreement": agreement,
        "total_time_sec": round(time.time() - t0, 1),
    }

    logger.info(
        f"[portfolio_synth] ✅ 完成: agreement={agreement}, "
        f"actions={len(primary.get('action_list', []))}, "
        f"models={model_names}, "
        f"({primary['_meta']['total_time_sec']}s)"
    )

    return primary


__all__ = ["run_portfolio_synthesis"]
