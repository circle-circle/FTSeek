"""
LLM 新闻驱动策略引擎 (LLM News-Driven Strategy Engine)

功能:
1. 实时新闻触发 LLM 策略分析 (绕过交易窗口限制)
2. 融合新闻 + 行情 + 板块数据生成结构化策略
3. 输出标准化 StrategySignal 供 SignalStore 消费

设计原则:
- 非阻塞: 所有 LLM 调用异步执行
- 幂等: 同新闻不重复分析 (dedup by news_hash)
- 降级: LLM 不可用时 fallback 到规则引擎

集成位置:
- rsshub_multi_source_cron.py: push_event_impacts() 中自动触发
- 也支持外部手动调用 (如 Harness 巡检)

作者: FTSeek Team
日期: 2026-04-15
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek.factors.llm_news_strategy")

# ============================================================
# 数据结构
# ============================================================

class StrategyDirection(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    WATCH_ONLY = "WATCH_ONLY"  # 仅关注，不立即行动


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"      # >= 0.8
    MEDIUM = "MEDIUM"  # 0.5 - 0.8
    LOW = "LOW"        # < 0.5


class PositionTier(str, Enum):
    FULL = "FULL"       # 满仓 (12%)
    HALF = "HALF"       # 半仓 (8%)
    LIGHT = "LIGHT"     # 轻仓 (4%)
    TRACE = "TRACE"     # 试探仓 (2%)
    SKIP = "SKIP"       # 不建仓


class StrategyUrgency(str, Enum):
    IMMEDIATE = "IMMEDIATE"      # 立即执行 (盘前/盘中)
    NEXT_OPEN = "NEXT_OPEN"      # 下一个开盘执行 (非交易时段)
    WAIT_CONFIRM = "WAIT_CONFIRM" # 等确认信号
    MONITOR_ONLY = "MONITOR_ONLY" # 仅监控


@dataclass
class StockStrategy:
    """单只股票的策略信号"""
    code: str
    name: str
    direction: str = "NEUTRAL"
    confidence: str = "LOW"
    position_tier: str = "SKIP"
    urgency: str = "MONITOR_ONLY"
    reasoning: str = ""
    entry_price: float = 0.0       # 建议入场价 (0=市价)
    stop_loss: float = 0.0         # 止损价
    take_profit: float = 0.0       # 止盈价
    impact_score: float = 0.0      # 新闻影响分数 (-1.0 ~ 1.0)
    
    # 新增: 时效性管理 (2026-04-15)
    sentiment_expiry_minutes: int = 120  # 情绪有效期 (分钟)
    us_hk_mapping: str = ""              # 美股→港股映射逻辑
    decay_logic: str = ""                # 情绪衰减说明
    
    second_order_effects: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)


@dataclass
class NewsStrategySignal:
    """新闻驱动的策略信号 (可能影响多只股票)"""
    news_id: str
    news_title: str
    news_content: str
    event_type: str = ""            # 命中的事件类型
    event_sector: str = ""          # 影响板块
    impact_chain: str = ""          # LLM 输出的传导路径
    timestamp: float = field(default_factory=time.time)
    stock_strategies: List[StockStrategy] = field(default_factory=list)
    overall_sentiment: str = "NEUTRAL"
    llm_raw_response: str = ""
    is_valid: bool = True
    
    # 新增: 时效性和风险偏好 (2026-04-15)
    market_overview: str = ""       # 市场整体判断
    decay_logic: str = ""           # 情绪衰减说明
    risk_bias: str = "中性"         # Risk-On / Risk-Off
    
    # 新增: 地缘政治事件预筛结果 (2026-04-26)
    geo_event_scan: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# LLM Prompt 模板
# ============================================================

STRATEGY_ANALYSIS_PROMPT = """# Role
你是 FTSeek 首席量化宏观分析师。你的任务是分析突发新闻，结合市场数据，生成可执行的交易策略。

# Input
## 新闻
标题: {title}
内容: {content}
来源: {source}
时间: {pub_time}

## 命中的事件类型
事件: {event_name}
板块: {event_sector}
静态逻辑: {static_logic}

