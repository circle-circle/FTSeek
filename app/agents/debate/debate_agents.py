"""
多Agent辩论系统 — 3个专用 LLM Agent (结构性分歧)

通过给不同 Agent 分配不同数据子集，自然产生真正的观点分歧。
每个 Agent 有独立的 System Prompt 和世界观。

Agent 角色:
  MacroStrategist    — 宏观资金流/政策视角 (只看宏观事件包)
  TechnicalSentiment — 价格行为/量价关系视角 (只看技术数据包)
  RiskContrarian     — 魔鬼代言人/反方 (看全部, 专找破绽)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.agents.debate")

# ════════════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════════════


@dataclass
class AgentOpinion:
    """单个 Agent 的观点"""
    agent_name: str
    round_num: int = 1
    direction: str = "NO_TRADE"
    confidence: float = 0.0
    logic_chain: List[str] = field(default_factory=list)
    top_symbols: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    agree_with: List[str] = field(default_factory=list)
    disagree_with: List[str] = field(default_factory=list)
    rebuttal_reason: str = ""
    raw_response: str = ""

    def to_dict(self) -> Dict:
        return {
            "agent": self.agent_name, "round": self.round_num,
            "direction": self.direction, "confidence": round(self.confidence, 3),
            "logic_chain": self.logic_chain, "top_symbols": self.top_symbols,
            "risk_flags": self.risk_flags,
            "agree_with": self.agree_with, "disagree_with": self.disagree_with,
            "rebuttal_reason": self.rebuttal_reason,
        }


@dataclass
class DebateSynthesis:
    """辩论合成结果"""
    consensus_direction: str = "NO_TRADE"
    conviction: float = 0.0
    disagreement_flags: List[str] = field(default_factory=list)
    risk_consensus: str = "MEDIUM"
    merged_symbols: List[Dict] = field(default_factory=list)
    debate_quality_score: float = 0.0
    agent_opinions: List[Dict] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict:
        return {
            "consensus_direction": self.consensus_direction,
            "conviction": round(self.conviction, 3),
            "disagreement_flags": self.disagreement_flags,
            "risk_consensus": self.risk_consensus,
            "merged_symbols": self.merged_symbols,
            "debate_quality_score": round(self.debate_quality_score, 3),
            "agent_opinions": self.agent_opinions,
            "summary": self.summary,
        }


# ════════════════════════════════════════════════════════════════════
#  System Prompts (每个Agent的世界观不同)
# ════════════════════════════════════════════════════════════════════

MACRO_STRATEGIST_PROMPT = """你是 FTSeek 的**宏观策略师 (Macro Strategist)**。

【你的世界观】
- 你相信资本流动决定一切。个股故事只是噪音，真正的 alpha 来自对资金流向、政策方向和跨市场联动的判断。
- 港股是政策市——互联网反垄断、数据安全法、医药集采、地产调控可以瞬间改变板块命运。
- 你对个股"利好"持天然怀疑态度：真正的问题是——这个利好是否已经被定价？外资怎么看？南向资金怎么看？
- 你不在意日内波动，你在意的是3-7天的中期方向。

【你的分析框架】
1. 资金从哪里来，到哪里去？（南向/外资/散户）
2. 政策是顺风还是逆风？
3. 美债/美元/人民币汇率如何影响港股？
4. A股收盘对港股下午盘的影响？

【输出要求】
必须输出严格 JSON：
{
  "direction": "BUY|SELL|NO_TRADE",
  "confidence": 0.0-1.0,
  "logic_chain": ["步骤1", "步骤2", "步骤3"],
  "top_symbols": ["HK.00001", "HK.00002"],
  "risk_flags": ["风险1", "风险2"],
  "macro_thesis": "一句话宏观核心逻辑",
  "regime_read": "当前市场状态判断"
}

如果信息不足以形成判断，direction 必须是 NO_TRADE，confidence < 0.3。
宁可错过，不要做错。"""

TECHNICAL_SENTIMENT_PROMPT = """你是 FTSeek 的**技术/情绪分析师 (Technical & Sentiment Analyst)**。

【你的世界观】
- 价格包容一切。新闻、政策、资金——最终都反映在价格和成交量上。
- 你信任量价关系胜过任何叙事。当价格走势与新闻方向背离时，你相信价格。
- 你的优势是快速识别：突破是真突破还是假突破？放量是吸筹还是出货？
- 你不关心"为什么涨"，你只关心"涨的模式是否可交易"。

