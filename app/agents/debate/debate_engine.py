"""
辩论引擎 - 多空研究员多轮辩论系统

使用Qwen3.6-Plus(多头)和GLM-5(空头)进行2-3轮辩论
"""
import json
import re
import logging
from typing import Dict, Any, Optional
from datetime import datetime

import requests
from pathlib import Path
from dotenv import load_dotenv

from .debate_models import DebateArgument, DebateRound, DebateSummary

# 加载环境变量
_env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

logger = logging.getLogger(__name__)


class ResearcherAgent:
    """研究员Agent (多头/空头)"""
    
    def __init__(self, side: str, model_name: str, api_key: str, base_url: str):
        """
        初始化研究员
        
        Args:
            side: "BULL" 或 "BEAR"
            model_name: 模型名称
            api_key: API密钥
            base_url: API基础URL
        """
        self.side = side
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.system_prompt = self._build_system_prompt()
    
    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        if self.side == "BULL":
            return """你是资深多头研究员,拥有100万上下文记忆。你的职责是:
1. 找出支持做多的最强理由
2. 回顾历史类似案例
3. 预判空头可能的反驳点
4. 准备充分的证据支撑观点

回答要求:
- 核心论点50字以内
- 支撑论据最多3条,每条30字以内
- 风险因素最多2条
- 信心度0.0-1.0

返回纯JSON格式:
{"core_thesis": "核心论点", "supporting_points": ["论据1", "论据2", "论据3"], "risk_factors": ["风险1", "风险2"], "confidence": 0.75}"""
        else:
            return """你是资深空头研究员,擅长批判性思维和多轮辩论。你的职责是:
1. 挖掘潜在风险、估值泡沫、宏观利空
2. 质疑多头逻辑的薄弱点
3. 挑战共识,发现被忽视的风险信号
4. 提供反面证据

回答要求:
- 核心论点50字以内
- 支撑论据最多3条,每条30字以内
- 风险因素最多2条
- 信心度0.0-1.0

返回纯JSON格式:
{"core_thesis": "核心论点", "supporting_points": ["论据1", "论据2", "论据3"], "risk_factors": ["风险1", "风险2"], "confidence": 0.75}"""
    
    def argue(self, voting_result: Dict[str, Any], market_data: Dict[str, Any], round_num: int = 1) -> DebateArgument:
        """
        首轮陈述
        
        Args:
            voting_result: 投票结果
            market_data: 市场数据
            round_num: 轮次
        
        Returns:
            DebateArgument: 辩论论点
        """
        prompt = self._build_first_round_prompt(voting_result, market_data, round_num)
        response = self._call_llm(prompt)
        
        if response:
            return self._parse_response(response)
        
        # Fallback
        return self._fallback_argument(voting_result, market_data)
    
    def rebut(self, opponent_argument: DebateArgument, market_data: Dict[str, Any]) -> DebateArgument:
        """
        反驳对方
        
        Args:
            opponent_argument: 对方论点
            market_data: 市场数据
        
        Returns:
            DebateArgument: 反驳论点
        """
        prompt = self._build_rebuttal_prompt(opponent_argument, market_data)
        response = self._call_llm(prompt)
        
        if response:
            return self._parse_response(response)
        
        # Fallback
        return self._fallback_rebuttal(opponent_argument, market_data)
    
    def final_statement(self, previous_rounds: list, market_data: Dict[str, Any]) -> DebateArgument:
        """
        最终陈述
        
        Args:
            previous_rounds: 之前的辩论轮次
            market_data: 市场数据
        
        Returns:
            DebateArgument: 最终陈述
        """
        prompt = self._build_final_prompt(previous_rounds, market_data)
        response = self._call_llm(prompt)
        
        if response:
            return self._parse_response(response)
        
        # Fallback
        return self._fallback_final(market_data)
    
    def _build_first_round_prompt(self, voting_result: Dict[str, Any], market_data: Dict[str, Any], round_num: int) -> str:
        """构建首轮提示词"""
        symbol = market_data.get("symbol") or market_data.get("ticker") or "UNKNOWN"
        
        # 安全获取价格 (可能是字典或数值)
        price_data = market_data.get("price", 0)
        if isinstance(price_data, dict):
            price = price_data.get("close", price_data.get("price", 0))
        else:
            price = float(price_data)
        
        strength = voting_result.get("final_strength", 50)
        direction = voting_result.get("direction", "NEUTRAL")
        
        side_desc = "做多" if self.side == "BULL" else "做空"
        
        # 安全获取市场数据
        vix = market_data.get("market_context", {}).get("vix", 20)
        tech = market_data.get("technical_indicators", {})
        volume_ratio = tech.get("volume_latest", 0) / tech.get("volume_avg", 1) if tech.get("volume_avg", 0) > 0 else 1.0
        
        return f"""请对 {symbol} (当前价格: ${price:.2f}) 进行{side_desc}分析。

投票结果: 综合评分 {strength}/100, 方向 {direction}

市场数据:
- VIX: {vix:.1f}
- 成交量比率: {volume_ratio:.2f}x

作为{side_desc}研究员,请给出你的核心论点、支撑论据和风险因素。"""
    
    def _build_rebuttal_prompt(self, opponent: DebateArgument, market_data: Dict[str, Any]) -> str:
        """构建反驳提示词"""
        side_desc = "多头" if self.side == "BULL" else "空头"
        opp_side = "空头" if self.side == "BULL" else "多头"
        
        return f"""{opp_side}研究员提出了以下观点:

核心论点: {opponent.core_thesis}
支撑论据:
{chr(10).join(['- ' + p for p in opponent.supporting_points])}
风险因素:
{chr(10).join(['- ' + r for r in opponent.risk_factors])}

作为{side_desc}研究员,请针对以上观点进行反驳,并强化你的立场。"""
    
    def _build_final_prompt(self, previous_rounds: list, market_data: Dict[str, Any]) -> str:
        """构建最终陈述提示词"""
        side_desc = "做多" if self.side == "BULL" else "做空"
        
        rounds_summary = f"\n经过 {len(previous_rounds)} 轮辩论后,"
        
        return f"""{rounds_summary}请作为{side_desc}研究员给出最终陈述。

总结你的核心观点,回应关键质疑,并给出最终信心度。"""
    
    def _call_llm(self, prompt: str) -> Optional[str]:
        """调用LLM API — 2026-05-21 迁移到 UnifiedLLMService

        旧: requests 直连 + enable_thinking=False 硬编码
        新: YAML llm.features.debate_engine 路由 + extra_body 透传
        """
        from app.llm.unified_service import UnifiedLLMService
        return UnifiedLLMService.call_text(
            "debate_engine", prompt,
            system=self.system_prompt,
            temperature=0.3,
            max_tokens=500,
            extra_body={"enable_thinking": False},
        )
    
    def _parse_response(self, response: str) -> DebateArgument:
        """解析LLM响应"""
        try:
            # 清理JSON
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()
            
            data = json.loads(response)
            
            return DebateArgument(
                side=self.side,
                core_thesis=data.get("core_thesis", "")[:50],
                supporting_points=data.get("supporting_points", [])[:3],
                risk_factors=data.get("risk_factors", [])[:2],
                confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
            )
        except Exception as e:
            logger.warning(f"[{self.side}] 解析失败: {e}")
            return None
    
    def _fallback_argument(self, voting_result: Dict[str, Any], market_data: Dict[str, Any]) -> DebateArgument:
        """Fallback: 规则生成论点"""
        strength = voting_result.get("final_strength", 50)
        
        if self.side == "BULL":
            thesis = f"综合评分{strength},技术指标偏向积极"
            points = ["趋势向上", "成交量放大"]
            risks = ["VIX偏高"] if market_data.get("vix", 20) > 25 else []
            confidence = 0.6
        else:
            thesis = f"综合评分{strength},存在潜在风险"
            points = ["估值偏高", "获利回吐压力"]
            risks = ["宏观不确定性"]
            confidence = 0.5
        
        return DebateArgument(
            side=self.side,
            core_thesis=thesis,
            supporting_points=points,
            risk_factors=risks,
            confidence=confidence,
        )
    
    def _fallback_rebuttal(self, opponent: DebateArgument, market_data: Dict[str, Any]) -> DebateArgument:
        """Fallback: 规则反驳"""
        if self.side == "BULL":
            return DebateArgument(
                side=self.side,
                core_thesis="多头趋势仍然成立",
                supporting_points=["技术指标支持", "基本面稳健"],
                risk_factors=["需关注风险因素"],
                confidence=0.55,
            )
        else:
            return DebateArgument(
                side=self.side,
                core_thesis="风险不容忽视",
                supporting_points=["估值压力", "技术超买"],
                risk_factors=["回调风险"],
                confidence=0.5,
            )
    
    def _fallback_final(self, market_data: Dict[str, Any]) -> DebateArgument:
        """Fallback: 最终陈述"""
        return self._fallback_argument({}, market_data)


