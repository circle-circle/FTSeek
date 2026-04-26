"""LLM 市场研判模块单元测试"""

import pytest
import asyncio
import json
import time
from unittest.mock import MagicMock, AsyncMock, patch


# ============================================================
# 测试 LLMOutputParser
# ============================================================

class TestLLMOutputParser:
    """测试 LLM 输出解析器"""

    def setup_method(self):
        from app.ai.llm_output_parser import LLMOutputParser
        self.parser = LLMOutputParser()

    def test_parse_valid_json(self):
        """正常 JSON 解析"""
        raw = json.dumps({
            "direction": "BUY",
            "confidence": "HIGH",
            "position_tier": "HALF",
            "timing": "IMMEDIATE",
            "key_factors": ["[FACT] 美股隔夜涨3%", "[INFER] 港股开盘跟涨"],
            "risk_flags": [],
            "news_impact": None,
            "cross_market_insight": "美股科技板块强势",
            "reasoning": "美股涨幅显著带动ADR溢价"
        })
        result = self.parser.parse(raw, code="HK.00700")
        assert result.direction == "BUY"
        assert result.confidence_level == "HIGH"
        assert result.position_tier == "HALF"
        assert result.recommended_timing == "IMMEDIATE"
        assert len(result.key_factors) == 2
        assert result.cross_market_insight == "美股科技板块强势"

    def test_parse_json_in_code_block(self):
        """从 markdown 代码块中提取 JSON"""
        raw = '```json\n{"direction": "SELL", "confidence": "LOW", "position_tier": "SKIP", "timing": "SKIP", "key_factors": ["test"], "risk_flags": [], "reasoning": "test"}\n```'
        result = self.parser.parse(raw, code="HK.09988")
        assert result.direction == "SELL"
        assert result.confidence_level == "LOW"

    def test_parse_json_with_extra_text(self):
        """JSON 前后有额外文本"""
        raw = 'Here is my analysis:\n{"direction": "NEUTRAL", "confidence": "MEDIUM", "position_tier": "LIGHT", "timing": "WAIT_CONFIRM", "key_factors": [], "risk_flags": ["vix_high"], "reasoning": "VIX偏高"}'
        result = self.parser.parse(raw)
        assert result.direction == "NEUTRAL"
        assert result.confidence_level == "MEDIUM"
        assert "vix_high" in result.risk_flags

    def test_parse_with_news_impact(self):
        """包含 news_impact 的解析"""
        raw = json.dumps({
            "direction": "STRONG_BUY",
            "confidence": "HIGH",
            "position_tier": "FULL",
            "timing": "IMMEDIATE",
            "key_factors": ["重大利好"],
            "risk_flags": [],
            "news_impact": {
                "severity": "critical",
                "duration_minutes": 120,
                "affected_codes": ["HK.00700", "HK.09988"]
            },
            "reasoning": "突发利好消息"
        })
        result = self.parser.parse(raw, code="HK.00700")
        assert result.news_impact is not None
        # severity "critical" should match VALID_SEVERITIES lowercase set correctly
        assert result.news_impact.severity == "critical"
        assert result.news_impact.duration_minutes == 120
        assert len(result.news_impact.affected_codes) == 2

    def test_parse_invalid_enum_defaults(self):
        """无效枚举值自动降级为默认
        
        注意: _validate_enum 有模糊匹配逻辑 (e.g. "BUY" in "MEGA_BUY" == True),
        所以测试值需确保不会被模糊匹配到任何合法值。
        """
        raw = json.dumps({
            "direction": "ZZZXXX",  # 无效且不含任何合法子串
            "confidence": "ZZZXXX",  # 无效
            "position_tier": "ZZZXXX",  # 无效
            "timing": "ZZZXXX",  # 无效
            "key_factors": [],
            "risk_flags": [],
            "reasoning": ""
        })
        result = self.parser.parse(raw)
        assert result.direction == "NEUTRAL"  # 默认
        assert result.confidence_level == "LOW"  # 默认
        assert result.position_tier == "SKIP"  # 默认
        assert result.recommended_timing == "SKIP"  # 默认

    def test_parse_invalid_json_raises(self):
        """完全无法解析 JSON 时抛出 ValueError"""
        with pytest.raises(ValueError):
            self.parser.parse("This is not JSON at all")