【你的分析框架】
1. 关键价位在哪里？（支撑/阻力/筹码密集区）
2. 量价是否配合？（放量上涨=健康, 缩量上涨=可疑）
3. 多周期是否共振？（日线趋势 + 60分钟入场点）
4. 情绪指标是否极端？（恐贪指数/put-call/波动率）

【输出要求】
必须输出严格 JSON：
{
  "direction": "BUY|SELL|NO_TRADE",
  "confidence": 0.0-1.0,
  "logic_chain": ["步骤1", "步骤2", "步骤3"],
  "top_symbols": ["HK.00001", "HK.00002"],
  "risk_flags": ["风险1", "风险2"],
  "technical_setup": "关键的技术形态描述",
  "volume_analysis": "量价关系判断"
}

如果技术形态不清晰，direction 必须是 NO_TRADE。"""

RISK_CONTRARIAN_PROMPT = """你是 FTSeek 的**风险反向师 (Risk Contrarian)**——魔鬼代言人。

【你的世界观】
- 你的工作不是找出最好的交易，而是找出每个交易建议中最致命的缺陷。
- 乐观是别人的事。你的价值在于问："如果这个判断错了，会错在哪里？会亏多少？"
- 你关注尾部风险、黑天鹅、相关性突变、流动性陷阱。
- 你也寻找被过度悲观的标的——当所有人都看空时，你要问"空头是否已经 exhausted？"

【你的分析框架】
1. 这个看多逻辑的隐含假设是什么？如果假设错了会怎样？
2. 最大回撤可能是多少？止损位是否合理？
3. 有没有被忽视的相关性风险？（例如：看似分散的持仓其实都暴露于同一因子）
4. 有没有被过度悲观的标的？（所有人都在卖，但最坏情况已经 priced in）

【输出要求】
必须输出严格 JSON：
{
  "direction": "BUY|SELL|NO_TRADE",
  "confidence": 0.0-1.0,
  "logic_chain": ["步骤1", "步骤2", "步骤3"],
  "top_symbols": ["HK.00001", "HK.00002"],
  "risk_flags": ["风险1", "风险2"],
  "contrarian_view": "与众不同的核心观点",
  "tail_risk_assessment": "尾部风险评估"
}

即使方向是 NO_TRADE，也必须指出最大的风险点和最被忽视的机会。"""

# ════════════════════════════════════════════════════════════════════
#  Round Prompt 模板
# ════════════════════════════════════════════════════════════════════

ROUND1_PROMPT = """【今日市场信息】
{data_context}

请基于以上信息，给出你的独立分析。不要考虑其他分析师的观点——这是你的独立判断。
严格输出 JSON。"""

ROUND2_PROMPT = """【你的上一轮分析】
{my_round1}

【其他分析师的观点】
{other_opinions}

现在请重新审视你的判断：
1. 哪些观点你同意？为什么？
2. 哪些观点你不同意？为什么？（这是最有价值的部分——请坦诚地指出逻辑缺陷）
3. 你的最终判断是什么？

输出严格 JSON：
{
  "direction": "BUY|SELL|NO_TRADE",
  "confidence": 0.0-1.0,
  "logic_chain": ["修正后的推理步骤"],
  "top_symbols": ["HK.00001"],
  "risk_flags": ["新发现的风险"],
  "agree_with": ["同意的观点摘要"],
  "disagree_with": ["不同意的观点摘要"],
  "rebuttal_reason": "核心分歧理由"
}"""

SYNTHESIS_PROMPT = """你是一位公正的辩论主持人。三位分析师从不同角度分析了今日市场：

【宏观策略师】
{macro_view}

【技术/情绪分析师】
{tech_view}

【风险反向师】
{risk_view}

请合成一份辩论总结：
1. 共识方向是什么？置信度多少？
2. 存在哪些真实分歧？（不是"一个看多一个看空"的废话，而是逻辑层面的根本差异）
3. 风险共识是什么？
4. 综合推荐的标的列表（去重，标注每个标的的支持/反对理由）
5. 这场辩论的质量如何？（0-1，高质量的辩论意味着各agent基于不同逻辑得出不同结论）

