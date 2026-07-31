"""
双 Agent 辩论计划生成器 (v2 — 真正多Agent + 长期记忆注入)

Hermes Agent (qwen3.6-plus) ↔ Claude Code CLI (deepseek-v4-pro)
格式: 类 debate_phase_session.md 的多轮辩论 → 盘前计划 / 盘后归因

架构升级 (2026-05-03):
  - Claude 从 API 直调升级为 claude -p CLI Agent (具备代码/日志读取能力)
  - 新增 MemoryInjector: 整合 Lessons DB + us.json + 历史交易
  - 降级链: CLI → API fallback → Rule Engine

流程:
  Round 1: 并行独立分析 (Hermes 多头 + Claude 空头审计)
  Round 2: 交叉验证 (串行: Hermes 回应 → Claude 审计)
  Arbiter: Hermes 综合双方观点 → BattlePlan JSON
"""
import json
import logging
import os
import threading
import time
from typing import Dict, Any, Optional, Tuple
from datetime import datetime

logger = logging.getLogger("ftseek_us.debate.planner")
from app.llm.prompts.debate import DEBATE_HERMES_R1_PROMPT, DEBATE_HERMES_R2_PROMPT, DEBATE_CLAUDE_R1_PROMPT, DEBATE_CLAUDE_VALIDATION_PROMPT


# =====================================================================
# DebatePlanner
# =====================================================================

