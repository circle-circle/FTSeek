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

# FTSeek 适配: 集成市场规则注入 (2026-05-17)
from app.utils.market_prompt_injector import inject_market_context

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
    notes: str = ""
    signal_impact: str = "neutral"    # boost / neutral / weaken / block
    avoid_codes: List[str] = field(default_factory=list)

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

【你的角色 — 技术信号的补充者，非决策者】:
你不再决定是否交易，而是为已有的技术信号提供额外的上下文和风险提示。
交易决策由规则引擎主导，你的输出仅用于调整信号的优先级。

你必须遵守以下原则：
- 只基于输入事实推理，不得编造未知内容
- **政策一票否决**: 如果涉及强监管/反垄断/制裁风险，必须给出 signal_impact="block" 并列出 avoid_codes。
- 如果事件影响已被市场价格充分反映，给出 signal_impact="weaken"。
- 如果有明确的新催化剂可能推动价格显著变动，给出 signal_impact="boost"。
- 如果信息不足以做出有意义的判断，给出 signal_impact="neutral"。
- 宁可错过，不要做错
- **突发事件特权**: 如果涉及地缘政治突变、战争、核心供应链断裂（如伊朗石油、芯片禁令），即使部分细节缺失，也应给出较高的置信度 (0.7+)。
- 不要输出泛泛而谈的新闻总结，必须给出交易可用的结构化判断
- 必须严格输出 JSON，禁止输出多余解释文字
- 输出内容必须适合自动交易系统直接消费

你需要重点关注以下问题：
1. 事件是否真实且重要
2. 直接影响对象是谁
3. 影响是否会传导到相关行业/板块/公司
4. 市场是否已经充分定价 (pricing_status)
5. 当前价格行为是否确认该事件
6. 该事件是否值得交易 (由 signal_impact 表达，非 direction)
7. 如果交易，应该采取什么方向和风险控制方式
8. 哪些因素会否定当前判断

【强制输出 JSON 格式要求】:
必须强制输出以下字段：
- session: 分析时段 (如 "pre_market_hk")
- focus_symbols: 必须输出最相关的标的代码，最多 5 个
- direction: BUY / SELL / NO_TRADE
- confidence: 0.0 ~ 1.0
- signal_impact: "boost" / "neutral" / "weaken" / "block" — 你对技术信号的调整建议
  * boost = 有明确催化剂，建议提升该标的优先级
  * neutral = 无明显影响，保持原信号权重
  * weaken = 事件已被定价或缺乏后续动能，建议降低优先级
  * block = 存在政策/制裁/强监管风险，建议回避
- avoid_codes: 如果你建议回避某些标的，列出其代码（仅在 signal_impact="block" 时有意义）
- pricing_status: "not_priced_in" / "partially_priced_in" / "fully_priced_in"
- second_order_insight: 用一句话描述二阶推理逻辑（如"监管收紧→广告收入预期下调→估值重估"）
- invalidating_factors: 必须明确写出哪些因素会推翻当前判断
- logic_chain: 必须用数组列出推理步骤

如果没有高确信度交易机会，direction 设为 NO_TRADE，signal_impact 设为 neutral（除非有明确的风险需要 block）。

