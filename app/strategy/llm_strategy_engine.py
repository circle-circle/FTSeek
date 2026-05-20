"""
FTSeek-US LLM 二阶推理策略引擎

核心功能:
1. 新闻聚合 → 一阶情感 + 二阶传导推理
2. 结合夜间行情数据 (VIX, 盘前异动, 板块动量)
3. 生成具体下单策略 (方向/仓位/止盈止损/时效)
4. 自动通过风控过滤 → 执行层

数据流:
  新闻(RSSHub+Finhub) → LLM推理 → OrderStrategy → 风控 → 执行

作者: FTSeek Team
日期: 2026-04-16
"""

import os
import re
import json
import queue
import logging
import time
import uuid
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

import requests
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

logger = logging.getLogger("ftseek.llm_strategy")


# ============================================================
#  数据结构 & Prompt 定义
# ============================================================

SYSTEM_PROMPT = """
你是 FTSeek 量化交易系统的首席宏观策略师（Chief Macro Strategist）。
你的任务是基于输入的多源事件包、港股/美股行情、跨市场联动信息和历史复盘信息，进行二阶推理（Second-Order Reasoning），输出适合自动交易系统使用的结构化结论。

你的核心能力不是新闻摘要，而是推导：
1. 事件事实本身
2. 事件的直接影响对象
3. 事件如何沿着行业、供应链、竞争链、资金链传导
4. 市场是否已经定价
5. 是否存在真正可交易的机会
6. 如果交易，应该如何控制风险

你必须遵守以下原则：
- 只基于输入事实推理，不得编造未知内容
- 如果信息不完整、逻辑链不闭环、或定价状态不明确，必须输出 NO_TRADE
- 置信度低于 0.6 时，必须输出 NO_TRADE
- 宁可错过，不要做错
- 不要输出泛泛而谈的新闻总结，必须给出交易可用的结构化判断
- 必须严格输出 JSON，禁止输出多余解释文字
- 输出内容必须适合自动交易系统直接消费

你需要重点关注以下问题：
1. 事件是否真实且重要
2. 直接影响对象是谁
3. 影响是否会传导到相关行业/板块/公司
4. 市场是否已经充分定价
5. 当前价格行为是否确认该事件
6. 该事件是否值得交易
7. 如果交易，应该采取什么方向和风险控制方式
8. 哪些因素会否定当前判断

必须强制输出以下字段：
- session
- primary_scene
- event_type
- direct_impact
- second_order_insight
- pricing_status
- key_events
- focus_symbols
- direction
- tradeability
- confidence
- event_quality
- time_horizon
- cross_market_signal
- risk_path
- stop_loss
- position_sizing
- logic_chain
- invalidating_factors
- notes

字段约束：
- direction 只能是 BUY / SELL / NO_TRADE
- tradeability 只能是 tradeable / watch_only / no_trade
- pricing_status 只能是 not_priced_in / partially_priced_in / fully_priced_in / unknown
- event_quality 只能是 high / medium / low
- time_horizon 只能是 intraday / short_term / mid_term / long_term
- confidence 范围必须是 0.0 ~ 1.0
- focus_symbols 必须输出最相关的标的代码，最多 5 个
- direct_impact 必须输出直接受影响的行业/公司/资产
- logic_chain 必须用数组列出推理步骤
- invalidating_factors 必须明确写出哪些因素会推翻当前判断
- 如果没有高确信度交易机会，direction 必须是 NO_TRADE，tradeability 必须是 no_trade

分析要求：
1. 先判断事实，再判断影响，再判断交易。
2. 先判断直接影响，再判断二阶传导。
3. 如果市场已充分定价，优先输出 NO_TRADE。
4. 如果新闻只是噪音、重复信息、或缺乏事件强度，不要交易。
5. 必须结合跨市场联动：
   - 美股收盘 / 盘后消息
   - ADR / 中概股反馈
   - 行业联动与资金轮动
6. 必须识别价格行为与事件方向是否一致：
   - 若新闻利多但价格不涨，需说明可能的背离
   - 若新闻利空但价格抗跌，需说明可能的消化或反身性
7. 必须考虑风险路径：
   - 这个交易为什么会失败
   - 哪个因素最容易推翻当前推理
8. 如果推理中存在任何事实缺口、逻辑跳跃或无法验证的部分，必须降级为 NO_TRADE

输出格式要求：
严格输出 JSON，结构如下：

{
  "session": "",
  "primary_scene": "",
  "event_type": "",
  "direct_impact": [],
  "second_order_insight": "",
  "pricing_status": "unknown",
  "key_events": [],
  "focus_symbols": [],
  "direction": "NO_TRADE",
  "tradeability": "no_trade",
  "confidence": 0.0,
  "event_quality": "low",
  "time_horizon": "short_term",
  "cross_market_signal": {
    "us_close": "",
    "adr_effect": "",
    "sector_lead": "",
    "hk_open_bias": ""
  },
  "risk_path": "",
  "stop_loss": "",
  "position_sizing": "",
  "logic_chain": [],
  "invalidating_factors": [],
  "notes": ""
}

输出规范：
- primary_scene 用一句话总结当前市场核心矛盾
- second_order_insight 必须是二阶逻辑，不得只重复新闻内容
- direct_impact 必须列出最直接受影响的对象
- key_events 必须列出最重要的事件节点
- logic_chain 必须简洁清晰，按因果顺序排列
- risk_path 必须说明交易失败最可能的路径
- stop_loss 必须给出风控逻辑或失效条件，而不是空话
- position_sizing 必须说明建议仓位强度，如 small / medium / large，或具体比例区间
- notes 可补充简短说明，但不能代替核心逻辑
"""

