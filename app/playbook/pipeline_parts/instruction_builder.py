"""候选构建/组合闸域 (P1a/3a 迁自 app/playbook/pipeline.py, DEB-20261008 §2 职责地图)。

首批只承接纯映射/纯裁剪件; _build_instructions_from_votes 巨型编排函数
留待阶段 3 按阶段函数拆解, 本批不动。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _direction_conditioned_ic_net(direction: str, bearish_hits, bullish_hits,
                                  factor_ic: dict):
    """方向条件化 IC 净分 (DEB §五十四, 2026-08-20)。

    旧法用 pooled IC (样本 ~91% LONG) 对 SHORT 候选投反对票 = 样本污染:
    AVGO 8/19 被 pooled net_ic=+0.09 拦空, 当日 -5.2% 方向正确 (空单未丢,
    但再评估被污染分否决; §五十三修正定性)。
    新法: 候选方向取同侧 IC (fwd_ret_t1 已方向调整, ic_short>0 = 该因子在
    SHORT 信号上做空赚钱)。n_<side>≥30 才计分, 不足者不参与 (不用 pooled
    顶替 — 宁缺毋滥)。bearish/bullish 分类不再参与计分: 方向信息已在
    ic_<side> 内, 双重方向化会翻转符号 (§五十四语义推演实锤)。

    Returns: (net_score, scored_factors) — net<-0.05 = 因子整体反对此方向。
    """
    side = "short" if direction == "SELL_SHORT" else "long"
    ic_key = "ic_" + side
    n_key = "n_" + side
    net_score = 0.0
    scored_factors = []
    for c in list(bearish_hits) + list(bullish_hits):
        fdata = factor_ic.get(c) or {}
        ic_val = fdata.get(ic_key)
        n_val = int(fdata.get(n_key) or 0)
        if ic_val is None or n_val < 30:
            continue
        net_score += float(ic_val)
        scored_factors.append(c)
    return net_score, scored_factors


def _normalize_regime_str(regime_context) -> str:
    """统一 regime 字符串为 RISK_ON/CAUTIOUS/RISK_OFF/NO_TRADE (大写枚举名)."""
    reg = getattr(regime_context, "regime", None)
    if reg is None:
        return "CAUTIOUS"
    if hasattr(reg, "name"):
        return reg.name
    return str(reg)


def _build_vote_market_data(symbol: str, data, regime_context, data_router):
    """构造决策适配器的候选上下文; 行情不可用返回 None。"""
    if data_router is None:
        return None
    try:
        raw_ctx = data_router.get_full_market_context(symbol)
    except Exception as e:
        logger.warning(f"[playbook] {symbol} get_full_market_context 异常: {e}")
        return None
    if not raw_ctx or float(raw_ctx.get("price", 0) or 0) <= 0:
        return None
    from app.core.pipeline_contracts import SymbolContext
    ctx = SymbolContext.from_raw(symbol, raw_ctx)
    ctx.vix = float(getattr(regime_context, "vix", 20.0) or 20.0)
    ctx.market_regime = _normalize_regime_str(regime_context)
    return ctx


def _load_factor_ic_global(path: str = "data/factor_ic_latest.json") -> dict:
    """构建前置态: factor IC 文件循环外一次读取; 失败保持空映射。"""
    try:
        import json as _json
        from pathlib import Path as _Path
        _ic_path = _Path(path)
        if _ic_path.exists():
            with open(_ic_path) as _f:
                return _json.load(_f)
    except Exception:
        return {}
    return {}


def _regime_multiplier(regime_str: str) -> float:
    """构建前置态: regime 仓位乘数, 未知 regime 沿用 CAUTIOUS 默认 0.5。"""
    regime_mults = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}
    return regime_mults.get(regime_str, 0.5)


def _new_filter_audit(total_input: int) -> Dict[str, int]:
    """构建前置态: 过滤漏斗计数器初始化 (键集合与既有日志口径一致)。"""
    return {
        "total_input": total_input,
        "market_data_none": 0,
        "vote_failed": 0,
        "vote_neutral": 0,
        "buy_macd_ma50_disabled": 0,
        "buy_low_strength_skip": 0,
        "buy_high_std_skip": 0,
        "low_confidence_skip": 0,
        "direction_flip": 0,
        "direction_conflict_skip": 0,
        "neg_edge_factor_skip": 0,
        "neg_pair_skip": 0,
        "no_entry_price": 0,
        "final_instructions": 0,
    }


def _load_direction_config() -> tuple:
    """构建前置态: 方向仲裁模式与冲突阈值, 配置失败走代码默认。"""
    direction_mode = "llm_vote"
    llm_conflict_threshold = 0.70
    try:
        from app.core.config_container import get_config
        config = get_config()
        direction_mode = str(config.get_dotted("playbook.direction_mode", "llm_vote")).lower()
        llm_conflict_threshold = float(config.get_dotted(
            "playbook.technical_arbitration.llm_conflict_confidence_threshold", 0.70))
    except Exception:
        pass
    return direction_mode, llm_conflict_threshold


def _record_vote_snapshot(symbol: str, vote_result: Dict[str, Any], factors) -> None:
    """投票结果先落 IC 快照; 失败保持非阻断但必须可观测。"""
    decision_id = vote_result.get("decision_id", "") or (
        f"SNAP-{symbol}-{int(time.time()*1000)}"
    )
    try:
        from app.analysis.signal_ic_tracker import record_signal_snapshot
        record_signal_snapshot(
            decision_id=decision_id,
            symbol=symbol,
            direction=str(vote_result.get("direction", "NEUTRAL")).upper(),
            strength=int(vote_result.get("final_strength", 50) or 50),
            confidence=float(vote_result.get("consensus_level", 0) or 0),
            consensus_level=float(vote_result.get("consensus_level", 0) or 0),
            strategy_id=vote_result.get("strategy_id", ""),
            factors=list(factors.keys()) if factors else [],
            market_context=factors if factors else None,
        )
    except Exception as error:
        logger.warning(
            f"[playbook] {symbol} 信号快照记录失败(非阻断, IC管线将缺此样本): "
            f"decision_id={decision_id}, err={error}"
        )


def _evaluate_technical_direction(market_ctx, symbol: str = "") -> Optional[str]:
    """best-effort 计算 technical 方向; 失败降级为 None。

    2026-10-09: 恢复 symbol 参数 (拆分时随日志丢了 {symbol} — 异常定位需要标的)。
    """
    try:
        from app.strategy.pure import evaluate as eval_strategy
        candidate = eval_strategy("technical", market_ctx)
        if candidate is not None:
            return str(candidate.direction).upper()
    except Exception as error:
        logger.debug(f"[playbook] {symbol} 技术信号评估异常: {error}")
    return None


def _apply_technical_arbitration(
    symbol: str,
    vote_direction: str,
    technical_direction: Optional[str],
    vote_result: Dict[str, Any],
    llm_conflict_threshold: float,
) -> tuple:
    """返回 (resolved_direction, skip_reason, audit_key)。"""
    if not technical_direction:
        return None, "无技术方向信号", "technical_no_signal"

    if vote_direction != technical_direction:
        llm_confidence = float(vote_result.get("final_confidence", 0) or 0)
        if llm_confidence >= llm_conflict_threshold:
            logger.info(
                f"[playbook] {symbol} 方向冲突 SKIP: "
                f"LLM={vote_direction}(conf={llm_confidence:.2f}) vs technical={technical_direction}"
            )
            return (
                None,
                f"方向冲突: LLM={vote_direction} vs technical={technical_direction}",
                "direction_conflict_skip",
            )
        logger.info(
            f"[playbook] {symbol} 技术方向覆盖 LLM: {technical_direction}"
        )
        return technical_direction, "", None

    return vote_direction, "", None


def _apply_buy_std_gate(symbol: str, vote_result: Dict[str, Any], direction: str) -> bool:
    """BUY 模型分歧门; 返回 True 表示应 SKIP。"""
    if direction != "BUY":
        return False

    strategy_id = vote_result.get("strategy_id", "") or ""
    try:
        from app.core.strategy_registry import get_strategy_config
        strategy_config = get_strategy_config(strategy_id)
        buy_max_std = float(strategy_config["direction_filter"].get("BUY_max_std", 15))
    except Exception:
        buy_max_std = 20

    vote_std = float(vote_result.get("std_dev", 0) or 0)
    if vote_std > buy_max_std:
        logger.info(
            f"[playbook] {symbol} BUY 模型分歧 std={vote_std:.1f} > {buy_max_std} → SKIP"
        )
        return True
    return False


def _factor_direction_conflict(direction: str, factors, factor_ic: dict) -> tuple:
    """方向条件化 IC 冲突判定; 返回 (hit, side, net_score, scored_factors)。"""
    from app.playbook.condition_taxonomy import BEARISH_CONDITIONS, BULLISH_CONDITIONS
    conditions = list(factors.keys()) if factors else []
    bearish_hits = [condition for condition in conditions if condition in BEARISH_CONDITIONS]
    bullish_hits = [condition for condition in conditions if condition in BULLISH_CONDITIONS]
    if not bearish_hits and not bullish_hits:
        return False, "", 0.0, []
    if direction not in ("BUY", "SELL_SHORT"):
        return False, "", 0.0, []

    net_score, scored_factors = _direction_conditioned_ic_net(
        direction, bearish_hits, bullish_hits, factor_ic
    )
    if net_score < -0.05:
        side = "short" if direction == "SELL_SHORT" else "long"
        return True, side, net_score, scored_factors
    return False, "", net_score, scored_factors


def _select_horizon(sr, regime_context, selector_provider=None) -> tuple:
    """Horizon 选择; 异常默认 short 并保留错误归因。"""
    try:
        if selector_provider is None:
            from app.playbook.pipeline_parts.runtime_context import _get_horizon_selector
            selector_provider = _get_horizon_selector
        decision = selector_provider().select(sr, regime_context)
        return decision.horizon, decision.confidence, decision.reasoning
    except Exception as error:
        symbol = getattr(sr, "symbol", "?")
        logger.warning(f"[playbook] {symbol} Horizon 选择失败: {error}, 默认 short")
        return "short", 0.0, {"source": "error_fallback", "error": str(error)}


def _build_instruction_decision(
    symbol: str, direction: str, factors, vote_result: Dict[str, Any]
) -> Dict[str, Any]:
    """构造数值计算层的 decision dict。"""
    return {
        "symbol": symbol,
        "direction": direction,
        "selected_conditions": list(factors.keys()) if factors else [],
        "thesis_template": _select_thesis_template(
            factors,
            direction,
            entry_archetype=vote_result.get("entry_archetype"),
        ),
    }


def _compute_instruction_values(
    decision: Dict[str, Any],
    data,
    total_equity: float,
    regime_multiplier: float,
    horizon: str,
    regime_context,
) -> Dict[str, Any]:
    """确定性计算 entry/SL/TP/size; 配置读取失败沿用原默认路径。"""
    from app.playbook.config import get_playbook_config
    from app.playbook.instruction_generator import compute_instruction_values
    risk_pct = float(get_playbook_config().get("risk_pct", 0.5))
    return compute_instruction_values(
        decision=decision,
        data=data,
        total_equity=total_equity,
        risk_pct=risk_pct,
        regime_multiplier=regime_multiplier,
        horizon=horizon,
        regime_context=regime_context,
    )


def _decision_confidence(
    vote_result: Dict[str, Any], data, decision: Dict[str, Any], factors
) -> int:
    """bridge 直读生成端 confidence; 非 bridge 用数据质量置信度。"""
    is_bridge = (
        vote_result.get("strategy_id", "") == "deep_analysis"
        or str(vote_result.get("decision_id", "")).startswith("DA")
    )
    if is_bridge:
        return int(float(vote_result.get("final_confidence", 0) or 0) * 100)
    from app.playbook.instruction_generator import compute_confidence
    return int(compute_confidence(data, decision["selected_conditions"], factors))


def _build_instruction(
    symbol: str,
    direction: str,
    data,
    factors,
    vote_result: Dict[str, Any],
    decision: Dict[str, Any],
    values: Dict[str, Any],
    decision_id: str,
    confidence: int,
    horizon: str,
    horizon_confidence: float,
    horizon_reasoning,
):
    """把决策结果与确定性数值组装为 Instruction。"""
    from app.playbook.instruction_generator import Instruction, build_thesis

    data_quality = {
        "fundamental": data.fundamental.quality,
        "technical": data.technical.quality,
        "insider": data.insider.quality,
        "news": data.news.quality,
        "earnings": data.earnings.quality,
        "sentiment": data.sentiment.quality,
    }
    thesis = build_thesis(decision["thesis_template"], decision["selected_conditions"])
    risks = []
    ok_count = sum(1 for quality in data_quality.values() if quality == "ok")
    if ok_count < 4:
        risks.append(f"数据不完整 ({ok_count}/6 维度 ok)")
    if data.earnings.days_until is not None and data.earnings.days_until < 14:
        risks.append(f"财报 {data.earnings.days_until} 天后")
    if str(values.get("atr_source", "")).startswith("FALLBACK"):
        risks.append("ATR 缺失, 使用 fallback 止损")

    return Instruction(
        symbol=symbol,
        direction=direction,
        entry_price=values.get("entry_price", 0),
        stop_loss=values.get("stop_loss", 0),
        take_profit=values.get("take_profit", 0),
        position_size=values.get("position_size", 0),
        risk_amount=values.get("risk_amount", 0),
        risk_reward=values.get("risk_reward", 0),
        confidence=confidence,
        thesis=thesis,
        conditions=(list(vote_result.get("conditions") or [])
                    or decision["selected_conditions"]),
        data_quality=data_quality,
        risks=risks,
        sector=data.fundamental.industry or "",
        entry_price_source=values.get("entry_price_source", ""),
        atr_source=values.get("atr_source", ""),
        decision_id=decision_id,
        horizon=horizon,
        horizon_confidence=horizon_confidence,
        horizon_reasoning=horizon_reasoning,
        invalidation=vote_result.get("invalidation", "") or "",
        da_thesis=vote_result.get("thesis", "") or "",
        anchor_dev_pct=vote_result.get("anchor_dev_pct"),
        entry_archetype=vote_result.get("entry_archetype"),
        confirmation=vote_result.get("confirmation"),
        chase_cap_price=vote_result.get("chase_cap_price"),
    )


def _log_filter_audit(instructions: List[Any], filter_audit: Dict[str, int]) -> None:
    """输出过滤漏斗审计; 键集合与历史口径一致。"""
    filter_audit["final_instructions"] = sum(
        1 for instruction in instructions if instruction.direction != "SKIP"
    )
    skip_count = filter_audit["total_input"] - filter_audit["final_instructions"]
    logger.info(
        f"[playbook] 过滤审计: input={filter_audit['total_input']} → "
        f"instructions={filter_audit['final_instructions']} skips={skip_count} | "
        f"breakdown: mkt_data_none={filter_audit['market_data_none']} "
        f"vote_fail={filter_audit['vote_failed']} "
        f"vote_none={filter_audit.get('vote_none', 0)} "
        f"tech_no_sig={filter_audit.get('technical_no_signal', 0)} "
        f"neutral={filter_audit['vote_neutral']} "
        f"buy_str_skip={filter_audit['buy_low_strength_skip']} "
        f"buy_std_skip={filter_audit['buy_high_std_skip']} "
        f"low_conf_skip={filter_audit['low_confidence_skip']} "
        f"buy_filter_disabled={filter_audit['buy_macd_ma50_disabled']} "
        f"dir_flip={filter_audit['direction_flip']} "
        f"dir_conflict={filter_audit['direction_conflict_skip']} "
        f"no_entry={filter_audit['no_entry_price']}"
    )


def _select_thesis_template(factors: Dict[str, Any], direction: str,
                            entry_archetype: Optional[str] = None) -> str:
    """根据触发因子选择 thesis 模板.

    [DEB-20260924-indexcaliber R4] 方向感知: SELL_SHORT 不得再落多头文案模板。
    原缺陷 (9/24 AXTI 实例): SHORT + macd_golden_cross → "mean_reversion"
    ⇒ thesis="均值回归: …, 超跌反弹机会" (多头叙事) 挂在空单上, 与 DA 真实空方
    依据 (FADE_SHORT: 利好兑现+PE2432x/盘前-4.36%) 相矛盾。BUY 侧映射保持不变。

    [DEB-20260924 F14] 结构化 archetype 优先: thesis 叙事必须与**执行语义**一致 —
    entry_archetype 驱动 entry_routing (限价/市价路由), 是机器可执行真值;
    而因子模板映射只是叙事猜测。矛盾实例 (9月实测 14 条): thesis="突破入场: above_ma20,
    macd_golden_cross…" 而 raw.entry_archetype=PULLBACK_LIMIT + confirmation.side=below
    ⇒ routing 开启时突破叙事单会被按回踩限价路由 (等不到回踩 → 流单), 且污染一切
    按 thesis 文本做的分型分析 (F13-① BREAKOUT 臂灭绝的直接成因)。
    修法: archetype 有值时按 archetype 选模板 (同源), 因子映射仅作 archetype 缺失时兜底。
    行为层零变更: thesis 是纯记录字段, compute_instruction_values 不消费它 (已 grep 验证)。
    """
    _dir = str(direction or "").upper()
    if _dir == "SKIP":
        return "skip"
    _short = _dir != "BUY"  # SELL_SHORT / SHORT / SELL 一律走空头模板

    # F14: 结构化 archetype 优先 (与方向一致性守卫: 多头 archetype 不落空头单, 反之亦然)
    _arch = str(entry_archetype or "").upper()
    if _arch == "FADE_SHORT":
        if _short:
            return "fade_short"
        logger.warning("[playbook][F14] 矛盾: BUY 单带 FADE_SHORT archetype — 按因子兜底")
    elif _arch == "PULLBACK_LIMIT":
        return "fade_short" if _short else "trend_pullback"
    elif _arch == "BREAKOUT":
        return "trend_short" if _short else "breakout"

    if not factors:
        return "trend_short" if _short else "momentum_continuation"

    factor_keys = set(factors.keys())

    if "insider_cluster_buy" in factor_keys or "insider_net_buy" in factor_keys:
        # 内部人净买入却做空 = 逆技术/内部人信号的衰退做空 (原缺陷: SHORT 也返回多头文案)
        return "fade_short" if _short else "insider_confidence"
    if "macd_golden_cross" in factor_keys:
        return "fade_short" if _short else "breakout"
    if any("rsi" in k for k in factor_keys):
        return "mean_reversion_short" if _short else "trend_pullback"

    return "trend_short" if _short else "momentum_continuation"


def _trim_majority_direction(valid: List[Any], majority_dir: str, target_skew: float) -> int:
    """多数方信号降级为 SKIP 直到偏斜 ≤ 阈值 (控制方向风险).

    2026-09-01 RFC-071 根因修复 (砍半 → SKIP):
      旧实现砍 position_size *= 0.5, 但 6% 单名帽下满仓≈$5,900 砍半≈$2,950 结构性
      穿破 admission min_notional 地板 → 砍半产物在执行前必死于地板, 且死因被覆写为
      NOTIONAL_TOO_SMALL (8/31 实证: 4 只高置信单 100% BUY 偏斜被砍半, 全灭, 真实
      死因被掩盖; 与日志文案"被降级为SKIP"亦不符)。
      直接 SKIP: 死因如实留痕, position_size 保留原值供审计。循环"SKIP 最低
      confidence 的多数方直到达标"与 docstring 原意("砍掉低 conv 的多数方信号直到
      平衡")及 sector 集中度孪生实现 (_trim_majority_sector) 对齐。
    """
    trimmed = 0
    while True:
        active_valid = [i for i in valid if i.direction in ("BUY", "SELL_SHORT")]
        total = len(active_valid)
        if total <= 1:
            break
        majority = [i for i in active_valid if i.direction == majority_dir]
        if not majority or len(majority) / total <= target_skew:
            break
        majority.sort(key=lambda x: getattr(x, "confidence", 0) or 0)
        victim = majority[0]
        victim.direction = "SKIP"
        victim.skip_reason = (
            f"方向偏斜超限 guardrail 降级 (多数方 {majority_dir}, "
            f"原size={getattr(victim, 'position_size', 0) or 0})"
        )
        trimmed += 1
    return trimmed


def _trim_majority_sector(valid: List[Any], sector: str, target_pct: float) -> int:
    """将指定 sector confidence 最低的信号降级, 直到占比 ≤ target_pct."""
    trimmed = 0
    from collections import Counter
    while True:
        active_valid = [i for i in valid if i.direction in ("BUY", "SELL_SHORT")]
        total = len(active_valid)
        sector_count = Counter(
            getattr(i, "sector", "") or "Unknown" for i in active_valid
        ).get(sector, 0)
        if total == 0 or sector_count / total <= target_pct:
            break
        candidates = [
            i for i in active_valid
            if (getattr(i, "sector", "") or "Unknown") == sector
        ]
        candidates.sort(key=lambda x: getattr(x, "confidence", 0) or 0)
        candidates[0].direction = "SKIP"
        candidates[0].skip_reason = f"sector 集中度 guardrail 降级 ({sector})"
        trimmed += 1
    return trimmed


def _confirmation_gap_pct(entry_price, confirmation) -> Optional[float]:
    """confirmation.price_level vs entry_price 的有符号偏离 (%).

    2026-10-20 10-19 复查: DA 的 FADE_SHORT/PULLBACK_LIMIT 确认位与执行锚
    可差 2–4%; 先只记 warning + raw 字段, 不改执行 (重锚是第二步)。
    """
    try:
        level = float((confirmation or {}).get("price_level"))
        entry = float(entry_price or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if entry <= 0 or level <= 0:
        return None
    return round((level - entry) / entry * 100.0, 4)


def _instruction_to_db_dict(inst, regime_ctx, today: str) -> Dict[str, Any]:
    """Instruction dataclass → save_instruction 期望的 dict (字段映射, spec 6.1)"""
    conditions = list(getattr(inst, "conditions", []) or [])
    risks = list(getattr(inst, "risks", []) or [])
    dq = getattr(inst, "data_quality", {}) or {}
    ok_dims = sum(1 for v in dq.values() if v == "ok")
    decision_id = str(getattr(inst, "decision_id", "") or "")
    is_da_instruction = decision_id.startswith("DA-")
    _conf = getattr(inst, "confirmation", None)
    _gap_pct = _confirmation_gap_pct(getattr(inst, "entry_price", 0), _conf)
    _arch = str(getattr(inst, "entry_archetype", "") or "").upper()
    if (is_da_instruction and _gap_pct is not None and abs(_gap_pct) > 1.0
            and _arch in ("PULLBACK_LIMIT", "FADE_SHORT")):
        _level = _conf.get("price_level") if isinstance(_conf, dict) else None
        logger.warning(
            f"[PlanGap] {inst.symbol} {inst.direction} {_arch} "
            f"confirmation_level={_level} entry_price={getattr(inst, 'entry_price', 0)} "
            f"gap={_gap_pct:+.2f}% (>1%, warning-only, 未改执行)")
    return {
        "date": today,
        "symbol": inst.symbol,
        "direction": inst.direction,
        "skip_reason": getattr(inst, "skip_reason", "") or "",
        "entry_price": inst.entry_price,
        "stop_loss": inst.stop_loss,
        "take_profit": inst.take_profit,
        "position_size": inst.position_size,
        "risk_amount": getattr(inst, "risk_amount", 0.0),
        "risk_reward": getattr(inst, "risk_reward", 0.0),
        "confidence": inst.confidence,
        # [DEB-fullchain #27 2026-08-16] decision_conf (0-1) 落库 — 列 2026-08-14 已加
        #   (DEB-20260814 P6) 但 pipeline 路径零写入, 全库 18 个非空值全来自 DA skill
        #   (口径=confidence/100)。本行补齐 pipeline 写入, 与 DA 同口径。
        "decision_conf": round(float(inst.confidence) / 100.0, 4) if inst.confidence else None,
        "conditions": conditions,
        "thesis": getattr(inst, "thesis", "") or "",
        "sector": getattr(inst, "sector", "") or "",
        "risks": risks,
        "market_regime": regime_ctx.regime.value,
        "vix_at_generation": regime_ctx.vix,
        "spy_trend_at_generation": "above_ma20" if regime_ctx.spy_ma20 >= regime_ctx.spy_ma50 else "below_ma20",
        "data_quality": dq,
        "ok_dimensions": ok_dims,
        "matched_patterns": [],
        "pattern_verdict": None,
        "raw_instruction": {
            "source": "deep_analysis" if is_da_instruction else "",
            "entry_price_source": getattr(inst, "entry_price_source", ""),
            "atr_source": getattr(inst, "atr_source", ""),
            # 2026-07-11 Stage 1: DA 真实论点存入 raw json (不占独立列, 不覆盖模板 thesis)
            "da_thesis": getattr(inst, "da_thesis", "") or "",
            # [DEB-20260901] 结构化入场语义落库 (消费方: Phase B 影子; 缺失=旧指令, 按现状行为)
            "entry_archetype": getattr(inst, "entry_archetype", None),
            "confirmation": getattr(inst, "confirmation", None),
            "confirmation_gap_pct": _gap_pct,
            "chase_cap_price": getattr(inst, "chase_cap_price", None),
        },
        "decision_id": getattr(inst, "decision_id", "") or "",
        "invalidation": getattr(inst, "invalidation", "") or "",
        # F1 (2026-07-31): 锚偏离度落库键 (save_instruction :202 读取, record-only)
        "anchor_dev_pct": getattr(inst, "anchor_dev_pct", None),
        # 2026-08-05 (DEB-20260805 项C 终裁): model_used 归因补全 — 本映射走 TechnicalFallbackAdapter
        #   确定性链 (无 LLM), 原恒 NULL (NVDA 08-04 topup_pool 缺口实例). UE 路径由
        #   llm_universe_scan 记录级写真实模型名, DA skill 写 config.LLM_MODEL, 互不覆盖.
        "model_used": None if is_da_instruction else (getattr(inst, "model_used", None) or "deterministic"),
        # [DEB-20261027 B] 行类型落库: 'instruction' | 'supply_placeholder'。
        #   供给占位行 (被筛过但 DA bridge 未提名) 显式标记, 读数面按列过滤。
        #   缺省 'instruction' ⇒ DA skill 直写的行 (未传该字段) 语义不变。
        "row_kind": getattr(inst, "row_kind", "instruction") or "instruction",
        "horizon": getattr(inst, "horizon", "short"),
        "horizon_confidence": getattr(inst, "horizon_confidence", 0.0),
        "horizon_reasoning": getattr(inst, "horizon_reasoning", {}),
    }
