"""LLM 市场研判引擎 - 多输入融合分析

仅在开盘前套利阶段（ARB_DOMINANT，默认 09:15-10:00 HKT）触发，
配合 cross_market_arbitrage 策略做开盘方向研判，不参与全天候实时交易。
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional, List, Dict, Any, ClassVar
from enum import Enum

logger = logging.getLogger(__name__)


class TriggerType(str, Enum):
    NEWS_ONLY = "NEWS_ONLY"
    MARKET_ONLY = "MARKET_ONLY"
    COMBINED = "COMBINED"
    SCHEDULED = "SCHEDULED"
    PRE_MARKET_PLAN = "PRE_MARKET_PLAN"  # 盘前计划生成 - 2026-04-12 新增
    POST_MARKET_REVIEW = "POST_MARKET_REVIEW"  # 盘后复盘 - 2026-04-12 新增


class Direction(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PositionTier(str, Enum):
    FULL = "FULL"
    HALF = "HALF"
    LIGHT = "LIGHT"
    SKIP = "SKIP"


class Timing(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    WAIT_CONFIRM = "WAIT_CONFIRM"
    WAIT_PULLBACK = "WAIT_PULLBACK"
    SKIP = "SKIP"


@dataclass
class NewsContext:
    """新闻上下文"""
    headlines: List[str]
    sentiment_score: float
    news_count: int
    top_news_summary: str
    avg_age_minutes: float
    has_breaking: bool = False
    source_credibility: float = 0.7


@dataclass
class CrossMarketContext:
    """跨市场上下文"""
    spy_change_pct: float
    qqq_change_pct: float
    vix: float
    vix_status: str
    us_bond_10y: float
    arb_pairs: List[Dict[str, Any]] = field(default_factory=list)
    hsi_change_pct: float = 0.0
    southbound_net: float = 0.0


@dataclass
class TechnicalSnapshot:
    """技术指标快照"""
    code: str
    price: float
    rsi_14: Optional[float] = None
    macd_histogram: Optional[float] = None
    bollinger_position: Optional[str] = None
    atr_14: Optional[float] = None
    adx: Optional[float] = None
    market_regime: str = "neutral"
    volume_ratio: float = 1.0


@dataclass
class NewsImpact:
    """新闻影响评估"""
    severity: str
    duration_minutes: int
    affected_codes: List[str]


# 模块级常量（避免 dataclass ClassVar 与默认值冲突问题）
_CONFIDENCE_MAP: Dict[str, float] = {"HIGH": 0.85, "MEDIUM": 0.65, "LOW": 0.45}
_DIRECTION_SCORE_MAP: Dict[str, float] = {
    "STRONG_BUY": 1.0, "BUY": 0.6, "NEUTRAL": 0.0,
    "SELL": -0.6, "STRONG_SELL": -1.0,
}
_POSITION_MAP: Dict[str, float] = {
    "FULL": 0.12, "HALF": 0.08, "LIGHT": 0.04, "SKIP": 0.0,
}


@dataclass
class MarketAssessment:
    """LLM 市场研判结果"""
    direction: str
    confidence_level: str
    position_tier: str
    key_factors: List[str]
    risk_flags: List[str]
    news_impact: Optional[NewsImpact] = None
    cross_market_insight: Optional[str] = None
    recommended_timing: str = "SKIP"
    reasoning: str = ""
    ttl_seconds: int = 300
    timestamp: float = 0.0
    trigger_type: str = "COMBINED"
    raw_response: Optional[str] = None

    # ClassVar 声明使 dataclass 不将其视为字段
    CONFIDENCE_MAP: ClassVar[Dict[str, float]] = _CONFIDENCE_MAP
    DIRECTION_SCORE_MAP: ClassVar[Dict[str, float]] = _DIRECTION_SCORE_MAP
    POSITION_MAP: ClassVar[Dict[str, float]] = _POSITION_MAP

    @property
    def confidence_value(self) -> float:
        return _CONFIDENCE_MAP.get(self.confidence_level, 0.45)

    @property
    def direction_score(self) -> float:
        return _DIRECTION_SCORE_MAP.get(self.direction, 0.0)

    @property
    def position_ratio(self) -> float:
        return _POSITION_MAP.get(self.position_tier, 0.0)

    def is_valid(self) -> bool:
        """检查结果是否仍在有效期内"""
        if self.timestamp == 0:
            return False
        return (time.time() - self.timestamp) < self.ttl_seconds

    def to_dict(self) -> Dict:
        result = {
            "direction": self.direction,
            "confidence_level": self.confidence_level,
            "confidence_value": self.confidence_value,
            "direction_score": self.direction_score,
            "position_tier": self.position_tier,
            "position_ratio": self.position_ratio,
            "key_factors": self.key_factors,
            "risk_flags": self.risk_flags,
            "recommended_timing": self.recommended_timing,
            "reasoning": self.reasoning,
            "trigger_type": self.trigger_type,
            "ttl_seconds": self.ttl_seconds,
            "timestamp": self.timestamp,
        }
        if self.news_impact:
            result["news_impact"] = {
                "severity": self.news_impact.severity,
                "duration_minutes": self.news_impact.duration_minutes,
                "affected_codes": self.news_impact.affected_codes,
            }
        if self.cross_market_insight:
            result["cross_market_insight"] = self.cross_market_insight
        return result


class LLMMarketAnalyst:
    """LLM 市场研判引擎

    作为共识引擎的置信度调节因子，不直接做交易决策。
    支持异步调用 + 结果缓存。
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.model = self.config.get("model", "bailian/qwen3.5-plus")
        self.enable_thinking = self.config.get("enable_thinking", True)
        self.timeout = self.config.get("timeout", 120)
        self.max_retries = self.config.get("max_retries", 2)
        self.cache_ttl = self.config.get("cache_ttl", 300)
        self.max_cache_size = self.config.get("max_cache_size", 200)
        self.urgent_timeout = self.config.get("urgent_timeout", 30)

        # 交易时间窗口配置（仅 ARB_DOMINANT 阶段触发）
        _tw = self.config.get("trading_window", {})
        self._window_start = self._parse_time(_tw.get("start", "09:15"))
        self._window_end = self._parse_time(_tw.get("end", "10:00"))
        self._window_tz = _tw.get("timezone", "Asia/Hong_Kong")

        self._cache: Dict[str, MarketAssessment] = {}
        self._llm_client = None
        self._output_parser = None
        self._prompt_builder = None

        self._stats: Dict[str, Any] = {
            "total_calls": 0,
            "cache_hits": 0,
            "llm_calls": 0,
            "llm_errors": 0,
            "fallback_count": 0,
            "avg_latency_ms": 0.0,
        }

        if self.enabled:
            self._init_components()

    def _init_components(self):
        """延迟初始化组件"""
        try:
            from .llm_output_parser import LLMOutputParser
            from .prompt_templates import PromptBuilder
            self._output_parser = LLMOutputParser()
            self._prompt_builder = PromptBuilder()
            logger.info(
                "LLMMarketAnalyst 初始化完成: model=%s, timeout=%ss, thinking=%s, window=%s-%s",
                self.model, self.timeout, self.enable_thinking,
                self._window_start.strftime("%H:%M"),
                self._window_end.strftime("%H:%M"),
            )
        except Exception as e:
            logger.error("LLMMarketAnalyst 初始化失败: %s", e)
            self.enabled = False

    @staticmethod
    def _parse_time(t_str: str) -> dtime:
        """解析 'HH:MM' 格式时间字符串"""
        try:
            h, m = t_str.split(":")
            return dtime(int(h), int(m))
        except Exception:
            return dtime(9, 15)

    def _in_trading_window(self) -> bool:
        """判断当前时刻是否在交易时间窗口内（HKT）

        使用本机时间做近似判断（服务器通常已配置为 HKT/CST，偏差在可接受范围内）。
        若需严格 pytz 支持，可在 requirements.txt 中添加 pytz 后升级本方法。
        """
        try:
            now = datetime.now().time()
            return self._window_start <= now <= self._window_end
        except Exception:
            # 判断失败时放行，避免误阻断
            return True

    def _get_llm_client(self):
        """延迟初始化 LLM 客户端（降级路径）"""
        if self._llm_client is None:
            try:
                import sys
                sys.path.insert(0, '/opt/ftseek/ExpertTeam')
                from utils.llm_client import LLMClient
                self._llm_client = LLMClient(
                    model=self.model,
                    timeout=self.timeout,
                    max_retries=self.max_retries,
                )
                logger.info("LLM客户端初始化(降级路径): model=%s", self.model)
            except Exception as e:
                logger.error("LLM客户端初始化失败: %s", e)
                raise
        return self._llm_client

    async def _call_llm(self, messages: list, timeout: int) -> str:
        """直接调用百炼 API，支持 enable_thinking 参数。

        优先使用 openai 兼容客户端（支持 extra_body）；
        若 openai 包不可用，则降级到 ExpertTeam LLMClient。
        """
        import os
        try:
            from app.core.unified_llm_factory import get_llm_client
            oai_client = get_llm_client()
            if not oai_client:
                raise ValueError("Unified LLM Client not available")
        except ImportError:
            logger.warning("openai 包未安装，降级到 ExpertTeam LLMClient（不支持 enable_thinking）")
            client = self._get_llm_client()
            resp = await asyncio.wait_for(
                client.chat(messages, use_cache=False),
                timeout=timeout,
            )
            return resp.content

        # qwen3.5-plus 的实际模型名（与 ExpertTeam MODEL_CONFIGS 对齐）
        model_name = "qwen-plus"

        extra_body: Dict[str, Any] = {}
        if self.enable_thinking:
            extra_body["enable_thinking"] = True

        response = await asyncio.wait_for(
            oai_client.chat.completions.create(
                model=model_name,
                messages=messages,
                extra_body=extra_body if extra_body else None,
            ),
            timeout=timeout,
        )
        return response.choices[0].message.content

    def _cache_key(
        self,
        code: str,
        trigger_type: str,
        news_context: Optional[NewsContext],
        cross_market_context: Optional[CrossMarketContext],
    ) -> str:
        """生成缓存键"""
        parts = [code, trigger_type]
        if news_context:
            parts.append(f"news_{news_context.news_count}_{news_context.has_breaking}")
            if news_context.headlines:
                headline_hash = hashlib.md5(
                    "|".join(news_context.headlines[:3]).encode()
                ).hexdigest()[:8]
                parts.append(headline_hash)
        if cross_market_context:
            parts.append(
                f"cm_{cross_market_context.spy_change_pct:.1f}_{cross_market_context.vix:.0f}"
            )
        return ":".join(parts)

    def _get_cached(self, cache_key: str) -> Optional[MarketAssessment]:
        """获取缓存结果（检查有效期）"""
        cached = self._cache.get(cache_key)
        if cached and cached.is_valid():
            return cached
        elif cached:
            del self._cache[cache_key]
        return None

    def _put_cache(self, cache_key: str, assessment: MarketAssessment):
        """写入缓存（LRU 淘汰）"""
        if len(self._cache) >= self.max_cache_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].timestamp)
            del self._cache[oldest_key]
        self._cache[cache_key] = assessment

    async def analyze(
        self,
        code: str,
        news_context: Optional[NewsContext] = None,
        cross_market_context: Optional[CrossMarketContext] = None,
        technical_snapshot: Optional[TechnicalSnapshot] = None,
        trigger_type: str = "COMBINED",
        urgent: bool = False,
    ) -> MarketAssessment:
        """执行 LLM 市场研判分析

        Args:
            code: 标的代码 (如 "HK.00700")
            news_context: 新闻上下文
            cross_market_context: 跨市场数据上下文
            technical_snapshot: 技术指标快照
            trigger_type: 触发类型 NEWS_ONLY/MARKET_ONLY/COMBINED/SCHEDULED
            urgent: 是否紧急（使用更短超时）

        Returns:
            MarketAssessment: 标准化市场评估结果
        """
        self._stats["total_calls"] += 1

        if not self.enabled:
            self._stats["fallback_count"] += 1
            return self._make_fallback("LLM研判未启用")

        # 时间窗口检查：仅在 ARB_DOMINANT 阶段（开盘前 09:15-10:00 HKT）触发
        if not urgent and not self._in_trading_window():
            self._stats["fallback_count"] += 1
            logger.debug(
                "LLM研判时间窗口外跳过: %s (window=%s-%s)",
                code,
                self._window_start.strftime("%H:%M"),
                self._window_end.strftime("%H:%M"),
            )
            return self._make_fallback("非交易时间窗口")

        # 1. 检查缓存
        cache_key = self._cache_key(code, trigger_type, news_context, cross_market_context)
        cached = self._get_cached(cache_key)
        if cached and not urgent:
            self._stats["cache_hits"] += 1
            logger.debug("LLM研判缓存命中: %s (%s)", code, trigger_type)
            return cached

        # 确保组件已初始化
        if self._prompt_builder is None or self._output_parser is None:
            self._init_components()
            if self._prompt_builder is None or self._output_parser is None:
                self._stats["fallback_count"] += 1
                return self._make_fallback("组件初始化失败")

        # 2. 构建 prompt
        try:
            system_prompt = self._prompt_builder.build_system_prompt()
            user_prompt = self._prompt_builder.build_user_prompt(
                code=code,
                trigger_type=trigger_type,
                news_context=news_context,
                cross_market_context=cross_market_context,
                technical_snapshot=technical_snapshot,
            )
        except Exception as e:
            logger.error("Prompt构建失败: %s - %s", code, e)
            self._stats["fallback_count"] += 1
            return self._make_fallback(f"Prompt构建失败: {e}")

        # 3. 调用 LLM (通过 agent_bridge — P2 2026-05-06)
        start_time = time.time()
        try:
            timeout = self.urgent_timeout if urgent else self.timeout

            # 构建市场数据上下文
            market_data = {
                "code": code,
                "trigger_type": trigger_type,
                "technical": str(technical_snapshot)[:1000] if technical_snapshot else "",
                "cross_market": str(cross_market_context)[:1000] if cross_market_context else "",
                "news": str(news_context)[:1000] if news_context else "",
            }

            # 通过 agent_bridge 调用 (带重试+降级)
            from app.llm.agent_bridge import get_ftseek_agent_bridge
            bridge = get_ftseek_agent_bridge("hk")
            result = bridge.analyze_market(code, market_data, news_context)

            if result and result.get("status") != "fallback":
                raw_content = json.dumps(result, ensure_ascii=False)
            else:
                # 降级: 使用原 _call_llm 作为 fallback
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                raw_content = await self._call_llm(messages, timeout)

            latency_ms = (time.time() - start_time) * 1000
            self._stats["llm_calls"] += 1
            self._update_avg_latency(latency_ms)
            logger.info(
                "LLM研判完成: %s (%s) %.0fms thinking=%s",
                code, trigger_type, latency_ms, self.enable_thinking,
            )

        except asyncio.TimeoutError:
            logger.warning(
                "LLM研判超时: %s (%s) timeout=%ss", code, trigger_type, timeout
            )
            self._stats["llm_errors"] += 1
            self._stats["fallback_count"] += 1
            return self._make_fallback(f"LLM超时({timeout}s)")
        except Exception as e:
            logger.error("LLM调用失败: %s - %s", code, e)
            self._stats["llm_errors"] += 1
            self._stats["fallback_count"] += 1
            return self._make_fallback(f"LLM调用异常: {e}")

        # 4. 解析输出
        try:
            assessment = self._output_parser.parse(
                raw_content,
                code=code,
                trigger_type=trigger_type,
            )
            assessment.timestamp = time.time()
            assessment.ttl_seconds = self.cache_ttl
            assessment.raw_response = raw_content
            self._put_cache(cache_key, assessment)
            return assessment

        except Exception as e:
            logger.error(
                "LLM输出解析失败: %s - %s, raw=%s", code, e, raw_content[:200]
            )
            self._stats["llm_errors"] += 1
            self._stats["fallback_count"] += 1
            return self._make_fallback(f"输出解析失败: {e}")

    def _make_fallback(self, reason: str) -> MarketAssessment:
        """生成降级结果"""
        return MarketAssessment(
            direction="NEUTRAL",
            confidence_level="LOW",
            position_tier="SKIP",
            key_factors=[f"降级原因: {reason}"],
            risk_flags=["llm_fallback"],
            recommended_timing="SKIP",
            reasoning=f"LLM降级: {reason}",
            ttl_seconds=60,
            timestamp=time.time(),
        )

    def _update_avg_latency(self, latency_ms: float):
        """滑动平均延迟"""
        n = self._stats["llm_calls"]
        prev = self._stats["avg_latency_ms"]
        self._stats["avg_latency_ms"] = prev + (latency_ms - prev) / n

    def get_stats(self) -> Dict:
        """获取统计信息"""
        stats = dict(self._stats)
        stats["cache_size"] = len(self._cache)
        stats["enabled"] = self.enabled
        stats["model"] = self.model
        if stats["total_calls"] > 0:
            stats["cache_hit_rate"] = stats["cache_hits"] / stats["total_calls"]
            stats["error_rate"] = stats["llm_errors"] / stats["total_calls"]
        return stats

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        logger.info("LLM研判缓存已清空")

    def get_cached_assessment(
        self, code: str, trigger_type: str = "COMBINED"
    ) -> Optional[MarketAssessment]:
        """获取指定标的的缓存研判结果（不触发新分析）"""
        prefix = f"{code}:{trigger_type}"
        for key, assessment in self._cache.items():
            if key.startswith(prefix) and assessment.is_valid():
                return assessment
        return None
