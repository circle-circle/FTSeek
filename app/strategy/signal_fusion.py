"""
信号融合器 (Signal Fusion Engine)

从 consensus_engine.py 提取的核心融合逻辑。
包含:
  - _evaluate: 窗口到期后的加权评分与信号生成
  - 结构/事件分层共识计算
  - 分歧度度量与灰区注入
  - 政策一票否决 (P0 修复)
  - ConsensusResult 构建

拆分自 consensus_engine.py 的 _evaluate 方法 (原行 1346-1850+)
"""
import logging
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("ftseek.strategy.signal_fusion")


def evaluate_consensus(
    code: str,
    votes: list,
    threshold: float,
    min_strategies: int,
    adx_cache: Dict[str, float],
    regime_detector,          # RegimeDetector instance
    weight_resolver,           # WeightResolver instance
    signal_scorer,             # ConfidenceCalculator instance
    quality_manager,           # SignalQualityManager instance
    # 可选增强
    scenario_cache: Dict = None,
    scenario_router=None,
    llm_enabled: bool = False,
    llm_analyst=None,
    get_capital_flow_fn=None,
    check_breaking_news_fn=None,
    # 外部注入的缓存
    _scenario_cache: Dict = None,
) -> Optional[Any]:
    """
    核心共识评估逻辑

    Returns:
        ConsensusResult or None
    """
    from app.strategy.consensus_engine import ConsensusResult, Vote
    from app.strategy.consensus_types import SignalSource
    from app.strategy.base import SignalAction

    scenario_cache = scenario_cache or {}
    _scenario_cache = _scenario_cache or {}

    # -------------------------------------------------------
    # 去重: 同策略只取最后一票
    # -------------------------------------------------------
    latest = {}
    for v in votes:
        latest[v.strategy] = v
    unique_votes = list(latest.values())

    # -------------------------------------------------------
    # 信号一致性检查
    # -------------------------------------------------------
    flip_count = 0
    strategy_directions: Dict[str, set] = defaultdict(set)
    for v in votes:
        strategy_directions[v.strategy].add(v.action.value)
    for strat, directions in strategy_directions.items():
        if len(directions) > 1:
            flip_count += 1
    total_strategies = len(strategy_directions)
    CHAOS_THRESHOLD = 0.6
    if total_strategies > 0 and flip_count / total_strategies > CHAOS_THRESHOLD:
        logger.info(f"[共识] {code} ⚠️ 信号混乱（{flip_count}/{total_strategies} 策略反转），放弃本窗口")
        return None

    # -------------------------------------------------------
    # 市场状态判定 (委托 regime_detector)
    # -------------------------------------------------------
    regime, adx_val = regime_detector.detect(code, adx_cache)

    # -------------------------------------------------------
    # 加权评分 (委托 weight_resolver)
    # -------------------------------------------------------
    score = 0.0
    weight_details = {}
    bullish_votes = []
    bearish_votes = []

    for v in unique_votes:
        w, w_details = weight_resolver.calc_weight(
            v.strategy, regime, code,
            scenario_router=scenario_router,
            scenario_cache=_scenario_cache,
            check_breaking_news_fn=check_breaking_news_fn,
        )
        conviction = _get_vote_conviction(v)
        adjusted_w = w * conviction
        weight_details[v.strategy] = round(adjusted_w, 2)

        if v.action == SignalAction.BUY:
            score += adjusted_w
            bullish_votes.append(v)
        elif v.action == SignalAction.SELL:
            score -= adjusted_w
            bearish_votes.append(v)

    # -------------------------------------------------------
    # 分层共识: 结构 vs 事件
    # -------------------------------------------------------
    structural_votes = []
    event_votes = []
    for v in unique_votes:
        if getattr(v.signal, 'source', SignalSource.TECHNICAL) == SignalSource.EVENT:
            event_votes.append(v)
        else:
            structural_votes.append(v)

    structural_score = 0.0
    structural_bullish = []
    structural_bearish = []
    for v in structural_votes:
        w, _ = weight_resolver.calc_weight(
            v.strategy, regime, code,
            scenario_router=scenario_router,
            scenario_cache=_scenario_cache,
        )
        conviction = _get_vote_conviction(v)
        cw = w * conviction
        if v.action == SignalAction.BUY:
            structural_score += cw
            structural_bullish.append(v)
        elif v.action == SignalAction.SELL:
            structural_score -= cw
            structural_bearish.append(v)

    event_score = 0.0
    if event_votes:
        event_score = (len([v for v in event_votes if v.action == SignalAction.BUY]) -
                       len([v for v in event_votes if v.action == SignalAction.SELL])) * 0.55

    # 融合逻辑
    fusion_score = structural_score
    if event_score != 0:
        tech_dir = 1 if structural_score >= 0 else -1
        evt_dir = 1 if event_score >= 0 else -1
        if structural_score != 0 and tech_dir != evt_dir:
            fusion_score *= 0.8  # 冲突惩罚
        else:
            fusion_score += event_score * 0.3  # 共振增强

    score = fusion_score

    adx_str = f"ADX={adx_val:.1f}" if adx_val else "ADX=N/A"
    logger.info(
        f"[共识] {code} 评估完毕 | 市场={regime}({adx_str}) | "
        f"融合分={score:+.2f} (技术分={structural_score:+.2f}, 事件分={event_score:+.2f})"
    )

    # -------------------------------------------------------
    # 分歧度度量
    # -------------------------------------------------------
    bullish_count = sum(1 for v in unique_votes if v.action == SignalAction.BUY)
    bearish_count = sum(1 for v in unique_votes if v.action == SignalAction.SELL)

    divergence = 0.0
    if bullish_count > 0 and bearish_count > 0:
        divergence = min(bullish_count, bearish_count) / max(bullish_count, bearish_count)
        from app.strategy.weight_resolver import DIVERGENCE_THRESHOLD, DIVERGENCE_SCORE_DECAY, DIVERGENCE_GRAY_ZONE
        if divergence >= DIVERGENCE_THRESHOLD:
            score *= (1 - divergence * DIVERGENCE_SCORE_DECAY)
            if DIVERGENCE_GRAY_ZONE:
                try:
                    from app.scenario.classifier import ScenarioType
                    _scenario_cache[code] = ScenarioType.GRAY_ZONE
                except ImportError:
                    pass

    # -------------------------------------------------------
    # 同向票数检查
    # -------------------------------------------------------
    MIN_AGREE_VOTES = 1
    if score >= threshold and bullish_count < MIN_AGREE_VOTES:
        logger.info(f"[同向票数不足] {code} score={score:.2f}>=阈值 但票数{bullish_count}<{MIN_AGREE_VOTES}")
        score = 0
    if score <= -threshold and bearish_count < MIN_AGREE_VOTES:
        logger.info(f"[同向票数不足] {code} score={score:.2f}<=-阈值 但票数{bearish_count}<{MIN_AGREE_VOTES}")
        score = 0

    # -------------------------------------------------------
    # 构建结果
    # -------------------------------------------------------
    result = None

    if score >= threshold:
        if len(set(v.strategy for v in bullish_votes)) < min_strategies:
            pass  # 不触发
        else:
            result = _build_buy_result(
                code, score, threshold, bullish_votes, bearish_votes,
                structural_votes, event_votes, adx_val, regime,
                weight_details, divergence, signal_scorer,
                quality_manager, scenario_cache, llm_enabled, llm_analyst,
                get_capital_flow_fn, unique_votes,
            )

    elif score <= -threshold:
        if len(set(v.strategy for v in bearish_votes)) < min_strategies:
            pass
        else:
            result = _build_sell_result(
                code, score, threshold, bullish_votes, bearish_votes,
                adx_val, regime, weight_details, divergence, signal_scorer,
                quality_manager, scenario_cache, llm_enabled, llm_analyst,
                get_capital_flow_fn, unique_votes,
            )

    return result