class DebatePlanner:
    """双 Agent 辩论计划生成器"""
    
    def __init__(self, agent_bridge=None, max_rounds: int = 2,
                 divergence_threshold: float = 0.4, max_total_timeout: int = 600):
        self.agent_bridge = agent_bridge  # FTSeekUSAgentBridge 实例
        self._lock = threading.Lock()
        
        # P3 优化 (2026-05-05): 从配置读取辩论参数，支持热重载
        self._load_debate_config()
        
        # 懒加载
        self._claude_runner = None       # API runner (fallback)
        self._cli_runner = None          # CLI runner (primary)
        self._memory_injector = None     # 长期记忆注入
    
    def _load_debate_config(self):
        """从 unified_config.yaml 加载辩论参数 (P3 配置化)"""
        try:
            from app.core.config_container import get_config as get_app_config
            cfg = get_app_config()
            debate_cfg = cfg.get_dotted("scheduler.debate", {})
            
            self.enabled = debate_cfg.get("enabled", True)
            self.max_rounds = debate_cfg.get("max_rounds", 2)
            self.divergence_threshold = debate_cfg.get("divergence_threshold", 0.4)
            self.max_total_timeout = debate_cfg.get("max_total_timeout_seconds", 600)
            self.hermes_model = debate_cfg.get("hermes_model", os.getenv("HERMES_DEBATE_MODEL", "qwen-max"))
            self.claude_cli_model = debate_cfg.get("claude_cli_model", "deepseek-v4-pro")
            self.claude_api_models = debate_cfg.get("claude_api_models", ["deepseek-v4-pro", "qwen-max", "minimax-m2.5"])
            self.claude_cli_timeout = debate_cfg.get("claude_cli_timeout_seconds", 180)
            self.claude_max_turns = debate_cfg.get("claude_max_turns", 5)
            
            logger.info(
                f"[DebatePlanner] 辩论配置已加载: "
                f"enabled={self.enabled}, rounds={self.max_rounds}, "
                f"timeout={self.max_total_timeout}s, divergence={self.divergence_threshold}"
            )
        except Exception as e:
            # 回退到硬编码默认值
            logger.warning(f"[DebatePlanner] 加载辩论配置失败，使用默认值: {e}")
            self.enabled = True
            self.max_rounds = 2
            self.divergence_threshold = 0.4
            self.max_total_timeout = 600
            self.hermes_model = os.getenv("HERMES_DEBATE_MODEL", "qwen-max")
            self.claude_cli_model = "deepseek-v4-pro"
            self.claude_api_models = ["deepseek-v4-pro", "qwen-max", "minimax-m2.5"]
            self.claude_cli_timeout = 180
            self.claude_max_turns = 5
    
    @property
    def claude_runner(self):
        """API Runner — 仅作为 CLI 失败时的降级"""
        if self._claude_runner is None:
            from app.agents.debate.claude_code_runner import get_claude_runner
            self._claude_runner = get_claude_runner()
        return self._claude_runner
    
    @property
    def claude_cli(self):
        """CLI Runner — 真正的 Claude Code Agent (优先使用)"""
        if self._cli_runner is None:
            from app.agents.debate.claude_cli_runner import get_cli_runner
            # P3 优化 (2026-05-05): 使用配置中的超时和轮数参数
            self._cli_runner = get_cli_runner(
                model=self.claude_cli_model,
                timeout=self.claude_cli_timeout,
                max_turns=self.claude_max_turns
            )
        return self._cli_runner
    
    @property
    def memory_injector(self):
        """长期记忆注入器"""
        if self._memory_injector is None:
            from app.agents.debate.memory_injector import get_memory_injector
            self._memory_injector = get_memory_injector()
        return self._memory_injector
    
    # =================================================================
    #  盘前辩论入口
    # =================================================================
    
    def generate_premarket_plan(self, context: dict) -> Optional[Dict]:
        """
        生成盘前作战计划 (完整辩论流程)
        
        2026-06-14: 优先使用 UnifiedDebateEngine (干净架构 + UnifiedLLMService)
        失败时回退到原有 _run_hermes/_run_claude 流程。
        """
        # ── 优先: UnifiedDebateEngine ──
        try:
            from app.agents.debate.unified_debate_engine import get_unified_debate_engine
            engine = get_unified_debate_engine()
            plan = engine.generate_premarket_plan(context)
            if plan and plan.get("targets"):
                logger.info("[DebatePlanner] ✅ UnifiedDebateEngine 成功")
                return plan
            else:
                logger.warning("[DebatePlanner] UnifiedDebateEngine 返回空，降级到旧流程")
        except Exception as e:
            logger.warning(f"[DebatePlanner] UnifiedDebateEngine 异常，降级: {e}")
        
        # ── 降级: 原有流程 ──
        return self._generate_premarket_plan_legacy(context)
    
    def _generate_premarket_plan_legacy(self, context: dict) -> Optional[Dict]:
        """原有辩论流程 (Hermes + Claude via agent_bridge/CLI)"""
        start_time = time.time()
        logger.info("[DebatePlanner] === 盘前辩论开始 ===")
        
        # 构建上下文
        ctx = self._build_context(context)
        
        # ── Round 1: 并行独立分析 ──
        hermes_r1 = None
        claude_r1 = None
        
        def run_hermes_r1():
            nonlocal hermes_r1
            hermes_r1 = self._run_hermes(ctx, round_num=1)
        
        def run_claude_r1():
            nonlocal claude_r1
            claude_r1 = self._run_claude(ctx, round_num=1)
        
        t_h = threading.Thread(target=run_hermes_r1, daemon=True, name="debate-hermes-r1")
        t_c = threading.Thread(target=run_claude_r1, daemon=True, name="debate-claude-r1")
        t_h.start()
        t_c.start()
        
        # 等待并行完成 (带超时) — 分别计算每个线程的剩余时间
        deadline = start_time + self.max_total_timeout
        for t in [t_h, t_c]:
            remaining = max(0, deadline - time.time())
            if remaining > 0:
                t.join(timeout=remaining)
            else:
                logger.warning("[DebatePlanner] 总超时，跳过等待")
        
        elapsed_r1 = time.time() - start_time
        logger.info(f"[DebatePlanner] Round 1 完成 | 耗时={elapsed_r1:.0f}s | Hermes={'✅' if hermes_r1 else '❌'} | Claude={'✅' if claude_r1 else '❌'}")
        
        # ── 计算分歧度 ──
        divergence = self._calculate_divergence(hermes_r1, claude_r1)
        logger.info(f"[DebatePlanner] 分歧度={divergence:.0%} (阈值={self.divergence_threshold:.0%})")
        
        # ── Round 2: 交叉验证 (分歧度 > 阈值时触发) ──
        hermes_r2 = None
        claude_r2 = None
        
        if divergence > self.divergence_threshold and self.max_rounds >= 2:
            logger.info("[DebatePlanner] 分歧度超过阈值，触发 Round 2 交叉验证")
            
            # 串行: 先 Hermes 回应 Claude
            if hermes_r1 and claude_r1:
                hermes_r2 = self._run_hermes_response(ctx, hermes_r1, claude_r1)
                logger.info(f"[DebatePlanner] Round 2 Hermes 回应完成 | {'✅' if hermes_r2 else '❌'}")
            
            # 然后 Claude 审计 Hermes 的回应
            if claude_r1 and hermes_r2:
                claude_r2 = self._run_claude_validation(ctx, hermes_r1, hermes_r2)
                logger.info(f"[DebatePlanner] Round 2 Claude 审计完成 | {'✅' if claude_r2 else '❌'}")
        
        # ── Arbiter: 收敛生成最终计划 ──
        final_plan = self._run_arbiter(
            hermes_result=hermes_r2 or hermes_r1,
            claude_result=claude_r2 or claude_r1,
            context=ctx,
        )
        
        total_elapsed = time.time() - start_time
        logger.info(f"[DebatePlanner] === 盘前辩论完成 === | 总耗时={total_elapsed:.0f}s")
        
        return final_plan
    
    # =================================================================
    #  Agent 调用
    # =================================================================
    
    def _run_hermes(self, context: dict, round_num: int = 1,
                    opponent: dict = None) -> Optional[Dict]:
        """
        运行 Hermes (多头策略师)
        
        2026-06-14 修复: 直接调用 UnifiedLLMService，不再依赖 agent_bridge。
        路由: debate_engine feature (primary: qwen3.7-max, fallback: deepseek-pro)
        """
        try:
            from app.agents.debate.agent_roles import (
                HERMES_PREMARKET_PROMPT_ROUND1,
                CROSS_VALIDATION_PROMPT_HERMES,
            )
            from app.llm.unified_service import UnifiedLLMService
            
            if round_num == 1:
                prompt = HERMES_PREMARKET_PROMPT_ROUND1.format(**context)
                system_prompt = DEBATE_HERMES_R1_PROMPT
            else:
                prompt = CROSS_VALIDATION_PROMPT_HERMES.format(
                    claude_analysis_json=json.dumps(opponent, ensure_ascii=False, indent=2)
                )
                system_prompt = DEBATE_HERMES_R2_PROMPT
            
            # 直接调用 UnifiedLLMService (不依赖 agent_bridge)
            res = UnifiedLLMService.call(
                "debate_engine", prompt,
                system=system_prompt or "",
                temperature=0.3,
                max_tokens=16000,
                json_mode=True,
                timeout=120,
            )
            if not res or not res.success:
                logger.warning(f"[DebatePlanner] Hermes R{round_num} 调用失败: {res.error if res else 'unknown'}")
                return None
            
            text = res.response or ""
            if isinstance(text, dict):
                return text
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                logger.warning(f"[DebatePlanner] Hermes R{round_num} JSON 解析失败: {e}")
                return None
                
        except Exception as e:
            logger.error(f"[DebatePlanner] Hermes 调用异常: {e}", exc_info=True)
            return None
    
    def _run_hermes_response(self, context: dict, hermes_r1: dict,
                              claude_r1: dict) -> Optional[Dict]:
        """Hermes 回应 Claude 的质疑"""
        return self._run_hermes(context, round_num=2, opponent=claude_r1)
    
    def _run_claude(self, context: dict, round_num: int = 1,
                    opponent: dict = None) -> Optional[Dict]:
        """
        通过 Claude Agent 运行分析
        
        优先级: CLI Agent (claude -p) → API Runner → 直调 UnifiedLLMService
        2026-06-14: 新增 Level 3 直调 fallback (deepseek-v4-pro)
        """
        try:
            from app.agents.debate.agent_roles import (
                CLAUDE_PREMARKET_PROMPT_ROUND1,
                CROSS_VALIDATION_PROMPT_CLAUDE,
            )
            
            # 注入长期记忆到 context
            enriched_context = self.enrich_with_memory(context)
            
            if round_num == 1:
                prompt = CLAUDE_PREMARKET_PROMPT_ROUND1.format(**enriched_context)
                system_prompt = DEBATE_CLAUDE_R1_PROMPT
            else:
                return self._run_claude_validation(context, hermes_r1=opponent, hermes_r2=None)
            
            # ── Level 1: CLI Agent (真正的 claude -p) ──
            cli_result = None
            try:
                cli_result = self.claude_cli.run_analysis(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    round_num=round_num,
                )
            except Exception as cli_err:
                logger.warning(f"[DebatePlanner] CLI 调用失败，降级到 API: {cli_err}")
            
            if cli_result is not None:
                logger.info("[DebatePlanner] Claude CLI Agent 成功")
                return cli_result
            
            # ── Level 2: API Runner ──
            logger.warning("[DebatePlanner] CLI 不可用，降级到 API Runner")
            try:
                api_result = self.claude_runner.run_analysis(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    round_num=round_num,
                )
                if api_result is not None:
                    logger.info("[DebatePlanner] Claude API Runner 成功")
                    return api_result
            except Exception as api_err:
                logger.warning(f"[DebatePlanner] API Runner 失败: {api_err}")
            
            # ── Level 3: 直调 UnifiedLLMService (deepseek-pro) ──
            logger.warning("[DebatePlanner] API Runner 不可用，降级到直调 LLM (claude_code feature)")
            from app.llm.unified_service import UnifiedLLMService
            res = UnifiedLLMService.call(
                "claude_code", prompt,
                system=system_prompt or "",
                temperature=0.3,
                max_tokens=16000,
                json_mode=True,
                timeout=120,
            )
            if not res or not res.success:
                logger.warning(f"[DebatePlanner] Claude 直调 LLM 失败: {res.error if res else 'unknown'}")
                return None
            
            text = res.response or ""
            if isinstance(text, dict):
                return text
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                logger.warning(f"[DebatePlanner] Claude 直调 JSON 解析失败: {e}")
                return None
            
        except Exception as e:
            logger.error(f"[DebatePlanner] Claude 调用异常: {e}", exc_info=True)
            return None
    
    def _run_claude_validation(self, context: dict, hermes_r1: dict = None,
                                hermes_r2: dict = None) -> Optional[Dict]:
        """
        Claude 审计 Hermes 的回应
        2026-06-14: CLI → API → 直调 UnifiedLLMService 三级降级
        """
        try:
            from app.agents.debate.agent_roles import CROSS_VALIDATION_PROMPT_CLAUDE
            
            prompt = CROSS_VALIDATION_PROMPT_CLAUDE.format(
                hermes_analysis_json=json.dumps(hermes_r1, ensure_ascii=False, indent=2) if hermes_r1 else "无",
                hermes_response_json=json.dumps(hermes_r2, ensure_ascii=False, indent=2) if hermes_r2 else "无回应",
            )
            
            system_prompt = DEBATE_CLAUDE_VALIDATION_PROMPT
            
            # ── Level 1: CLI Agent ──
            cli_result = None
            try:
                cli_result = self.claude_cli.run_analysis(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    round_num=2,
                )
            except Exception as cli_err:
                logger.warning(f"[DebatePlanner] CLI 验证失败，降级到 API: {cli_err}")
            
            if cli_result is not None:
                logger.info("[DebatePlanner] Claude CLI 验证成功")
                return cli_result
            
            # ── Level 2: API Runner ──
            logger.warning("[DebatePlanner] CLI 不可用，降级到 API Runner")
            try:
                api_result = self.claude_runner.run_analysis(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    round_num=2,
                )
                if api_result is not None:
                    logger.info("[DebatePlanner] Claude API 验证成功")
                    return api_result
            except Exception as api_err:
                logger.warning(f"[DebatePlanner] API Runner 验证失败: {api_err}")
            
            # ── Level 3: 直调 UnifiedLLMService (deepseek-pro) ──
            logger.warning("[DebatePlanner] API Runner 不可用，降级到直调 LLM (claude_code feature)")
            from app.llm.unified_service import UnifiedLLMService
            res = UnifiedLLMService.call(
                "claude_code", prompt,
                system=system_prompt or "",
                temperature=0.3,
                max_tokens=16000,
                json_mode=True,
                timeout=120,
            )
            if not res or not res.success:
                logger.warning(f"[DebatePlanner] Claude 直调验证失败: {res.error if res else 'unknown'}")
                return None
            
            text = res.response or ""
            if isinstance(text, dict):
                return text
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                logger.warning(f"[DebatePlanner] Claude 验证 JSON 解析失败: {e}")
                return None
            
        except Exception as e:
            logger.error(f"[DebatePlanner] Claude 验证异常: {e}", exc_info=True)
            return None
    
    def _run_arbiter(self, hermes_result: dict = None,
                      claude_result: dict = None, context: dict = None) -> Optional[Dict]:
        """
        P3 优化 (2026-05-05): 确定性规则综合替代 LLM Arbiter
        消除 Arbiter 本身的 LLM 失败风险，提高盘前计划生成可靠性。
        """
        # 记录辩论质量指标
        self._log_debate_metrics(hermes_result, claude_result, context)
        
        # 确定性综合
        return self._synthesize_results_deterministic(hermes_result, claude_result)
    
    def _synthesize_results_deterministic(self, hermes: dict, claude: dict) -> Optional[Dict]:
        """
        确定性综合: Hermes 的 targets + Claude 的 restricted
        如果双方方向冲突，降低目标置信度。
        保存双方视角和分歧点供 Dashboard 展示。
        """
        if not hermes and not claude:
            return None
        if hermes and not claude:
            merged = hermes.copy()
            merged["hermes_perspective"] = {
                "targets": hermes.get("targets", []),
                "regime_reasoning": hermes.get("regime_reasoning", ""),
                "key_disagreements": hermes.get("key_disagreements", []),
                "confidence": hermes.get("confidence", 0.5),
            }
            return merged
        
        # 以 Hermes 为基础
        merged = hermes.copy() if hermes else {}
        
        if claude:
            # 1. 合并 restricted 列表 (取并集)
            claude_restricted = set(claude.get("restricted", []))
            existing_restricted = set(merged.get("restricted", []))
            merged["restricted"] = list(existing_restricted | claude_restricted)
            
            # 2. 检查方向冲突并降低置信度，同时记录分歧
            disagreements = []
            claude_targets = {t.get("symbol"): t.get("direction", "") for t in claude.get("targets", [])}
            for t in merged.get("targets", []):
                sym = t.get("symbol", "")
                claude_dir = claude_targets.get(sym, "")
                hermes_dir = t.get("direction", "")
                
                if claude_dir and hermes_dir and claude_dir != hermes_dir:
                    orig_conf = t.get("confidence", 0.5)
                    t["confidence"] = max(orig_conf * 0.5, 0.1)
                    t["rationale"] = t.get("rationale", "") + f" [与空头观点冲突: Hermes={hermes_dir} vs Claude={claude_dir}, 置信度已降低]"
                    t["flag"] = "CONFLICT_REDUCED"
                    disagreements.append({
                        "symbol": sym,
                        "hermes_view": f"{hermes_dir} @ {t.get('entry_zone', '?')}",
                        "claude_view": f"{claude_dir}",
                        "resolution": "置信度减半，建议观望或轻仓",
                    })
            
            # 3. 检查 Hermes target 是否在 Claude bear_cases 中 (空头 vs 多头冲突)
            claude_bear_syms = {bc.get("symbol"): bc for bc in claude.get("bear_cases", [])}
            for t in merged.get("targets", []):
                sym = t.get("symbol", "")
                if sym in claude_bear_syms and t.get("direction") == "LONG":
                    bc = claude_bear_syms[sym]
                    disagreements.append({
                        "symbol": sym,
                        "hermes_view": f"LONG @ {t.get('entry_zone', '?')}",
                        "claude_view": f"SHORT (bear case: {bc.get('risk_level', '?')} risk)",
                        "resolution": "方向严重冲突，建议回避或极轻仓",
                    })
                    t["flag"] = "SEVERE_CONFLICT"
                    t["confidence"] = max(t.get("confidence", 0.5) * 0.3, 0.05)
            
            # 4. 保存 Hermes 视角 (独立记录，不被 Claude 覆盖)
            merged["hermes_perspective"] = {
                "targets": hermes.get("targets", []),
                "regime_reasoning": hermes.get("regime_reasoning", ""),
                "key_disagreements": hermes.get("key_disagreements", []),
                "responses_to_claude": hermes.get("responses_to_claude", []),
                "confidence": hermes.get("confidence", 0.5),
            }
            
            # 5. 保存分歧摘要
            merged["disagreements"] = disagreements
            merged["agreement_count"] = len([
                t for t in merged.get("targets", [])
                if t.get("flag") not in ("CONFLICT_REDUCED", "SEVERE_CONFLICT")
            ])
            merged["conflict_count"] = len(disagreements)
            
            # 6. 记录综合来源
            merged["arbiter_mode"] = "deterministic_rule_based"
            merged["claude_audit"] = claude
            merged["debate_summary"] = (
                f"Hermes 计划 ({len(hermes.get('targets', []))}标的) + "
                f"Claude 风险审计 ({len(claude_restricted)}受限, "
                f"{len(claude.get('bear_cases', []))}做空) | "
                f"共识={merged['agreement_count']} 分歧={merged['conflict_count']}"
            )
            
            logger.info(
                f"[DebatePlanner] 确定性综合完成: "
                f"{len(merged.get('targets', []))} targets, "
                f"{len(merged.get('restricted', []))} restricted, "
                f"{len(disagreements)} disagreements"
            )
        
        return merged
    
    def _log_debate_metrics(self, hermes: dict, claude: dict, context: dict = None):
        """记录辩论质量指标到日志 (供后续分析)"""
        try:
            h_targets = len(hermes.get("targets", [])) if hermes else 0
            c_targets = len(claude.get("targets", [])) if claude else 0
            h_restricted = len(hermes.get("restricted", [])) if hermes else 0
            c_restricted = len(claude.get("restricted", [])) if claude else 0
            
            # 计算分歧度
            h_syms = {t.get("symbol") for t in hermes.get("targets", [])} if hermes else set()
            c_syms = {t.get("symbol") for t in claude.get("targets", [])} if claude else set()
            common_syms = h_syms & c_syms
            
            dir_agreements = 0
            dir_disagreements = 0
            for sym in common_syms:
                h_dir = next((t["direction"] for t in hermes["targets"] if t["symbol"] == sym), "")
                c_dir = next((t["direction"] for t in claude["targets"] if t["symbol"] == sym), "")
                if h_dir and c_dir:
                    if h_dir == c_dir:
                        dir_agreements += 1
                    else:
                        dir_disagreements += 1
            
            logger.info(
                f"[DebateMetrics] targets(H={h_targets}, C={c_targets}) | "
                f"restricted(H={h_restricted}, C={c_restricted}) | "
                f"common_symbols={len(common_syms)} | "
                f"agreements={dir_agreements} | disagreements={dir_disagreements} | "
                f"arbiter=确定性规则"
            )
        except Exception as e:
            logger.debug(f"[DebateMetrics] 记录指标失败: {e}")
    
    def _synthesize_results(self, hermes: dict, claude: dict) -> Optional[Dict]:
        """保留兼容性 (不再调用)"""
        return self._synthesize_results_deterministic(hermes, claude)
    
    # =================================================================
    #  上下文构建
    # =================================================================
    
    def _build_context(self, raw_context: dict) -> dict:
        """构建格式化上下文"""
        md = raw_context.get("market_data", {})
        cap = raw_context.get("capital_state", {})
        
        # ── 催化事件注入 (RFC-Catalyst 2026-05-27) ──
        # TOMBSTONE 2026-07-31 (DEB 第5轮): 生产端 detector 零调度已死, battle plan 键恒缺失
        #   → catalyst_events 永为空, 本注入块是死代码 (08-07 后随物理删除批清理)。
        overnight_news = raw_context.get("overnight_news", "无隔夜新闻")
        catalyst_events = raw_context.get("catalyst_events", [])
        if catalyst_events:
            catalyst_lines = ["\n\n【⚡ 催化事件检测 (多模型辩论结论)】"]
            for cv in catalyst_events:
                icon = "🔴" if cv.get("direction") == "SHORT" else "🟢" if cv.get("direction") == "LONG" else "⚪"
                catalyst_lines.append(
                    f"- {icon} **{cv.get('symbol', '?')}** {cv.get('direction', '?')} "
                    f"strength={cv.get('strength', 0)} consensus={cv.get('consensus_level', 0):.0%} | "
                    f"{cv.get('reasoning', '')[:60]}"
                )
            if catalyst_events:
                catalyst_lines.append(
                    "\n⚠️ 以上催化事件由独立多模型辩论引擎检测, 请在 targets/restricted 中体现这些事件的影响。"
                )
            overnight_news = overnight_news + "\n".join(catalyst_lines)
        
        return {
            "vix": md.get("vix", "N/A"),
            "spy": md.get("spy", "N/A"),
            "qqq": md.get("qqq", "N/A"),
            "dxy": md.get("dxy", "N/A"),
            "regime": md.get("regime", "NEUTRAL"),
            "overnight_news": overnight_news,
            "earnings_info": raw_context.get("earnings_info", "本周无重要财报"),
            "positions_json": json.dumps(raw_context.get("positions", []), ensure_ascii=False, indent=2),
            "current_equity": cap.get("current_equity", "N/A"),
            "drawdown_pct": cap.get("drawdown_pct", "N/A"),
            "water_level": cap.get("water_level", "N/A"),
            "watchlist_json": json.dumps(raw_context.get("watchlist_info", [])[:10], ensure_ascii=False, indent=2),
            "memory_context": raw_context.get("memory", ""),
            "memory_section": "",  # P3 新增: 长期记忆注入
            # 2026-06-14: 注入盘前 enrichment 数据 (7 源) + 价格校准
            "premarket_enrichment": raw_context.get("premarket_enrichment", "盘前数据不可用"),
            "price_calibration": json.dumps(raw_context.get("price_calibration", {}), ensure_ascii=False, indent=2) or "价格校准数据不可用",
        }
    
    def enrich_with_memory(self, raw_context: dict) -> dict:
        """
        用长期记忆增强上下文 (P1 修复: 公共 API)
        
        注入: Lessons DB + us.json + 历史交易 + 模式识别
        """
        context = self._build_context(raw_context)
        try:
            memory_section = self.memory_injector.to_prompt_section()
            context["memory_section"] = memory_section
            logger.info(f"[DebatePlanner] 记忆注入成功 | {len(memory_section)} chars")
        except Exception as e:
            logger.warning(f"[DebatePlanner] 记忆注入失败 (不影响辩论): {e}")
            context["memory_section"] = ""
        return context
    
    # =================================================================
    #  分歧度计算
    # =================================================================
    
    def _calculate_divergence(self, hermes: dict, claude: dict) -> float:
        """计算 Hermes 和 Claude 之间的分歧度"""
        if not hermes or not claude:
            return 1.0  # 一方失败视为完全分歧
        
        # 类型保护
        hermes_targets = hermes.get("targets", [])
        claude_bear = claude.get("bear_cases", [])
        if not isinstance(hermes_targets, list):
            hermes_targets = []
        if not isinstance(claude_bear, list):
            claude_bear = []
        
        if not hermes_targets and not claude_bear:
            return 0.0  # 都无建议
        
        # 计算方向分歧
        hermes_directions = {t.get("symbol"): t.get("direction", "LONG") for t in hermes_targets if isinstance(t, dict)}
        claude_directions = {t.get("symbol"): t.get("direction", "SHORT") for t in claude_bear if isinstance(t, dict)}
        
        common_symbols = set(hermes_directions.keys()) & set(claude_directions.keys())
        if common_symbols:
            conflicts = sum(
                1 for s in common_symbols
                if hermes_directions[s] != claude_directions[s]
            )
            dir_divergence = conflicts / len(common_symbols)
        else:
            dir_divergence = 0.0  # 无共同标的，不算分歧
        
        # 基于信心度的分歧
        h_conf = hermes.get("confidence", 0.5)
        c_conf = claude.get("confidence", 0.5)
        conf_divergence = abs(h_conf - c_conf)
        
        # 区分真正的观点分歧 vs 单方失败
        # 如果一方完全失败 (None already handled above), 分歧度 = 1.0 表示需要降级而非辩论
        return max(dir_divergence, conf_divergence)
    
    # =================================================================
    #  Fallback
    # =================================================================
    
    def generate_fallback_plan(self, context: dict) -> Optional[Dict]:
        """
        降级计划: 仅 Hermes 单 Agent
        
        当辩论完全失败时使用
        """
        logger.warning("[DebatePlanner] 使用 Fallback: Hermes 单 Agent")
        ctx = self._build_context(context)
        
        try:
            from app.agents.debate.agent_roles import HERMES_PREMARKET_PROMPT_ROUND1
            prompt = HERMES_PREMARKET_PROMPT_ROUND1.format(**ctx)
            
            if self.agent_bridge:
                return self.agent_bridge._call_llm_direct(
                    prompt=prompt,
                    system_prompt="你是 FTSeek-US 美股盘前策略师。核心目标：稳定赚钱。输出严格 JSON。",
                    round_num=1,
                    model=os.getenv("HERMES_DEBATE_MODEL", getattr(self, "hermes_model", os.getenv("HERMES_DEBATE_MODEL", "qwen-max"))),
                )
        except Exception as e:
            logger.error(f"[DebatePlanner] Fallback 失败: {e}")
        
        # 最终兜底: 规则引擎
        return self.generate_rule_based_plan(context)
    # =================================================================
    #  规则引擎兜底 (Level 4)
    # =================================================================
    
    def generate_rule_based_plan(self, context: dict) -> Dict:
        """
        硬降级: 基于规则生成保守计划
        当所有 LLM 都失败时使用
        """
        logger.warning("[DebatePlanner] 使用 Level 4 兜底: 规则引擎")
        
        watchlist = context.get("watchlist_info", [])
        positions = context.get("positions", [])
        
        plan = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "market_regime": "NEUTRAL",
            "regime_reasoning": "LLM 不可用，使用保守默认策略",
            "targets": [],
            "restricted": [],
            "restricted_reasons": {},
            "position_actions": [
                {"symbol": p.get("symbol", ""), "action": "HOLD", "reason": "LLM 不可用，保守持有"}
                for p in positions
            ],
            "strategy_adjustments": ["LLM 不可用，今日采用保守策略"],
            "risk_warnings": ["LLM 分析不可用，所有交易需额外确认"],
            "debate_summary": "LLM 不可用，使用规则兜底",
            "confidence": 0.3,
        }
        
        return plan


# =====================================================================
#  模块级单例
# =====================================================================

_debate_planner: Optional[DebatePlanner] = None
_debate_planner_lock = threading.Lock()


def get_debate_planner(agent_bridge=None) -> DebatePlanner:
    global _debate_planner
    # P0 修复 (2026-05-03): 加锁防止并发初始化竞态
    with _debate_planner_lock:
        if _debate_planner is None:
            _debate_planner = DebatePlanner(agent_bridge=agent_bridge)
    return _debate_planner


def reset_debate_planner():
    """测试用: 重置单例"""
    global _debate_planner
    with _debate_planner_lock:
        _debate_planner = None
