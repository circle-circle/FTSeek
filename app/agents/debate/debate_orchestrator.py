"""
多Agent辩论编排器 (DebateOrchestrator)

编排 2 轮辩论流程:
  Round 1: 3个Agent独立分析 (并行)
  Cross-read: 交换观点
  Round 2: 3个Agent驳论 (并行)
  Synthesis: 辩论合成 (1次LLM调用)

输入: 事件包列表 + 市场上下文
输出: DebateSynthesis (共识方向/分歧/风险共识/标的推荐)

v2 (2026-05-05): 委托给共享的 MultiAgentRunner — 上下文构建逻辑保留在此文件，
  执行逻辑从 runner.py 复用。向后兼容。
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from .debate_agents import (
    DebateAgent, AgentOpinion, DebateSynthesis,
    create_debate_agents, SYNTHESIS_PROMPT,
)
from ..base import BaseLLMService
from ..scenarios.debate import DEBATE_SCENARIO

logger = logging.getLogger("ftseek.agents.debate_orchestrator")

ARCHIVE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "debate_archive"


class DebateOrchestrator:
    """多Agent辩论编排器 — 2轮辩论 + 合成"""

    def __init__(self, llm_config: Dict = None):
        self.agents = create_debate_agents(llm_config)
        self._llm_config = llm_config or {}
        self._fast_model = self._llm_config.get("fast_model", "qwen3.6-flash")

    async def run(self, event_packs: List[Any] = None,
                  market_context: str = "",
                  feedback_context: str = "",
                  price_data: str = "") -> DebateSynthesis:
        """执行完整辩论流程

        Args:
            event_packs: 事件包列表 (盘前新闻/分析)
            market_context: 市场环境描述 (美股收盘/ADR/期货)
            feedback_context: 历史教训/昨日复盘
            price_data: 价格技术数据 (K线/指标/量价)

        Returns:
            DebateSynthesis 辩论合成结果
        """
        t0 = time.time()
        logger.info("[辩论] === 多Agent辩论开始 ===")

        # Build contexts (debate-specific formatting logic — kept here)
        macro_ctx = self._build_macro_context(event_packs, market_context, feedback_context)
        tech_ctx = self._build_technical_context(event_packs, price_data)
        full_ctx = self._build_full_context(event_packs, market_context, feedback_context, price_data)

        # Delegate execution to shared MultiAgentRunner
        from ..runner import MultiAgentRunner
        runner = MultiAgentRunner(DEBATE_SCENARIO, self._llm_config)
        result = await runner.run({
            "macro": macro_ctx,
            "technical": tech_ctx,
            "full": full_ctx,
        })

        # Map runner result back to DebateSynthesis (backward compat)
        synth_data = result.get("synthesis", {})
        agent_opinions_list = []
        for agent_def in DEBATE_SCENARIO["agents"]:
            agent_id = agent_def["id"]
            rounds_list = result["agent_results"].get(agent_id, [])
            if rounds_list:
                agent_opinions_list.append(rounds_list[-1]["data"])

        synthesis = DebateSynthesis(
            consensus_direction=synth_data.get("consensus_direction", "NO_TRADE"),
            conviction=float(synth_data.get("conviction", 0)),
            disagreement_flags=synth_data.get("disagreement_flags", []),
            risk_consensus=synth_data.get("risk_consensus", "MEDIUM"),
            merged_symbols=[dict(s) for s in synth_data.get("merged_symbols", [])],
            debate_quality_score=float(synth_data.get("debate_quality_score", 0)),
            agent_opinions=agent_opinions_list,
            summary=synth_data.get("summary", ""),
        )

        elapsed = time.time() - t0
        logger.info(
            "[辩论] === 辩论完成 (%.1fs) === 共识=%s 质量=%.2f",
            elapsed, synthesis.consensus_direction, synthesis.debate_quality_score,
        )

        # 归档
        self._archive(synthesis, elapsed)

        return synthesis

    # ─── 数据上下文构建 (结构性分歧的关键) ──────────────────────────

    def _build_macro_context(self, assessments, market_ctx: str, feedback: str) -> str:
        """宏观视角 — 只看宏观事件 + 市场环境 + 历史教训"""
        parts = []

        if market_ctx:
            parts.append(f"【跨市场环境】\n{market_ctx[:1500]}")

        if feedback:
            parts.append(f"【历史教训】\n{feedback[:1500]}")

        if packs:
            macro_packs = [p for p in packs if self._is_macro_event(p)]
            if macro_packs:
                parts.append(f"【宏观事件 ({len(macro_packs)}条)】")
                for p in macro_packs[:5]:
                    parts.append(self._format_pack(p))
            else:
                parts.append("【宏观事件】无显著宏观事件")

        return "\n\n".join(parts) if parts else "暂无宏观数据"

    def _build_technical_context(self, assessments, price_data: str) -> str:
        """技术视角 — 只看价格/量/技术指标"""
        parts = []

        if price_data:
            parts.append(f"【量价技术数据】\n{price_data[:2000]}")

        if packs:
            tech_packs = [p for p in packs if not self._is_macro_event(p)]
            if tech_packs:
                parts.append(f"【个股事件 ({len(tech_packs)}条)】")
                for p in tech_packs[:8]:
                    parts.append(self._format_pack(p))

        return "\n\n".join(parts) if parts else "暂无技术数据"

    def _build_full_context(self, assessments, market_ctx, feedback, price_data) -> str:
        """风险视角 — 看全部信息"""
        parts = []
        if market_ctx:
            parts.append(f"【跨市场环境】\n{market_ctx[:1200]}")
        if price_data:
            parts.append(f"【量价数据】\n{price_data[:1200]}")
        if feedback:
            parts.append(f"【历史教训】\n{feedback[:1200]}")
        if packs:
            parts.append(f"【全部事件 ({len(packs)}条)】")
            for p in packs[:10]:
                parts.append(self._format_pack(p))
        return "\n\n".join(parts) if parts else "暂无数据"

    @staticmethod
    def _is_macro_event(pack) -> bool:
        """判断是否为宏观事件"""
        title = getattr(pack, "title", "") or getattr(pack, "headline", "") or ""
        industry = getattr(pack, "industry", "") or ""
        macro_keywords = ["央行", "美联储", "利率", "GDP", "CPI", "PMI", "政策", "监管",
                          "制裁", "关税", "汇率", "原油", "黄金", "地缘", "南向", "资金流",
                          "指数", "大盘", "板块轮动", "港股通", "A股"]
        return any(kw in title for kw in macro_keywords) or any(kw in industry for kw in macro_keywords)

    @staticmethod
    def _format_pack(pack) -> str:
        title = getattr(pack, "title", "") or getattr(pack, "headline", "") or ""
        body = (getattr(pack, "content", "") or getattr(pack, "summary", "") or "")[:300]
        symbols = ", ".join(getattr(pack, "focus_symbols", [])[:5])
        source = getattr(pack, "source", "")
        return f"- {title}\n  标的: {symbols}\n  来源: {source}\n  内容: {body}"

    # ─── 合成 ──────────────────────────────────────────────────────

    async def _synthesize(self, macro: AgentOpinion, tech: AgentOpinion,
                          risk: AgentOpinion) -> DebateSynthesis:
        """合成辩论结果"""
        prompt = SYNTHESIS_PROMPT.format(
            macro_view=json.dumps(macro.to_dict(), ensure_ascii=False, indent=2),
            tech_view=json.dumps(tech.to_dict(), ensure_ascii=False, indent=2),
            risk_view=json.dumps(risk.to_dict(), ensure_ascii=False, indent=2),
        )
        messages = [
            {"role": "system", "content": "你是公正的辩论主持人。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]

        try:
            from app.core.unified_llm_factory import get_llm_client
            client = get_llm_client(self._llm_config)
            if not client:
                return self._fallback_synthesis(macro, tech, risk)

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._fast_model, messages=messages,
                ),
                timeout=60,
            )
            raw = response.choices[0].message.content or "{}"
            try:
                await client.close()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[辩论] 合成 LLM 调用失败: {e}")
            return self._fallback_synthesis(macro, tech, risk)

        # 解析
        cleaned = re.sub(r'^```json\s*', '', raw, flags=re.MULTILINE)
        cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.MULTILINE)
        try:
            data = json.loads(cleaned.strip())
        except json.JSONDecodeError:
            return self._fallback_synthesis(macro, tech, risk)

        return DebateSynthesis(
            consensus_direction=data.get("consensus_direction", "NO_TRADE"),
            conviction=float(data.get("conviction", 0)),
            disagreement_flags=data.get("disagreement_flags", []),
            risk_consensus=data.get("risk_consensus", "MEDIUM"),
            merged_symbols=data.get("merged_symbols", []),
            debate_quality_score=float(data.get("debate_quality_score", 0)),
            agent_opinions=[macro.to_dict(), tech.to_dict(), risk.to_dict()],
            summary=data.get("summary", ""),
        )

    def _fallback_synthesis(self, macro, tech, risk) -> DebateSynthesis:
        """降级合成 (无LLM时使用规则)"""
        directions = [macro.direction, tech.direction, risk.direction]
        no_trades = sum(1 for d in directions if d == "NO_TRADE")
        buys = sum(1 for d in directions if d == "BUY")
        sells = sum(1 for d in directions if d == "SELL")

        if no_trades >= 2:
            consensus = "NO_TRADE"
        elif buys > sells:
            consensus = "BUY"
        elif sells > buys:
            consensus = "SELL"
        else:
            consensus = "NO_TRADE"

        return DebateSynthesis(
            consensus_direction=consensus,
            conviction=0.1,
            disagreement_flags=["降级合成 (LLM不可用)"],
            risk_consensus="HIGH",
            merged_symbols=[],
            debate_quality_score=0.0,
            agent_opinions=[macro.to_dict(), tech.to_dict(), risk.to_dict()],
            summary="辩论合成不可用 (LLM调用失败)，降级为规则投票",
        )

    # ─── 归档 ──────────────────────────────────────────────────────

    def _archive(self, synthesis: DebateSynthesis, elapsed: float):
        """归档辩论记录"""
        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            path = ARCHIVE_DIR / f"{date_str}.json"

            existing = {}
            if path.exists():
                existing = json.loads(path.read_text())

            round_key = f"round_{len(existing) + 1}_{datetime.now().strftime('%H%M%S')}"
            existing[round_key] = {
                "timestamp": time.time(),
                "elapsed_sec": round(elapsed, 1),
                "synthesis": synthesis.to_dict(),
            }

            path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
            logger.info(f"[辩论] 已归档: {path}")
        except Exception as e:
            logger.warning(f"[辩论] 归档失败: {e}")

    # ─── 日志 ──────────────────────────────────────────────────────

    @staticmethod
    def _log_opinion(o: AgentOpinion, round_num: int):
        logger.info(
            f"[辩论 R{round_num}] {o.agent_name}: {o.direction} "
            f"conf={o.confidence:.2f} symbols={o.top_symbols[:3]}"
        )


# ════════════════════════════════════════════════════════════════════
#  便捷函数 (同步包装, 用于非async上下文)
# ════════════════════════════════════════════════════════════════════

def run_debate_sync(assessments: List[Any] = None,
                    market_context: str = "",
                    feedback_context: str = "",
                    price_data: str = "",
                    llm_config: Dict = None) -> DebateSynthesis:
    """同步运行辩论 (用于调度器/非async线程)

    P0 fix: 使用 assessments 替代 event_packs.\n    v2: 委托给共享的 MultiAgentRunner.run_sync().
    """
    orchestrator = DebateOrchestrator(llm_config)
    macro_ctx = orchestrator._build_macro_context(assessments, market_context, feedback_context)
    tech_ctx = orchestrator._build_technical_context(assessments, price_data)
    full_ctx = orchestrator._build_full_context(assessments, market_context, feedback_context, price_data)

    from ..runner import run_scenario_sync
    result = run_scenario_sync(
        DEBATE_SCENARIO,
        {"macro": macro_ctx, "technical": tech_ctx, "full": full_ctx},
        llm_config,
        timeout=180,
    )

    synth_data = result.get("synthesis", {})
    agent_opinions_list = []
    for agent_def in DEBATE_SCENARIO["agents"]:
        agent_id = agent_def["id"]
        rounds_list = result["agent_results"].get(agent_id, [])
        if rounds_list:
            agent_opinions_list.append(rounds_list[-1]["data"])

    return DebateSynthesis(
        consensus_direction=synth_data.get("consensus_direction", "NO_TRADE"),
        conviction=float(synth_data.get("conviction", 0)),
        disagreement_flags=synth_data.get("disagreement_flags", []),
        risk_consensus=synth_data.get("risk_consensus", "MEDIUM"),
        merged_symbols=[dict(s) for s in synth_data.get("merged_symbols", [])],
        debate_quality_score=float(synth_data.get("debate_quality_score", 0)),
        agent_opinions=agent_opinions_list,
        summary=synth_data.get("summary", ""),
    )