USER_PROMPT_TEMPLATE = """
请基于以下事件包，输出严格 JSON。

【事件包】
标题：{headline}
正文：{body}
来源：{source}
发布时间：{publish_time}
涉及标的：{symbols}
相关行业：{industries}
市场环境：{market_regime}
美股收盘情况：{us_close}
ADR 表现：{adr_effect}
近期价格行为：{price_action}
近期成交量/波动率：{volume_volatility}
历史相似事件：{historical_cases}
相关新闻摘要：{related_news}

请按 SYSTEM_PROMPT 的要求输出。
"""

@dataclass
class OrderStrategy:
    """生成的下单策略 (支持新版 Prompt 输出)"""
    strategy_id: str = field(default_factory=lambda: f"STRAT-{uuid.uuid4().hex[:10].upper()}")
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # 策略核心
    symbol: str = ""                    # US.AAPL
    direction: str = ""                 # LONG / SHORT / OPTIONS_PUT / OPTIONS_CALL
    confidence: float = 0.0             # 0-1
    reasoning: str = ""                 # LLM 推理摘要 (对应 second_order_insight)
    
    # 新增: 业务规则校验字段 (Phase 2)
    pricing_status: str = "unknown"     # not_priced_in / partially_priced_in / fully_priced_in
    tradeability: str = "watch_only"    # tradeable / watch_only / no_trade
    event_quality: str = "medium"       # high / medium / low
    logic_chain: List[str] = field(default_factory=list)
    invalidating_factors: List[str] = field(default_factory=list)
    risk_path: str = ""
    focus_symbols: List[str] = field(default_factory=list)

    # 入场参数
    entry_price_target: float = 0.0
    entry_type: str = "market"          # market / limit
    max_position_pct: float = 0.05      # 仓位占总资产比例 (对应 position_sizing)
    
    # 退出参数
    take_profit_pct: float = 0.15       # 止盈百分比
    stop_loss_pct: float = 0.25         # 止损百分比 (对应 stop_loss)
    time_horizon_hours: int = 72        # 持有期限（小时）
    
    # 触发条件
    trigger_condition: str = "immediate"  # immediate / on_pullback / on_breakout
    expiry_time: str = ""               # ISO 时间
    
    # 关联信息
    related_event: str = ""             # 关联的新闻/事件摘要
    impact_chain: List[str] = field(default_factory=list)  # 事件传导链
    source_news_ids: List[str] = field(default_factory=list)
    
    # 状态
    status: str = "pending"             # pending / validated / executing / executed / rejected / expired
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def to_vote_dict(self) -> Dict[str, Any]:
        """转换为投票系统兼容格式 (用于注入共识引擎)"""
        # 方向映射: LONG → +1, SHORT/OPTIONS_PUT → -1
        if self.direction in ("LONG", "OPTIONS_CALL"):
            vote_direction = 1
        elif self.direction in ("SHORT", "OPTIONS_PUT"):
            vote_direction = -1
        else:
            vote_direction = 0
        
        return {
            "model": "llm_strategy",
            "direction": vote_direction,
            "strength": int(self.confidence * 100),  # 0-100 范围
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "metadata": {
                "strategy_id": self.strategy_id,
                "related_event": self.related_event,
                "impact_chain": self.impact_chain,
                "trigger_condition": self.trigger_condition,
                "time_horizon_hours": self.time_horizon_hours,
            }
        }


@dataclass
class EventImpact:
    """事件影响评估 (LLM 推理输出)"""
    event_category: str = ""            # 个股 / 行业 / 宏观 / 地缘 / 政策
    direct_impact: str = ""             # 直接影响描述
    second_order_impact: str = ""       # 二阶传导描述
    affected_symbols: Dict[str, float] = field(default_factory=dict)  # {symbol: impact_score}
    impact_chain: List[str] = field(default_factory=list)
    strategy_suggestions: List[Dict] = field(default_factory=list)
    confidence: float = 0.0


