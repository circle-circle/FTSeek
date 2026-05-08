"""LLM 盘前分析模块

使用混合模型策略:
  1. qwen-plus 批量初筛所有事件包
  2. relevance >= 0.5 的高价值包用 qwen-max 深度分析

输出 PreMarketAssessment (与用户指定的 JSON Schema 对齐)。
"""

import asyncio
import json
import logging
import re
import time
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger("ftseek.pipeline.premarket_analyst")


# ============================================================
#  盘前评估结果 (对齐用户 JSON Schema)
# ============================================================
@dataclass
class PreMarketAssessment:
    """LLM 盘前分析结果 (支持新版二阶推理 Prompt)"""

    session: str = "pre_market"
    primary_scene: str = ""
    event_pack_id: str = ""
    focus_symbols: List[str] = field(default_factory=list)
    key_events: List[str] = field(default_factory=list)

    # 核心决策字段
    direction: str = "NO_TRADE"       # BUY / SELL / NO_TRADE
    tradeability: str = "no_trade"    # tradeable / watch_only / no_trade
    confidence: float = 0.0
    event_quality: str = "low"        # high / medium / low
    event_type: str = ""              # 事件类型 (e.g., geopolitical, earnings)
    direct_impact: List[str] = field(default_factory=list)
    second_order_insight: str = ""    # 二阶推理核心逻辑
    pricing_status: str = "unknown"   # not_priced_in / partially_priced_in / fully_priced_in
    cross_market_signal: Dict[str, Any] = field(default_factory=dict)
    logic_chain: List[str] = field(default_factory=list)
    invalidating_factors: List[str] = field(default_factory=list)
    risk_path: str = ""
    stop_loss: str = ""
    position_sizing: str = ""
    notes: str = ""                   # 补充说明 (新增字段以匹配 Prompt)

    # 兼容旧字段
    relevance_score: float = 0.0
    strength_score: float = 0.0
    duration: str = "intraday"
    position_ratio: float = 0.0
    entry_style: str = "wait"
    exit_style: str = "fixed_stop"
    risk_flags: List[str] = field(default_factory=list)
    trading_thesis: List[str] = field(default_factory=list)
    recommendation: str = ""
    execution_mode: str = "manual"

    timestamp: float = field(default_factory=time.time)
    raw_response: Optional[str] = None
    trace_id: str = ""

    # ── 路由判断 ─────────────────────────────────────────────────

    def should_auto_execute(self) -> bool:
        """confidence >= 0.75 && relevance >= 0.75 -> 自动执行候选"""
        # 必须 tradeability 允许
        if self.tradeability == "no_trade": return False
        return self.confidence >= 0.75 and self.relevance_score >= 0.75

    def should_review(self) -> bool:
        """0.55 <= confidence < 0.75 -> 人工确认"""
        return 0.55 <= self.confidence < 0.75

    def should_observe(self) -> bool:
        """confidence < 0.55 -> 仅观察"""
        return self.confidence < 0.55

    def to_dict(self) -> Dict:
        return {
            "session": self.session,
            "primary_scene": self.primary_scene,
            "event_pack_id": self.event_pack_id,
            "focus_symbols": self.focus_symbols,
            "key_events": self.key_events,
            "direction": self.direction,
            "tradeability": self.tradeability,
            "confidence": round(self.confidence, 3),
            "event_quality": self.event_quality,
            "pricing_status": self.pricing_status,
            "cross_market_signal": self.cross_market_signal,
            "logic_chain": self.logic_chain,
            "risk_path": self.risk_path,
            "stop_loss": self.stop_loss,
            "position_sizing": self.position_sizing,
            # 兼容
            "relevance_score": round(self.relevance_score, 3),
            "strength_score": round(self.strength_score, 3),
            "duration": self.duration,
            "position_ratio": round(self.position_ratio, 3),
            "entry_style": self.entry_style,
            "exit_style": self.exit_style,
            "risk_flags": self.risk_flags,
            "trading_thesis": self.trading_thesis,
            "recommendation": self.recommendation,
            "execution_mode": self.execution_mode,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ============================================================
#  LLM 盘前分析师
# ============================================================

SYSTEM_PROMPT = """
你是 FTSeek 量化交易系统的首席宏观策略师（Chief Macro Strategist）。
你的任务是基于输入的多源事件包、港股/美股行情、跨市场联动信息和历史复盘信息，进行二阶推理（Second-Order Reasoning），输出适合自动交易系统使用的结构化结论。

【核心能力 - 港股特异性强化】:
- **政策敏感度极高**: 港股受宏观政策和监管环境影响极大 (如互联网反垄断、数据安全法、教育双减、医药集采)。
- **资金面驱动**: 重点关注南向资金 (Southbound Flow) 和外资流向。
- **二阶推理**: 事件事实 -> 传导链 -> 资金链 -> 交易机会。

你必须遵守以下原则：
- 只基于输入事实推理，不得编造未知内容
- **政策一票否决**: 如果涉及强监管/反垄断/制裁风险，必须给出极低的置信度 (Confidence < 0.3) 并建议 NO_TRADE 或 SHORT。
- 如果信息不完整、逻辑链不闭环、或定价状态不明确，必须输出 NO_TRADE
- 置信度低于 0.6 时，必须输出 NO_TRADE
- 宁可错过，不要做错
- **突发事件特权**: 如果涉及地缘政治突变、战争、核心供应链断裂（如伊朗石油、芯片禁令），即使部分细节缺失，也应给出较高的置信度 (0.7+) 并推荐对冲方向。
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

【强制输出 JSON 格式要求】:
必须强制输出以下字段：
- session: 分析时段 (如 "pre_market_hk")
- market_regime: 整体市场环境 ("RISK_ON" / "NEUTRAL" / "RISK_OFF")
- risk_level: 风险等级 ("LOW" / "MEDIUM" / "HIGH")
- stop_trading: 如果大盘极差，必须设为 true 并说明原因
- focus_symbols: 必须输出最相关的标的代码，最多 5 个
- direction: BUY / SELL / NO_TRADE
- confidence: 0.0 ~ 1.0
- invalidating_factors: 必须明确写出哪些因素会推翻当前判断
- logic_chain: 必须用数组列出推理步骤

如果没有高确信度交易机会，direction 必须是 NO_TRADE，risk_level 设为 HIGH，stop_trading 设为 false (除非大盘崩了)。

输出格式要求：
严格输出 JSON，结构如下：
{
  "session": "pre_market_hk",
  "market_regime": "NEUTRAL",
  "risk_level": "MEDIUM",
  "stop_trading": false,
  "plans": [
    {
      "symbol": "HK.00700",
      "bias": "LONG",
      "conviction": 0.85,
      "key_trigger": "突破 380 且成交量放大",
      "invalidation": "如果跌破 365，逻辑失效",
      "news_impact": "财报利好，外资流入"
    }
  ]
}
"""


# ============================================================
#  Prompt 模板
# ============================================================

USER_PROMPT_TEMPLATE = """
请基于以下事件包，输出严格 JSON。

【昨日复盘反馈】
{feedback_context}

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

请按 SYSTEM_PROMPT 的要求进行二阶推理并输出。
"""

@dataclass
class PreMarketAnalyst:
    """LLM 盘前分析师 (二阶推理增强版)

    混合模型策略:
      - fast_model (qwen3.6-flash): 批量初筛所有事件包，快速推理 (默认)
      - deep_model (qwen3.6-plus): 深度分析高价值包/宏观策略 (高精度)
    """

    config: Dict = field(default_factory=dict)
    fast_model: str = "qwen3.6-flash"  # 使用 qwen3.6-flash 进行快速推理
    deep_model: str = "qwen3.6-plus"   # 稳定期: 默认使用 qwen3.6-plus 进行宏观策略分析
    fallback_model: str = "qwen-turbo"  # 备用模型
    deep_threshold: float = 0.9         # 优化: 提高阈值，避免慢速模型阻塞突发新闻
    timeout: int = 120                   # P2 优化: 提升推理质量，延长至 120s
    deep_timeout: int = 120              # P2 优化: 提升推理质量，延长至 120s
    max_retries: int = 1
    enable_thinking: bool = False
    batch_size: int = 3

    def __post_init__(self):
        self.config = self.config or {}
        self.fast_model = self.config.get("fast_model", self.fast_model)
        self.deep_model = self.config.get("deep_model", self.deep_model)
        self.deep_threshold = self.config.get("deep_threshold", self.deep_threshold)
        self.timeout = self.config.get("timeout", self.timeout)
        self.deep_timeout = self.config.get("deep_timeout", self.deep_timeout)
        self.max_retries = self.config.get("max_retries", self.max_retries)
        self.enable_thinking = self.config.get("enable_thinking", self.enable_thinking)
        self.batch_size = self.config.get("batch_size", self.batch_size)

        self._client = None
        self._stats = {
            "total_analyzed": 0,
            "fast_model_calls": 0,
            "deep_model_calls": 0,
            "errors": 0,
            "fallbacks": 0,
        }

        # 美股市场上下文缓存 (2026-04-13 新增)
        self._us_market_context_cache: Optional[str] = None
        self._us_context_fetch_time: float = 0
        self._us_context_ttl = 3600  # 1 小时 TTL (盘前分析期间有效)

    async def analyze(self, pack: Any) -> PreMarketAssessment:
        """分析单个事件包

        根据 relevance 决定使用 fast_model 还是 deep_model。
        """
        relevance = getattr(pack, "relevance", {}).get("overall", 0.0)
        use_deep = relevance >= self.deep_threshold

        # 确定模型: 如果 relevance 高则用 deep_model，否则用 fast_model
        model = self.deep_model if use_deep else self.fast_model
        timeout = self.deep_timeout if use_deep else self.timeout

        # 构建 Prompt (整合 System + User Prompt)
        messages = self._build_prompt(pack)

        # 调用 LLM
        raw_response = await self._call_llm(messages, model, timeout)
        if raw_response is None:
            self._stats["fallbacks"] += 1
            return self._make_fallback("LLM 调用失败", pack)

        # 解析并校验响应
        try:
            assessment = self._parse_response(raw_response, pack)
            assessment.raw_response = raw_response
            assessment.timestamp = time.time()
            
            # P2 新增: 强制校验输出 (置信度/Tradeability)
            validated_assessment = self._validate_assessment(assessment)
            
            logger.info(
                f"[PreMarketAnalyst] 分析完成: {pack.id} -> {validated_assessment.direction} "
                f"(conf={validated_assessment.confidence:.2f}, model={model})"
            )
            return validated_assessment
        except Exception as e:
            logger.error(f"[PreMarketAnalyst] 解析失败: {pack.id} - {e}")
            self._stats["fallbacks"] += 1
            return self._make_fallback(f"解析失败: {e}", pack, raw_response)

    def _validate_assessment(self, assessment: PreMarketAssessment) -> PreMarketAssessment:
        """
        P2 新增: 校验 LLM 输出，确保符合风控要求 (业务规则)
        """
        # 规则 0: 标的为空 -> 无法交易
        if not assessment.focus_symbols:
            logger.warning(f"🛑 [校验] 标的为空 -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            return assessment

        # 规则 1: 定价状态已充分 (fully_priced_in) -> 默认 NO_TRADE
        if assessment.pricing_status == "fully_priced_in":
            logger.warning(f"⚠️ [校验] 事件已充分定价 ({assessment.pricing_status}) -> 降级为 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            return assessment

        # 规则 2: 置信度低于 0.6 -> 强制 NO_TRADE
        if assessment.confidence < 0.60:
            logger.warning(f"📉 [校验] 置信度过低 ({assessment.confidence:.2f} < 0.6) -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            
        # 规则 3: Tradeability 为 no_trade -> 强制 NO_TRADE
        elif assessment.tradeability == "no_trade":
            logger.warning(f"🛑 [校验] Tradeability 为 no_trade -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            
        return assessment

    async def analyze_batch(self, packs: List[Any]) -> List[PreMarketAssessment]:
        """批量分析事件包 (分批并行)"""
        if not packs:
            return []

        logger.info(
            f"[PreMarketAnalyst] 开始批量分析: {len(packs)} 个事件包 "
            f"(batch_size={self.batch_size})"
        )

        results = []
        for i in range(0, len(packs), self.batch_size):
            batch = packs[i : i + self.batch_size]
            tasks = [self.analyze(pack) for pack in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.error(f"[PreMarketAnalyst] 分析失败: {batch[j].id} - {result}")
                    self._stats["errors"] += 1
                    results.append(self._make_fallback(f"分析异常: {result}", batch[j]))
                else:
                    results.append(result)

        self._stats["total_analyzed"] += len(packs)
        logger.info(f"[PreMarketAnalyst] 批量分析完成: {len(results)} 个结果")
        return results

    async def _analyze_with_model(
        self, pack: Any, model: str, timeout: int
    ) -> PreMarketAssessment:
        """使用指定模型分析事件包"""
        is_deep = model == self.deep_model
        self._stats["deep_model_calls" if is_deep else "fast_model_calls"] += 1

        # 构建 prompt
        messages = self._build_prompt(pack)

        # 调用 LLM
        raw_response = await self._call_llm(messages, model, timeout)
        if raw_response is None:
            self._stats["fallbacks"] += 1
            return self._make_fallback("LLM 调用失败", pack)

        # 解析响应
        try:
            assessment = self._parse_response(raw_response, pack)
            assessment.raw_response = raw_response
            assessment.timestamp = time.time()
            logger.info(
                f"[PreMarketAnalyst] 分析完成: {pack.id} -> {assessment.direction} "
                f"(conf={assessment.confidence:.2f}, model={model})"
            )
            return assessment
        except Exception as e:
            logger.error(f"[PreMarketAnalyst] 解析失败: {pack.id} - {e}")
            self._stats["fallbacks"] += 1
            return self._make_fallback(f"解析失败: {e}", pack, raw_response)

    def _build_prompt(self, pack: Any) -> List[Dict[str, str]]:
        """构建 LLM prompt (使用 USER_PROMPT_TEMPLATE)"""
        
        # 1. 获取事件包信息
        headline = getattr(pack, "title", "") or getattr(pack, "headline", "")
        body = getattr(pack, "content", "") or getattr(pack, "summary", "")
        source = getattr(pack, "source", "Unknown")
        publish_time = getattr(pack, "published_at", getattr(pack, "fetched_at", "Unknown"))
        symbols = ", ".join(getattr(pack, "focus_symbols", []))
        industries = getattr(pack, "industry", "N/A")
        
        # 2. 获取市场环境 & 跨市场信息
        market_regime = getattr(pack, "market_regime", "Normal")
        us_market_context = self._get_us_market_context()
        
        # 尝试解析 US 市场上下文以填充特定字段 (简化处理)
        us_close = "见跨市场联动信息"
        adr_effect = "N/A"
        price_action = getattr(pack, "price_action", "N/A")
        volume_volatility = getattr(pack, "volatility", "N/A")
        historical_cases = "N/A"
        related_news = "N/A"

        # 3. 加载昨日复盘反馈 (Phase 5: 闭环反馈)
        feedback_context = self._load_feedback_context()

        # 4. 使用模板格式化
        try:
            user_prompt = USER_PROMPT_TEMPLATE.format(
                feedback_context=feedback_context,
                headline=headline,
                body=body,
                source=source,
                publish_time=publish_time,
                symbols=symbols,
                industries=industries,
                market_regime=market_regime,
                us_close=us_market_context,
                adr_effect=adr_effect,
                price_action=price_action,
                volume_volatility=volume_volatility,
                historical_cases=historical_cases,
                related_news=related_news
            )
        except KeyError as e:
            logger.error(f"[PreMarketAnalyst] Prompt 模板缺少字段: {e}")
            user_prompt = f"请分析以下事件包: {headline}\n\n{body}"

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    def _load_feedback_context(self) -> str:
        """加载历史教训 + 昨日复盘反馈 (增强版)

        Phase 5 升级: 注入 Agent 记忆 + Lessons DB (港股记忆闭环)
        """
        try:
            # 1. 优先: Agent 记忆注入 (lessons_db + hk.json, Token 预算控制)
            from app.review.hk_memory_injector import get_hk_memory_injector
            injector = get_hk_memory_injector()
            memory_context = injector.inject()

            # 2. 追加: 昨日复盘报告 (原有逻辑, 保留向后兼容)
            report_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'data', 'daily_review_report.json'
            )
            report_context = ""
            if os.path.exists(report_path):
                with open(report_path, 'r') as f:
                    report = json.load(f)
                hit_rate = report.get('hit_rate', 0)
                total = report.get('total_targets', 0)
                hits = report.get('hits', 0)
                details = report.get('details', [])
                report_context = f"昨日预测命中率: {hit_rate:.0%} ({hits}/{total})。\n复盘案例:\n"
                for d in details[:3]:
                    sym = d.get('symbol', '')
                    logic = d.get('logic', '')
                    reason = d.get('reason', '')
                    is_hit = d.get('is_hit', False)
                    report_context += f"- {'✅' if is_hit else '❌'} {sym}: {logic} -> {reason}\n"

            if report_context:
                return f"{memory_context}\n\n【昨日复盘统计】\n{report_context}"
            return memory_context

        except Exception as e:
            logger.warning(f"加载反馈上下文失败: {e}")
            return "历史教训加载异常，请基于当前信息独立推理。"

    def _get_us_market_context(self) -> str:
        """获取美股市场上下文 (带缓存)"""
        import time
        now = time.time()

        # 检查缓存
        if self._us_market_context_cache and (now - self._us_context_fetch_time) < self._us_context_ttl:
            return self._us_market_context_cache

        try:
            from app.utils.cross_market_mapper import get_cross_market_mapper
            from app.data.us_data_fetcher import get_us_data_fetcher

            mapper = get_cross_market_mapper()
            fetcher = get_us_data_fetcher()

            # 获取核心美股代码
            core_codes = mapper.get_core_us_codes()
            if not core_codes:
                # fallback: 使用 watch list
                from app.config import WATCH_LIST_US
                core_codes = WATCH_LIST_US[:15]  # 限制数量

            if not core_codes:
                return "⚠️ 美股监控名单为空"

            parts = []

            # 1. 美股市场收盘数据
            market_context = fetcher.get_premarket_context(core_codes)
            if market_context and "数据获取失败" not in market_context:
                parts.append(market_context)

            # 2. 跨市场事件缓存 (2026-04-13 新增)
            event_context = self._get_cross_market_events_context()
            if event_context:
                parts.append(event_context)

            # 3. 双重上市套利检查 (2026-04-13 新增)
            arb_context = self._get_arb_context(fetcher)
            if arb_context:
                parts.append(arb_context)

            # 4. 财报日历 (2026-04-23 新增)
            earnings_context = self._get_earnings_context()
            if earnings_context:
                parts.append(earnings_context)

            context = "\n\n".join(parts) if parts else "⚠️ 美股数据获取失败，请检查 API Key 配置"

            # 缓存
            self._us_market_context_cache = context
            self._us_context_fetch_time = now

            return context

        except Exception as e:
            logger.warning(f"[PreMarketAnalyst] 获取美股上下文失败: {e}")
            return "⚠️ 美股数据获取失败，请检查富途 OpenD 连接"

    def _get_cross_market_events_context(self) -> str:
        """获取跨市场事件缓存上下文"""
        try:
            from app.data.cross_market_event_cache import get_cross_market_event_cache
            cache = get_cross_market_event_cache()

            events = cache.read_for_premarket(hours=12)
            if not events:
                return ""

            lines = ["## 📰 跨市场事件 (美股交易时段)"]
            lines.append("")

            for event in events[:10]:  # 最多显示 10 个
                impact_emoji = "🔴" if event.impact_score < -0.3 else "🟢" if event.impact_score > 0.3 else "⚪"
                hk_list = ", ".join(event.hk_codes[:3])
                lines.append(
                    f"{impact_emoji} **{event.title}**\n"
                    f"   美股: {event.us_code} → 港股: {hk_list}\n"
                    f"   影响: {event.impact_score:+.2f} | 类型: {event.event_type}"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"[PreMarketAnalyst] 获取跨市场事件失败: {e}")
            return ""

    def _get_arb_context(self, fetcher) -> str:
        """获取双重上市套利上下文"""
        try:
            from app.strategy.arb_dual_listed import get_dual_listed_arb

            arb = get_dual_listed_arb()

            # 获取美股价格
            us_prices = {}
            for pair in arb.pairs:
                quote = fetcher.get_quote(pair.us_code)
                if quote and quote.get("price", 0) > 0:
                    us_prices[pair.us_code] = quote["price"]

            if not us_prices:
                return ""

            # 获取港股价格 (从配置或缓存)
            # 盘前分析时港股未开盘，使用上一交易日收盘价
            hk_prices = self._get_hk_prev_close_prices(list(us_prices.keys()))
            # 过滤掉 0 值 (TODO placeholder 返回 0.0 表示数据不可用)
            hk_prices = {k: v for k, v in hk_prices.items() if v > 0}

            if not hk_prices:
                return ""

            # 生成信号
            signals = arb.generate_signals(us_prices, hk_prices)

            if not signals:
                return ""

            return arb.format_report(signals)

        except Exception as e:
            logger.warning(f"[PreMarketAnalyst] 获取套利上下文失败: {e}")
            return ""

    def _get_earnings_context(self) -> str:
        """获取财报日历上下文 (2026-04-23 新增)"""
        try:
            from app.data.earnings_calendar import get_earnings_calendar
            cal = get_earnings_calendar()
            return cal.format_context()
        except Exception as e:
            logger.warning(f"[PreMarketAnalyst] 获取财报日历失败: {e}")
            return ""

    def _get_hk_prev_close_prices(self, us_codes: List[str]) -> Dict[str, float]:
        """
        获取港股上一交易日收盘价

        盘前分析时港股未开盘，使用上一交易日收盘价计算套利

        Returns:
            {hk_code: price}
        """
        # 尝试从数据源获取，如果没有则返回空
        # 实际应用中应从富途 API 或数据库获取
        try:
            from app.utils.cross_market_mapper import get_cross_market_mapper
            mapper = get_cross_market_mapper()

            hk_prices = {}
            for us_code in us_codes:
                hk_codes = mapper.get_hk_codes_by_us(us_code)
                for hk_code in hk_codes:
                    # TODO: 从富途 API 获取上一交易日收盘价
                    # 目前返回 0 表示数据不可用 → generate_signals 会跳过该配对
                    hk_prices[hk_code] = 0.0

            return hk_prices

        except Exception:
            return {}

    async def _call_llm(self, messages: List[Dict], model: str, timeout: int) -> Optional[str]:
        """调用 DashScope API (带自动降级)"""
        for attempt in range(self.max_retries + 1):
            # P1 修复 (2026-05-06): 重试间添加延迟，让瞬态错误有时间自愈
            if attempt > 0:
                delay = 2 ** attempt  # 指数退避: 2s, 4s
                logger.warning(f"[PreMarketAnalyst] 重试等待 {delay}s (attempt={attempt})")
                await asyncio.sleep(delay)
            
            current_model = model
            
            # 自动降级：如果主模型失败，切换到备用模型
            if attempt > 0 and model == self.fast_model:
                current_model = self.fallback_model
                logger.warning(f"[PreMarketAnalyst] ⚡ 降级到备用模型 {current_model}")
            
            try:
                from app.core.unified_llm_factory import get_llm_client
                
                client = get_llm_client(self.config)
                if not client:
                    logger.error("Unified LLM Client not available")
                    return None

                extra_body: Dict[str, Any] = {}
                if self.enable_thinking:
                    extra_body["enable_thinking"] = True

                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=current_model,
                        messages=messages,
                        extra_body=extra_body if extra_body else None,
                    ),
                    timeout=timeout,
                )
                result = response.choices[0].message.content
                # ⚠️ P0 修复 (2026-05-06): 禁止关闭全局单例 client
                # get_llm_client() 返回全局单例 AsyncOpenAI，close() 会销毁底层 httpx 连接
                # 但单例引用仍存在，后续调用会返回已关闭的 client → 所有 LLM 调用失败
                # 正确做法：不关闭 client，由 unified_llm_factory 管理生命周期
                return result

            except ImportError:
                logger.error("openai 包未安装，无法调用 LLM")
                return None
            except asyncio.TimeoutError:
                logger.warning(
                    f"[PreMarketAnalyst] LLM 超时 (attempt={attempt+1}, model={current_model})"
                )
                if attempt >= self.max_retries:
                    return None
            except Exception as e:
                logger.error(
                    f"[PreMarketAnalyst] LLM 调用异常 (attempt={attempt+1}, model={current_model}): {e}"
                )
                if attempt >= self.max_retries:
                    return None

        return None

    def _parse_response(self, raw: str, pack: Any) -> PreMarketAssessment:
        """解析 LLM JSON 响应 (适配新版 Chief Macro Strategist Prompt)"""
        
        # 1. 尝试解析 JSON
        try:
            # 清理可能的 Markdown 标记
            cleaned_raw = re.sub(r'^```json\s*', '', raw, flags=re.MULTILINE)
            cleaned_raw = re.sub(r'\s*```$', '', cleaned_raw, flags=re.MULTILINE)
            validated_data = json.loads(cleaned_raw.strip())
        except json.JSONDecodeError as e:
            logger.error(f"[PreMarketAnalyst] JSON 解析失败: {e}")
            return self._make_fallback("JSON 解析失败", pack, raw)

        # 2. 提取核心字段并校验类型
        direction = str(validated_data.get("direction", "NO_TRADE")).upper()
        tradeability = str(validated_data.get("tradeability", "no_trade"))
        confidence = float(validated_data.get("confidence", 0.0))
        focus_symbols = validated_data.get("focus_symbols", [])
        
        # 兼容旧逻辑：如果 LLM 输出了旧字段，尝试映射
        if not focus_symbols:
             focus_symbols = validated_data.get("focus_symbols", getattr(pack, "focus_symbols", []))
             
        # 3. 构建 Assessment 对象
        assessment = PreMarketAssessment(
            session=validated_data.get("session", "pre_market"),
            primary_scene=validated_data.get("primary_scene", ""),
            event_pack_id=getattr(pack, "id", ""),
            focus_symbols=focus_symbols if isinstance(focus_symbols, list) else [],
            key_events=validated_data.get("key_events", []),
            
            # 核心决策
            direction=direction,
            tradeability=tradeability,
            confidence=confidence,
            event_quality=validated_data.get("event_quality", "medium"),
            
            # 二阶推理与分析
            event_type=validated_data.get("event_type", ""),
            direct_impact=validated_data.get("direct_impact", []),
            second_order_insight=validated_data.get("second_order_insight", ""),
            pricing_status=validated_data.get("pricing_status", "unknown"),
            cross_market_signal=validated_data.get("cross_market_signal", {}),
            logic_chain=validated_data.get("logic_chain", []),
            invalidating_factors=validated_data.get("invalidating_factors", []),
            risk_path=validated_data.get("risk_path", ""),
            
            # 交易执行参数
            stop_loss=validated_data.get("stop_loss", ""),
            position_sizing=validated_data.get("position_sizing", ""),
            notes=validated_data.get("notes", ""),
            
            # 兼容字段映射
            relevance_score=float(validated_data.get("relevance_score", validated_data.get("event_quality") == "high" and 0.8 or 0.5)),
            strength_score=float(validated_data.get("strength_score", confidence * 0.9)),
            duration=validated_data.get("time_horizon", "intraday"),
            position_ratio=0.05, # 默认值，建议通过 position_sizing 解析
            entry_style="wait",
            exit_style="fixed_stop",
            risk_flags=validated_data.get("invalidating_factors", []),
            trading_thesis=validated_data.get("logic_chain", []),
            recommendation=validated_data.get("primary_scene", ""),
            trace_id=getattr(pack, "trace_id", ""),
        )
        
        # 解析 position_ratio (如果 LLM 返回了百分比)
        if isinstance(assessment.position_sizing, str) and '%' in assessment.position_sizing:
            try:
                assessment.position_ratio = float(assessment.position_sizing.replace('%', '')) / 100.0
            except ValueError as e:
                logger.warning(f"[盘前分析] position_sizing 解析失败: {assessment.position_sizing} → {e}")
            
        return assessment

    def _validate_assessment(self, assessment: PreMarketAssessment) -> PreMarketAssessment:
        """
        P2 新增: 校验 LLM 输出，确保符合风控要求 (业务规则)
        """
        # 规则 1: 标的为空 -> 无法交易
        if not assessment.focus_symbols:
            logger.warning(f"🛑 [校验] 标的为空 -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            return assessment

        # 规则 2: 定价状态已充分 (fully_priced_in) -> 默认 NO_TRADE
        if assessment.pricing_status == "fully_priced_in":
            logger.warning(f"⚠️ [校验] 事件已充分定价 ({assessment.pricing_status}) -> 降级为 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            return assessment

        # 规则 3: 置信度低于 0.6 -> 强制 NO_TRADE
        if assessment.confidence < 0.60:
            logger.warning(f"📉 [校验] 置信度过低 ({assessment.confidence:.2f} < 0.6) -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            
        # 规则 4: Tradeability 为 no_trade -> 强制 NO_TRADE
        elif assessment.tradeability == "no_trade":
            logger.warning(f"🛑 [校验] Tradeability 为 no_trade -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            
        return assessment

    def _make_fallback(
        self, reason: str, pack: Any, raw: Optional[str] = None
    ) -> PreMarketAssessment:
        """生成降级结果"""
        return PreMarketAssessment(
            session="pre_market",
            primary_scene=f"降级分析: {reason}",
            event_pack_id=getattr(pack, "id", ""),
            focus_symbols=getattr(pack, "focus_symbols", []),
            direction="WATCH",
            confidence=0.0,
            relevance_score=getattr(pack, "relevance", {}).get("overall", 0.0),
            strength_score=0.0,
            duration="intraday",
            position_ratio=0.0,
            entry_style="wait",
            exit_style="fixed_stop",
            risk_flags=["llm_fallback"],
            trading_thesis=[f"分析失败: {reason}"],
            recommendation=f"盘前分析失败: {reason}",
            execution_mode="manual",
            timestamp=time.time(),
            raw_response=raw,
            trace_id=getattr(pack, "trace_id", ""),
        )

    def get_stats(self) -> Dict:
        return dict(self._stats)

    def generate_battle_plan_json(self, assessments: List[PreMarketAssessment], output_path: Optional[str] = None) -> Dict:
        """
        Phase 1 新增: 将 LLM 分析结果汇总为“日内作战计划” JSON
        
        输出结构:
        {
          "date": "YYYY-MM-DD",
          "market_regime": "Risk-On/Risk-Off/Neutral",
          "overall_bias": "Bullish/Bearish/Neutral",
          "targets": [
            {
              "symbol": "HK.00700",
              "direction": "LONG",
              "logic": "二阶推理核心逻辑",
              "entry_zone": "昨收 ± 2%",
              "stop_loss": "前低 / 关键支撑",
              "invalidation": "失效条件 (如跌破某价位)",
              "max_position_pct": 0.08,
              "urgency": "High/Medium/Low"
            }
          ],
          "restricted": ["HK.00708"],
          "notes": "重点关注..."
        }
        """
        if not output_path:
            output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'daily_battle_plan.json')

        plan = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "generated_at": time.time(),
            "market_regime": "Unknown",
            "overall_bias": "Neutral",
            "targets": [],
            "restricted": [],
            "notes": ""
        }

        bull_count = 0
        bear_count = 0
        
        # 1. 聚合分析结果
        for assmt in assessments:
            # 忽略无交易价值的事件
            if assmt.direction == "NO_TRADE" or assmt.tradeability == "no_trade":
                continue
            
            # 统计市场情绪
            if assmt.direction == "BUY": bull_count += 1
            elif assmt.direction == "SELL": bear_count += 1
            
            # 提取标的 (限制每个事件最多 3 个目标)
            targets = assmt.focus_symbols[:3]
            
            for sym in targets:
                # 解析仓位建议 (simple parsing from string like "small", "medium", "5%")
                size_str = assmt.position_sizing.lower()
                pos_pct = 0.05 # default
                if "small" in size_str or "5%" in size_str: pos_pct = 0.05
                elif "medium" in size_str or "10%" in size_str: pos_pct = 0.08
                elif "large" in size_str or "15%" in size_str: pos_pct = 0.12
                
                # 紧急度
                urgency = "Medium"
                if assmt.confidence > 0.75 or "urgent" in assmt.event_quality:
                    urgency = "High"
                elif assmt.confidence < 0.65:
                    urgency = "Low"
                
                # 构建目标条目
                target_entry = {
                    "symbol": sym,
                    "direction": "LONG" if assmt.direction == "BUY" else "SHORT",
                    "logic": assmt.second_order_insight,
                    "entry_zone": "Market / Limit (见计划细节)", # TODO: 结合技术面计算
                    "stop_loss": assmt.stop_loss,
                    "invalidation": ", ".join(assmt.invalidating_factors) if assmt.invalidating_factors else "逻辑证伪",
                    "max_position_pct": pos_pct,
                    "urgency": urgency,
                    "event_quality": assmt.event_quality,
                    "pricing_status": assmt.pricing_status,
                    "regime_hint": assmt.cross_market_signal.get("hk_open_bias", "")
                }
                
                # 去重: 如果标的已存在，保留更高置信度的逻辑
                existing = next((t for t in plan["targets"] if t["symbol"] == sym), None)
                if existing:
                    if assmt.confidence > existing.get("confidence", 0):
                        existing.update(target_entry)
                        existing["confidence"] = assmt.confidence
                else:
                    target_entry["confidence"] = assmt.confidence
                    plan["targets"].append(target_entry)
            
            # 记录风险/黑名单
            if "risk" in assmt.event_quality or "avoid" in assmt.notes.lower():
                for sym in targets:
                    if sym not in plan["restricted"]:
                        plan["restricted"].append(sym)

        # 2. 整体基调
        total_signals = bull_count + bear_count
        if total_signals == 0:
            plan["overall_bias"] = "Neutral (No actionable signals)"
            plan["notes"] = "今日无明显交易机会，建议观望"
        else:
            net_bull = bull_count - bear_count
            if net_bull > 2:
                plan["overall_bias"] = "Risk-On (偏多)"
            elif net_bull < -2:
                plan["overall_bias"] = "Risk-Off (偏空)"
            else:
                plan["overall_bias"] = "Neutral (震荡)"
            
            # 排序: 紧急度 + 置信度
            plan["targets"].sort(key=lambda x: (0 if x["urgency"] == "High" else 1, -x.get("confidence", 0)))
            
            # 备注
            plan["notes"] = f"今日生成 {len(plan['targets'])} 个交易计划。重点关注: {', '.join([t['symbol'] for t in plan['targets'][:3]])}"

        # 3. 保存 JSON
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
            logger.info(f"📝 [BattlePlan] 作战计划已生成: {output_path}")
            logger.info(f"   包含 {len(plan['targets'])} 个目标, {len(plan['restricted'])} 个受限标的")
        except Exception as e:
            logger.error(f"❌ [BattlePlan] 生成作战计划失败: {e}")
            
        return plan