def _build_buy_result(
    code, score, threshold, bullish_votes, bearish_votes,
    structural_votes, event_votes, adx_val, regime,
    weight_details, divergence, signal_scorer,
    quality_manager, scenario_cache, llm_enabled, llm_analyst,
    get_capital_flow_fn, unique_votes,
):
    """构建 BUY ConsensusResult"""
    from app.strategy.consensus_engine import ConsensusResult
    from app.strategy.consensus_types import SignalSource
    from app.strategy.base import SignalAction

    avg_price = sum(v.price for v in bullish_votes) / len(bullish_votes)

    capital_sentiment = get_capital_flow_fn(code) if get_capital_flow_fn else 0.0

    # 置信度计算 (委托 signal_scorer)
    scenario_obj = scenario_cache.get(code)
    scenario_confidence = 0.5
    if scenario_obj:
        if hasattr(scenario_obj, 'confidence'):
            scenario_confidence = scenario_obj.confidence
        elif hasattr(scenario_obj, 'confirm_count'):
            scenario_confidence = scenario_obj.confirm_count / 3.0
        scenario_confidence = max(0.3, min(1.0, scenario_confidence))

    # 相关折扣 (confluence_v2 已禁用，使用因子策略的平均折扣)
    corr_discount = 1.0
    for v in bullish_votes:
        if v.strategy in ("rsi_factor", "macd_factor", "bb_factor"):
            extra = getattr(v.signal, 'extra', {}) or {}
            cd = extra.get("avg_correlation_discount", 1.0)
            if cd < corr_discount:
                corr_discount = cd
            break

    # LLM
    llm_part = 0.0
    llm_conf = 0.0
    llm_align = 0.0
    if llm_enabled and llm_analyst:
        try:
            assessment = llm_analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
            if assessment and assessment.is_valid():
                llm_conf = assessment.confidence_value
                llm_align = assessment.direction_score
                llm_part = llm_conf * max(0.5, min(1.3, 1.0 + llm_align * 0.3))
        except Exception:
            pass

    confidence, breakdown = signal_scorer.calculate_buy_confidence(
        score=score,
        threshold=threshold,
        bullish_votes=bullish_votes,
        bearish_votes=bearish_votes,
        structural_votes=structural_votes,
        event_votes=event_votes,
        capital_sentiment=capital_sentiment,
        scenario_confidence=scenario_confidence,
        correlation_discount=corr_discount,
        llm_enabled=llm_enabled,
        llm_weight=0.15,
        llm_confidence=llm_conf,
        llm_direction_alignment=llm_align,
    )

    # ── 政策一票否决 (P0 修复) ──
    from app.market_profile import get_market_profile
    _hk_profile = get_market_profile("HK")
    if _hk_profile.signal_fusion.veto_policy_negative:
        _policy_risk_score = 0.0
        _policy_flags = []
        for _v in unique_votes:
            _strat = _v.strategy
            if _strat == "sentiment_ai" and _v.action == SignalAction.SELL:
                _s = getattr(_v.signal, 'extra', {}) or {}
                _sent = _s.get('sentiment_score', 0)
                if _sent < 0:
                    _policy_risk_score = max(_policy_risk_score, abs(_sent))
            if _strat == "event_driven" and _v.action == SignalAction.SELL:
                _ev = getattr(_v.signal, 'extra', {}) or {}
                _ev_type = _ev.get('event_type', '')
                if _ev_type in ('regulation', 'geopolitical', 'MACRO'):
                    _ev_impact = _ev.get('impact_level', 'low')
                    _impact_map = {'critical': 0.95, 'high': 0.80, 'medium': 0.60, 'low': 0.30}
                    _policy_risk_score = max(_policy_risk_score, _impact_map.get(_ev_impact, 0.3))
                    _policy_flags.append(_ev_type)
        for _v in bearish_votes:
            _reason = getattr(_v, 'reason', '') or ''
            if any(kw in _reason for kw in ['政策', '监管', '制裁', '地缘']):
                _policy_risk_score = max(_policy_risk_score, 0.70)
                _policy_flags.append('keyword_match')
        if _hk_profile.should_veto(_policy_risk_score, threshold=0.80):
            logger.warning(
                f"[共识/fusion] {code} BUY 信号被政策风险否决 | "
                f"risk_score={_policy_risk_score:.2f} | flags={_policy_flags}"
            )
            return None

    trace_id = f"TRC-{uuid.uuid4().hex[:12].upper()}"

    scenario_label = ""
    if scenario_obj:
        scenario_label = getattr(scenario_obj, 'value', str(scenario_obj))

    tech_buy_count = len([v for v in structural_votes if v.action == SignalAction.BUY])
    event_buy_count = len([v for v in event_votes if v.action == SignalAction.BUY])
    if tech_buy_count > 0 and event_buy_count > 0:
        signal_source = "HYBRID"
    elif tech_buy_count > 0:
        signal_source = "TECHNICAL"
    else:
        signal_source = "EVENT"

    extra = {}
    if trace_id in getattr(quality_manager, '_llm_audit_log', {}):
        extra = {"llm_assessment": quality_manager._llm_audit_log[trace_id]}

    return ConsensusResult(
        confidence=round(confidence, 2),
        code=code,
        action=SignalAction.BUY,
        price=avg_price,
        score=round(score, 2),
        threshold=threshold,
        market_regime=regime,
        scenario=scenario_label,
        adx_value=round(adx_val, 1) if adx_val else None,
        agree_strategies=[v.strategy for v in bullish_votes],
        oppose_strategies=[v.strategy for v in bearish_votes],
        weight_details=weight_details,
        reasons=[v.reason for v in bullish_votes],
        votes=bullish_votes,
        trace_id=trace_id,
        divergence=round(divergence, 2),
        source=signal_source,
        extra=extra,
    )