# ============================================================
#  LLM 推理提示词
# ============================================================

LLM_REASONING_PROMPT = """你是一个专业的美股策略分析师，擅长事件传导链推理和量化策略生成。
融合 Jesse Livermore 交易原则: "最小阻力线" + "关键点突破" + "顺势而为"。

## 输入数据

### 新闻事件
{news_items}

### 当前市场状态
- VIX: {vix_level}
- SPY 趋势: {spy_trend}
- 盘前异动: {pre_market_changes}
- 板块动量: {sector_momentum}

### Livermore 大盘状态 (重要: 决定交易方向)
- 运行模式: {livermore_regime_label}
- VIX 等级: {livermore_vix_level}
- SPY vs MA50: {livermore_spy_vs_ma50}
- 5日趋势: {livermore_trend_5d}

### Livermore 板块龙头 (只交易热点板块的龙头股)
{sectors_info}
{leaders_info}

⚠️ 交易规则 (根据大盘模式调整):
- **突发事件特权**: 如果涉及地缘政治突变、战争、核心供应链断裂（如伊朗石油、芯片禁令），即使部分细节缺失，也应给出较高的置信度 (0.7+) 并推荐对冲方向。
- 防御模式/VIX恐惧: 只做 SHORT, 不做 LONG
- SPY < MA50: LONG 信号需极强置信度 (>0.7)
- SPY > MA50 + 上涨: 顺势做多, 信号可适当放宽
- 市场过热 (VIX<12): 不追高, 关注回调机会
- 优先交易热点板块的龙头股, 不碰跟风股

### 观察名单与持仓
- 观察名单: {watchlist}
- 当前持仓: {positions}

## 任务

请完成以下分析：

### 1. 事件分类与直接影响
- 分类: [个股 / 行业 / 宏观 / 地缘 / 政策]
- 哪些公司/板块直接受影响？

### 2. 二阶传导推理 (Livermore 式)
- 通过什么机制传导到其他行业/个股？
- 举例: 伊朗停火 → 油价下跌 → 能源股承压 (如 COP/XOM)
- 举例: 关税升级 → 供应链转移 → 越南/墨西哥受益股上涨

### 3. 关键价格位分析 (Pivot Points)
- 对影响评分 > 0.5 的股票, 评估其关键支撑/阻力位
- 突破关键阻力 → 买入信号
- 跌破关键支撑 → 卖出信号

### 4. 策略建议 (仅针对影响评分 > 0.5 的股票)
对每个相关股票，生成具体策略：
- Livermore 铁律: 止损不低于 10%, 让利润奔跑 (不设固定止盈, 用 trailing stop)
- 永不摊平亏损: 亏损头寸绝不加仓
- 金字塔加仓: 盈利后分批加仓, 每次递减

## 输出格式

请返回纯 JSON (不要使用 ```json 代码块):

{{
  "event_category": "行业",
  "direct_impact": "一句话描述直接影响",
  "second_order_impact": "一句话描述二阶传导",
  "impact_chain": ["事件A", "传导到B", "影响C"],
  "affected_symbols": {{
    "US.AAPL": 0.75,
    "US.GOOG": 0.60
  }},
  "strategies": [
    {{
      "symbol": "US.AAPL",
      "direction": "SHORT",
      "confidence": 0.75,
      "reasoning": "简要推理 (含二阶传导)",
      "take_profit_pct": 0.15,
      "stop_loss_pct": 0.10,
      "time_horizon_hours": 72,
      "trigger_condition": "immediate",
      "max_position_pct": 0.05,
      "priority": "high",
      "key_levels": {{
        "support": 180.0,
        "resistance": 195.0,
        "pivot_note": "突破195阻力位确认上涨"
      }}
    }}
  ]
}}"""

# ============================================================
#  人类可读深度分析 System Prompt (用于盘前推送)
# ============================================================
# 与 SYSTEM_PROMPT 的区别: 不强制 JSON 输出，用于生成人类可读的深度分析报告

