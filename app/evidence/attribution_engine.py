"""
Attribution Engine (归因引擎) - FTSeek v6.0

功能:
1. 规则归因: 基于明确条件自动打标签(止损触发/滑点过高)
2. 统计归因: 基于历史数据分析策略×场景表现
3. LLM一致性检查: 验证LLM推理与实际收益是否一致(Gemini亮点)
4. 防御性更新: 防止LLM重复犯错
5. 反馈建议生成: 自动产生优化建议

设计理念:
- 因果归因优先于结果归因
- 结构化标签优先于文本描述
- 分级自动化(L1完全自动/L2建议模式/L3人工审批)
"""
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

from app.evidence.trade_evidence_chain import (
    TradeEvidenceChain,
    ResultTag, DirectionTag, SceneTag, StrategyTag, SignalTag,
    ExecutionTag, RiskTag, TimingTag, NewsTag, MarketTag
)

# Prompt 提取到独立文件 (2026-05-21)
from app.llm.prompts.attribution import SYSTEM_PROMPT as ATTR_SYSTEM_PROMPT
from app.llm.prompts.attribution import USER_PROMPT_TEMPLATE as ATTR_USER_PROMPT_TEMPLATE

logger = logging.getLogger("ftseek.evidence.attribution")
logger.propagate = False


