"""
动态轮次调度器 (Convergence-Driven Rounds)

轮次不硬编码为 3，而是由收敛度驱动:
- Round 1: 发散 — 各模型独立分析 (必须)
- Round 2: 交叉质疑 — 互相挑战 (必须)
- Round 3: 初步收敛 — 尝试共识 (必须)
- Round 4+: 深度收敛 — 仅当共识度不足时继续
- 终止: 共识度达标 OR max_rounds OR 分歧连续扩大
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from debate_engine.topic import DebateTopic
from debate_engine.verdict import DebateVerdict, Injection
from debate_engine.knowledge import DebateKnowledgeBase, DebateRecord, Lesson
from debate_engine.models.bailian import BailianClient

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass
class RoundResult:
    """单轮辩论结果"""
    round_num: int
    model_outputs: Dict[str, Any] = field(default_factory=dict)
    consensus_score: float = 0.0
    forced_stop: bool = False
    stop_reason: str = ""


@dataclass
class DebateConfig:
    """辩论配置"""
    min_rounds: int = 3
    max_rounds: int = 7
    convergence_threshold: float = 0.8
    divergence_limit: int = 2
    models: List[str] = field(default_factory=lambda: ["qwen3.7-max", "deepseek-v4-pro"])


class DynamicRoundScheduler:
    """
    动态轮次调度器
    
    Usage:
        scheduler = DynamicRoundScheduler(config, client, knowledge_base)
        verdict = scheduler.run(topic)
    """
    
    def __init__(self, config: Optional[DebateConfig] = None,
                 client: Optional[BailianClient] = None,
                 knowledge_base: Optional[DebateKnowledgeBase] = None):
        self.config = config or DebateConfig()
        self.client = client or BailianClient()
        self.kb = knowledge_base or DebateKnowledgeBase()
    
    def run(self, topic: DebateTopic) -> DebateVerdict:
        """
        执行完整辩论，返回结构化结论
        
        流程:
        1. 从知识库检索相关历史
        2. 依次执行各轮 (min_rounds 必须，之后按收敛度决定是否继续)
        3. 编译最终结论
        4. 保存辩论记录 + 提炼教训
        """
        debate_id = f"debate_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{topic.topic_id}"
        
        # 构建知识上下文
        knowledge_ctx = self.kb.build_knowledge_context(
            topic_type=topic.topic_type.value,
            market=topic.market,
            factors=self._extract_factors(topic),
        )
        
        all_rounds: List[RoundResult] = []
        consensus_scores: List[float] = []
        stop_reason = "max_rounds_reached"
        
        for round_num in range(1, self.config.max_rounds + 1):
            logger.info(f"Debate {debate_id}: Round {round_num}")
            
            round_result = self._execute_round(
                round_num, topic, all_rounds, knowledge_ctx
            )
            all_rounds.append(round_result)
            
            if round_result.forced_stop:
                stop_reason = round_result.stop_reason
                break
            
            # Round min_rounds+ 检测收敛
            if round_num >= self.config.min_rounds:
                score = round_result.consensus_score
                consensus_scores.append(score)
                
                if score >= self.config.convergence_threshold:
                    stop_reason = "converged"
                    logger.info(f"Converged at round {round_num}: score={score:.2f}")
                    break
                
                # 分歧扩大检测
                if len(consensus_scores) >= self.config.divergence_limit:
                    recent = consensus_scores[-self.config.divergence_limit:]
                    if all(recent[i] < recent[i-1] for i in range(1, len(recent))):
                        stop_reason = "divergence_expanding"
                        logger.info(f"Divergence expanding at round {round_num}")
                        break
            
            if round_num == self.config.max_rounds:
                stop_reason = "max_rounds_reached"
        
        # 编译最终结论
        final_score = consensus_scores[-1] if consensus_scores else 0.0
        verdict = self._compile_verdict(debate_id, topic, all_rounds, final_score, stop_reason)
        
        # 保存辩论记录
        record = DebateRecord(
            debate_id=debate_id,
            topic_id=topic.topic_id,
            topic_type=topic.topic_type.value,
            market=topic.market,
            timestamp=datetime.now().isoformat(),
            models_used=self.config.models,
            total_rounds=len(all_rounds),
            stop_reason=stop_reason,
            consensus_score=final_score,
            consensus_items=verdict.consensus_items,
            disagreement_items=verdict.disagreement_items,
            action_items=verdict.action_items,
        )
        self.kb.save_debate(record)
        
        return verdict
    
    def _execute_round(self, round_num: int, topic: DebateTopic,
                       previous_rounds: List[RoundResult],
                       knowledge_ctx: Dict) -> RoundResult:
        """执行单轮辩论"""
        result = RoundResult(round_num=round_num)
        
        # 加载 prompt 模板
        prompt_template = self._load_prompt(round_num)
        
        # 构建 prompt
        prompt = self._build_prompt(round_num, topic, previous_rounds, 
                                     knowledge_ctx, prompt_template)
        
        # 调用各模型
        system_msg = "你是一个量化交易策略分析师。请用 JSON 格式输出你的分析。"
        
        for model_name in self.config.models:
            try:
                output = self.client.call(model_name, prompt, system=system_msg)
                result.model_outputs[model_name] = output
                logger.info(f"  {model_name}: {len(output.get('content', ''))} chars")
            except Exception as e:
                logger.error(f"  {model_name} failed: {e}")
                result.model_outputs[model_name] = {"error": str(e)}
        
        # 计算共识度
        result.consensus_score = self._compute_consensus(result.model_outputs)
        
        return result
    
    def _load_prompt(self, round_num: int) -> str:
        """加载 prompt 模板"""
        if round_num == 1:
            fname = "round1_diverge.md"
        elif round_num == 2:
            fname = "round2_challenge.md"
        elif round_num == 3:
            fname = "round3_converge.md"
        else:
            fname = "round_deep.md"
        
        path = _PROMPTS_DIR / fname
        if path.exists():
            return path.read_text()
        return "Analyze the following debate topic and provide your assessment in JSON format."
    
    def _build_prompt(self, round_num: int, topic: DebateTopic,
                      previous_rounds: List[RoundResult],
                      knowledge_ctx: Dict, template: str) -> str:
        """构建完整 prompt"""
        parts = [
            f"## Round {round_num} — 辩议论题",
            f"",
            f"**议题**: {topic.title}",
            f"**类型**: {topic.topic_type.value}",
            f"**市场**: {topic.market}",
            f"**描述**: {topic.description}",
            f"",
        ]
        
        if topic.evidence:
            parts.append("### 证据数据")
            parts.append(f"```json\n{self._format_dict(topic.evidence)}\n```")
            parts.append("")
        
        if topic.injection_targets:
            parts.append("### 注入目标")
            for t in topic.injection_targets:
                parts.append(f"- {t}")
            parts.append("")
        
        # 历史知识注入 (Round 1)
        if round_num == 1 and knowledge_ctx.get("related_lessons"):
            parts.append("### 历史教训 (相关知识)")
            for lesson in knowledge_ctx["related_lessons"]:
                status = "✅已验证" if lesson.get("validated") else "⏳待验证"
                parts.append(f"- [{status}] {lesson['statement']} (confidence={lesson.get('confidence', 0):.1f})")
            parts.append("")
        
        if knowledge_ctx.get("anti_patterns"):
            parts.append("### 已知反模式")
            for p in knowledge_ctx["anti_patterns"]:
                parts.append(f"- {p}")
            parts.append("")
        
        # 前几轮输出 (Round 2+)
        if round_num >= 2 and previous_rounds:
            parts.append("### 前轮辩论输出")
            for pr in previous_rounds:
                parts.append(f"\n#### Round {pr.round_num}")
                for model, output in pr.model_outputs.items():
                    content = output.get("content", "ERROR")[:2000]
                    parts.append(f"\n**{model}**:\n{content}")
            parts.append("")
        
        # Prompt 模板
        parts.append("### 要求")
        parts.append(template)
        
        return "\n".join(parts)
    
    def _compute_consensus(self, model_outputs: Dict[str, Any]) -> float:
        """
        计算共识度 (0-1)
        
        基于各模型输出 parsed JSON 中的 action_items 重叠度
        """
        parsed_list = []
        for model, output in model_outputs.items():
            parsed = output.get("parsed")
            if isinstance(parsed, dict):
                parsed_list.append(parsed)
        
        if len(parsed_list) < 2:
            return 0.0
        
        # 提取各模型的 action_items (或 suggestions/recommendations)
        action_sets = []
        for parsed in parsed_list:
            items = set()
            for key in ["action_items", "suggestions", "recommendations", "consensus"]:
                if key in parsed and isinstance(parsed[key], list):
                    for item in parsed[key]:
                        if isinstance(item, str):
                            items.add(item.lower()[:50])  # 简化匹配
                        elif isinstance(item, dict):
                            items.add(str(item.get("action", item.get("statement", "")))[:50].lower())
            action_sets.append(items)
        
        if not action_sets or all(len(s) == 0 for s in action_sets):
            return 0.3  # 无法解析时给基础分
        
        # 计算 Jaccard 相似度 (所有对的平均)
        total_sim = 0.0
        n_pairs = 0
        for i in range(len(action_sets)):
            for j in range(i + 1, len(action_sets)):
                union = action_sets[i] | action_sets[j]
                if union:
                    sim = len(action_sets[i] & action_sets[j]) / len(union)
                    total_sim += sim
                    n_pairs += 1
        
        return total_sim / n_pairs if n_pairs > 0 else 0.0
    
    def _compile_verdict(self, debate_id: str, topic: DebateTopic,
                         rounds: List[RoundResult], consensus_score: float,
                         stop_reason: str) -> DebateVerdict:
        """编译最终结论"""
        # 提取最后一轮的共识/分歧
        last_round = rounds[-1] if rounds else None
        
        consensus_items = []
        disagreement_items = []
        action_items = []
        injections = []
        
        if last_round:
            for model, output in last_round.model_outputs.items():
                parsed = output.get("parsed", {})
                if isinstance(parsed, dict):
                    consensus_items.extend(parsed.get("consensus", []))
                    disagreement_items.extend(parsed.get("disagreements", []))
                    action_items.extend(parsed.get("action_items", []))
        
        verdict = DebateVerdict(
            debate_id=debate_id,
            topic_id=topic.topic_id,
            topic_type=topic.topic_type.value,
            market=topic.market,
            total_rounds=len(rounds),
            consensus_score=consensus_score,
            stop_reason=stop_reason,
            timestamp=datetime.now().isoformat(),
            consensus_items=consensus_items,
            disagreement_items=disagreement_items,
            action_items=action_items,
            injections=injections,
        )
        
        return verdict
    
    def _extract_factors(self, topic: DebateTopic) -> List[str]:
        """从议题中提取因子名"""
        factors = []
        for comp in topic.affected_components:
            if comp.startswith("factor:"):
                factors.append(comp.split(":")[1])
        return factors
    
    @staticmethod
    def _format_dict(d: Dict) -> str:
        """格式化字典为 JSON 字符串"""
        import json
        try:
            return json.dumps(d, indent=2, ensure_ascii=False)
        except Exception:
            return str(d)
