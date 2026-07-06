# app/agents/voting/voting_aggregator.py
from typing import Dict, Any, List
import statistics
import logging
from .voting_config import VotingConfig, ModelRole
from .model_agent import ModelScore

logger = logging.getLogger(__name__)

class VotingAggregator:
    """投票聚合器"""
    
    def __init__(self, config: VotingConfig):
        self.config = config
    
    def aggregate(self, scores: List[ModelScore], market_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        聚合多个模型评分
        
        Args:
            scores: 模型评分列表
            market_data: 市场数据 (用于动态权重调整)
        
        Returns:
            {
                "final_strength": int,  # 最终评分
                "final_confidence": float,  # 最终置信度
                "direction": str,  # 方向（LONG/SHORT/NEUTRAL）
                "consensus_level": float,  # 一致性水平 (0-1)
                "std_dev": float,  # 标准差
                "needs_manual_review": bool,  # 是否需要人工审核
                "model_scores": List[Dict],  # 各模型评分详情
                "is_neutral": bool,  # 是否为中性信号（应跳过下单）
            }
        """
        if not scores:
            return self._empty_result()
        
        # 动态权重调整：高波动市场降低宏观模型(Qwen)权重，提高风控模型(DeepSeek)权重
        weights = self.config.model_weights.copy()

        if market_data:
            vix = market_data.get("vix", 20)
            if vix > self.config.VIX_HIGH_THRESHOLD:  # 高波动市场 (>30)
                # 3模型动态权重: 降低 Qwen(宏观), 提高 DeepSeek(风控)
                weights[ModelRole.QWEN] = max(0.20, weights[ModelRole.QWEN] - self.config.VIX_WEIGHT_ADJUSTMENT)
                weights[ModelRole.DEEPSEEK] = min(0.55, weights[ModelRole.DEEPSEEK] + self.config.VIX_WEIGHT_ADJUSTMENT)
                logger.info(f"[动态权重] VIX={vix:.1f} > {self.config.VIX_HIGH_THRESHOLD}, 降低宏观模型权重，提高风控模型权重")

        # P2.1 (RFC-060): 接通投票准确率闭环 — 按各模型历史方向准确率调整权重
        # 数据源: VotingAccuracyTracker (持久化; 已在 voting_coordinator.record_prediction +
        #         order_executor.update_outcome 闭环记录预测↔实际结果)
        # 此前断点: 准确率算了但从未喂回权重 → 权重永远固定 Qwen70/MiniMax30
        # 冷启动: 样本不足(<MIN)或无数据 → 不调整 (multiplier=1.0), 安全
        # 注: weights 用于加权平均 (sum/total), 故乘系数即改变相对比例, 无需显式归一化
        try:
            from app.analysis.voting_accuracy_tracker import get_voting_accuracy_tracker
            _acc_tracker = get_voting_accuracy_tracker()
            _ACC_PERIOD_DAYS = 30
            _ACC_MIN_SAMPLES = 20  # 每模型至少 20 个已回填样本才调权, 防小样本噪音
            for _role in list(weights.keys()):
                _mid = _role.value if hasattr(_role, "value") else str(_role)  # ModelRole.value="qwen"/"minimax"
                try:
                    _bench = _acc_tracker.get_model_benchmark(_mid, _ACC_PERIOD_DAYS)
                except Exception:
                    continue
                if not isinstance(_bench, dict):
                    continue
                _acc = _bench.get("accuracy")
                _n = int(_bench.get("total_votes") or 0)
                if _acc is None or _n < _ACC_MIN_SAMPLES:
                    continue  # 冷启动/样本不足, 保持默认权重
                # 准确率 → 权重系数 (沿用 PerformanceTracker 已验证方案)
                if _acc >= 0.60:
                    _mult = 1.2
                elif _acc > 0.50:
                    _mult = 1.0
                elif _acc > 0.40:
                    _mult = 0.9
                else:
                    _mult = 0.8
                if _mult != 1.0:
                    weights[_role] *= _mult
                    logger.info(
                        f"[闭环权重] {_mid} acc={_acc:.2f}(n={_n}) → ×{_mult} "
                        f"(weight={weights[_role]:.2f})"
                    )
        except Exception as _p2_err:
            logger.debug(f"[闭环权重] 准确率调整失败 (用默认权重): {_p2_err}")

        # P1 (2026-06-29): Signal IC 反馈 — 按方向IC调整各模型权重
        # 逻辑: 如果某模型投LONG, 而LONG方向IC < -0.03 → 该模型权重×0.5
        #        如果某模型投SHORT, 而SHORT方向IC > 0.08 → 该模型权重×1.5
        # 乘数范围 [0.0, 1.5], 与 signal_scorer._apply_ic_adaptation 边界一致
        # FeatureFlag: ic_feedback_to_voting (默认 False)
        try:
            from app.core.feature_flags import FeatureFlags
            if FeatureFlags().is_enabled("ic_feedback_to_voting"):
                _ic_adj = self._get_direction_ic_adjustment(scores)
                if _ic_adj:
                    for _role, _mult in _ic_adj.items():
                        if _mult != 1.0 and _role in weights:
                            weights[_role] *= _mult
                            logger.info(
                                f"[IC-Adjust] {_role.value if hasattr(_role, 'value') else _role} "
                                f"×{_mult:.1f} (weight={weights[_role]:.2f})"
                            )
        except Exception as _ic_err:
            logger.debug(f"[IC-Adjust] Signal IC 权重调整失败: {_ic_err}")

        # P0 优化 (2026-04-17): 分离内部投票和外部投票 (LLM 策略)
        internal_scores = []
        external_scores = []

        for score in scores:
            if score.model_id == "llm_strategy":
                external_scores.append(score)
            else:
                internal_scores.append(score)

        # P0 修复 (2026-06-28): 3模型偏差校正
        # DeepSeek 作为风险审查官, 输出可能系统性偏保守, 使用轻微比例放大
        # 旧方法 (2模型): minimax * 0.75
        # 新方法 (3模型): deepseek * 1.05
        bias_config = getattr(self.config, 'MODEL_BIAS_CORRECTION', {})
        scale = bias_config.get("deepseek_scale", 1.0)

        # P1-2 修复 (2026-04-21): 方向一致性保护
        # 问题: 直接修改 score.strength 会导致 std_dev 过大 → consensus=0
        # 修复: 只在校正后方向翻转时钳位，但使用校正后的原始值计算 std_dev
        # 然后在 final_strength 上做方向保护，不影响 consensus
        direction_protection_needed = False
        qwen_original = None
        deepseek_original = None
        deepseek_corrected = None

        for score in internal_scores:
            if score.model_id == "qwen3.7_max":
                qwen_original = score.strength
                break

        for score in internal_scores:
            if score.model_id == "deepseek_v4_pro" and scale != 1.0:
                original = score.strength
                deepseek_original = original
                corrected = max(0, min(100, round(score.strength * scale)))
                deepseek_corrected = corrected

                # 方向一致性检查：如果校正后方向翻转，标记需要保护
                if qwen_original is not None:
                    qwen_dir = "LONG" if qwen_original >= 50 else "SHORT"
                    deep_dir_orig = "LONG" if original >= 50 else "SHORT"
                    deep_dir_corr = "LONG" if corrected >= 50 else "SHORT"

                    if qwen_dir == deep_dir_orig and qwen_dir != deep_dir_corr:
                        direction_protection_needed = True
                        logger.info(
                            f"[方向保护] 标记: qwen={qwen_original}{qwen_dir}, "
                            f"deepseek {original}→{corrected} 方向翻转"
                        )

                score.strength = corrected
                logger.info(
                    f"[偏差校正] {score.model_id}: {original} → {corrected} (scale={scale})"
                )

        # 计算内部投票的加权平均
        # 2026-07-06 P0修复: 降级票权重降至30%, 与告警文案一致
        internal_weighted_sum = 0
        internal_total_weight = 0

        for score in internal_scores:
            role = self._get_model_role(score.model_id)
            weight = weights.get(role, 0.25)
            if getattr(score, "degraded_vote", False):
                weight *= 0.30
                logger.info(f"[降级权重] {score.model_id}: 原权重 {weights.get(role, 0.25):.2f} → {weight:.2f} (degraded_vote)")
            internal_weighted_sum += score.strength * weight
            internal_total_weight += weight

        internal_strength = round(internal_weighted_sum / internal_total_weight) if internal_total_weight > 0 else 50
        
        # 计算外部投票 (LLM 策略) 的平均强度
        external_strength = 50
        if external_scores:
            ext_weights_sum = sum(s.strength for s in external_scores)
            external_strength = int(ext_weights_sum / len(external_scores))
            logger.info(
                f"[LLM投票] 外部投票强度={external_strength}, "
                f"模型数={len(external_scores)}"
            )
        
        # 融合内部和外部投票 (P1 优化: 动态权重 + round 替代 int)
        llm_weight_used = self.config.LLM_EXTERNAL_VOTE_WEIGHT
        if external_scores and self.config.LLM_EXTERNAL_VOTE_ENABLED:
            # 计算动态权重
            llm_weight_used, weight_reason = self._compute_dynamic_llm_weight(
                internal_strength, internal_scores, external_scores
            )
            # 使用 round() 替代 int() 避免截断吞没
            final_strength = round(internal_strength * (1 - llm_weight_used) + external_strength * llm_weight_used)
            logger.info(
                f"[投票融合] 内部={internal_strength}, LLM={external_strength}, "
                f"LLM动态权重={llm_weight_used:.2f} ({weight_reason}), 最终={final_strength}"
            )
        else:
            final_strength = internal_strength

        # 2026-06-25 修复: 全局方向保护 — 纳入 external_scores.
        # 原逻辑只保护 qwen/minimax 内部一致方向, 但 LLM 策略 (external) 权重较高时
        # 可能把 final_strength 翻转到与多数模型相反的方向.
        # 修复: 计算所有 scores 的加权多数方向, final_strength 不得与多数方向相反.
        _majority_dir = None
        try:
            _all_dirs = []
            for _s in scores:
                # 外部投票模型权重使用 LLM 动态权重
                _w = weights.get(self._get_model_role(_s.model_id), 0.25)
                if _s.model_id == "llm_strategy" and external_scores:
                    _w = llm_weight_used
                # 2026-06-28 P0修复: 方向用LLM返回的direction字段, 不用strength>=50
                # 根因: LLM的strength是"信号强度"(0-100), direction是"方向"(LONG/SHORT)
                # strength=65+direction=SHORT = "强空信号", 不是"强多信号"
                # 旧逻辑用strength>=50判方向 → 3模型全SHORT但strength>=50的被判LONG → 方向翻转
                _s_dir = getattr(_s, 'direction', '').upper()
                _all_dirs.append((1 if _s_dir == "LONG" else (-1 if _s_dir == "SHORT" else 0), _w))
            _long_weight = sum(_w for _d, _w in _all_dirs if _d > 0)
            _short_weight = sum(_w for _d, _w in _all_dirs if _d < 0)
            _majority_dir = "LONG" if _long_weight >= _short_weight else "SHORT"

            _final_dir = "LONG" if final_strength >= 50 else "SHORT"
            if _final_dir != _majority_dir:
                if _majority_dir == "LONG":
                    final_strength = max(final_strength, 51)
                else:
                    final_strength = min(final_strength, 49)
                logger.info(
                    f"[方向保护·全局] final_strength 钳位 → {final_strength} "
                    f"(多数方向={_majority_dir}, LONG_w={_long_weight:.2f}, SHORT_w={_short_weight:.2f})"
                )
        except Exception as _dir_err:
            logger.debug(f"[方向保护·全局] 失败 (忽略): {_dir_err}")

        # P1-2 修复 (2026-04-21): 方向保护 -- 在 final_strength 上应用，不影响 std_dev
        # 当 qwen 和 deepseek 原始方向一致时，final_strength 不应翻转方向
        if direction_protection_needed and qwen_original is not None and deepseek_original is not None:
            # 2026-06-28 P0修复: 用direction字段判方向, 不用strength>=50
            _qwen_score = next((s for s in scores if s.model_id == "qwen3.7_max"), None)
            qwen_dir = getattr(_qwen_score, 'direction', 'LONG').upper() if _qwen_score else "LONG"
            final_dir = "LONG" if final_strength >= 50 else "SHORT"
            if qwen_dir != final_dir:
                # 翻转了 → 钳位到方向边界
                if qwen_dir == "LONG":
                    final_strength = max(final_strength, 51)
                else:
                    final_strength = min(final_strength, 49)
                logger.info(
                    f"[方向保护] final_strength 钳位 → {final_strength} "
                    f"(原始同向={qwen_dir}, 防翻转)"
                )
        
        # 2. 计算平均置信度
        avg_confidence = sum(s.confidence for s in scores) / len(scores)

        # 3. 一致性计算 - P0 修复 (2026-04-17): 2模型架构
        # 2026-07-06 P0修复: 降级票(degraded_vote)不参与 consensus/std_dev 计算
        # 根因: 3模型全部超时 → 3个相同技术分 → std_dev=0 → consensus=1.0 (虚假共识)
        # 修复: 只用真实LLM投票计算 consensus; 降级票仅参与 final_strength 加权
        real_scores = [s for s in scores if not getattr(s, "degraded_vote", False)]
        if not real_scores:
            # 全部降级 → consensus=0, 不假装有共识
            std_dev = 0.0
            consensus_level = 0.0
            direction_agreed = False
        else:
            strengths = [s.strength for s in real_scores]
            std_dev = statistics.stdev(strengths) if len(strengths) > 1 else 0

            direction_votes = [s.direction for s in real_scores]
            short_votes = direction_votes.count("SHORT")
            long_votes = direction_votes.count("LONG")
            direction_agreed = (short_votes == len(real_scores)) or (long_votes == len(real_scores))

            # P0 修复: 方向一致性优先, 分数接近度辅助
            if direction_agreed:
                consensus_level = max(0.30, 1.0 - (std_dev / 40.0))
            else:
                consensus_level = max(0.0, 0.30 - (std_dev / 40.0))

        # 4. 中性信号检测：评分在中性区间且模型分歧小 → 无交易信号
        # 2026-04-16 修复: 如果所有模型方向一致(都SHORT或都LONG), 即使strength在中性区也不应跳过
        is_neutral = (
            self.config.NEUTRAL_LOW <= final_strength <= self.config.NEUTRAL_HIGH
            and std_dev <= self.config.NEUTRAL_MAX_STD
            and not direction_agreed  # 新增: 全同向时不判定为中性
        )
        
        # 5. 确定方向 (2026-07-06: 优先使用加权多数方向, 避免 final_strength 与模型方向背离)
        if is_neutral:
            direction = "NEUTRAL"
        elif _majority_dir in ("LONG", "SHORT"):
            direction = _majority_dir
        else:
            direction = "LONG" if final_strength > 50 else "SHORT"
        
        # 6. 一致性水平已在上方根据方向一致性计算 (L175-182)
        # 不再覆盖 consensus_level，保留方向一致性保护
        # consensus_level = max(0, 1 - (std_dev / self.config.MAX_STD_DEV))  # REMOVED: was overriding L175-182
        
        # 7. 判断是否需要人工审核
        needs_review = (
            std_dev > self.config.MAX_STD_DEV or
            consensus_level < self.config.MIN_CONSENSUS
        )
        
        # 8. 一致性过高 → 自动辩论触发 (2026-04-17 新增)
        auto_debate_triggered = (
            self.config.AUTO_DEBATE_ENABLED
            and consensus_level > self.config.AUTO_DEBATE_CONSENSUS_THRESHOLD
            and final_strength > self.config.AUTO_DEBATE_STRENGTH_THRESHOLD
        )
        if auto_debate_triggered:
            logger.info(
                f"[自动辩论] 一致性={consensus_level:.2f} > {self.config.AUTO_DEBATE_CONSENSUS_THRESHOLD}, "
                f"强度={final_strength} > {self.config.AUTO_DEBATE_STRENGTH_THRESHOLD} → 触发反向辩论"
            )
        
        return {
            "final_strength": final_strength,
            "final_confidence": round(avg_confidence, 2),
            "direction": direction,
            "consensus_level": round(consensus_level, 2),
            "std_dev": round(std_dev, 2),
            "needs_manual_review": needs_review,
            "model_scores": [s.to_dict() for s in scores],
            "is_neutral": is_neutral,
            "auto_debate_triggered": auto_debate_triggered,  # 新增
            "llm_weight_used": round(llm_weight_used, 3) if external_scores else None,  # 新增
        }
    
    def _get_direction_ic_adjustment(self, scores: List[ModelScore]) -> Dict[Any, float]:
        """Signal IC → 方向级权重乘数 (P1 2026-06-29)
        
        读取 signal_snapshots 的 by_direction IC, 按各模型投票方向调整权重:
        - LONG 方向 IC < -0.03 → 投LONG的模型 ×0.5
        - LONG 方向 IC > 0.08  → 投LONG的模型 ×1.3
        - SHORT 方向 IC < -0.03 → 投SHORT的模型 ×0.5
        - SHORT 方向 IC > 0.08  → 投SHORT的模型 ×1.3
        - NEUTRAL → 不调整
        
        缓存: 5min TTL (与 signal_scorer._apply_ic_adaptation 对齐)
        Returns: {ModelRole: multiplier}
        """
        import time as _time
        _now = _time.time()
        _cache = getattr(self, '_ic_dir_cache', None)
        if _cache and (_now - _cache.get('ts', 0)) < 300:
            _ic_map = _cache.get('ic_map', {})
        else:
            try:
                from app.analysis.signal_ic_tracker import get_ic_summary
                _summary = get_ic_summary(min_signals=10, days_back=30)
                _ic_map = _summary.get("by_direction", {}) if _summary.get("status") == "ok" else {}
                self._ic_dir_cache = {'ts': _now, 'ic_map': _ic_map}
            except Exception:
                return {}
        
        if not _ic_map:
            return {}
        
        result: Dict[Any, float] = {}
        for score in scores:
            role = self._get_model_role(score.model_id)
            if role in result:
                continue  # 同角色只算一次
            _dir = getattr(score, 'direction', '').upper()
            _dir_key = _dir if _dir in ("LONG", "SHORT") else None
            if not _dir_key:
                continue
            _ic_info = _ic_map.get(_dir_key)
            if not _ic_info or _ic_info.get("n", 0) < 10:
                continue
            _ic = _ic_info.get("ic", 0.0)
            if _ic < -0.03:
                result[role] = 0.5
            elif _ic > 0.08:
                result[role] = 1.3
            # IC in [-0.03, 0.08] → 不调整 (1.0)
        return result

    def _compute_dynamic_llm_weight(
        self,
        internal_strength: int,
        internal_scores: List[ModelScore],
        external_scores: List[ModelScore],
    ) -> tuple:
        """
        计算 LLM 外部投票动态权重 (2026-04-17 优化)
        
        动态调整规则:
        - 基础权重: 15%
        - 方向一致: +10% (奖励共识)
        - LLM 置信度高于内部平均: +5% (奖励高质量推理)
        - 方向分歧: -10% (降低噪声影响)
        - 范围: 5% ~ 30%
        
        Returns:
            (weight: float, reason: str)
        """
        base = self.config.LLM_EXTERNAL_VOTE_WEIGHT  # 0.15
        weight = base
        reasons = [f"基础{base*100:.0f}%"]
        
        # 1. 方向一致性检测
        internal_dir = "LONG" if internal_strength > 50 else "SHORT"
        external_dirs = [s.direction for s in external_scores if isinstance(s.direction, str)]
        if not external_dirs:
            # direction 是 int 类型 (1/-1)
            ext_dir_int = external_scores[0].direction if external_scores else 0
            external_dirs = ["LONG" if ext_dir_int > 0 else "SHORT" if ext_dir_int < 0 else "NEUTRAL"]
        
        if all(d == internal_dir for d in external_dirs):
            weight += self.config.LLM_DIRECTION_AGREE_BONUS
            reasons.append("方向一致+10%")
        elif any(d != internal_dir for d in external_dirs):
            weight -= self.config.LLM_DIRECTION_AGREE_BONUS
            reasons.append("方向分歧-10%")
        
        # 2. 置信度对比
        if internal_scores:
            internal_avg_conf = sum(s.confidence for s in internal_scores) / len(internal_scores)
        else:
            internal_avg_conf = 0.5
        
        external_avg_conf = sum(s.confidence for s in external_scores) / len(external_scores) if external_scores else 0.5
        if external_avg_conf > internal_avg_conf:
            weight += self.config.LLM_CONFIDENCE_BONUS
            reasons.append(f"LLM置信度更高+5%")
        
        # 3. 钳位到 [MIN, MAX]
        weight = max(self.config.LLM_EXTERNAL_VOTE_MIN, min(self.config.LLM_EXTERNAL_VOTE_MAX, weight))
        
        return weight, " → ".join(reasons)
    
    def _get_model_role(self, model_id: str) -> ModelRole:
        """获取模型角色 - 3模型架构 2026-06-28"""
        mapping = {
            "qwen3.7_max": ModelRole.QWEN,
            "glm_5_2": ModelRole.GLM,
            "deepseek_v4_pro": ModelRole.DEEPSEEK,
        }
        return mapping.get(model_id.lower(), ModelRole.QWEN)
    
    def _empty_result(self) -> Dict[str, Any]:
        """空结果"""
        return {
            "final_strength": 50,
            "final_confidence": 0.0,
            "direction": "NEUTRAL",
            "consensus_level": 0.0,
            "std_dev": 0.0,
            "needs_manual_review": True,
            "model_scores": [],
            "is_neutral": True,
        }