## 相关股票池
{watchlist_str}

## 最新行情数据 (如可用)
{market_data_str}

# Analysis Logic (必须逐步执行)

## 第一步: 事件定性
- 这是宏观政策/地缘政治/行业周期/财报/并购/突发事件?
- 风险偏好方向: Risk-On (利好风险资产) 还是 Risk-Off (利好避险资产)?
- 情绪强度: 极强/强/中/弱?

## 第二步: 资产联动分析
基于新闻内容，判断对以下资产类别的影响方向:
- 股票 (美股→港股传导逻辑)
- 债券 (美债收益率变化→降息预期→流动性)
- 商品 (油价/金价/铜价)
- 货币 (美元指数→新兴市场流动性)
- 加密货币 (Risk-On 指标)

## 第三步: 跨市场映射 (美股→港股)
**核心规则**:
1. 美股隔夜表现 → 港股同板块开盘跟涨/跟跌 (强度: 80-100%)
2. 美元连跌 → 港股流动性改善 → 恒指/科技股偏多
3. 油价下跌 → 港股航空股成本下降 (直接利好) / 能源股利润压缩 (直接利空)
4. 黄金新高 → 港股黄金股跟涨 (紫金/招金)
5. 比特币新高 → 港股加密概念股异动 (OSL/新火)
6. 纳指科技股大涨 → 港股科技权重 (腾讯/美团/阿里) 开盘溢价

## 第四步: 二阶效应
- 替代效应: 利空A是否间接利好B?
- 情绪传染: 美股隔夜情绪在港股开盘后多久衰减?
- 资金流向: 南向/北向可能的方向?

## 第五步: 时效性评估
**关键! 评估情绪有效期**:
- "极强"情绪 (如重大和谈/战争爆发): 开盘前2小时最强，随后可能分化
- "强"情绪 (如财报超预期): 开盘半天有效，午后可能消化
- "中"情绪: 持续1-2天
- "弱"情绪: 仅开盘集合竞价有反应
- 给出每个策略的 `sentiment_expiry_minutes` (情绪有效分钟数)

## 第六步: 生成策略
为每个相关标的生成具体策略

# Output Format (严格 JSON, 不要添加任何其他文本)
{{
  "event_classification": "宏观地缘/行业/财报...",
  "risk_bias": "Risk-On / Risk-Off / 中性",
  "asset_impacts": {{
    "stocks_us_hk": "上涨/下跌/分化",
    "bonds": "上涨/下跌",
    "oil": "上涨/下跌",
    "gold": "上涨/下跌",
    "usd": "上涨/下跌",
    "crypto": "上涨/下跌"
  }},
  "market_overview": "整体市场判断 (50字以内)",
  "stocks": [
    {{
      "code": "HK.00753",
      "name": "中国国航",
      "direction": "STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL/WATCH_ONLY",
      "confidence": "HIGH/MEDIUM/LOW",
      "position_tier": "FULL/HALF/LIGHT/TRACE/SKIP",
      "urgency": "IMMEDIATE/NEXT_OPEN/WAIT_CONFIRM/MONITOR_ONLY",
      "sentiment_expiry_minutes": 120,
      "reasoning": "油价下跌降低航司成本，美伊和谈降低地缘溢价 (50字以内)",
      "stop_loss_pct": -3.0,
      "take_profit_pct": 5.0,
      "impact_score": 0.7,
      "us_hk_mapping": "美航+8% → 港股航空开盘跟涨预期",
      "second_order_effects": ["效应1", "效应2"],
      "risk_flags": ["风险1"]
    }}
  ],
  "decay_logic": "情绪衰减逻辑说明 (100字以内)",
  "action_plan": "今日行动计划 (100字以内)"
}}