# ============================================================
# 测试 PromptBuilder
# ============================================================

class TestPromptBuilder:
    """测试 Prompt 构建器"""

    def setup_method(self):
        from app.ai.prompt_templates import PromptBuilder
        self.builder = PromptBuilder()

    def test_system_prompt_not_empty(self):
        prompt = self.builder.build_system_prompt()
        assert len(prompt) > 100
        assert "JSON" in prompt
        assert "direction" in prompt

    def test_news_only_prompt(self):
        from app.ai.llm_market_analyst import NewsContext
        ctx = NewsContext(
            headlines=["腾讯Q1利润超预期", "港股高开预期"],
            sentiment_score=0.75,
            news_count=5,
            top_news_summary="腾讯发布Q1财报，利润同比增长30%",
            avg_age_minutes=45,
            has_breaking=True,
        )
        prompt = self.builder.build_user_prompt(
            code="HK.00700",
            trigger_type="NEWS_ONLY",
            news_context=ctx,
        )
        assert "HK.00700" in prompt
        assert "NEWS_ONLY" in prompt
        assert "腾讯" in prompt
        assert "突发" in prompt  # has_breaking=True -> "包含突发新闻"

    def test_market_only_prompt(self):
        from app.ai.llm_market_analyst import CrossMarketContext, TechnicalSnapshot
        cm = CrossMarketContext(
            spy_change_pct=1.5, qqq_change_pct=2.0,
            vix=18.5, vix_status="normal",
            us_bond_10y=4.25,
            hsi_change_pct=0.5, southbound_net=15.3,
        )
        tech = TechnicalSnapshot(
            code="HK.00700", price=350.0,
            rsi_14=55.0, adx=28.0,
            market_regime="trending",
        )
        prompt = self.builder.build_user_prompt(
            code="HK.00700",
            trigger_type="MARKET_ONLY",
            cross_market_context=cm,
            technical_snapshot=tech,
        )
        assert "MARKET_ONLY" in prompt
        assert "SPY" in prompt
        assert "VIX" in prompt
        assert "RSI" in prompt

    def test_combined_prompt(self):
        from app.ai.llm_market_analyst import NewsContext, CrossMarketContext, TechnicalSnapshot
        prompt = self.builder.build_user_prompt(
            code="HK.09988",
            trigger_type="COMBINED",
            news_context=NewsContext(
                headlines=["test"], sentiment_score=0.5,
                news_count=1, top_news_summary="test",
                avg_age_minutes=30,
            ),
            cross_market_context=CrossMarketContext(
                spy_change_pct=0.5, qqq_change_pct=0.3,
                vix=20.0, vix_status="normal", us_bond_10y=4.0,
            ),
            technical_snapshot=TechnicalSnapshot(
                code="HK.09988", price=85.0,
            ),
        )
        assert "COMBINED" in prompt


# ============================================================
# 测试 MarketAssessment
# ============================================================

class TestMarketAssessment:
    """测试市场评估数据类"""

    def test_confidence_mapping(self):
        from app.ai.llm_market_analyst import MarketAssessment
        a = MarketAssessment(
            direction="BUY", confidence_level="HIGH",
            position_tier="FULL", key_factors=[], risk_flags=[],
        )
        assert a.confidence_value == 0.85
        assert a.direction_score == 0.6
        assert a.position_ratio == 0.12

    def test_sell_direction_score(self):
        from app.ai.llm_market_analyst import MarketAssessment
        a = MarketAssessment(
            direction="STRONG_SELL", confidence_level="LOW",
            position_tier="SKIP", key_factors=[], risk_flags=[],
        )
        assert a.direction_score == -1.0
        assert a.confidence_value == 0.45
        assert a.position_ratio == 0.0

    def test_is_valid_ttl(self):
        from app.ai.llm_market_analyst import MarketAssessment
        a = MarketAssessment(
            direction="NEUTRAL", confidence_level="MEDIUM",
            position_tier="LIGHT", key_factors=[], risk_flags=[],
            timestamp=time.time(), ttl_seconds=300,
        )
        assert a.is_valid() is True

        # 过期
        a2 = MarketAssessment(
            direction="NEUTRAL", confidence_level="MEDIUM",
            position_tier="LIGHT", key_factors=[], risk_flags=[],
            timestamp=time.time() - 400, ttl_seconds=300,
        )
        assert a2.is_valid() is False

    def test_to_dict(self):
        from app.ai.llm_market_analyst import MarketAssessment, NewsImpact
        a = MarketAssessment(
            direction="BUY", confidence_level="MEDIUM",
            position_tier="HALF", key_factors=["factor1"],
            risk_flags=["risk1"],
            news_impact=NewsImpact(severity="high", duration_minutes=60, affected_codes=["HK.00700"]),
            reasoning="test reasoning",
            timestamp=time.time(),
        )
        d = a.to_dict()
        assert d["direction"] == "BUY"
        assert d["confidence_value"] == 0.65
        assert d["news_impact"]["severity"] == "high"


