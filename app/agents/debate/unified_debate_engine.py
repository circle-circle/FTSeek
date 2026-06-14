"""
统一辩论引擎 (Unified Debate Engine)
======================================

2026-06-14: 替代原 debate_planner 中的 _run_hermes/_run_claude 直接调用。
所有 LLM 调用通过 UnifiedLLMService，可随时在 YAML 中更换模型。

架构:
┌──────────────────────────────────────────────────────────────┐
│                    UnifiedDebateEngine                        │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  Role 1: BullAnalyst (多头策略师)                             │
│    feature: debate_bull (qwen-max → deepseek-pro)            │
│    职责: 识别做多机会、构建因果链、引用数据                      │
│                                                              │
│  Role 2: BearAnalyst (空头审计师)                             │
│    feature: debate_bear (deepseek-pro → qwen-max)            │
│    职责: 发现风险、质疑逻辑、做空分析                           │
│                                                              │
│  Role 3: Arbiter (仲裁综合)                                   │
│    feature: debate_arbiter (qwen-max → deepseek-pro)         │
│    职责: 综合双方观点、生成分歧摘要、输出最终计划               │
│                                                              │
│  Message Flow:                                               │
│    R1: Bull + Bear 并行分析同一上下文                         │
│    R2: 互相看到对方输出，回应质疑 (分歧>阈值时触发)            │
│    Arbiter: 看到 R1+R2 全部输出，生成共识计划                 │
│                                                              │
│  IC Tracking:                                                │
│    每次辩论记录 feature/model/consensus/disagreement          │
│    供 daily_ic_analysis.py 按 LLM 特征分组统计                │
│                                                              │
└──────────────────────────────────────────────────────────────┘
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek_us.unified_debate_engine")

# ═══════════════════════════════════════════════════════════════
# Role Definitions
# ═══════════════════════════════════════════════════════════════

ROLE_BULL = {
    "name": "BullAnalyst",
    "feature": "debate_bull",
    "description": "多头策略师: 识别做多机会，构建因果链",
    "system_prompt": """你是 FTSeek-US 多头策略师。核心目标: 找到高概率做多机会。

【推理要求 — 违反即作废】
1. 数据引用: 至少引用 2 个具体数据点 (VIX/板块/国债/盘前异动)
2. 因果链: 数据 → 行业传导 → 个股逻辑 (3步以上)
3. 反证: 什么情况下这个判断会错？
4. 入场触发: 具体价格/形态/量能确认条件
5. 放弃条件: 什么情况下必须止损离场

❌ 禁止: "股池权重股"、"区间震荡策略"、"逢低做多" 等无数据套话
✅ 必须: "XLK+0.88%领涨, ARM盘前+11.3% → 半导体动量 → NVDA作为龙头可能受益"

【发散思维引导】
1. 新闻 → 行业 → 个股 (例: "伊朗停火 → 原油跌 → 做空 XOM")
2. 板块轮动信号 → 领先板块标的
3. 国债曲线形态 → 风格偏好
4. 分析师共识变化 → 动量确认
5. 盘前异动 → 催化事件""",
}

ROLE_BEAR = {
    "name": "BearAnalyst",
    "feature": "debate_bear",
    "description": "空头审计师: 发现风险、质疑逻辑、做空分析",
    "system_prompt": """你是 FTSeek-US 空头审计师。核心目标: 发现风险、验证逻辑、找到做空机会。

【推理要求 — 违反即作废】
1. 数据引用: 至少引用 2 个具体数据点
2. 反证链: 做空逻辑的 3 步因果链
3. 风险: 什么情况下做空会失败？
4. 持仓诊断: 逐一检查现有持仓的风险点

❌ 禁止: "高位阻力"、"技术破位" 等无数据套话
✅ 必须: "AAPL分析师评级从93%降至69%, 盘前无异动 → 缺乏下跌催化 → 做空风险高"

【审计维度】
1. 做多逻辑的漏洞 (数据是否支撑? 因果链是否断裂?)
2. 持仓集中度 (科技/中国/单一行业暴露)
3. 财报/新闻催化风险
4. 系统性风险 (VIX/国债/板块分化)""",
}

ROLE_ARBITER = {
    "name": "Arbiter",
    "feature": "debate_arbiter",
    "description": "仲裁综合: 合并双方观点，生成分歧摘要和最终计划",
    "system_prompt": """你是 FTSeek-US 首席仲裁师。综合多头和空头的分析，生成最终作战计划。