输出格式要求：
严格输出 JSON，结构如下：
{
  "session": "pre_market_hk",
  "focus_symbols": ["HK.00700", "HK.09988"],
  "direction": "NO_TRADE",
  "confidence": 0.45,
  "signal_impact": "boost",
  "avoid_codes": [],
  "pricing_status": "partially_priced_in",
  "second_order_insight": "南向资金持续流入互联网龙头，估值修复动能增强",
  "invalidating_factors": ["美联储意外加息", "南向资金突然大幅流出"],
  "logic_chain": ["事件A→影响B→资金流向C→标的受益D"]
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
        pack_symbols = getattr(pack, "focus_symbols", [])
        price_action = self._build_technical_context(pack_symbols)
        # 量比/波动率: 优先从 sentiment_summary 获取，否则从 market_context 获取
        vol_info = getattr(pack, "sentiment_summary", {})
        volume_volatility = f"情绪一致性={vol_info.get('consensus', 0):+.2f}, 分歧度={vol_info.get('dispersion', 0):.2f}"
        historical_cases = self._load_historical_context(pack_symbols)
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
                price_action=price_action,
                volume_volatility=volume_volatility,
                historical_cases=historical_cases,
                related_news=related_news
            )
        except KeyError as e:
            logger.error(f"[PreMarketAnalyst] Prompt 模板缺少字段: {e}")
            user_prompt = f"请分析以下事件包: {headline}\n\n{body}"

        # FTSeek 适配: 注入市场规则 (2026-05-17)
        # 根据所有 focus_symbols 识别市场并注入规则 (支持跨市场事件包)
        final_system_prompt = SYSTEM_PROMPT
        symbols = getattr(pack, "focus_symbols", [])
        if symbols:
            # 传递所有标的，inject_market_context 自动识别多市场
            final_system_prompt = inject_market_context(",".join(symbols), SYSTEM_PROMPT)
            
        return [
            {"role": "system", "content": final_system_prompt},
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

    def _build_technical_context(self, symbols: list) -> str:
        """2026-05-17: 注入标的真实交易数据 + 技术指标

        盘前分析时 (08:20-08:30 HKT) 港股未开盘，但可从 Futu 获取:
        - 上一交易日 OHLCV + 涨跌幅
        - 20 日 ATR / 成交量比 / 均线位置
        - 近期交易历史 P&L (已有逻辑)

        Returns:
            技术上下文文本
        """
        if not symbols:
            return ""
        parts = []

        # 1. 实时技术指标 (Futu 盘前快照)
        try:
            from futu import OpenQuoteContext, KLType, AuType, RET_OK
            from app import config
            import pandas as pd

            quote_ctx = None
            try:
                quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                for sym in symbols[:5]:
                    try:
                        # 获取日 K 线 (近 25 日)
                        ret, kline_df = quote_ctx.get_cur_kline(sym, 25, KLType.K_DAY, AuType.QFQ)
                        if ret == RET_OK and kline_df is not None and len(kline_df) > 0:
                            last = kline_df.iloc[-1]
                            prev = kline_df.iloc[-2] if len(kline_df) > 1 else last
                            close = float(last['close'])
                            prev_close = float(prev['close'])
                            change_pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0

                            # 量能: 量比 = 最新量 / 20日均量
                            vol_20 = kline_df.tail(20)['volume'].mean()
                            vol_ratio = float(last['volume']) / vol_20 if vol_20 > 0 else 1.0

                            # 波动率: ATR(20)
                            atr_val = 0.0
                            if len(kline_df) >= 20:
                                atr_df = kline_df.tail(21)
                                tr = pd.concat([
                                    atr_df['high'] - atr_df['low'],
                                    (atr_df['high'] - atr_df['close'].shift(1)).abs(),
                                    (atr_df['low'] - atr_df['close'].shift(1)).abs()
                                ], axis=1).max(axis=1)
                                atr_val = tr.iloc[-20:].mean()
                            atr_pct = atr_val / close * 100 if close > 0 else 0

                            # 均线位置
                            ma5 = float(kline_df.tail(5)['close'].mean())
                            ma20 = float(kline_df.tail(20)['close'].mean())
                            ma_pos = "上方" if close > ma20 else "下方"

                            parts.append(
                                f"## {sym} 技术快照 (上一交易日)\n"
                                f"  收盘={close:.2f} | 涨跌={change_pct:+.1f}%\n"
                                f"  量比={vol_ratio:.1f}x | ATR(20)={atr_val:.2f}({atr_pct:.1f}%)\n"
                                f"  MA5={ma5:.2f} | MA20={ma20:.2f} | 价格在MA20{ma_pos}"
                            )
                    except Exception as e:
                        logger.debug(f"[LLM技术上下文] {sym} Futu 数据获取失败: {e}")

                if not parts:
                    parts.append("## 技术快照: 盘前无法获取实时行情，使用历史复盘数据")
            finally:
                if quote_ctx:
                    try:
                        quote_ctx.close()
                    except Exception:
                        pass
        except ImportError:
            logger.debug("[LLM技术上下文] futu 模块不可用，跳过实时技术指标")
        except Exception as e:
            logger.debug(f"[LLM技术上下文] Futu 连接失败: {e}")

        # 2. 历史交易复盘 (已有逻辑)
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'ftseek.db')
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            for sym in symbols[:5]:
                row = cur.execute(
                    "SELECT code, action, entry_price, exit_price, unrealized_pl, strategy, entry_time "
                    "FROM positions WHERE code=? AND market='HK' AND status='CLOSED' "
                    "AND strategy NOT LIKE '%sync%' ORDER BY entry_time DESC LIMIT 3",
                    (sym,)
                ).fetchall()
                if row:
                    lines = [f"## {sym} 近期交易复盘"]
                    for r in row:
                        pnl = r[4] if r[4] else 0
                        lines.append(f"  {r[5]}: 入场={r[2]} 出场={r[3] or 'N/A'} PnL={pnl:+.0f}")
                    # 只在无技术快照时追加 (有技术快照时复盘数据在下一段)
                    if not any(sym in p for p in parts):
                        parts.append("\n".join(lines))
                    else:
                        # 追加到已有条目
                        idx = next(i for i, p in enumerate(parts) if sym in p)
                        parts[idx] += "\n" + "\n".join(lines)
                else:
                    stats = cur.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN passed=1 THEN 1 ELSE 0 END), "
                        "ROUND(AVG(ABS(score)),2) FROM decision_traces WHERE code=?",
                        (sym,)
                    ).fetchone()
                    if stats and stats[0] > 0:
                        parts.append(f"## {sym}: {stats[0]}次扫描, {stats[1]}次通过, 均分={stats[2]}")
                    else:
                        parts.append(f"## {sym}: 无历史数据")
            conn.close()
        except Exception as e:
            logger.debug(f"[LLM技术上下文] 历史复盘加载失败: {e}")

        return "\n".join(parts) if parts else ""

    def _load_historical_context(self, symbols: list) -> str:
        """2026-05-17: 从历史模式库加载相似案例"""
        if not symbols:
            return ""
        try:
            from app.review.pattern_miner import get_pattern_miner
            patterns = get_pattern_miner().mine(days=60)
            parts = []
            for sym in symbols[:5]:
                sp = [p for p in patterns if sym in p.get('pattern', '') or sym in p.get('affected_symbols', '')]
                if sp:
                    lines = [f"## {sym} 历史模式"]
                    for p in sp[:2]:
                        lines.append(f"  {p.get('pattern', '')}")
                        lines.append(f"    -> {p.get('actionable_rule', '')}")
                    parts.append("\n".join(lines))
            return "\n".join(parts) if parts else "无历史案例"
        except Exception as e:
            logger.debug(f"历史上下文加载失败: {e}")
            return ""

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
            cleaned_raw = re.sub(r'[\x00-\x1f]+', ' ', cleaned_raw)
            validated_data = json.loads(cleaned_raw.strip())
        except json.JSONDecodeError as e:
            logger.error(f"[PreMarketAnalyst] JSON 解析失败: {e}")
            try:
                json_match = re.search(r'\{[\s\S]*\}', raw)
                if json_match:
                    cleaned = re.sub(r'[\x00-\x1f]+', ' ', json_match.group())
                    validated_data = json.loads(cleaned)
                else:
                    raise ValueError()
            except Exception:
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
            signal_impact=str(validated_data.get("signal_impact", "neutral")).lower(),
            avoid_codes=validated_data.get("avoid_codes", []),
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
        P2 修复 (2026-05-17): LLM 不再决定是否交易，而是提供上下文和风险提示。
        2026-05-17 修复: 删除旧版本重复方法（原 L829-858 的保守规则覆盖新版逻辑）。
        """
        # 规则 1: 标的为空 -> 无法提供信号
        if not assessment.focus_symbols:
            logger.warning(f"🛑 [校验] 标的为空 -> 强制 NO_TRADE")
            assessment.direction = "NO_TRADE"
            assessment.tradeability = "no_trade"
            return assessment

        # 规则 2: signal_impact 默认值
        if not assessment.signal_impact:
            assessment.signal_impact = 'neutral'

        # 规则 3: 置信度过低 (<0.15) -> 信号无参考价值
        if assessment.confidence < 0.15:
            logger.warning(f"置信度过低 ({assessment.confidence:.2f}) -> signal_impact=neutral")
            assessment.signal_impact = 'neutral'

        # 规则 4: 事件已充分定价 -> 降级信号影响
        if assessment.pricing_status == "fully_priced_in":
            logger.warning(f"事件已充分定价 -> signal_impact=weaken")
            assessment.signal_impact = 'weaken'

        # 规则 5: LLM 标记回避标的 -> 记录警告
        if getattr(assessment, 'avoid_codes', None):
            logger.warning(f"LLM 标记回避标的: {assessment.avoid_codes}")

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
            direction="NO_TRADE",  # 降级=无法交易，与 battle_plan_generator 对齐
            confidence=0.0,
            signal_impact="neutral",  # 降级标的无 LLM 信号影响
            avoid_codes=[],
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
                    "regime_hint": assmt.cross_market_signal.get("hk_open_bias", ""),
                    "signal_impact": assmt.signal_impact,  # 2026-05-17 新增
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
            
            # 记录风险/黑名单 (2026-05-17 增强: signal_impact=block 和 avoid_codes)
            block_codes = list(getattr(assmt, 'avoid_codes', []))
            if assmt.signal_impact == "block":
                block_codes.extend(targets)
            if "risk" in assmt.event_quality or "avoid" in assmt.notes.lower():
                block_codes.extend(targets)
            for sym in block_codes:
                if sym not in plan["restricted"]:
                    plan["restricted"].append(sym)
                    logger.info(f"[BattlePlan] LLM 标记回避: {sym} (signal_impact={assmt.signal_impact})")

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