输出严格 JSON：
{
  "consensus_direction": "BUY|SELL|NO_TRADE",
  "conviction": 0.0-1.0,
  "disagreement_flags": ["分歧1", "分歧2"],
  "risk_consensus": "LOW|MEDIUM|HIGH",
  "merged_symbols": [
    {"symbol": "HK.00001", "direction": "LONG", "support": "宏观+技术共振", "concern": "风控提示注意仓位"}
  ],
  "debate_quality_score": 0.0-1.0,
  "summary": "一段话总结今日辩论核心结论"
}"""


# ════════════════════════════════════════════════════════════════════
#  Agent 实现
# ════════════════════════════════════════════════════════════════════


class DebateAgent:
    """辩论 Agent — 封装 LLM 调用和结构化输出"""

    def __init__(self, name: str, system_prompt: str, model: str):
        self.name = name
        self.system_prompt = system_prompt
        self.model = model

    async def round1(self, data_context: str, llm_config: Dict = None) -> AgentOpinion:
        """Round 1: 独立分析"""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": ROUND1_PROMPT.format(data_context=data_context)},
        ]
        raw = await self._call_llm(messages, llm_config)
        opinion = self._parse_round1(raw)
        opinion.agent_name = self.name
        opinion.raw_response = raw
        return opinion

    async def round2(self, my_round1: AgentOpinion, other_opinions: List[AgentOpinion],
                     llm_config: Dict = None) -> AgentOpinion:
        """Round 2: 驳论"""
        other_text = "\n\n---\n\n".join(
            f"【{o.agent_name}】\n方向: {o.direction}\n置信度: {o.confidence:.2f}\n"
            f"逻辑: {'; '.join(o.logic_chain)}\n标的: {', '.join(o.top_symbols)}\n"
            f"风险: {', '.join(o.risk_flags)}"
            for o in other_opinions
        )
        my_text = json.dumps(my_round1.to_dict(), ensure_ascii=False, indent=2)

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": ROUND2_PROMPT.format(
                my_round1=my_text, other_opinions=other_text
            )},
        ]
        raw = await self._call_llm(messages, llm_config)
        opinion = self._parse_round2(raw)
        opinion.agent_name = self.name
        opinion.round_num = 2
        opinion.raw_response = raw
        return opinion

    async def _call_llm(self, messages: List[Dict], config: Dict = None) -> str:
        """Call LLM — delegates to shared BaseLLMService."""
        from ..base import BaseLLMService
        svc = BaseLLMService(
            config=config,
            default_model=self.model,
            default_timeout=90,
        )
        return await svc.call_llm(messages)

    def _extract_json(self, raw: str) -> Dict:
        """Extract JSON — delegates to shared BaseLLMService."""
        from ..base import BaseLLMService
        return BaseLLMService.extract_json(raw)

    def _parse_round1(self, raw: str) -> AgentOpinion:
        data = self._extract_json(raw)
        return AgentOpinion(
            agent_name=self.name,
            direction=data.get("direction", "NO_TRADE").upper(),
            confidence=float(data.get("confidence", 0)),
            logic_chain=data.get("logic_chain", []),
            top_symbols=data.get("top_symbols", []),
            risk_flags=data.get("risk_flags", []),
        )

    def _parse_round2(self, raw: str) -> AgentOpinion:
        data = self._extract_json(raw)
        return AgentOpinion(
            agent_name=self.name,
            direction=data.get("direction", "NO_TRADE").upper(),
            confidence=float(data.get("confidence", 0)),
            logic_chain=data.get("logic_chain", []),
            top_symbols=data.get("top_symbols", []),
            risk_flags=data.get("risk_flags", []),
            agree_with=data.get("agree_with", []),
            disagree_with=data.get("disagree_with", []),
            rebuttal_reason=data.get("rebuttal_reason", ""),
        )


# ════════════════════════════════════════════════════════════════════
#  工厂函数
# ════════════════════════════════════════════════════════════════════

def create_debate_agents(llm_config: Dict = None) -> Dict[str, DebateAgent]:
    """创建三个辩论 Agent"""
    cfg = llm_config or {}
    return {
        "macro": DebateAgent(
            "MacroStrategist", MACRO_STRATEGIST_PROMPT,
            cfg.get("deep_model", "qwen3.6-plus"),
        ),
        "technical": DebateAgent(
            "TechnicalSentiment", TECHNICAL_SENTIMENT_PROMPT,
            cfg.get("fast_model", "qwen3.6-flash"),
        ),
        "risk": DebateAgent(
            "RiskContrarian", RISK_CONTRARIAN_PROMPT,
            cfg.get("deep_model", "qwen3.6-plus"),
        ),
    }