# Constraints
- 必须输出严格 JSON，不要添加任何其他文本
- 至少包含3只相关标的 (如无则留空数组)
- `sentiment_expiry_minutes` 必须合理 (极强:60-120, 强:120-240, 中:240-480, 弱:60以内)
- 止盈止损必须合理 (止损不超过-8%, 止盈不低于+2%)
- `us_hk_mapping` 字段必须说明美股到港股的映射逻辑
"""

# 降级规则引擎 (LLM 不可用时使用)
FALLBACK_RULES = {
    "收购": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "并购": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "入股": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "合作": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "战略合作": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "扩产": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "突破": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "超预期": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "HALF"},
    "新高": {"direction": "BUY", "confidence": "LOW", "position_tier": "TRACE"},
    "利好": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "增长": {"direction": "BUY", "confidence": "LOW", "position_tier": "TRACE"},
    "不及预期": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "制裁": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "处罚": {"direction": "SELL", "confidence": "HIGH", "position_tier": "LIGHT"},
    "暴跌": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "下滑": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "裁员": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "停火": {"direction": "NEUTRAL", "confidence": "LOW", "position_tier": "SKIP"},
}


# ============================================================
# 策略引擎
# ============================================================

class LLMNewsStrategyEngine:
    """
    LLM 新闻驱动策略引擎
    
    核心流程:
    1. 接收新闻 + 事件匹配结果
    2. 获取相关股票的行情数据
    3. 异步调用 LLM 生成策略
    4. 输出标准化的 NewsStrategySignal
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        self.model = self.config.get("model", "qwen-plus")
        self.timeout = self.config.get("timeout", 60)
        self.enable_thinking = self.config.get("enable_thinking", True)
        self.fallback_enabled = self.config.get("fallback_enabled", True)
        
        # 去重
        self._analyzed_news: Dict[str, float] = {}  # news_hash -> timestamp
        self._dedup_ttl = self.config.get("dedup_ttl", 3600)  # 1小时
        
        # 统计
        self._stats = {
            "total_analyses": 0,
            "llm_success": 0,
            "llm_failed": 0,
            "fallback_used": 0,
            "avg_latency_ms": 0.0,
        }
        
        # 行情缓存
        self._market_cache: Dict[str, dict] = {}
        self._market_cache_ttl = 300  # 5分钟
        
        self._llm_client = None
        self._init_client()
        
        logger.info(
            "LLMNewsStrategyEngine 初始化: enabled=%s, model=%s, timeout=%ds",
            self.enabled, self.model, self.timeout
        )
    
    def _init_client(self):
        """初始化 LLM 客户端"""
        if not self.enabled:
            return
        
        try:
            # 优先使用 openai 兼容客户端
            from openai import AsyncOpenAI
            api_key = os.environ.get("BAILIAN_API_KEY", "")
            if not api_key:
                raise ValueError("BAILIAN_API_KEY 未配置")
            
            self._llm_client = AsyncOpenAI(
                api_key=api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            logger.info("LLM 客户端初始化成功 (OpenAI 兼容)")
        except ImportError:
            # 降级到 ExpertTeam LLMClient
            try:
                import sys
                sys.path.insert(0, '/opt/ftseek/ExpertTeam')
                from utils.llm_client import LLMClient
                self._llm_client = LLMClient(
                    model=f"bailian/{self.model}",
                    timeout=self.timeout,
                    max_retries=2,
                )
                logger.info("LLM 客户端初始化成功 (ExpertTeam)")
            except Exception as e:
                logger.error("LLM 客户端初始化失败: %s", e)
                self.enabled = False
        except Exception as e:
            logger.error("LLM 客户端初始化失败: %s", e)
            self.enabled = False
    
    async def analyze_news(
        self,
        news_title: str,
        news_content: str,
        news_source: str = "",
        pub_time: str = "",
        event_name: str = "",
        event_sector: str = "",
        static_logic: str = "",
        related_stocks: Dict[str, str] = None,
        market_data: Dict[str, dict] = None,
    ) -> Optional[NewsStrategySignal]:
        """
        分析新闻并生成策略信号
        
        Args:
            news_title: 新闻标题
            news_content: 新闻内容
            news_source: 新闻来源
            pub_time: 发布时间
            event_name: 命中的事件类型名称
            event_sector: 影响板块
            static_logic: 静态传导逻辑 (来自 event_impact_map)
            related_stocks: {code: name} 相关股票
            market_data: {code: {price, change_pct, volume, ...}} 行情数据
            
        Returns:
            NewsStrategySignal 或 None (去重/降级失败)
        """
        self._stats["total_analyses"] += 1
        
        # 去重检查
        news_hash = self._compute_hash(news_title, news_content)
        if self._is_duplicate(news_hash):
            logger.debug("新闻去重拦截: %s", news_title[:40])
            return None
        
        if not related_stocks:
            logger.warning("无相关股票，跳过策略分析: %s", news_title[:40])
            return None
        
        # 创建信号骨架
        signal = NewsStrategySignal(
            news_id=news_hash,
            news_title=news_title,
            news_content=news_content[:500],
            event_type=event_name,
            event_sector=event_sector,
        )
        
        start_time = time.time()
        
        try:
            if self.enabled and self._llm_client:
                strategies = await self._call_llm_strategy(
                    news_title, news_content, news_source, pub_time,
                    event_name, event_sector, static_logic,
                    related_stocks, market_data, signal
                )
                signal.stock_strategies = strategies
                signal.llm_raw_response = "LLM_OK"
                self._stats["llm_success"] += 1
            else:
                logger.warning("LLM 不可用，使用降级规则引擎")
                strategies = self._fallback_strategy(
                    news_title, news_content, related_stocks, event_sector, static_logic
                )
                signal.stock_strategies = strategies
                self._stats["fallback_used"] += 1
            
        except Exception as e:
            logger.error("LLM 策略分析失败: %s - %s", news_title[:40], e)
            if self.fallback_enabled:
                strategies = self._fallback_strategy(
                    news_title, news_content, related_stocks, event_sector, static_logic
                )
                signal.stock_strategies = strategies
                self._stats["fallback_used"] += 1
            else:
                self._stats["llm_failed"] += 1
                return None
        
        # 计算整体情绪
        signal.overall_sentiment = self._compute_overall_sentiment(strategies)
        
        # 构建影响链
        if signal.stock_strategies and signal.stock_strategies[0].second_order_effects:
            signal.impact_chain = "; ".join(
                signal.stock_strategies[0].second_order_effects[:3]
            )
        elif static_logic:
            signal.impact_chain = static_logic
        
        # 标记去重
        self._mark_analyzed(news_hash)
        
        latency_ms = (time.time() - start_time) * 1000
        self._update_latency(latency_ms)
        
        logger.info(
            "新闻策略分析完成: %s | %s条信号 | %.0fms | LLM=%s",
            news_title[:40], len(signal.stock_strategies), latency_ms,
            self._stats["llm_success"] > self._stats["llm_failed"]
        )
        
        return signal
    
    async def _call_llm_strategy(
        self, title, content, source, pub_time,
        event_name, event_sector, static_logic,
        related_stocks, market_data, signal=None
    ) -> List[StockStrategy]:
        """调用 LLM 生成策略"""
        
        # 构建 watchlist 字符串
        watchlist_lines = []
        for code, name in related_stocks.items():
            md = market_data.get(code, {}) if market_data else {}
            price_str = f" (现价: {md.get('price', 'N/A')}, 涨跌: {md.get('change_pct', 'N/A')}%)" if md else ""
            watchlist_lines.append(f"- {code}: {name}{price_str}")
        watchlist_str = "\n".join(watchlist_lines)
        
        # 构建行情数据字符串
        if market_data:
            market_lines = []
            for code, data in market_data.items():
                market_lines.append(
                    f"{code}: price={data.get('price', 'N/A')}, "
                    f"change={data.get('change_pct', 'N/A')}%, "
                    f"volume={data.get('volume', 'N/A')}"
                )
            market_data_str = "\n".join(market_lines)
        else:
            market_data_str = "暂无实时行情数据"
        
        # P1 新增: 地缘政治事件预筛 (灵感: QuantDinger)
        from app.factors.geo_pre_filter import GeoEventPreFilter
        prefilter = GeoEventPreFilter()
        geo_scan = prefilter.scan(title, content[:1000])
        
        if geo_scan["is_geo_event"]:
            logger.info(
                "[GeoPreFilter] 地缘事件标记 | severity=%s | matched=%s | flags=%s",
                geo_scan["severity"], geo_scan["matched_patterns"], geo_scan["context_flags"]
            )
            signal.geo_event_scan = geo_scan  # 附加到信号对象
        
        # 构建 prompt (整合预筛标记)
        from app.factors.news_prompt_templates import build_strategy_analysis_prompt
        prompt = build_strategy_analysis_prompt(
            title=title,
            content=content[:1000],
            source=source,
            pub_time=pub_time,
            event_name=event_name,
            event_sector=event_sector,
            static_logic=static_logic,
            watchlist_str=watchlist_str,
            market_data_str=market_data_str,
            geo_scan=geo_scan,
        )
        
        # 调用 LLM
        if hasattr(self._llm_client, 'chat'):
            # OpenAI 兼容客户端
            extra_body = {}
            if self.enable_thinking:
                extra_body["enable_thinking"] = True
            
            response = await asyncio.wait_for(
                self._llm_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是 FTSeek 首席量化策略分析师，输出严格 JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=2000,
                    extra_body=extra_body if extra_body else None,
                ),
                timeout=self.timeout,
            )
            raw_content = response.choices[0].message.content
        else:
            # ExpertTeam LLMClient (同步)
            import sys
            sys.path.insert(0, '/opt/ftseek/ExpertTeam')
            from utils.llm_client import LLMClient
            client = LLMClient(
                model=f"bailian/{self.model}",
                timeout=self.timeout,
                max_retries=1,
            )
            messages = [
                {"role": "system", "content": "你是 FTSeek 首席量化策略分析师，输出严格 JSON。"},
                {"role": "user", "content": prompt},
            ]
            resp = await asyncio.wait_for(
                client.chat(messages, use_cache=False),
                timeout=self.timeout,
            )
            raw_content = resp.content
        
        # 解析 JSON 响应
        strategies = self._parse_llm_response(raw_content, related_stocks, signal)
        return strategies
    
    def _parse_llm_response(self, raw_content: str, related_stocks: Dict, signal: 'NewsStrategySignal' = None) -> List[StockStrategy]:
        """解析 LLM 的 JSON 响应为 StockStrategy 列表"""
        strategies = []
        
        try:
            # 提取 JSON 块
            import re
            json_match = re.search(r'\{[\s\S]*\}', raw_content)
            if not json_match:
                logger.warning("LLM 响应无 JSON: %s", raw_content[:200])
                return self._fallback_from_response(raw_content, related_stocks)
            
            data = json.loads(json_match.group())
            
            # 解析整体市场判断和情绪衰减逻辑 (新增)
            if signal:
                signal.market_overview = data.get("market_overview", "")
                signal.decay_logic = data.get("decay_logic", "")
                signal.risk_bias = data.get("risk_bias", "中性")
            
            # 解析股票策略
            for stock_data in data.get("stocks", []):
                code = stock_data.get("code", "")
                if not code:
                    continue
                
                # 验证股票代码在相关列表中
                if code not in related_stocks:
                    logger.debug("LLM 返回未知股票: %s", code)
                    continue
                
                stop_loss_pct = stock_data.get("stop_loss_pct", -3.0)
                take_profit_pct = stock_data.get("take_profit_pct", 8.0)
                
                strategy = StockStrategy(
                    code=code,
                    name=stock_data.get("name", related_stocks.get(code, code)),
                    direction=stock_data.get("direction", "NEUTRAL"),
                    confidence=stock_data.get("confidence", "LOW"),
                    position_tier=stock_data.get("position_tier", "SKIP"),
                    urgency=stock_data.get("urgency", "MONITOR_ONLY"),
                    reasoning=stock_data.get("reasoning", ""),
                    stop_loss=stop_loss_pct,
                    take_profit=take_profit_pct,
                    impact_score=stock_data.get("impact_score", 0.0),
                    # 新增时效性字段
                    sentiment_expiry_minutes=stock_data.get("sentiment_expiry_minutes", 120),
                    us_hk_mapping=stock_data.get("us_hk_mapping", ""),
                    second_order_effects=stock_data.get("second_order_effects", []),
                    risk_flags=stock_data.get("risk_flags", []),
                )
                strategies.append(strategy)
            
        except Exception as e:
            logger.error("LLM 响应解析失败: %s", e)
            strategies = self._fallback_from_response(raw_content, related_stocks)
        
        if not strategies:
            strategies = self._fallback_strategy(
                "", "", related_stocks, "", ""
            )
        
        return strategies
    
    def _fallback_from_response(self, raw_content: str, related_stocks: Dict) -> List[StockStrategy]:
        """从 LLM 原始响应中提取信息，降级处理"""
        strategies = []
        for code, name in related_stocks.items():
            strategy = StockStrategy(
                code=code,
                name=name,
                direction="NEUTRAL",
                confidence="LOW",
                position_tier="SKIP",
                reasoning=f"LLM 响应解析失败，降级为观望",
            )
            strategies.append(strategy)
        return strategies
    
    def _fallback_strategy(
        self, title: str, content: str, related_stocks: Dict,
        event_sector: str, static_logic: str
    ) -> List[StockStrategy]:
        """降级规则引擎"""
        text = f"{title} {content}"
        strategies = []
        
        for code, name in related_stocks.items():
            rule = None
            for keyword, r in FALLBACK_RULES.items():
                if keyword in text:
                    rule = r
                    break
            
            if rule:
                strategy = StockStrategy(
                    code=code,
                    name=name,
                    direction=rule["direction"],
                    confidence=rule["confidence"],
                    position_tier=rule["position_tier"],
                    urgency="NEXT_OPEN" if rule["direction"] in ("BUY", "SELL") else "MONITOR_ONLY",
                    reasoning=f"规则引擎命中关键词 '{keyword}'",
                    stop_loss=-3.0 if rule["direction"] == "BUY" else 3.0,
                    take_profit=8.0 if rule["direction"] == "BUY" else -8.0,
                    impact_score=0.5 if rule["direction"] == "BUY" else (-0.5 if rule["direction"] == "SELL" else 0.0),
                )
            else:
                strategy = StockStrategy(
                    code=code,
                    name=name,
                    direction="WATCH_ONLY",
                    confidence="LOW",
                    position_tier="SKIP",
                    urgency="MONITOR_ONLY",
                    reasoning=f"无匹配规则，建议关注",
                )
            
            strategies.append(strategy)
        
        return strategies
    
    def _compute_overall_sentiment(self, strategies: List[StockStrategy]) -> str:
        """计算整体情绪"""
        if not strategies:
            return "NEUTRAL"
        
        score_sum = sum(s.impact_score for s in strategies)
        avg = score_sum / len(strategies)
        
        if avg > 0.3:
            return "BULLISH"
        elif avg < -0.3:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    def _compute_hash(self, title: str, content: str) -> str:
        return hashlib.md5(f"{title}{content[:200]}".encode()).hexdigest()[:16]
    
    def _is_duplicate(self, news_hash: str) -> bool:
        now = time.time()
        if news_hash in self._analyzed_news:
            if now - self._analyzed_news[news_hash] < self._dedup_ttl:
                return True
            else:
                del self._analyzed_news[news_hash]
        return False
    
    def _mark_analyzed(self, news_hash: str):
        self._analyzed_news[news_hash] = time.time()
    
    def _update_latency(self, latency_ms: float):
        n = self._stats["llm_success"] + self._stats["llm_failed"]
        if n > 0:
            prev = self._stats["avg_latency_ms"]
            self._stats["avg_latency_ms"] = prev + (latency_ms - prev) / n
    
    def get_stats(self) -> dict:
        return {**self._stats}


# ============================================================
# 全局单例
# ============================================================

_global_engine: Optional[LLMNewsStrategyEngine] = None

def get_news_strategy_engine(config: dict = None) -> LLMNewsStrategyEngine:
    """获取全局策略引擎单例"""
    global _global_engine
    if _global_engine is None:
        _global_engine = LLMNewsStrategyEngine(config)
    return _global_engine


def reset_engine():
    """重置引擎 (测试用)"""
    global _global_engine
    _global_engine = None
