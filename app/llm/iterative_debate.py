"""
迭代辩论控制器 - 多轮修正直到收敛或超时

设计目标:
1. 自动检查收敛度 - 低于阈值时启动下一轮
2. 聚焦分歧点 - 每轮prompt针对上一轮分歧
3. 最多3轮迭代 - 避免无限循环
4. 保留完整历史 - 记录每轮辩论结果

版本: 1.0.0
日期: 2026-05-03
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger("ftseek_us.iterative_debate")


class IterativeDebate:
    """迭代辩论控制器"""
    
    MAX_ROUNDS = 3
    CONVERGENCE_THRESHOLD = 0.4  # 收敛度阈值 (40%)
    
    def __init__(self, debate_executor):
        """
        初始化
        
        Args:
            debate_executor: DualAgentDebate实例
        """
        self.executor = debate_executor
        self.round = 0
        self.history = []
        self.all_disagreements = []
    
    async def run_iterative_debate(
        self,
        prompt: str,
        system_prompt: str,
        lessons: Optional[Dict] = None,
        max_rounds: int = None
    ) -> Dict:
        """
        运行迭代辩论直到收敛或超时
        
        Args:
            prompt: 初始prompt
            system_prompt: 系统提示
            lessons: 历史教训
            max_rounds: 最大轮次 (默认使用MAX_ROUNDS)
            
        Returns:
            最终辩论结果 + 迭代摘要
        """
        max_rounds = max_rounds or self.MAX_ROUNDS
        previous_result = None
        
        logger.info(f"[迭代辩论] 开始 | 最大轮次={max_rounds} | 收敛阈值={self.CONVERGENCE_THRESHOLD}")
        
        for round_num in range(1, max_rounds + 1):
            self.round = round_num
            
            logger.info(f"[迭代辩论] === 第{round_num}轮 ===")
            
            # 执行本轮辩论
            result = await self.executor.run_debate(
                prompt=prompt,
                system_prompt=system_prompt,
                lessons=lessons,
                round_num=round_num
            )
            
            # 获取收敛度
            convergence = result.get("debate_stats", {}).get("convergence_score", 0)
            result["round"] = round_num
            result["convergence_score"] = convergence
            self.history.append(result)
            
            # 收集分歧
            round_disagreements = result.get("divergences", [])
            if round_disagreements:
                self.all_disagreements.extend(round_disagreements)
            
            logger.info(
                f"[迭代辩论] 第{round_num}轮完成 | "
                f"收敛度={convergence:.2%} | "
                f"分歧数={len(round_disagreements)}"
            )
            
            # 检查收敛度
            if convergence >= self.CONVERGENCE_THRESHOLD:
                result["iteration_summary"] = {
                    "total_rounds": round_num,
                    "final_convergence": convergence,
                    "status": "converged",
                    "converged_at_round": round_num
                }
                logger.info(f"[迭代辩论] 已收敛 | 轮次={round_num} | 收敛度={convergence:.2%}")
                return result
            
            # 未收敛，准备下一轮
            if round_num < max_rounds:
                # 生成聚焦prompt，针对分歧点
                prompt = self._build_refocused_prompt(result, previous_result)
                logger.info(f"[迭代辩论] 准备第{round_num+1}轮 | 聚焦{len(self.all_disagreements)}个分歧点")
                previous_result = result
            else:
                # 达到最大轮次仍未收敛
                result["iteration_summary"] = {
                    "total_rounds": max_rounds,
                    "final_convergence": convergence,
                    "status": "max_rounds_reached",
                    "pending_disagreements": self.all_disagreements
                }
                logger.warning(
                    f"[迭代辩论] 达到最大轮次 | "
                    f"收敛度={convergence:.2%} < {self.CONVERGENCE_THRESHOLD:.2%} | "
                    f"未解决分歧={len(self.all_disagreements)}"
                )
        
        return result
    
    def _build_refocused_prompt(self, current: Dict, previous: Dict) -> str:
        """
        构建聚焦分歧点的下一轮prompt
        
        Args:
            current: 当前轮结果
            previous: 上一轮结果
            
        Returns:
            新的prompt
        """
        divergences = current.get("divergences", [])
        original_prompt = current.get("prompt", "")
        
        if not divergences:
            return original_prompt
        
        focus_text = "\n\n═══════════════════════════════════════════\n"
        focus_text += "【本轮聚焦 - 请重点分析以下分歧点】\n"
        focus_text += "═══════════════════════════════════════════\n\n"
        
        for i, d in enumerate(divergences, 1):
            focus_text += f"分歧点 {i}: {d.get('symbol', 'unknown')}\n"
            focus_text += f"  - Hermes观点: {d.get('hermes', 'N/A')}\n"
            focus_text += f"  - Claude观点: {d.get('claude', 'N/A')}\n"
            
            # 添加收敛建议
            focus_text += f"  → 请提供更详细的推理链来支持你的判断\n"
            focus_text += f"  → 如果可能，提供实证依据 (技术面/基本面/历史数据)\n\n"
        
        # 添加收敛要求
        focus_text += "═══════════════════════════════════════════\n"
        focus_text += "【收敛要求】\n"
        focus_text += "1. 对上述每个分歧点给出明确的判断\n"
        focus_text += "2. 提供至少2条支持证据\n"
        focus_text += "3. 如果仍坚持原观点，说明为什么对方的观点不成立\n"
        focus_text += "═══════════════════════════════════════════\n"
        
        return original_prompt + focus_text
    
    def get_debate_summary(self) -> Dict:
        """
        获取辩论摘要
        
        Returns:
            摘要字典
        """
        if not self.history:
            return {}
        
        last_result = self.history[-1]
        iteration = last_result.get("iteration_summary", {})
        
        return {
            "total_rounds": iteration.get("total_rounds", len(self.history)),
            "final_convergence": iteration.get("final_convergence", 0),
            "status": iteration.get("status", "unknown"),
            "total_disagreements": len(self.all_disagreements),
            "convergence_history": [
                h.get("convergence_score", 0) for h in self.history
            ]
        }