def _build_sell_result(
    code, score, threshold, bullish_votes, bearish_votes,
    adx_val, regime, weight_details, divergence, signal_scorer,
    quality_manager, scenario_cache, llm_enabled, llm_analyst,
    get_capital_flow_fn, unique_votes,
):
    """构建 SELL ConsensusResult (对称版本)"""
    from app.strategy.consensus_engine import ConsensusResult
    from app.strategy.base import SignalAction

    avg_price = sum(v.price for v in bearish_votes) / len(bearish_votes)
    capital_sentiment = get_capital_flow_fn(code) if get_capital_flow_fn else 0.0

    scenario_obj = scenario_cache.get(code)
    scenario_confidence = 0.5
    if scenario_obj:
        if hasattr(scenario_obj, 'confidence'):
            scenario_confidence = scenario_obj.confidence
        elif hasattr(scenario_obj, 'confirm_count'):
            scenario_confidence = scenario_obj.confirm_count / 3.0
        scenario_confidence = max(0.3, min(1.0, scenario_confidence))

    corr_discount = 1.0
    for v in bearish_votes:
        if v.strategy in ("rsi_factor", "macd_factor", "bb_factor"):
            extra = getattr(v.signal, 'extra', {}) or {}
            cd = extra.get("avg_correlation_discount", 1.0)
            if cd < corr_discount:
                corr_discount = cd
            break

    llm_part = 0.0
    llm_conf = 0.0
    llm_align = 0.0
    if llm_enabled and llm_analyst:
        try:
            assessment = llm_analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
            if assessment and assessment.is_valid():
                llm_conf = assessment.confidence_value
                llm_align = -assessment.direction_score
                llm_part = llm_conf * max(0.5, min(1.3, 1.0 + llm_align * 0.3))
        except Exception:
            pass

    confidence, breakdown = signal_scorer.calculate_sell_confidence(
        score=score,
        threshold=threshold,
        bullish_votes=bullish_votes,
        bearish_votes=bearish_votes,
        capital_sentiment=capital_sentiment,
        scenario_confidence=scenario_confidence,
        correlation_discount=corr_discount,
        llm_enabled=llm_enabled,
        llm_weight=0.15,
        llm_confidence=llm_conf,
        llm_direction_alignment=llm_align,
    )

    trace_id = f"TRC-{uuid.uuid4().hex[:12].upper()}"
    scenario_label = ""
    if scenario_obj:
        scenario_label = getattr(scenario_obj, 'value', str(scenario_obj))

    return ConsensusResult(
        confidence=round(confidence, 2),
        code=code,
        action=SignalAction.SELL,
        price=avg_price,
        score=round(score, 2),
        threshold=threshold,
        market_regime=regime,
        scenario=scenario_label,
        adx_value=round(adx_val, 1) if adx_val else None,
        agree_strategies=[v.strategy for v in bearish_votes],
        oppose_strategies=[v.strategy for v in bullish_votes],
        weight_details=weight_details,
        reasons=[v.reason for v in bearish_votes],
        votes=bearish_votes,
        trace_id=trace_id,
        divergence=round(divergence, 2),
        source="TECHNICAL",
        extra={},
    )


def _get_vote_conviction(vote) -> float:
    """从 Vote 对象获取 conviction 分数"""
    signal = getattr(vote, 'signal', None)
    if signal is None:
        return getattr(vote, 'conviction', 1.0)
    extra = getattr(signal, 'extra', {}) or {}
    return extra.get('conviction', 1.0)