ANALYSIS_SYSTEM_PROMPT = """你是 FTSeek 量化交易系统的首席宏观策略师（Chief Macro Strategist）。

你的任务是基于多源事件包、行情数据和市场环境，进行深度二阶推理分析，生成适合人类阅读的美股盘前分析报告。

你的核心能力不是新闻摘要，而是推导：
1. 事件事实本身
2. 事件的直接影响对象
3. 事件如何沿着行业、供应链、竞争链、资金链传导
4. 市场是否已经定价
5. 是否存在真正可交易的机会
6. 如果交易，应如何控制风险

你必须遵守以下原则：
- 只基于输入事实推理，不得编造未知内容
- 必须给出深度的二阶推理（如：宏观事件 → 行业影响 → 具体标的）
- 必须结合 VIX、大盘趋势等市场状态进行综合判断
- 输出必须结构清晰、人类可读（不要输出 JSON）
- 每个推荐标的必须包含：标的代码、方向（看多/看空）、详细推理过程
- 推理过程必须展示因果链条，不能只给结论
- 必须指出风险因素和否定当前判断的条件

你的输出应该包含以下结构：
1. 宏观市场定调（1-2 句话）
2. 关键事件分析（2-3 条最重要事件及其二阶影响）
3. 推荐标的（3-5 个，每个包含：代码、方向、详细推理、风险提示）
4. 整体操作建议

记住：你是给专业交易员写报告，不是给普通投资者。要求深度、严谨、可执行。"""


# ============================================================
#  LLM 策略引擎
# ============================================================