# ============================================================
# 测试 LLMMarketAnalyst
# ============================================================

class TestLLMMarketAnalyst:
    """测试 LLM 市场研判引擎"""

    def test_disabled_returns_fallback(self):
        """未启用时返回降级结果"""
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        analyst = LLMMarketAnalyst({"enabled": False})
        result = asyncio.get_event_loop().run_until_complete(
            analyst.analyze("HK.00700")
        )
        assert result.direction == "NEUTRAL"
        assert result.confidence_level == "LOW"
        assert "llm_fallback" in result.risk_flags or "未启用" in str(result.key_factors)

    def test_cache_hit(self):
        """缓存命中测试"""
        from app.ai.llm_market_analyst import LLMMarketAnalyst, MarketAssessment
        analyst = LLMMarketAnalyst({"enabled": True, "model": "bailian/qwen3.5-plus", "timeout": 120})
        # 手动写入缓存
        assessment = MarketAssessment(
            direction="BUY", confidence_level="HIGH",
            position_tier="HALF", key_factors=["cached"],
            risk_flags=[], timestamp=time.time(), ttl_seconds=300,
        )
        cache_key = "HK.00700:COMBINED"
        analyst._cache[cache_key] = assessment
        # 获取缓存
        cached = analyst.get_cached_assessment("HK.00700", "COMBINED")
        assert cached is not None
        assert cached.direction == "BUY"

    def test_stats_tracking(self):
        """统计信息跟踪"""
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        analyst = LLMMarketAnalyst({"enabled": False})
        asyncio.get_event_loop().run_until_complete(analyst.analyze("HK.00700"))
        stats = analyst.get_stats()
        assert stats["total_calls"] == 1
        assert stats["fallback_count"] >= 1

    def test_config_values(self):
        """配置值正确读取"""
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        config = {
            "enabled": True,
            "model": "bailian/qwen3.5-plus",
            "enable_thinking": True,
            "timeout": 120,
            "urgent_timeout": 30,
            "cache_ttl": 300,
        }
        analyst = LLMMarketAnalyst(config)
        assert analyst.model == "bailian/qwen3.5-plus"
        assert analyst.timeout == 120
        assert analyst.enable_thinking is True


# ============================================================
# 测试降级路径
# ============================================================

class TestFallbackPaths:
    """测试 LLM 不可用时的降级路径"""

    def test_llm_timeout_fallback(self):
        """LLM 超时时返回降级结果"""
        from app.ai.llm_market_analyst import LLMMarketAnalyst

        analyst = LLMMarketAnalyst({"enabled": True, "model": "bailian/qwen3.5-plus", "timeout": 1})

        # Mock _call_llm to raise TimeoutError
        async def mock_call_llm(*args, **kwargs):
            raise asyncio.TimeoutError()

        analyst._call_llm = mock_call_llm

        result = asyncio.get_event_loop().run_until_complete(
            analyst.analyze("HK.00700", urgent=True)
        )
        # 应返回降级结果
        assert result.direction == "NEUTRAL"
        assert result.confidence_level == "LOW"
        assert "llm_fallback" in result.risk_flags

    def test_parse_error_fallback(self):
        """LLM 返回非 JSON 时降级"""
        from app.ai.llm_output_parser import LLMOutputParser
        parser = LLMOutputParser()
        with pytest.raises(ValueError):
            parser.parse("I cannot provide trading advice")