【综合原则】
1. 双方共识 → 高置信度目标
2. 方向冲突 → 降低置信度或回避
3. 严重冲突 (多头LONG vs 空头bear_case) → 标记 SEVERE_CONFLICT，建议回避
4. 综合 restricted 列表 (取并集)
5. 保留双方的独立观点 (key_disagreements)

【输出质量】
- 每个 target 必须有 trigger_condition + invalidation
- risk_reward > 2:1 才保留
- confidence 基于共识程度: 共识=0.7+, 分歧=0.3-, 严重冲突=0.1""",
}

# ═══════════════════════════════════════════════════════════════
# Prompt Templates
# ═══════════════════════════════════════════════════════════════

BULL_R1_TEMPLATE = """你是 FTSeek-US 多头策略师，正在生成今日作战计划。

{memory_section}

【历史记忆与教训】
{memory_context}

【市场数据】
VIX: {vix}
SPY: {spy}
QQQ: {qqq}
DXY: {dxy}
市场阶段: {regime}

【盘前市场全景 (实时数据)】
{premarket_enrichment}

【隔夜新闻】
{overnight_news}

【财报日历】
{earnings_info}

【当前持仓】
{positions_json}

【资金状态】
权益: {current_equity}
回撤: {drawdown_pct}%
水位: {water_level}

【股池】
{watchlist_json}

【个股价格校准】
{price_calibration}

═══════════════════════════════════════════
【任务】生成多头作战计划
═══════════════════════════════════════════

1. 市场阶段判断 (2-3句话，引用具体数据)
2. 识别做多目标 (最多 5 只)
3. 标注回避标的
4. 对现有持仓逐一给出操作建议
5. 策略调整建议 (基于教训+当日环境)
6. 今日风险点

输出严格 JSON:
{{
  "date": "YYYY-MM-DD",
  "market_regime": "RISK_ON/RISK_OFF/ROTATION/NEUTRAL",
  "regime_reasoning": "引用具体数据的判断逻辑",
  "targets": [
    {{
      "symbol": "US.NVDA",
      "direction": "LONG",
      "entry_zone": [142.0, 144.5],
      "stop_loss": 139.0,
      "take_profit": 152.0,
      "urgency": "HIGH/MEDIUM/LOW",
      "rationale": "数据引用+因果链(3步)+反证+历史参照",
      "data_cited": ["板块轮动", "盘前异动"],
      "trigger_condition": "入场触发: 具体价格/形态/量能确认",
      "risk_reward": 2.7,
      "position_size_pct": 5,
      "invalidation": "放弃条件: 跌破$X且不回收"
    }}
  ],
  "restricted": ["US.TSLA"],
  "restricted_reasons": {{"US.TSLA": "回避原因"}},
  "position_actions": [
    {{"symbol": "US.QQQ", "action": "HOLD/ADJUST_SL/EXIT/ADD", "reason": "操作理由", "priority": "URGENT/NORMAL/LOW"}}
  ],
  "strategy_adjustments": ["基于教训的具体调整"],
  "risk_warnings": ["今日风险点"],
  "key_disagreements": ["你认为市场会怎样但大多数人可能不认同的观点"],
  "confidence": 0.75
}}"""

BEAR_R1_TEMPLATE = """你是 FTSeek-US 空头审计师，正在审计今日市场风险和持仓安全。

{memory_section}

【市场数据】
VIX: {vix}
SPY: {spy}
QQQ: {qqq}
DXY: {dxy}
市场阶段: {regime}

【盘前市场全景 (实时数据)】
{premarket_enrichment}

【隔夜新闻】
{overnight_news}

【财报日历】
{earnings_info}

【当前持仓】
{positions_json}

【资金状态】
权益: {current_equity}
回撤: {drawdown_pct}%

【股池】
{watchlist_json}

【个股价格校准】
{price_calibration}

═══════════════════════════════════════════
【任务】独立风险审计 + 做空分析
═══════════════════════════════════════════

1. 做空机会 (最多 3 只)
2. 持仓风险逐一检查
3. 回避名单 (引用具体数据)
4. 系统性风险