class LLMStrategyEngine:
    """LLM 二阶推理策略引擎"""
    
    def __init__(self, config_path: str = None):
        # P1 修复 (2026-04-17): 优先从 unified_config.yaml 读取，环境变量作为降级备份
        try:
            from app.core.config_container import get_config
            cfg = get_config(config_path).get('llm.strategy_engine', {})
        except Exception:
            cfg = {}
            
        self.enabled = cfg.get('enabled', False)
        # 默认使用 qwen3.6-flash (二阶推理专用)
        self.model = cfg.get('model', os.getenv("BAILIAN_MODEL_STRATEGY", "qwen3.6-flash"))
        self.bailian_base_url = cfg.get('base_url', os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
        
        # 读取 API Key: 优先 config -> 其次环境变量
        api_key_from_cfg = cfg.get('api_key', '')
        api_key_env_name = cfg.get('api_key_env', 'BAILIAN_API_KEY')
        self.bailian_api_key = api_key_from_cfg or os.getenv(api_key_env_name, "")
        
        self.enabled = self.enabled and bool(self.bailian_api_key)
        
        # 策略历史 (用于冷却/去重)
        self._recent_strategies: List[OrderStrategy] = []
        self._strategy_cooldown: Dict[str, datetime] = {}  # {symbol_direction: last_time}
        
        # 2026-04-23 新增: 即时触发队列 (Event-Triggered Scan)
        self._urgent_queue: queue.Queue = queue.Queue(maxsize=50)
        self._scan_event: threading.Event = threading.Event()

        logger.info(f"[LLM策略引擎] 初始化 | 模型={self.model} | 启用={self.enabled} | 来源=unified_config")
    
    def analyze_and_generate(
        self,
        news_items: List[Dict],
        market_state: Dict[str, Any],
        watchlist: List[str] = None,
        positions: List[Dict] = None,
    ) -> List[OrderStrategy]:
        """
        核心方法: 新闻 + 行情 → LLM 二阶推理 → 策略生成
        
        Args:
            news_items: 新闻列表 (from RSSHub + Finnhub)
            market_state: {vix, spy_trend, pre_market_changes, sector_momentum, us_close, adr_effect}
            watchlist: 观察名单
            positions: 当前持仓
        """
        if not self.enabled:
            logger.warning("[LLM策略引擎] 未配置 API Key, 跳过推理")
            return []
        
        if not news_items:
            logger.debug("[LLM策略引擎] 无新闻输入, 跳过推理")
            return []
        
        # 构建用户 Prompt (使用新模板适配二阶推理)
        # 我们取最相关的前几条新闻进行综合分析，或针对单条新闻生成策略
        # 这里为了保持系统稳定，我们将新闻聚合为 Context 传入
        context_text = self._build_user_prompt_context(news_items, market_state, watchlist, positions)
        
        # 调用 LLM
        try:
            response = self._call_llm(context_text)
            if not response:
                return []
            
            # 解析输出
            raw_strategies = self._parse_response(response, news_items)
            
            # P2 新增: 校验输出 (JSON Schema + 业务规则)
            validated_strategies = [self._validate_strategy_output(s) for s in raw_strategies]
            
            # 过滤掉被强制为 NO_TRADE 的策略
            strategies = [s for s in validated_strategies if s.direction != "NO_TRADE"]
            
            # 应用冷却/去重
            strategies = self._apply_cooldown(strategies)
            
            logger.info(f"[LLM策略引擎] 最终生成 {len(strategies)} 条可执行策略 (过滤前 {len(raw_strategies)})")
            return strategies
            
        except Exception as e:
            logger.error(f"[LLM策略引擎] 推理失败: {e}")
            return []

    def _build_user_prompt_context(
        self, 
        news_items: List[Dict], 
        market_state: Dict[str, Any], 
        watchlist: List[str], 
        positions: List[Dict]
    ) -> str:
        """构建符合 USER_PROMPT_TEMPLATE 格式的上下文"""
        # 聚合新闻或选取最重要的
        # 这里我们构造一个包含多条新闻的上下文，适配模板
        news_section = ""
        for i, n in enumerate(news_items[:5]): # 最多分析 5 条
            headline = n.get('title', n.get('headline', ''))
            body = n.get('summary', n.get('content', ''))
            source = n.get('source', 'Unknown')
            pub_time = n.get('published_at', 'Unknown')
            symbols = n.get('symbols', '')
            
            # 尝试从市场状态获取行业信息
            industries = n.get('industry', 'N/A') 
            
            news_section += f"""
--- 新闻 {i+1} ---
标题：{headline}
正文：{body}
来源：{source}
发布时间：{pub_time}
涉及标的：{symbols}
相关行业：{industries}
"""
        if not news_section:
            news_section = "无详细新闻数据。"

        # 提取市场状态字段
        us_close = market_state.get('us_close_summary', market_state.get('spy_trend', 'N/A'))
        adr_effect = market_state.get('adr_effect', 'N/A')
        price_action = market_state.get('price_action', 'N/A')
        volume_vol = market_state.get('volume_volatility', 'N/A')
        
        return USER_PROMPT_TEMPLATE.format(
            headline="多条新闻聚合分析" if len(news_items) > 1 else (news_items[0].get('title', '') if news_items else ""),
            body=news_section,
            source="Multi-Source",
            publish_time=datetime.now().isoformat(),
            symbols=", ".join([n.get('symbols', '') for n in news_items[:3]]),
            industries="Multi-Sector",
            market_regime=market_state.get('livermore_regime', {}).get('regime_label', 'Unknown'),
            us_close=us_close,
            adr_effect=adr_effect,
            price_action=price_action,
            volume_volatility=volume_vol,
            historical_cases="N/A",
            related_news="See Context Above"
        )

    def _validate_strategy_output(self, strategy: OrderStrategy) -> OrderStrategy:
        """
        P2 新增: 校验 LLM 输出
        业务规则:
        1. confidence < 0.6 -> 强制 NO_TRADE
        2. tradeability == 'no_trade' -> 强制 NO_TRADE
        3. pricing_status == 'fully_priced_in' -> 强制 NO_TRADE (保守策略)
        4. focus_symbols 为空 -> 丢弃
        """
        # 这里我们将校验逻辑嵌入到解析阶段，或者在这里修正
        # 为了安全，直接修改 strategy 对象
        
        if not strategy:
            return None

        # 1. 检查置信度
        if strategy.confidence < 0.60:
            logger.warning(f"📉 [校验] 策略 {strategy.symbol} 置信度过低 ({strategy.confidence:.2f}) -> 拒绝")
            return None # 返回 None 以便后续过滤

        # 2. 检查交易可行性
        if hasattr(strategy, 'tradeability') and strategy.tradeability == 'no_trade':
            logger.warning(f"🛑 [校验] 策略 {strategy.symbol} 标记为不可交易 -> 拒绝")
            return None
            
        # 3. 检查定价状态
        if hasattr(strategy, 'pricing_status') and strategy.pricing_status == 'fully_priced_in':
            logger.warning(f"⚠️ [校验] 策略 {strategy.symbol} 已充分定价 -> 拒绝")
            return None

        # 4. 检查标的
        if not strategy.symbol:
            logger.warning(f"❌ [校验] 策略无明确标的 -> 丢弃")
            return None

        return strategy
    
    def _format_news(self, news_items: List[Dict]) -> str:
        """格式化新闻列表为文本 (P1 优化: 注入情感分数)"""
        lines = []
        for i, n in enumerate(news_items[:15]):  # 最多15条
            title = n.get('title', n.get('headline', ''))
            summary = n.get('summary', n.get('content', ''))[:200]
            source = n.get('source_name', n.get('source', ''))
            time_str = n.get('published_at', n.get('fetched_at', ''))
            symbols = n.get('symbols', '')
            
            # P1 优化 (2026-04-17): 注入量化情感分数
            sentiment_score = n.get('qwen_sentiment', n.get('sentiment_score', None))
            sentiment_str = ""
            if sentiment_score is not None:
                if sentiment_score > 0.3:
                    sentiment_label = "正向"
                elif sentiment_score < -0.3:
                    sentiment_label = "负向"
                else:
                    sentiment_label = "中性"
                sentiment_str = f" | 情感: {sentiment_label} ({sentiment_score:+.2f})"
            
            lines.append(f"{i+1}. [{source}]{sentiment_str} {title}")
            if summary:
                lines.append(f"   摘要: {summary}")
            if symbols:
                lines.append(f"   关联: {symbols}")
            lines.append(f"   时间: {time_str}")
            lines.append("")
        
        return "\n".join(lines) if lines else "无新闻数据"
    
    def _call_llm(self, prompt: str) -> Optional[str]:
        """调用 Qwen/Bailian API (带重试) — 默认 JSON 模式"""
        return self._call_llm_api(prompt, json_mode=True)

    def _call_llm_analysis(self, prompt: str, system_prompt: str = None, timeout: int = 120) -> Optional[str]:
        """
        调用 LLM 进行深度分析 (人类可读文本模式)
        
        与 _call_llm 的区别:
        - 不使用 response_format json_object
        - 可使用自定义 system prompt
        - 更高超时 (默认 120s，适合深度推理)
        - max_tokens 更大 (4000)
        
        Args:
            prompt: 用户 prompt
            system_prompt: 自定义系统提示词，None 则使用 ANALYSIS_SYSTEM_PROMPT
            timeout: 超时秒数
        
        Returns:
            LLM 返回的文本内容
        """
        return self._call_llm_api(prompt, json_mode=False, system_prompt=system_prompt, timeout=timeout)

    def _call_llm_api(
        self, 
        prompt: str, 
        json_mode: bool = True, 
        system_prompt: str = None,
        timeout: int = 90
    ) -> Optional[str]:
        """
        底层 LLM API 调用
        
        Args:
            prompt: 用户 prompt
            json_mode: True=强制 JSON 输出, False=自由文本输出
            system_prompt: 自定义系统提示词 (仅 json_mode=False 时生效)
            timeout: 超时秒数
        """
        try:
            base = self.bailian_base_url.rstrip('/')
            url = f"{base}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.bailian_api_key}",
                "Content-Type": "application/json",
            }
            
            # 根据模式选择不同的 system prompt
            if json_mode:
                sys_prompt = SYSTEM_PROMPT
            else:
                sys_prompt = system_prompt or ANALYSIS_SYSTEM_PROMPT
            
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3 if json_mode else 0.7,
                "max_tokens": 4000,
            }
            
            # 仅 JSON 模式强制 response_format
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            
            for attempt in range(2):
                try:
                    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    # JSON 模式: 清理 markdown 代码块
                    if json_mode:
                        content = re.sub(r'^```json\s*', '', content, flags=re.MULTILINE)
                        content = re.sub(r'\s*```$', '', content, flags=re.MULTILINE)
                    
                    return content.strip()
                except requests.exceptions.Timeout:
                    logger.warning(f"[LLM API] 超时 (尝试 {attempt+1}/2), 等待重试...")
                    time.sleep(5)
                except requests.exceptions.RequestException as e:
                    logger.error(f"[LLM API] 请求异常: {e}")
                    return None
                    
            logger.error(f"[LLM API] 重试后仍然超时 (mode={'json' if json_mode else 'text'})")
            return None
            
        except Exception as e:
            logger.error(f"[LLM API] 调用失败: {e}")
            return None

    def _parse_response(self, response_text: str, news_items: List[Dict]) -> List[OrderStrategy]:
        """解析 LLM 响应为策略列表 (适配新版 Prompt 结构)"""
        strategies = []
        
        try:
            # 尝试解析 JSON (可能包含在一个数组中或者单个对象)
            result = json.loads(response_text)
            
            # 兼容两种格式：单个对象 或 包含 strategies 键的对象
            if isinstance(result, dict):
                if "strategies" in result:
                    analysis_list = result.get("strategies", [])
                elif "direction" in result:
                    # 单个分析结果
                    analysis_list = [result]
                else:
                    # 可能是其他格式，尝试找 list
                    for v in result.values():
                        if isinstance(v, list):
                            analysis_list = v
                            break
                    else:
                        analysis_list = []
            else:
                analysis_list = result if isinstance(result, list) else []

            for analysis in analysis_list:
                direction_val = analysis.get("direction", "NO_TRADE")
                tradeability = analysis.get("tradeability", "no_trade")
                confidence = float(analysis.get("confidence", 0.0))
                
                # 过滤 NO_TRADE 或 tradeability 为 no_trade 的
                if direction_val == "NO_TRADE" or tradeability == "no_trade":
                    continue
                
                # 获取标的列表
                focus_symbols = analysis.get("focus_symbols", [])
                if not focus_symbols and analysis.get("symbol"):
                    focus_symbols = [analysis.get("symbol")]
                
                # 增强鲁棒性: 如果方向明确但未给出 focus_symbols，尝试从 reasoning 中提取
                if not focus_symbols and direction_val != "NO_TRADE":
                    logger.warning(f"[解析] 缺失 focus_symbols，尝试从上下文推断")
                    # 这里暂时跳过，依赖 LLM 下一次输出或手动修复
                    continue
                    
                if not focus_symbols:
                    logger.warning(f"[解析] 无明确标的 (focus_symbols 为空), 跳过")
                    continue
                
                # 解析仓位大小 (position_sizing)
                pos_sizing = analysis.get("position_sizing", "small")
                pos_pct = 0.02 # default small
                if "medium" in str(pos_sizing) or "中" in str(pos_sizing): pos_pct = 0.05
                elif "large" in str(pos_sizing) or "大" in str(pos_sizing): pos_pct = 0.10
                # 尝试提取数字百分比
                import re as _re
                match = _re.search(r'(\d+)%', str(pos_sizing))
                if match: pos_pct = int(match.group(1)) / 100.0
                
                # 解析止损 (stop_loss)
                stop_loss_pct = 0.10 # default
                sl_text = str(analysis.get("stop_loss", ""))
                match_sl = _re.search(r'(\d+)%', sl_text)
                if match_sl: stop_loss_pct = int(match_sl.group(1)) / 100.0

                # 创建策略对象
                for sym in focus_symbols:
                    strategy = OrderStrategy()
                    strategy.symbol = sym
                    strategy.direction = "LONG" if direction_val == "BUY" else "SHORT"
                    strategy.confidence = confidence
                    strategy.reasoning = analysis.get("second_order_insight", analysis.get("notes", ""))
                    strategy.max_position_pct = pos_pct
                    strategy.stop_loss_pct = stop_loss_pct
                    strategy.related_event = "; ".join(analysis.get("key_events", []))
                    strategy.impact_chain = analysis.get("logic_chain", [])
                    
                    # 新字段映射
                    strategy.pricing_status = analysis.get("pricing_status", "unknown")
                    strategy.tradeability = tradeability
                    strategy.event_quality = analysis.get("event_quality", "medium")
                    strategy.logic_chain = analysis.get("logic_chain", [])
                    strategy.invalidating_factors = analysis.get("invalidating_factors", [])
                    strategy.risk_path = analysis.get("risk_path", "")
                    strategy.focus_symbols = focus_symbols
                    
                    strategies.append(strategy)
                    
            logger.info(f"[解析] 成功生成 {len(strategies)} 个策略")
            return strategies

        except json.JSONDecodeError as e:
            logger.error(f"[解析] JSON 解码失败: {e}")
            return []
        except Exception as e:
            logger.error(f"[解析] 解析异常: {e}")
            return []
            event_category = result.get("event_category", "")
            direct_impact = result.get("direct_impact", "")
            second_order = result.get("second_order_impact", "")
            
            event_summary = f"[{event_category}] {direct_impact}"
            if second_order:
                event_summary += f" → {second_order}"
            
            for s in strategy_list:
                strategy = OrderStrategy(
                    symbol=s.get("symbol", ""),
                    direction=s.get("direction", ""),
                    confidence=min(1.0, max(0.0, float(s.get("confidence", 0)))),
                    reasoning=s.get("reasoning", ""),
                    take_profit_pct=float(s.get("take_profit_pct", 0.15)),
                    stop_loss_pct=float(s.get("stop_loss_pct", 0.25)),
                    time_horizon_hours=int(s.get("time_horizon_hours", 72)),
                    trigger_condition=s.get("trigger_condition", "immediate"),
                    max_position_pct=min(0.25, float(s.get("max_position_pct", 0.05))),
                    related_event=event_summary,
                    impact_chain=impact_chain,
                    source_news_ids=[n.get('id', '') for n in news_items[:5]],
                    expiry_time=(datetime.now() + timedelta(hours=4)).isoformat(),
                )
                strategies.append(strategy)
                
        except json.JSONDecodeError as e:
            logger.error(f"[LLM策略引擎] JSON 解析失败: {e}")
            logger.debug(f"[LLM策略引擎] 原始响应: {response_text[:500]}")
        
        return strategies
    
    def _apply_cooldown(self, strategies: List[OrderStrategy]) -> List[OrderStrategy]:
        """应用冷却过滤: 同股票同方向 2 小时内不重复"""
        filtered = []
        now = datetime.now()
        
        for s in strategies:
            cooldown_key = f"{s.symbol}_{s.direction}"
            last_time = self._strategy_cooldown.get(cooldown_key)
            
            if last_time and (now - last_time).total_seconds() < 7200:  # 2 小时冷却
                logger.info(f"[LLM策略引擎] 冷却过滤: {cooldown_key} (上次: {last_time})")
                s.status = "rejected"
                s.reasoning += " [冷却过滤]"
                continue
            
            filtered.append(s)
            self._strategy_cooldown[cooldown_key] = now
            self._recent_strategies.append(s)
        
        # 保留最近 50 条策略
        self._recent_strategies = self._recent_strategies[-50:]
        
        return filtered
    
    def get_recent_strategies(self, limit: int = 10) -> List[Dict]:
        """获取最近生成的策略"""
        return [s.to_dict() for s in self._recent_strategies[-limit:]]
    
    def get_latest_for_symbol(self, symbol: str) -> Optional[OrderStrategy]:
        """获取指定股票的最新策略 (用于注入投票管线)"""
        for s in reversed(self._recent_strategies):
            if s.symbol == symbol and s.status in ("pending", "validated"):
                return s
        return None
    
    def audit_position_async(self, symbol: str, context: Dict, rsshub=None, timeout: int = 15):
        """
        盘中异步审计：交易已执行，LLM 负责检查是否有“暴雷”风险。
        运行在独立线程中，绝不阻塞主进程 (Fire-and-Forget)。
        """
        import threading
        import re
        import logging
        
        def _run_audit():
            logger = logging.getLogger("ftseek_us.llm_audit")
            try:
                # 1. 获取相关新闻 (仅最近 30 分钟)
                news_snippet = "None"
                if rsshub:
                    try:
                        items = rsshub.get_recent_news(hours=0.5, limit=5)
                        ticker = symbol.replace("US.", "")
                        relevant = [i for i in items if ticker.upper() in i.get('title', '').upper()]
                        if relevant:
                            news_snippet = relevant[0].get('title', 'No title')
                    except Exception:
                        pass
                
                # 2. 构造 Prompt (专注于找茬/风险识别)
                prompt = f"""
Role: Risk Auditor. We just executed a {context.get('direction', 'LONG')} trade on {symbol}.
Current Status:
- Price: ${context.get('price', 0):.2f}
- RSI: {context.get('rsi', 0):.1f}
- MACD: {context.get('macd', 0):.2f}
- Trend: {context.get('trend', 'NEUTRAL')}

Latest News: {news_snippet}

Task: Identify any hidden risk (e.g., earnings surprise, sudden bad news, pump & dump pattern).
Output: 
- SAFE: No specific risks found.
- RISK: [Specific Risk Reason] (e.g., RISK: Earnings report in 1 hour).
"""
                
                # 3. 执行推理 (带超时保护)
                try:
                    response = self._call_llm_analysis(prompt, timeout=timeout)
                    
                    # 4. 处理结果
                    if response and "RISK" in response.upper():
                        risk_match = re.search(r"RISK:?\s*(.*)", response, re.IGNORECASE)
                        reason = risk_match.group(1).strip() if risk_match else "Unknown Risk"
                        logger.warning(f"⚠️ [LLM 审计] {symbol} 发现风险: {reason}")
                        # 🚨 动作: 收紧止损 (例如移至成本价)
                        self._tighten_stop_loss_risk(symbol, reason)
                    else:
                        logger.info(f"✅ [LLM 审计] {symbol} 安全确认 (SAFE)")
                        
                except Exception as e:
                    logger.debug(f"[LLM 审计] {symbol} 调用异常 (已忽略): {e}")
            except Exception as e:
                logger.debug(f"[LLM 审计] {symbol} 线程异常 (已忽略): {e}")

        # 🚀 启动后台线程 (Fire-and-Forget)
        threading.Thread(target=_run_audit, daemon=True, name=f"LLM-Audit-{symbol}").start()

    def _tighten_stop_loss_risk(self, symbol: str, reason: str):
        """
        基于风险审计收紧止损
        """
        # 这里需要访问 scheduler 的 position_store 或 trade_store 来更新止损
        # 由于 engine 是单例，可以通过全局引用或者回调注入
        # 暂时只记录日志，后续可接入 TradeExecutor 的修改止损接口
        import logging
        logger = logging.getLogger("ftseek_us.llm_audit")
        logger.warning(f"🚨 [风控动作] {symbol}: 建议收紧止损 | 原因: {reason}")
        # TODO: 调用 scheduler.execution_engine.modify_stop_loss(symbol, new_sl=cost_price)
    
    def get_vote_for_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指定股票的投票格式策略 (用于注入共识引擎)"""
        strategy = self.get_latest_for_symbol(symbol)
        if strategy:
            return strategy.to_vote_dict()
        return None