class AttributionEngine:
    """
    归因引擎 - 自动打标签+生成建议
    
    3种归因方法:
    1. 规则归因: 明确情况(止损触发/滑点过高/套利窗口过期)
    2. 统计归因: 长期趋势(某策略某场景胜率低)
    3. LLM辅助归因: 模型判断亏损主因(可选)
    """
    
    def __init__(self, db=None, enable_llm_attribution=False):
        """
        Args:
            db: 数据库实例(用于统计归因)
            enable_llm_attribution: 是否启用LLM辅助归因
        """
        self.db = db
        self.enable_llm_attribution = enable_llm_attribution
        
        # LLM一致性检查阈值
        self.llm_overconfident_threshold = -2.0  # LLM置信度>0.8但亏损>2%
        self.llm_confidence_threshold = 0.8
        self.llm_high_confidence_threshold = 0.7
        
        logger.info("归因引擎初始化: LLM一致性检查=启用, LLM辅助归因=%s", 
                   enable_llm_attribution)
    
    def attribute_trade(self, chain: TradeEvidenceChain) -> TradeEvidenceChain:
        """
        自动归因 - 3种方法组合
        
        Args:
            chain: 交易证据链
        
        Returns:
            更新后的证据链(包含归因标签和反馈建议)
        """
        try:
            # 方法1: 规则归因
            self._rule_based_attribution(chain)
            
            # 方法2: 统计归因(需要历史数据)
            if self.db:
                self._statistical_attribution(chain)
            
            # 方法3: LLM辅助归因(可选)
            if self.enable_llm_attribution:
                self._llm_assisted_attribution(chain)
            
            # LLM一致性检查(Gemini亮点)
            self.check_llm_consistency(chain)
            
            # 生成反馈建议
            chain.feedback = self._generate_feedback(chain)
            
            # 防御性更新(Gemini亮点)
            self.apply_defensive_update(chain)
            
            logger.info(
                f"[归因完成] {chain.trace_id}: "
                f"方向{'✓' if chain.attribution.direction_correct else '✗'}, "
                f"场景{'✓' if chain.attribution.scene_correct else '✗'}, "
                f"综合评分{chain.attribution.overall_score}"
            )
            
            return chain
            
        except Exception as e:
            logger.error(f"[归因失败] {chain.trace_id}: {e}", exc_info=True)
            return chain
    
    # ==================== 方法1: 规则归因 ====================
    
    def _rule_based_attribution(self, chain: TradeEvidenceChain):
        """
        规则归因 - 基于明确条件自动打标签
        
        适用场景:
        - 止损触发 → stop_loss_hit
        - 滑点过高 → slippage_high
        - 套利窗口过期 → arbitrage_expired
        """
        risk_tags = []
        
        # 1. 止损/止盈触发
        if chain.exit_reason:
            exit_lower = chain.exit_reason.lower()
            
            if 'stop_loss' in exit_lower or '止损' in exit_lower:
                risk_tags.append(RiskTag.STOP_LOSS_HIT.value)
                chain.attribution.risk_quality = "good"  # 风控生效
            
            elif 'take_profit' in exit_lower or '止盈' in exit_lower:
                risk_tags.append(RiskTag.TAKE_PROFIT_HIT.value)
                chain.attribution.risk_quality = "good"
            
            elif 'trailing' in exit_lower or '移动止损' in exit_lower:
                risk_tags.append(RiskTag.TRAILING_STOP_HIT.value)
                chain.attribution.risk_quality = "good"
            
            elif 'expired' in exit_lower or '过期' in exit_lower:
                risk_tags.append(MarketTag.ARBITRAGE_EXPIRED.value)
        
        if not chain.attribution.risk_quality:
            chain.attribution.risk_quality = "fair"
        
        chain.attribution.risk_tags.extend(risk_tags)
        
        # 2. 滑点分析
        slippage_bps = chain.attribution.slippage_bps or 0
        if slippage_bps > 5:  # >5bps认为过高
            chain.attribution.execution_tags.append(ExecutionTag.SLIPPAGE_HIGH.value)
            chain.attribution.execution_quality = "poor"
            chain.attribution.slippage_category = "high"
        elif slippage_bps > 2:
            chain.attribution.execution_quality = "fair"
            chain.attribution.slippage_category = "medium"
        else:
            chain.attribution.execution_tags.append(ExecutionTag.EXECUTION_GOOD.value)
            chain.attribution.execution_quality = "good"
            chain.attribution.slippage_category = "low"
        
        # 执行评分: 100分基础，滑点越高分越低
        chain.attribution.execution_score = min(100, max(0, 100 - int(slippage_bps * 5)))
        
        # 3. 方向正确性
        pnl = chain.pnl_percent
        if pnl > 0.5:
            chain.attribution.direction_tags.append(DirectionTag.DIRECTION_CORRECT.value)
            chain.attribution.direction_correct = True
            chain.attribution.direction_score = min(100, int(pnl * 20))
        elif pnl < -0.5:
            chain.attribution.direction_tags.append(DirectionTag.DIRECTION_WRONG.value)
            chain.attribution.direction_correct = False
            chain.attribution.direction_score = max(0, int(50 + pnl * 10))
        else:
            chain.attribution.direction_tags.append(DirectionTag.MARKET_FLAT.value)
            chain.attribution.direction_score = 50
        
        # 4. 结果分类
        if pnl > 3:
            chain.attribution.result_tags.append(ResultTag.BIG_WIN.value)
            chain.attribution.pnl_category = "big_win"
        elif pnl > 1:
            chain.attribution.result_tags.append(ResultTag.WIN.value)
            chain.attribution.pnl_category = "win"
        elif pnl > 0:
            chain.attribution.result_tags.append(ResultTag.SMALL_WIN.value)
            chain.attribution.pnl_category = "small_win"
        elif pnl > -1:
            chain.attribution.result_tags.append(ResultTag.SMALL_LOSS.value)
            chain.attribution.pnl_category = "small_loss"
        elif pnl > -3:
            chain.attribution.result_tags.append(ResultTag.LOSS.value)
            chain.attribution.pnl_category = "loss"
        else:
            chain.attribution.result_tags.append(ResultTag.BIG_LOSS.value)
            chain.attribution.pnl_category = "big_loss"
        
        # 5. 时机分析 - 写入 timing_tags
        holding_minutes = chain.holding_period_minutes
        if holding_minutes < 5 and pnl < 0:
            chain.attribution.timing_tags.append(TimingTag.EARLY_EXIT.value)
            chain.attribution.timing_quality = "poor"
            chain.attribution.timing_score = 20
        elif holding_minutes > 60 and pnl > 0:
            chain.attribution.timing_tags.append(TimingTag.LATE_EXIT.value)
            chain.attribution.timing_quality = "fair"
            chain.attribution.timing_score = 50
        elif holding_minutes > 10 and pnl > 1:
            chain.attribution.timing_tags.append(TimingTag.TIMING_EXCELLENT.value)
            chain.attribution.timing_quality = "excellent"
            chain.attribution.timing_score = 90
        else:
            chain.attribution.timing_tags.append(TimingTag.TIMING_GOOD.value)
            chain.attribution.timing_quality = "good"
            chain.attribution.timing_score = 70
        
        # 6. 信号质量评分
        signal_sources = chain.evidence.signal_sources or []
        if len(signal_sources) >= 3:
            chain.attribution.signal_tags.append(SignalTag.SIGNAL_STRONG.value)
            chain.attribution.signal_quality = "high"
            chain.attribution.signal_score = 85
        elif len(signal_sources) >= 2:
            chain.attribution.signal_quality = "medium"
            chain.attribution.signal_score = 60
        else:
            chain.attribution.signal_tags.append(SignalTag.SIGNAL_WEAK.value)
            chain.attribution.signal_quality = "low"
            chain.attribution.signal_score = 30
        
        # 7. 风控评分
        if chain.attribution.risk_quality == "good":
            chain.attribution.risk_score = 80
        elif chain.attribution.risk_quality == "fair":
            chain.attribution.risk_score = 50
        else:
            chain.attribution.risk_score = 20
        # 大亏>3% 风控扣分
        if pnl < -3:
            chain.attribution.risk_score = max(0, chain.attribution.risk_score - 30)
        
        # 8. 场景正确性评估
        # 逻辑: 如果方向正确且有明确场景，认为场景识别正确
        if chain.evidence.primary_scene:
            if chain.attribution.direction_correct and pnl > 0:
                chain.attribution.scene_correct = True
                chain.attribution.scene_tags.append(SceneTag.SCENE_CORRECT.value)
                chain.attribution.scene_score = min(100, 60 + int(pnl * 10))
            elif not chain.attribution.direction_correct and pnl < -1:
                chain.attribution.scene_correct = False
                chain.attribution.scene_tags.append(SceneTag.SCENE_MISMATCH.value)
                chain.attribution.scene_score = max(0, 40 + int(pnl * 10))
            else:
                # 小盈小亏或平盘，场景无法判断
                chain.attribution.scene_score = 50
        
        # 9. 综合评分 (overall_score) - 加权计算
        chain.attribution.overall_score = int(
            chain.attribution.direction_score * 0.25 +
            chain.attribution.scene_score * 0.15 +
            chain.attribution.strategy_score * 0.15 +
            chain.attribution.signal_score * 0.10 +
            chain.attribution.execution_score * 0.10 +
            chain.attribution.timing_score * 0.15 +
            chain.attribution.risk_score * 0.10
        )
        
        # 10. 成功/失败因素
        if pnl > 0:
            if chain.attribution.direction_correct:
                chain.attribution.success_factors.append("方向判断正确")
            if chain.attribution.execution_quality in ["good", "excellent"]:
                chain.attribution.success_factors.append("执行良好")
            if chain.attribution.timing_quality in ["good", "excellent"]:
                chain.attribution.success_factors.append("时机把握准确")
        else:
            if not chain.attribution.direction_correct:
                chain.attribution.failure_factors.append("方向判断错误")
            if chain.attribution.execution_quality == "poor":
                chain.attribution.failure_factors.append("执行滑点过高")
            if chain.attribution.timing_quality == "poor":
                chain.attribution.failure_factors.append("时机把握不佳")
    
    # ==================== 方法2: 统计归因 ====================
    
    def _statistical_attribution(self, chain: TradeEvidenceChain):
        """
        统计归因 - 基于历史数据
        
        查询该策略在该场景下的历史表现:
        - 胜率<30% → strategy_misfit
        - 胜率>70%且夏普>1.5 → strategy_fit
        """
        try:
            if not self.db:
                return
            
            strategy = chain.evidence.primary_strategy
            scene = chain.evidence.primary_scene
            
            if not strategy or not scene:
                return
            
            # 查询过去30天表现
            stats = self.db.query_strategy_scene_performance(
                strategy=strategy,
                scene=scene,
                window_days=30
            )
            
            if stats and stats.get('trade_count', 0) >= 5:  # 至少5笔交易
                win_rate = stats.get('win_rate', 0)
                
                if win_rate < 0.3:
                    chain.attribution.strategy_tags.append(StrategyTag.STRATEGY_MISFIT.value)
                    chain.attribution.strategy_fit = False
                    chain.feedback.strategy_weight_adjustment = -0.10  # 降权10%
                    chain.attribution.strategy_score = max(0, int(win_rate * 100))
                
                elif win_rate > 0.7:
                    chain.attribution.strategy_tags.append(StrategyTag.STRATEGY_FIT.value)
                    chain.attribution.strategy_fit = True
                    sharpe = stats.get('sharpe', 0)
                    if sharpe > 1.5:
                        chain.feedback.strategy_weight_adjustment = 0.15  # 升权15%
                    chain.attribution.strategy_score = min(100, int(win_rate * 100))
                
                else:
                    chain.attribution.strategy_score = int(win_rate * 100)
        
        except Exception as e:
            logger.debug(f"[统计归因] {chain.trace_id} 失败: {e}")
    
    # ==================== 方法3: LLM辅助归因 ====================
    
    def _llm_assisted_attribution(self, chain: TradeEvidenceChain):
        """
        LLM 辅助归因 - 模型判断盈亏主因 (2026-05-21)
        
        触发条件：PnL 绝对值 > 1% 或 归因标签不明确时调用
        使用 UnifiedLLMService — 统一审计/缓存/降级
        """
        try:
            # 触发条件：显著盈亏或归因不明确
            if abs(chain.pnl_percent) < 1.0 and chain.attribution.direction_correct:
                return  # 小盈亏且方向正确，跳过 LLM
            
            # 构建上下文
            symbol = chain.trace_id.split('-')[0] if '-' in chain.trace_id else chain.trace_id
            direction = chain.evidence.direction
            pnl = chain.pnl_percent
            scene = chain.evidence.scene
            strategy = chain.evidence.strategy_id
            news_sentiment = chain.evidence.news_sentiment
            llm_reasoning = chain.evidence.llm_reasoning[:500] if chain.evidence.llm_reasoning else "N/A"
            
            # 2026-05-21: 增强版 Prompt (含思维链引导 + Few-shot)
            system_prompt = ATTR_SYSTEM_PROMPT
            
            user_prompt = ATTR_USER_PROMPT_TEMPLATE.format(
                symbol=symbol, direction=direction, pnl=pnl,
                scene=scene, strategy=strategy,
                news_sentiment=news_sentiment,
                llm_reasoning=llm_reasoning
            )
            
            # 使用 UnifiedLLMService (2026-05-21 升级)
            import asyncio
            from app.llm.unified_service import get_unified_llm
            
            async def _call_llm():
                svc = get_unified_llm(
                    default_model="qwen-plus",
                    default_timeout=60,
                    max_retries=2
                )
                
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
                
                return await svc.call(
                    messages=messages,
                    model="qwen-plus",
                    timeout=60,
                    task_type="attribution"
                )
            
            raw_content = asyncio.run(_call_llm())
            
            if not raw_content:
                logger.warning("[LLM 归因] 返回空响应，跳过")
                return
            
            # 解析 JSON
            import json
            import re
            
            # 尝试提取 JSON 块
            json_match = re.search(r'\{[\s\S]*\}', raw_content)
            if not json_match:
                logger.warning("[LLM 归因] 响应无 JSON: %s", raw_content[:200])
                return
            
            data = json.loads(json_match.group())
            
            # 写入归因标签
            chain.attribution.llm_main_cause = data.get("main_cause", "unknown")
            chain.attribution.llm_confidence = data.get("confidence", 0.0)
            chain.attribution.llm_suggestion = data.get("suggestion", "")[:200]
            
            # 新增 lesson 字段 (2026-05-21)
            if hasattr(chain.attribution, 'llm_lesson'):
                chain.attribution.llm_lesson = data.get("lesson", "")[:200]
            
            logger.info(
                f"[LLM 归因] {chain.trace_id}: {data.get('main_cause')} "
                f"(置信度={data.get('confidence', 0):.2f})"
            )
            
        except Exception as e:
            logger.warning(f"[LLM 归因] 失败 (非阻塞): {e}")
    
    # ==================== LLM一致性检查 (Gemini亮点) ====================
    
    def check_llm_consistency(self, chain: TradeEvidenceChain) -> Dict:
        """
        检查LLM推理逻辑与实际收益是否一致
        
        规则:
        1. LLM看多但实际亏损>2% → "llm_overconfident"
        2. LLM推理提到"利好"但新闻情绪<0 → "llm_news_misread"
        3. LLM置信度>0.8但方向错误 → "llm_false_confidence"
        
        Returns:
            检查结果 {"issues": [...], "consistent": bool}
        """
        issues = []
        
        pnl = chain.pnl_percent
        llm_reasoning = chain.evidence.llm_reasoning.lower()
        llm_confidence = chain.evidence.llm_confidence
        
        # 规则1: 过度自信
        if llm_confidence > self.llm_confidence_threshold and pnl < self.llm_overconfident_threshold:
            issues.append({
                "type": "llm_overconfident",
                "severity": "high",
                "detail": f"LLM置信度{llm_confidence:.2f}但亏损{pnl:.2f}%",
                "action": "reduce_llm_weight_in_prompt",
                "suggestion": f"在类似场景下降低LLM权重，当前置信度{llm_confidence:.2f}可能过高"
            })
            logger.warning(
                f"[LLM一致性] {chain.trace_id}: 过度自信 "
                f"(置信度={llm_confidence:.2f}, 盈亏={pnl:.2f}%)"
            )
        
        # 规则2: 新闻误读
        if llm_reasoning and ("利好" in llm_reasoning or "bullish" in llm_reasoning):
            if chain.evidence.news_sentiment < 0:
                issues.append({
                    "type": "llm_news_misread",
                    "severity": "medium",
                    "detail": f"LLM判断利好但新闻情绪{chain.evidence.news_sentiment:.2f}",
                    "action": "add_news_sentiment_check_to_prompt",
                    "suggestion": "LLM可能误读新闻，建议在Prompt中增加新闻情绪强制检查"
                })
                logger.warning(
                    f"[LLM一致性] {chain.trace_id}: 新闻误读 "
                    f"(LLM=利好, 情绪={chain.evidence.news_sentiment:.2f})"
                )
        
        # 规则3: 虚假信心
        if not chain.attribution.direction_correct and llm_confidence > self.llm_high_confidence_threshold:
            issues.append({
                "type": "llm_false_confidence",
                "severity": "high",
                "detail": f"LLM方向错误但置信度{llm_confidence:.2f}",
                "action": "apply_defensive_update",
                "suggestion": "对此类场景应用防御性更新，降低LLM权重15%"
            })
        
        # 保存检查结果
        chain.feedback.llm_consistency_issues = issues
        chain.feedback.llm_consistent = len(issues) == 0
        
        return {
            "issues": issues,
            "consistent": len(issues) == 0
        }
    
    # ==================== 防御性更新机制 (Gemini亮点) ====================
    
    def apply_defensive_update(self, chain: TradeEvidenceChain):
        """
        防御性更新 - 防止LLM重复犯错
        
        当LLM推理与市场方向完全相反时:
        1. 在数据库记录"负反馈标记"
        2. 下次同类新闻/场景时降低LLM权重
        3. 钉钉推送告警
        """
        if not chain.attribution.direction_correct:
            if chain.evidence.llm_confidence > self.llm_high_confidence_threshold:
                try:
                    # 提取新闻主题(简化版)
                    news_topic = self._extract_news_topic(chain)
                    scene = chain.evidence.primary_scene
                    
                    # 记录负反馈到数据库
                    if self.db:
                        self.db.record_llm_negative_feedback(
                            trace_id=chain.trace_id,
                            news_topic=news_topic,
                            scene=scene,
                            penalty=-0.15,  # 降低15%权重
                            expires_at=(datetime.now() + timedelta(hours=24)).isoformat()
                        )
                    
                    logger.warning(
                        f"[防御性更新] {chain.trace_id}: "
                        f"场景={scene}, 新闻主题={news_topic}, "
                        f"权重惩罚=-15%(24小时)"
                    )
                    
                except Exception as e:
                    logger.error(f"[防御性更新] {chain.trace_id} 失败: {e}")
    
    def _extract_news_topic(self, chain: TradeEvidenceChain) -> str:
        """
        从LLM推理中提取新闻主题
        
        简化实现: 取前20个字符
        完整实现: 使用NLP提取关键词
        """
        reasoning = chain.evidence.llm_reasoning
        if not reasoning:
            return "unknown"
        
        # 简单提取
        return reasoning[:30].strip()
    
    # ==================== 反馈建议生成 ====================
    
    def _generate_feedback(self, chain: TradeEvidenceChain):
        """
        生成反馈建议
        
        基于归因结果产生:
        - 策略权重调整
        - 场景置信度调整
        - 风控规则优化
        - 执行规则更新
        """
        feedback = chain.feedback
        suggestions = []
        
        # 1. 策略层反馈
        if chain.attribution.strategy_tags:
            if StrategyTag.STRATEGY_MISFIT.value in chain.attribution.strategy_tags:
                suggestions.append({
                    "type": "strategy",
                    "priority": "high",
                    "suggestion": f"策略{chain.evidence.primary_strategy}在场景{chain.evidence.primary_scene}下表现不佳，建议降权10-15%",
                    "action": "decrease_strategy_weight",
                    "target": chain.evidence.primary_strategy,
                    "scene": chain.evidence.primary_scene,
                    "adjustment": -0.10
                })
        
        # 2. 执行层反馈
        if ExecutionTag.SLIPPAGE_HIGH.value in chain.attribution.execution_tags:
            suggestions.append({
                "type": "execution",
                "priority": "medium",
                "suggestion": "滑点过高，建议优化订单执行策略(考虑使用限价单)",
                "action": "switch_to_limit_order",
                "target": chain.evidence.symbol
            })
        
        # 3. 时机层反馈
        if TimingTag.EARLY_EXIT.value in chain.attribution.timing_tags:
            suggestions.append({
                "type": "timing",
                "priority": "medium",
                "suggestion": "出场过早，建议放宽止盈条件或使用移动止损",
                "action": "relax_take_profit_condition"
            })
        
        # 4. LLM一致性反馈
        if hasattr(feedback, 'llm_consistency_issues') and feedback.llm_consistency_issues:
            for issue in feedback.llm_consistency_issues:
                if issue['severity'] == 'high':
                    suggestions.append({
                        "type": "llm",
                        "priority": "high",
                        "suggestion": issue['suggestion'],
                        "action": issue['action'],
                        "detail": issue['detail']
                    })
        
        feedback.auto_optimization_suggestions = suggestions
        
        return feedback
    
    # ==================== 因果链条摘要生成 (Gemini亮点) ====================
    
    @staticmethod
    def generate_causal_summary(chain: TradeEvidenceChain) -> str:
        """
        生成因果链条式摘要 (Gemini风格)
        
        格式:
        [交易复盘 TRC-xxx]
        - 场景识别: GAP_EVENT (Confidence: 0.85)
        - 仲裁逻辑: 跨市场套利信号压制趋势信号，因为Z-Score=2.8
        - LLM推理: "美股昨夜受利好驱动，预计港股开盘存在2%补涨空间"
        - 实际结果: 成交价10.2，离场价10.5 (盈亏+2.9%)
        - 归因分析: 方向正确(85分)，场景正确(90分)，执行良好(滑点1.5bps)
        - 优化建议: 仲裁逻辑正确，无需干预
        """
        lines = []
        
        # 标题
        lines.append(f"**[交易复盘 {chain.trace_id}]**")
        lines.append("")
        
        # 场景识别
        lines.append(
            f"- **场景识别**: `{chain.evidence.primary_scene}` "
            f"(Confidence: {chain.evidence.confidence:.2f})"
        )
        
        # 时段信息
        if chain.evidence.trading_session:
            lines.append(f"- **交易时段**: {chain.evidence.trading_session}")
        
        # 仲裁逻辑
        if chain.evidence.conflict_resolution_log:
            last_conflict = chain.evidence.conflict_resolution_log[-1]
            reason = last_conflict.get('reason', 'N/A')
            lines.append(f"- **仲裁逻辑**: {reason}")
        
        # LLM推理
        if chain.evidence.llm_reasoning:
            reasoning_preview = chain.evidence.llm_reasoning[:80]
            lines.append(f"- **LLM推理**: \"{reasoning_preview}...\"")
            lines.append(f"- **LLM置信度**: {chain.evidence.llm_confidence:.2f}")
        
        # 实际结果
        pnl_emoji = "✅" if chain.pnl > 0 else "❌"
        lines.append(
            f"- **实际结果**: 盈亏{pnl_emoji} {chain.pnl_percent:.2f}% "
            f"(持有{chain.holding_period_minutes}分钟)"
        )
        
        # 归因分析
        scores = []
        if chain.attribution.direction_score > 0:
            scores.append(f"方向{chain.attribution.direction_score}分")
        if chain.attribution.scene_score > 0:
            scores.append(f"场景{chain.attribution.scene_score}分")
        if chain.attribution.strategy_score > 0:
            scores.append(f"策略{chain.attribution.strategy_score}分")
        if chain.attribution.execution_score > 0:
            scores.append(f"执行{chain.attribution.execution_score}分")
        
        if scores:
            lines.append(f"- **归因分析**: {', '.join(scores)}")
        
        # 优化建议
        if chain.feedback.auto_optimization_suggestions:
            top_suggestion = chain.feedback.auto_optimization_suggestions[0]
            lines.append(f"- **优化建议**: {top_suggestion['suggestion']}")
        else:
            lines.append("- **优化建议**: 无需干预，逻辑正确")
        
        # LLM一致性检查
        if hasattr(chain.feedback, 'llm_consistency_issues') and chain.feedback.llm_consistency_issues:
            issues_count = len(chain.feedback.llm_consistency_issues)
            lines.append(f"- **LLM一致性**: 发现{issues_count}个问题")
        
        return "\n".join(lines)
    
    # ==================== 盘后归因复盘方法 (2026-04-12 新增) ====================
    
    def generate_post_market_review(self, date: str = None) -> Dict:
        """
        生成盘后归因复盘报告
        
        Args:
            date: 日期 (默认今天)
        
        Returns:
            {
                "session": "post_market",
                "date": "2026-04-13",
                "summary": {
                    "total_trades": 5,
                    "win_trades": 3,
                    "loss_trades": 2,
                    "win_rate": 0.60,
                    "total_pnl": 1250.50,
                    "total_pnl_percent": 0.0125
                },
                "trades": [...],
                "strategy_performance": {...},
                "optimization_suggestions": [...],
                "generated_at": "2026-04-13T16:30:00"
            }
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        try:
            logger.info(f"[盘后复盘] 开始生成 {date} 归因报告...")
            
            # 1. 获取当日交易
            trades = self._get_day_trades(date)
            
            if not trades:
                logger.info(f"[盘后复盘] {date} 无交易记录")
                return self._empty_review(date)
            
            # 2. 逐笔归因
            trade_reviews = []
            for trade in trades:
                review = self._review_single_trade(trade)
                trade_reviews.append(review)
            
            # 3. 策略表现统计
            strategy_perf = self._calculate_strategy_performance(trades)
            
            # 4. 生成优化建议
            suggestions = self._generate_optimization_suggestions(
                trade_reviews, strategy_perf
            )
            
            # 5. 组装报告
            report = {
                "session": "post_market",
                "date": date,
                "summary": self._calculate_summary(trades),
                "trades": trade_reviews,
                "strategy_performance": strategy_perf,
                "optimization_suggestions": suggestions,
                "generated_at": datetime.now().isoformat()
            }
            
            # 6. 保存报告
            self._save_review_report(report)
            
            logger.info(f"[盘后复盘] 完成 | 交易{report['summary']['total_trades']}笔 | 胜率{report['summary']['win_rate']:.0%}")
            return report
            
        except Exception as e:
            logger.error(f"[盘后复盘] 生成失败: {e}", exc_info=True)
            return self._empty_review(date)
    
    def _get_day_trades(self, date: str) -> List[TradeEvidenceChain]:
        """获取当日平仓交易"""
        if not self.db:
            logger.warning("[盘后复盘] 无数据库连接，无法获取交易")
            return []
        
        try:
            # 从trades表查询当日已平仓的交易
            query = """
                SELECT id, signal_id, code, name, action, entry_price, exit_price,
                       quantity, pnl, pnl_percent, fees, strategy,
                       entry_time, exit_time, exit_reason, status
                FROM trades
                WHERE DATE(exit_time) = ? AND status = 'closed'
                ORDER BY exit_time DESC
            """
            
            rows = []
            if hasattr(self.db, 'get_connection'):
                with self.db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(query, (date,))
                    rows = [dict(row) for row in cursor.fetchall()]
            elif hasattr(self.db, 'execute_query'):
                rows = self.db.execute_query(query, (date,))
            
            if not rows:
                logger.info(f"[盘后复盘] {date} 无平仓记录")
                return []
            
            # 转换为TradeEvidenceChain对象
            chains = []
            for row in rows:
                chain = TradeEvidenceChain(
                    trace_id=row.get('id', ''),
                    symbol=row.get('code', ''),
                )
                chain.evidence.trace_id = row.get('id', '')
                chain.evidence.symbol = row.get('code', '')
                chain.evidence.timestamp_open = row.get('entry_time', '')
                chain.evidence.timestamp_close = row.get('exit_time', '')
                chain.evidence.primary_strategy = row.get('strategy', 'unknown')
                chain.evidence.strategy_mode = row.get('strategy', 'unknown')
                chain.pnl = row.get('pnl', 0) or 0
                chain.pnl_percent = row.get('pnl_percent', 0) or 0
                chain.exit_reason = row.get('exit_reason', '')
                chain.is_closed = True
                chain.status = 'closed'
                chains.append(chain)
            
            logger.info(f"[盘后复盘] {date} 获取到 {len(chains)} 笔平仓交易")
            return chains
            
        except Exception as e:
            logger.error(f"[盘后复盘] 获取交易数据失败: {e}")
            return []
    
    def _empty_review(self, date: str) -> Dict:
        """生成空复盘报告"""
        return {
            "session": "post_market",
            "date": date,
            "summary": {
                "total_trades": 0,
                "win_trades": 0,
                "loss_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_pnl_percent": 0.0
            },
            "trades": [],
            "strategy_performance": {},
            "optimization_suggestions": ["当日无交易"],
            "generated_at": datetime.now().isoformat()
        }
    
    def _review_single_trade(self, trade: TradeEvidenceChain) -> Dict:
        """审查单笔交易 — 修复字段引用错误"""
        # 正确字段: trade.pnl, trade.pnl_percent (无 result_ 前缀)
        # action 从证据层获取
        pnl = getattr(trade, 'pnl', 0.0) or 0.0
        pnl_pct = getattr(trade, 'pnl_percent', 0.0) or 0.0
        is_win = pnl_pct > 0
        action = getattr(trade, 'evidence', None)
        action_val = action.primary_strategy if action else 'unknown'
        
        # 归因层安全访问
        attr = getattr(trade, 'attribution', None)
        
        return {
            "trade_id": getattr(trade, 'trace_id', 'unknown'),
            "symbol": getattr(trade, 'symbol', 'unknown'),
            "action": action_val,
            "result": "win" if is_win else "loss",
            "pnl": round(pnl, 2),
            "pnl_percent": round(pnl_pct, 4),
            "attribution": {
                "scene_correct": attr.scene_correct if attr else False,
                "strategy_fit": attr.strategy_fit if attr else False,
                "execution_quality": attr.execution_quality if attr else "unknown",
                "execution_score": attr.execution_score if attr else 0,
                "timing_quality": attr.timing_quality if attr else "unknown",
                "timing_score": attr.timing_score if attr else 0,
                "overall_score": attr.overall_score if attr else 0,
                "direction_correct": attr.direction_correct if attr else False,
                "success_factors": attr.success_factors if attr else [],
                "failure_factors": attr.failure_factors if attr else []
            },
            "lesson": self._extract_lesson(trade),
            "optimization": self._extract_optimization(trade)
        }
    
    def _calculate_strategy_performance(self, trades: List[TradeEvidenceChain]) -> Dict:
        """计算策略表现 — 修复字段引用"""
        strategy_stats = {}
        
        for trade in trades:
            # 正确字段: trade.evidence.primary_strategy (非 trade.strategy)
            evidence = getattr(trade, 'evidence', None)
            strategy = evidence.primary_strategy if evidence else 'unknown'
            pnl_pct = getattr(trade, 'pnl_percent', 0.0) or 0.0
            pnl = getattr(trade, 'pnl', 0.0) or 0.0
            
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    "trades": 0,
                    "wins": 0,
                    "total_pnl": 0.0,
                    "avg_score": 0.0,
                    "score_sum": 0.0
                }
            
            strategy_stats[strategy]["trades"] += 1
            if pnl_pct > 0:
                strategy_stats[strategy]["wins"] += 1
            strategy_stats[strategy]["total_pnl"] += pnl
            
            # 累积归因评分
            attr = getattr(trade, 'attribution', None)
            if attr:
                strategy_stats[strategy]["score_sum"] += attr.overall_score
        
        # 计算胜率和平均盈亏
        for strategy, stats in strategy_stats.items():
            stats["win_rate"] = stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0.0
            stats["avg_pnl"] = stats["total_pnl"] / stats["trades"] if stats["trades"] > 0 else 0.0
            stats["avg_score"] = stats["score_sum"] / stats["trades"] if stats["trades"] > 0 else 0.0
            # 清理内部字段
            stats.pop("score_sum", None)
        
        return strategy_stats
    
    def _generate_optimization_suggestions(self, trade_reviews: List[Dict], strategy_perf: Dict) -> List[str]:
        """生成优化建议"""
        suggestions = []
        
        # 基于胜率生成建议
        for strategy, perf in strategy_perf.items():
            if perf["win_rate"] < 0.5 and perf["trades"] > 2:
                suggestions.append(f"{strategy} 胜率偏低({perf['win_rate']:.0%})，建议调整参数或减少使用")
        
        # 基于归因生成建议
        scene_errors = sum(1 for t in trade_reviews if not t['attribution']['scene_correct'])
        if scene_errors > len(trade_reviews) * 0.3:
            suggestions.append("场景识别准确率偏低，建议优化场景判断逻辑")
        
        if not suggestions:
            suggestions.append("整体表现良好，继续保持")
        
        return suggestions
    
    def _calculate_summary(self, trades: List[TradeEvidenceChain]) -> Dict:
        """计算总结统计 — 修复字段引用"""
        total = len(trades)
        wins = sum(1 for t in trades if (getattr(t, 'pnl_percent', 0.0) or 0.0) > 0)
        total_pnl = sum(getattr(t, 'pnl', 0.0) or 0.0 for t in trades)
        
        return {
            "total_trades": total,
            "win_trades": wins,
            "loss_trades": total - wins,
            "win_rate": wins / total if total > 0 else 0.0,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_percent": round(total_pnl / 10000, 4)  # 假设本金10000
        }
    
    def _extract_lesson(self, trade: TradeEvidenceChain) -> str:
        """提取经验教训 — 修复字段引用"""
        attr = getattr(trade, 'attribution', None)
        if attr:
            if not attr.scene_correct:
                return "场景识别错误，需改进场景判断"
            if not attr.strategy_fit:
                return "策略不适配当前场景，需优化策略匹配"
            if not attr.direction_correct:
                return "方向判断错误，需重新评估信号"
        return "表现正常"
    
    def _extract_optimization(self, trade: TradeEvidenceChain) -> str:
        """提取优化点 — 修复评分范围 (0-100 非 0-1)"""
        attr = getattr(trade, 'attribution', None)
        if attr:
            if attr.execution_score < 60:
                return "改进执行时机，减少滑点"
            if attr.timing_score < 60:
                return "优化入场时机"
        return "继续保持"
    
    def _save_review_report(self, report: Dict):
        """保存复盘报告"""
        import json
        import os
        
        try:
            date = report['date']
            filepath = f"data/post_market_reviews/{date}.json"
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            
            logger.info(f"[盘后复盘] 报告已保存至 {filepath}")
            
        except Exception as e:
            logger.error(f"[盘后复盘] 保存失败: {e}", exc_info=True)