class DebateEngine:
    """辩论引擎 - 协调多轮辩论 - P0优化: 支持快速辩论模式"""
    
    def __init__(
        self,
        max_rounds: int = 1,  # P0优化: 默认1轮快速辩论 (原3轮)
        bull_model: str = "bailian/MiniMax-M2.5",  # P0优化: MiniMax做多头情绪分析
        bear_model: str = "bailian/qwen-max",         # P0优化: qwen-max做空头分析 (DeepSeek已失效)
        fast_mode: bool = True,  # P0优化: 默认快速模式
    ):
        """
        初始化辩论引擎
        
        Args:
            max_rounds: 最大辩论轮次 (P0优化: 1轮快速模式)
            bull_model: 多头研究员模型 (P0优化: MiniMax情绪分析)
            bear_model: 空头研究员模型 (P0优化: DeepSeek推理)
            fast_mode: 快速模式 (仅1轮陈述,无反驳)
        """
        self.max_rounds = max_rounds
        self.fast_mode = fast_mode
        
        # 从环境变量读取API配置
        import os
        bailian_api_key = os.getenv("BAILIAN_API_KEY", "")
        bailian_base_url = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        
        # 提取模型ID
        bull_model_id = bull_model.split("/")[-1] if "/" in bull_model else bull_model
        bear_model_id = bear_model.split("/")[-1] if "/" in bear_model else bear_model
        
        # P0优化: 多头用MiniMax (百炼), 空头用DeepSeek
        self.bull_researcher = ResearcherAgent("BULL", bull_model_id, bailian_api_key, bailian_base_url)
        
        # DeepSeek需要独立的API配置
        if "deepseek" in bear_model.lower():
            self.bear_researcher = ResearcherAgent("BEAR", bear_model_id, deepseek_api_key, deepseek_base_url)
        else:
            self.bear_researcher = ResearcherAgent("BEAR", bear_model_id, bailian_api_key, bailian_base_url)
        
        logger.info(f"[辩论引擎-P0优化] 已初始化: 多头={bull_model_id}, 空头={bear_model_id}, 快速模式={fast_mode}")
    
    def run_debate(self, voting_result: Dict[str, Any], market_data: Dict[str, Any]) -> DebateSummary:
        """
        运行完整辩论流程 - P0优化: 支持快速模式
        
        Args:
            voting_result: 投票结果
            market_data: 市场数据
        
        Returns:
            DebateSummary: 辩论总结
        """
        rounds = []
        
        logger.info(f"[辩论引擎-P0] 开始辩论: {market_data.get('symbol', '?')}, 快速模式={self.fast_mode}")
        
        # 第1轮: 各自陈述 (始终执行)
        bull_arg = self.bull_researcher.argue(voting_result, market_data, round_num=1)
        bear_arg = self.bear_researcher.argue(voting_result, market_data, round_num=1)
        rounds.append(DebateRound(round_number=1, bull_argument=bull_arg, bear_argument=bear_arg))
        logger.info(f"[辩论引擎-P0] 第1轮完成")
        
        # P0优化: 快速模式仅1轮,跳过反驳和最终陈述
        if self.fast_mode:
            logger.info(f"[辩论引擎-P0] 快速模式,跳过反驳轮次")
        else:
            # 第2轮: 互相反驳 (仅非快速模式)
            if self.max_rounds >= 2:
                bull_rebuttal = self.bull_researcher.rebut(bear_arg, market_data)
                bear_rebuttal = self.bear_researcher.rebut(bull_arg, market_data)
                rounds.append(DebateRound(round_number=2, bull_argument=bull_rebuttal, bear_argument=bear_rebuttal))
                logger.info(f"[辩论引擎-P0] 第2轮完成")
            
            # 第3轮: 最终陈述 (仅非快速模式)
            if self.max_rounds >= 3:
                bull_final = self.bull_researcher.final_statement(rounds, market_data)
                bear_final = self.bear_researcher.final_statement(rounds, market_data)
                rounds.append(DebateRound(round_number=3, bull_argument=bull_final, bear_argument=bear_final))
                logger.info(f"[辩论引擎-P0] 第3轮完成")
        
        # 生成总结
        summary = self._generate_summary(rounds, voting_result)
        
        logger.info(f"[辩论引擎-P0] 辩论完成: {summary.recommendation}, 分歧度={summary.divergence_score:.0%}")
        
        return summary
    
    def _generate_summary(self, rounds: list, voting_result: Dict[str, Any]) -> DebateSummary:
        """生成辩论总结"""
        if not rounds:
            return DebateSummary(
                bull_case=DebateArgument(side="BULL", core_thesis="无数据", supporting_points=[], risk_factors=[], confidence=0.5),
                bear_case=DebateArgument(side="BEAR", core_thesis="无数据", supporting_points=[], risk_factors=[], confidence=0.5),
                divergence_score=0.0,
                consensus_points=[],
                key_risks=[],
                recommendation="NEUTRAL",
            )
        
        # 获取最终轮次的论点
        final_round = rounds[-1]
        bull_case = final_round.bull_argument
        bear_case = final_round.bear_argument
        
        # 计算分歧度
        divergence = abs(bull_case.confidence - bear_case.confidence)
        
        # 提取共识点
        consensus = []
        for bull_point in bull_case.supporting_points:
            for bear_point in bear_case.supporting_points:
                if self._is_similar(bull_point, bear_point):
                    consensus.append(bull_point)
        
        # 收集关键风险
        key_risks = list(set(bull_case.risk_factors + bear_case.risk_factors))
        
        # 生成建议
        recommendation = self._generate_recommendation(bull_case, bear_case, voting_result)
        
        return DebateSummary(
            bull_case=bull_case,
            bear_case=bear_case,
            divergence_score=round(divergence, 2),
            consensus_points=consensus,
            key_risks=key_risks,
            recommendation=recommendation,
            debate_rounds=rounds,
        )
    
    def _is_similar(self, text1: str, text2: str, threshold: float = 0.6) -> bool:
        """简单文本相似度检测"""
        # 简化的关键词匹配
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return False
        
        intersection = words1 & words2
        union = words1 | words2
        
        similarity = len(intersection) / len(union) if union else 0
        return similarity >= threshold
    
    def _generate_recommendation(self, bull: DebateArgument, bear: DebateArgument, voting_result: Dict[str, Any]) -> str:
        """生成投资建议"""
        strength = voting_result.get("final_strength", 50)
        bull_conf = bull.confidence
        bear_conf = bear.confidence
        
        # 综合评分和辩论信心
        if strength >= 70 and bull_conf >= 0.7:
            return "STRONG_BULL"
        elif strength >= 60 and bull_conf >= 0.6:
            return "WEAK_BULL"
        elif strength <= 30 and bear_conf >= 0.7:
            return "STRONG_BEAR"
        elif strength <= 40 and bear_conf >= 0.6:
            return "WEAK_BEAR"
        else:
            return "NEUTRAL"