输出严格 JSON:
{{
  "bear_cases": [
    {{
      "symbol": "US.XXX",
      "direction": "SHORT",
      "entry_zone": [0, 0],
      "stop_loss": 0,
      "take_profit": 0,
      "rationale": "数据引用+反证链(3步)+风险",
      "data_cited": ["具体数据"],
      "risk_level": "HIGH/MEDIUM/LOW"
    }}
  ],
  "position_risks": [
    {{
      "symbol": "US.QQQ",
      "risk_type": "EARNINGS/TECHNICAL/CONCENTRATION/NEWS",
      "description": "具体风险描述",
      "suggested_action": "HOLD/ADJUST_SL/EXIT/REDUCE"
    }}
  ],
  "restricted": ["US.TSLA"],
  "restricted_reasons": {{"US.TSLA": "具体数据原因"}},
  "systemic_risks": ["系统性风险描述"],
  "market_bias": "BULLISH/BEARISH/NEUTRAL",
  "confidence": 0.75
}}"""

R2_RESPONSE_TEMPLATE = """你是 FTSeek-US {role_name}。

以下是对方对你 R1 分析的质疑:

【对方分析结果】
{opponent_output}

═══════════════════════════════════════════
【任务】回应质疑 + 修正计划
═══════════════════════════════════════════

1. 回应对方的每一个风险点 (AGREE/DISAGREE + 理由)
2. 如果对方发现了你计划中的漏洞，修正
3. 重新评估被对方标记的 restricted 标的
4. 更新风险警告

输出严格 JSON:
{{
  "responses": [
    {{
      "opponent_point": "对方原话要点",
      "response": "AGREE/DISAGREE + 理由",
      "plan_adjustment": "如有调整"
    }}
  ],
  "updated_targets": [],
  "updated_restricted": [],
  "updated_position_actions": [],
  "final_risk_warnings": [],
  "final_confidence": 0.75
}}"""

ARBITER_TEMPLATE = """你是 FTSeek-US 首席仲裁师。综合多头和空头的分析，生成最终作战计划。

【多头策略师 (R1) 分析】
{bull_r1}

【空头审计师 (R1) 分析】
{bear_r1}

{r2_section}

【市场数据摘要】
{market_summary}

═══════════════════════════════════════════
【任务】生成最终共识计划
═══════════════════════════════════════════

1. 合并双方 targets (方向冲突 → 降低置信度或标记 SEVERE_CONFLICT)
2. 合并 restricted (取并集)
3. 识别分歧点 (disagreements)
4. 生成最终 position_actions
5. 保留双方的 hermes_perspective / claude_audit 原始输出

