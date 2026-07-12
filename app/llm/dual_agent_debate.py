"""
双Agent辩论模块 - 直接嵌入premarket_planner.py和eod_attribution.py

设计原则:
1. 不创建适配层，直接修改现有代码
2. 使用agent_bridge._call_llm_direct()，不用delegate_task
3. 保留现有逻辑，仅叠加辩论功能
4. 降级策略：双Agent失败时回退到单模型
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger("ftseek_us.dual_agent_debate")


class DualAgentDebate:
    """双Agent辩论 - 用于盘前计划和盘后归因

    v3.0 (2026-05-06): 新增 HermesClient 驱动的多模型辩论
    """

    def __init__(self, agent_bridge, config=None):
        self.agent_bridge = agent_bridge
        self.config = config or {}
        self.timeout = self.config.get("timeout", 600)
        self._client = None
        self._client_ok = False
    
    async def run_debate(self, prompt: str, system_prompt: str, 
                        lessons: Optional[Dict] = None,
                        round_num: int = 1) -> Dict:
        """
        运行双Agent辩论
        
        Args:
            prompt: 主prompt
            system_prompt: 系统提示
            lessons: 历史教训（可选）
            round_num: 辩论轮次
        
        Returns:
            {
                "hermes_result": {...},
                "claude_result": {...},
                "converged_result": {...},
                "debate_stats": {...}
            }
        """
        logger.info(f"[辩论] 开始第{round_num}轮辩论")
        
        # 1. 并行调用双Agent
        hermes_task = asyncio.create_task(
            self._call_hermes(prompt, system_prompt, lessons)
        )
        claude_task = asyncio.create_task(
            self._call_claude(prompt, system_prompt, lessons)
        )
        
        # 2. 等待结果（带超时）
        try:
            hermes_result, claude_result = await asyncio.wait_for(
                asyncio.gather(hermes_task, claude_task, return_exceptions=True),
                timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.warning("[辩论] 双Agent超时，降级为单模型")
            return await self._fallback_single(prompt, system_prompt, lessons)
        
        # 3. 处理异常
        hermes_ok = not isinstance(hermes_result, Exception)
        claude_ok = not isinstance(claude_result, Exception)
        
        if not hermes_ok:
            logger.error(f"[辩论] Hermes失败: {hermes_result}")
        if not claude_ok:
            logger.error(f"[辩论] Claude失败: {claude_result}")
        
        # 4. 降级策略
        if not hermes_ok and not claude_ok:
            logger.warning("[辩论] 双Agent都失败，降级为单模型")
            return await self._fallback_single(prompt, system_prompt, lessons)
        
        if not hermes_ok:
            logger.info("[辩论] 仅Claude成功，使用Claude结果")
            return {"result": claude_result, "mode": "claude_only"}
        
        if not claude_ok:
            logger.info("[辩论] 仅Hermes成功，使用Hermes结果")
            return {"result": hermes_result, "mode": "hermes_only"}
        
        # 5. 收敛合并
        converged = self._converge_results(hermes_result, claude_result, round_num)
        
        return {
            "hermes_result": hermes_result,
            "claude_result": claude_result,
            "converged_result": converged,
            "debate_stats": {
                "round": round_num,
                "hermes_success": True,
                "claude_success": True,
                "convergence_score": self._calculate_convergence(hermes_result, claude_result)
            }
        }
    
    # ── v3.0 HermesClient 驱动的辩论 ──

    def _init_hermes_client(self) -> bool:
        """延迟初始化 HermesClient"""
        if self._client_ok and self._client is not None:
            return True
        try:
            from app.llm.hermes_client import HermesClient
            self._client = HermesClient()
            if self._client.ping():
                self._client_ok = True
                logger.info("[辩论] HermesClient 已连接")
                return True
        except Exception as e:
            logger.warning("[辩论] HermesClient 初始化失败: %s", e)
        self._client_ok = False
        return False

    def run_agent_debate(self, topic: str, prompt: str = None,
                         roles: List[Dict] = None,
                         judge_prompt: str = None) -> Dict:
        """
        v3.0: 通过 HermesClient 运行多角色辩论

        Args:
            topic: 辩论主题
            prompt: 发给分析师的具体问题 (默认使用 topic)
            roles: 分析师角色列表 (默认 3 角色)
            judge_prompt: 法官综合提示

        Returns:
            {"verdict": "...", "analysts": [...], "mode": "hermes_debate"}
        """
        default_roles = [
            {"name": "技术分析师",
             "system": "你是纯粹的技术分析专家，看K线、指标、量价关系。倾向用技术面证据做判断。"},
            {"name": "宏观分析师",
             "system": "你是宏观经济学家，关注利率、通胀、地缘政治、资金流向。倾向用宏观面证据做判断。"},
            {"name": "情绪分析师",
             "system": "你是市场情绪/行为金融专家，关注期权链、做空比率、新闻情绪、散户热度。倾向用情绪面证据做判断。"},
        ]

        if self._init_hermes_client():
            try:
                result = self._client.debate(
                    topic=topic,
                    debate_prompt=prompt or topic,
                    roles=roles or default_roles,
                    judge_prompt=judge_prompt,
                    timeout=self.timeout,
                )
                if result:
                    return {
                        "verdict": result.get("final_response", ""),
                        "analysts": result.get("analysts", []),
                        "tokens": result.get("total_tokens", 0),
                        "mode": "hermes_debate",
                    }
            except Exception as e:
                logger.warning("[辩论] HermesClient debate 异常: %s", e)

        # 降级到旧的双路 API 调用
        logger.info("[辩论] 降级到双路 API 直连模式")
        return {"mode": "fallback", "note": "使用 run_debate() 或单模型降级"}

    async def run_agent_debate_async(self, topic: str, **kwargs) -> Dict:
        """run_agent_debate 的异步包装器"""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.run_agent_debate(topic, **kwargs)
        )

    # ── 原有双路 API 调用 (降级保留) ──

    async def _call_hermes(self, prompt: str, system_prompt: str,
                          lessons: Optional[Dict]) -> Dict:
        """调用Hermes (qwen-max)"""
        try:
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            
            enhanced_prompt = self._inject_lessons(prompt, lessons) if lessons else prompt
            
            # 在线程池中执行同步的HTTP调用 (兼容Python 3.11)
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    lambda: self.agent_bridge._call_llm_direct(
                        prompt=enhanced_prompt,
                        system_prompt=system_prompt,
                        model="qwen-max",
                        round_num="premarket_debate"
                    )
                )
            
            logger.info("[辩论] Hermes调用成功")
            return result
        except Exception as e:
            logger.error(f"[辩论] Hermes调用失败: {e}")
            raise
    
    async def _call_claude(self, prompt: str, system_prompt: str, 
                          lessons: Optional[Dict]) -> Dict:
        """调用Claude (deepseek-v4-pro)"""
        try:
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            
            # 使用不同的系统提示，让Claude做风险审计视角
            claude_system = system_prompt + "\n\n**你的角色**: 你是独立审查员（红队），请从风险和挑战角度审查计划。"
            
            enhanced_prompt = self._inject_lessons(prompt, lessons) if lessons else prompt
            
            # 在线程池中执行 (兼容Python 3.11)
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    lambda: self.agent_bridge._call_llm_direct(
                        prompt=enhanced_prompt,
                        system_prompt=claude_system,
                        model="deepseek-v4-pro",
                        round_num="premarket_debate"
                    )
                )
            
            logger.info("[辩论] Claude调用成功")
            return result
        except Exception as e:
            logger.error(f"[辩论] Claude调用失败: {e}")
            raise
    
    def _converge_results(self, hermes: Dict, claude: Dict, round_num: int) -> Dict:
        """
        收敛合并双Agent结果
        
        策略:
        1. 提取共识项 (基于symbol)
        2. 识别分歧项
        3. 综合双方优势
        """
        converged = {}
        
        # 1. 提取共识项 (基于symbol比较)
        def get_symbols(data):
            targets = data.get("targets", [])
            if isinstance(targets, list):
                return set(t.get("symbol", "") for t in targets if isinstance(t, dict) and t.get("symbol"))
            return set()
        
        hermes_symbols = get_symbols(hermes)
        claude_symbols = get_symbols(claude)
        consensus_symbols = hermes_symbols & claude_symbols
        
        # 提取共识目标的完整数据
        converged["consensus_targets"] = [
            t for t in hermes.get("targets", []) 
            if isinstance(t, dict) and t.get("symbol") in consensus_symbols
        ]
        converged["consensus_count"] = len(consensus_symbols)
        
        # 2. 独家发现
        converged["hermes_exclusive"] = [
            t for t in hermes.get("targets", [])
            if isinstance(t, dict) and t.get("symbol") in (hermes_symbols - claude_symbols)
        ]
        converged["claude_exclusive"] = [
            t for t in claude.get("targets", [])
            if isinstance(t, dict) and t.get("symbol") in (claude_symbols - hermes_symbols)
        ]
        
        # 3. 综合计划（优先共识，补充独家高置信度项）
        converged_targets = list(converged["consensus_targets"])
        
        # 添加Claude的风险警示
        if "risk_warnings" in claude:
            converged["risk_warnings"] = claude["risk_warnings"]
        
        # 添加Hermes的机会识别
        if "opportunities" in hermes:
            converged["opportunities"] = hermes["opportunities"]
        
        # 4. 分歧处理
        divergences = []
        all_symbols = hermes_symbols | claude_symbols
        
        for sym in all_symbols:
            h_view = next((t for t in hermes.get("targets", []) if isinstance(t, dict) and t.get("symbol") == sym), {})
            c_view = next((t for t in claude.get("targets", []) if isinstance(t, dict) and t.get("symbol") == sym), {})
            
            if h_view.get("direction") != c_view.get("direction"):
                divergences.append({
                    "symbol": sym,
                    "hermes": h_view.get("direction"),
                    "claude": c_view.get("direction"),
                    "resolution": "conservative"  # 分歧时采用保守策略
                })
        
        converged["divergences"] = divergences
        converged["divergence_count"] = len(divergences)
        
        # 6. 合并为标准格式 (确保包含 'targets' 键供下游系统使用)
        all_targets = converged_targets + converged["hermes_exclusive"] + converged["claude_exclusive"]
        converged["targets"] = all_targets
        
        # 5. 添加元数据
        converged["debate_round"] = round_num
        converged["timestamp"] = datetime.now().isoformat()
        converged["mode"] = "converged"
        
        return converged
    
    def _calculate_convergence(self, hermes: Dict, claude: Dict) -> float:
        """计算收敛度 (0.0-1.0)"""
        def get_symbols(data):
            targets = data.get("targets", [])
            if isinstance(targets, list):
                return set(t.get("symbol", "") for t in targets if isinstance(t, dict) and t.get("symbol"))
            return set()
            
        hermes_symbols = get_symbols(hermes)
        claude_symbols = get_symbols(claude)
        
        if not hermes_symbols and not claude_symbols:
            return 0.0
        
        intersection = hermes_symbols & claude_symbols
        union = hermes_symbols | claude_symbols
        
        return len(intersection) / len(union) if union else 0.0
    
    def _inject_lessons(self, prompt: str, lessons: Dict) -> str:
        """注入历史教训到prompt"""
        if not lessons:
            return prompt
        
        lesson_text = "\n\n## 历史教训（必须参考）\n"
        for lesson in lessons.get("lessons", [])[:5]:  # 最多5条
            lesson_text += f"- {lesson.get('text', '')}\n"
        
        return prompt + lesson_text
    
    async def _fallback_single(self, prompt: str, system_prompt: str, 
                              lessons: Optional[Dict]) -> Dict:
        """降级为单模型"""
        try:
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            
            # 在线程池中执行
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    lambda: self.agent_bridge._call_llm_direct(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        model=self.config.get("fallback_model", "qwen-max"),
                        round_num="premarket_fallback"
                    )
                )
            
            return {
                "result": result,
                "mode": "fallback_single",
                "warning": "双Agent失败，降级为单模型"
            }
        except Exception as e:
            logger.error(f"[辩论] 降级单模型也失败: {e}")
            raise
    
    @staticmethod
    def merge_eod_results(hermes: Dict, claude: Dict) -> Dict:
        """
        盘后归因结构化合并
        
        替代原有的简单深拷贝合并
        """
        merged = {
            "date": hermes.get("date") or claude.get("date"),
            "consensus": [],
            "divergences": [],
            "exclusive_findings": {
                "hermes": [],
                "claude": []
            },
            "systemic_insights": {},
            "next_day_hints": {}
        }
        
        # 1. 提取共识
        hermes_insights = set(hermes.get("systemic_insights", {}).keys())
        claude_insights = set(claude.get("systemic_insights", {}).keys())
        consensus_keys = hermes_insights & claude_insights
        
        for key in consensus_keys:
            merged["consensus"].append({
                "insight": key,
                "hermes_value": hermes["systemic_insights"][key],
                "claude_value": claude["systemic_insights"][key]
            })
        
        # 2. 识别分歧
        for key in hermes_insights - claude_insights:
            merged["exclusive_findings"]["hermes"].append({
                "insight": key,
                "value": hermes["systemic_insights"][key]
            })
        
        for key in claude_insights - hermes_insights:
            merged["exclusive_findings"]["claude"].append({
                "insight": key,
                "value": claude["systemic_insights"][key]
            })
        
        # 3. 合并systemic_insights（Hermes为基础，补充Claude）
        merged["systemic_insights"] = {**hermes.get("systemic_insights", {})}
        merged["systemic_insights"].update(claude.get("systemic_insights", {}))
        
        # 4. 合并次日提示
        merged["next_day_hints"] = {
            **hermes.get("next_day_hints", {}),
            **claude.get("next_day_hints", {})
        }
        
        return merged