输出严格 JSON:
{{
  "date": "YYYY-MM-DD",
  "market_regime": "RISK_ON/RISK_OFF/ROTATION/NEUTRAL",
  "regime_reasoning": "综合双方判断的最终市场阶段分析",
  "targets": [
    {{
      "symbol": "US.XXX",
      "direction": "LONG/SHORT",
      "entry_zone": [0, 0],
      "stop_loss": 0,
      "take_profit": 0,
      "urgency": "HIGH/MEDIUM/LOW",
      "rationale": "综合推理",
      "trigger_condition": "入场触发",
      "risk_reward": 2.5,
      "position_size_pct": 5,
      "invalidation": "放弃条件",
      "consensus": "AGREE/CONFLICT/SEVERE_CONFLICT",
      "confidence": 0.75
    }}
  ],
  "restricted": [],
  "restricted_reasons": {{}},
  "position_actions": [],
  "strategy_adjustments": [],
  "risk_warnings": [],
  "disagreements": [
    {{
      "symbol": "US.XXX",
      "bull_view": "多头观点",
      "bear_view": "空头观点",
      "resolution": "建议: 回避/轻仓/观望"
    }}
  ],
  "agreement_count": 3,
  "conflict_count": 1,
  "hermes_perspective": {{}},
  "claude_audit": {{}},
  "debate_summary": "总结字符串",
  "arbiter_mode": "unified_debate_engine",
  "confidence": 0.75
}}"""


# ═══════════════════════════════════════════════════════════════
# Core Engine
# ═══════════════════════════════════════════════════════════════

class UnifiedDebateEngine:
    """
    统一辩论引擎: 所有 LLM 调用通过 UnifiedLLMService。
    
    特性:
    - 角色定义清晰 (Bull/Bear/Arbiter)
    - 消息传递: R1 并行 → R2 交叉回应 → Arbiter 综合
    - 模型可换: 每个角色独立 feature，在 YAML 中配置
    - IC 追踪: 每次辩论记录 feature/model/divergence
    """

    def __init__(self, divergence_threshold: float = 0.4, max_rounds: int = 2):
        self.divergence_threshold = divergence_threshold
        self.max_rounds = max_rounds
        self._debate_log_path = Path("data/debates")
        self._debate_log_path.mkdir(exist_ok=True)

    # ── Public API ──

    def generate_premarket_plan(self, context: dict) -> Optional[Dict]:
        """
        完整辩论流程: R1(并行) → R2(条件触发) → Arbiter
        返回最终共识计划 dict
        """
        start_time = time.time()
        logger.info("[UnifiedDebate] === 开始盘前辩论 ===")

        # 注入记忆
        context = self._enrich_context(context)

        # ── Round 1: 并行 ──
        bull_r1 = None
        bear_r1 = None

        def run_bull():
            nonlocal bull_r1
            bull_r1 = self._call_role(ROLE_BULL, BULL_R1_TEMPLATE, context)

        def run_bear():
            nonlocal bear_r1
            bear_r1 = self._call_role(ROLE_BEAR, BEAR_R1_TEMPLATE, context)

        t_bull = threading.Thread(target=run_bull, daemon=True, name="debate-bull-r1")
        t_bear = threading.Thread(target=run_bear, daemon=True, name="debate-bear-r1")
        t_bull.start()
        t_bear.start()
        t_bull.join(timeout=150)
        t_bear.join(timeout=150)

        elapsed_r1 = time.time() - start_time
        logger.info(
            f"[UnifiedDebate] R1 完成 | 耗时={elapsed_r1:.0f}s | "
            f"Bull={'✅' if bull_r1 else '❌'} | Bear={'✅' if bear_r1 else '❌'}"
        )

        # ── 计算分歧度 ──
        divergence = self._calculate_divergence(bull_r1, bear_r1)
        logger.info(f"[UnifiedDebate] 分歧度={divergence:.0%} (阈值={self.divergence_threshold:.0%})")

        # ── Round 2: 交叉回应 (分歧度 > 阈值时) ──
        bull_r2 = None
        bear_r2 = None

        if divergence > self.divergence_threshold and self.max_rounds >= 2:
            logger.info("[UnifiedDebate] 分歧度超过阈值，触发 R2 交叉回应")

            # Bull 回应 Bear 的质疑
            if bull_r1 and bear_r1:
                bull_r2 = self._call_r2_response(
                    ROLE_BULL, "多头策略师", bull_r1, bear_r1
                )
                logger.info(f"[UnifiedDebate] R2 Bull 回应: {'✅' if bull_r2 else '❌'}")

            # Bear 回应 Bull 的分析
            if bear_r1 and bull_r1:
                bear_r2 = self._call_r2_response(
                    ROLE_BEAR, "空头审计师", bear_r1, bull_r1
                )
                logger.info(f"[UnifiedDebate] R2 Bear 回应: {'✅' if bear_r2 else '❌'}")

        # ── Arbiter: 综合 ──
        final_plan = self._run_arbiter(
            bull_r1=bull_r2 or bull_r1,
            bear_r1=bear_r2 or bear_r1,
            bull_r2=bull_r2,
            bear_r2=bear_r2,
            context=context,
        )

        total_elapsed = time.time() - start_time
        logger.info(f"[UnifiedDebate] === 辩论完成 === | 总耗时={total_elapsed:.0f}s")

        # ── 记录辩论日志 (供 IC 分析) ──
        self._log_debate(
            bull_r1=bull_r1, bear_r1=bear_r1,
            bull_r2=bull_r2, bear_r2=bear_r2,
            final_plan=final_plan,
            divergence=divergence,
            elapsed=total_elapsed,
        )

        return final_plan

    # ── LLM Call (统一入口) ──

    def _call_role(self, role: dict, template: str, context: dict,
                   extra_vars: dict = None) -> Optional[Dict]:
        """
        通过 UnifiedLLMService 调用指定角色的 LLM。
        
        Args:
            role: 角色定义 dict (name, feature, system_prompt)
            template: prompt 模板
            context: 上下文变量
            extra_vars: 额外模板变量
        """
        try:
            from app.llm.unified_service import UnifiedLLMService

            # 构建 prompt
            vars_dict = {**context}
            if extra_vars:
                vars_dict.update(extra_vars)

            # 安全 format (缺失 key 不报错)
            try:
                prompt = template.format(**vars_dict)
            except KeyError as e:
                # 用默认值填充缺失 key
                for key in self._find_missing_keys(template, vars_dict):
                    vars_dict[key] = f"[{key} 数据不可用]"
                prompt = template.format(**vars_dict)

            # 调用 UnifiedLLMService
            res = UnifiedLLMService.call(
                role["feature"],
                prompt,
                system=role["system_prompt"],
                temperature=0.3,
                max_tokens=16000,
                json_mode=True,
                timeout=120,
            )

            if not res or not res.success:
                logger.warning(
                    f"[UnifiedDebate] {role['name']} 调用失败: "
                    f"{res.error if res else 'unknown'}"
                )
                return None

            return self._parse_json_response(res.response)

        except Exception as e:
            logger.error(f"[UnifiedDebate] {role['name']} 异常: {e}", exc_info=True)
            return None

    def _call_r2_response(self, role: dict, role_label: str,
                          own_output: dict, opponent_output: dict) -> Optional[Dict]:
        """Round 2: 回应对方质疑"""
        return self._call_role(
            role,
            R2_RESPONSE_TEMPLATE,
            context={},
            extra_vars={
                "role_name": role_label,
                "opponent_output": json.dumps(opponent_output, ensure_ascii=False, indent=2),
            },
        )

    def _run_arbiter(self, bull_r1: dict, bear_r1: dict,
                     bull_r2: dict = None, bear_r2: dict = None,
                     context: dict = None) -> Optional[Dict]:
        """Arbiter: 综合双方观点生成最终计划"""
        if not bull_r1 and not bear_r1:
            return None

        # 只有单边时直接返回
        if bull_r1 and not bear_r1:
            return self._wrap_single_side(bull_r1, "bull")
        if bear_r1 and not bull_r1:
            return self._wrap_single_side(bear_r1, "bear")

        # 构建 R2 section
        r2_section = ""
        if bull_r2:
            r2_section += f"\n【多头策略师 R2 回应】\n{json.dumps(bull_r2, ensure_ascii=False, indent=2)}\n"
        if bear_r2:
            r2_section += f"\n【空头审计师 R2 回应】\n{json.dumps(bear_r2, ensure_ascii=False, indent=2)}\n"

        # 市场摘要
        market_summary = ""
        if context:
            parts = []
            if context.get("vix"): parts.append(f"VIX={context['vix']}")
            if context.get("spy"): parts.append(f"SPY={context['spy']}")
            if context.get("regime"): parts.append(f"Regime={context['regime']}")
            market_summary = ", ".join(parts)

        result = self._call_role(
            ROLE_ARBITER,
            ARBITER_TEMPLATE,
            context={},
            extra_vars={
                "bull_r1": json.dumps(bull_r1, ensure_ascii=False, indent=2),
                "bear_r1": json.dumps(bear_r1, ensure_ascii=False, indent=2),
                "r2_section": r2_section,
                "market_summary": market_summary,
            },
        )

        if result:
            # 确保保留双方原始输出
            if "hermes_perspective" not in result:
                result["hermes_perspective"] = {
                    "targets": bull_r1.get("targets", []),
                    "regime_reasoning": bull_r1.get("regime_reasoning", ""),
                    "key_disagreements": bull_r1.get("key_disagreements", []),
                    "responses": bull_r2.get("responses", []) if bull_r2 else [],
                    "confidence": bull_r1.get("confidence", 0.5),
                }
            if "claude_audit" not in result:
                result["claude_audit"] = bear_r1

            result["arbiter_mode"] = "unified_debate_engine"
            result["debate_summary"] = (
                f"Bull ({len(bull_r1.get('targets', []))}标的) + "
                f"Bear ({len(bear_r1.get('bear_cases', []))}做空, "
                f"{len(bear_r1.get('restricted', []))}受限) | "
                f"共识={result.get('agreement_count', '?')} "
                f"分歧={result.get('conflict_count', '?')}"
            )

        return result

    # ── Divergence Calculation ──

    def _calculate_divergence(self, bull: Optional[Dict], bear: Optional[Dict]) -> float:
        """计算双方分歧度 (0-1)"""
        if not bull or not bear:
            return 0.5  # 默认中等分歧

        bull_syms = {t.get("symbol"): t.get("direction", "") for t in bull.get("targets", [])}
        bear_syms = {bc.get("symbol"): bc.get("direction", "") for bc in bear.get("bear_cases", [])}

        # 方向冲突数
        conflicts = 0
        total = 0
        for sym, bull_dir in bull_syms.items():
            if sym in bear_syms:
                total += 1
                if bear_syms[sym] and bull_dir and bear_syms[sym] != bull_dir:
                    conflicts += 1

        # Restricted 重叠
        bull_rest = set(bull.get("restricted", []))
        bear_rest = set(bear.get("restricted", []))
        rest_union = bull_rest | bear_rest
        rest_inter = bull_rest & bear_rest

        # 综合分歧度
        dir_divergence = conflicts / max(total, 1)
        rest_divergence = 1 - (len(rest_inter) / max(len(rest_union), 1))
        regime_divergence = 0.0
        if bull.get("market_regime") and bear.get("market_bias"):
            if bull["market_regime"] == "RISK_ON" and bear["market_bias"] == "BEARISH":
                regime_divergence = 1.0
            elif bull["market_regime"] == "RISK_OFF" and bear["market_bias"] == "BULLISH":
                regime_divergence = 1.0

        return (dir_divergence * 0.4 + rest_divergence * 0.3 + regime_divergence * 0.3)

    # ── Context Enrichment ──

    def _enrich_context(self, context: dict) -> dict:
        """注入记忆和默认值"""
        enriched = dict(context)

        # 确保所有模板变量存在
        defaults = {
            "memory_section": "", "memory_context": "",
            "vix": "N/A", "spy": "N/A", "qqq": "N/A", "dxy": "N/A",
            "regime": "NEUTRAL", "premarket_enrichment": "数据采集中...",
            "overnight_news": "暂无", "earnings_info": "暂无",
            "positions_json": "[]", "current_equity": "N/A",
            "drawdown_pct": "0", "water_level": "N/A",
            "watchlist_json": "[]", "price_calibration": "暂无校准数据",
        }
        for k, v in defaults.items():
            if k not in enriched or enriched[k] is None:
                enriched[k] = v

        # 注入长期记忆
        try:
            from app.agents.debate.memory_manager import DebateMemoryManager
            mm = DebateMemoryManager()
            memory = mm.get_relevant_memories(context)
            if memory:
                enriched["memory_section"] = "【FTSeek 长期记忆】"
                enriched["memory_context"] = memory
        except Exception:
            pass

        return enriched

    # ── Helpers ──

    def _parse_json_response(self, response: Any) -> Optional[Dict]:
        """解析 LLM JSON 响应"""
        if isinstance(response, dict):
            return response

        text = str(response).strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"[UnifiedDebate] JSON 解析失败, raw={text[:200]!r}")
            return None

    def _find_missing_keys(self, template: str, vars_dict: dict) -> list:
        """找到模板中缺失的变量名"""
        import re
        keys = re.findall(r'\{(\w+)\}', template)
        return [k for k in keys if k not in vars_dict]

    def _wrap_single_side(self, output: dict, side: str) -> dict:
        """单边输出包装"""
        result = dict(output)
        if side == "bull":
            result["hermes_perspective"] = {
                "targets": output.get("targets", []),
                "regime_reasoning": output.get("regime_reasoning", ""),
                "confidence": output.get("confidence", 0.5),
            }
            result["debate_summary"] = f"仅 Bull 输出 (Bear 不可用)"
        else:
            result["claude_audit"] = output
            result["debate_summary"] = f"仅 Bear 输出 (Bull 不可用)"
        result["arbiter_mode"] = "single_side_fallback"
        result["conflict_count"] = 0
        result["agreement_count"] = len(output.get("targets", []))
        return result

    def _log_debate(self, bull_r1, bear_r1, bull_r2, bear_r2,
                    final_plan, divergence, elapsed):
        """记录辩论日志 (供 IC 分析)"""
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "roles": {
                    "bull": {"feature": ROLE_BULL["feature"], "success": bull_r1 is not None},
                    "bear": {"feature": ROLE_BEAR["feature"], "success": bear_r1 is not None},
                    "arbiter": {"feature": ROLE_ARBITER["feature"], "success": final_plan is not None},
                },
                "divergence": divergence,
                "r2_triggered": divergence > self.divergence_threshold,
                "bull_r2": bull_r2 is not None,
                "bear_r2": bear_r2 is not None,
                "elapsed_seconds": round(elapsed, 1),
                "targets_count": len((final_plan or {}).get("targets", [])),
                "conflict_count": (final_plan or {}).get("conflict_count", 0),
                "agreement_count": (final_plan or {}).get("agreement_count", 0),
                "confidence": (final_plan or {}).get("confidence", 0),
            }

            log_path = self._debate_log_path / f"unified_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(log_path, "w") as f:
                json.dump(log_entry, f, ensure_ascii=False, indent=2)

            logger.info(f"[UnifiedDebate] 日志保存: {log_path.name}")
        except Exception as e:
            logger.debug(f"[UnifiedDebate] 日志保存失败: {e}")


# ═══════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════

_engine_instance = None

def get_unified_debate_engine() -> UnifiedDebateEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = UnifiedDebateEngine()
    return _engine_instance
