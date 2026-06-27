"""Tests for app.playbook.instruction_generator — 反幻觉架构"""
import json
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from app.playbook.instruction_generator import (
    Instruction,
    InstructionGenerator,
    compute_instruction_values,
    build_thesis,
    compute_confidence,
    THESIS_TEMPLATES,
    DECISION_SCHEMA,
)
from app.playbook.data_collector import (
    PlaybookData,
    FundamentalData,
    TechnicalData,
    InsiderData,
    NewsData,
    EarningsData,
    SentimentData,
)
from app.playbook.market_regime import MarketRegime, RegimeContext


# ========== Fixtures ==========

def _make_regime(regime=MarketRegime.CAUTIOUS, score=2, top_n=5, mult=0.5):
    """创建 RegimeContext fixture"""
    return RegimeContext(
        regime=regime,
        vix=18.5,
        spy_price=540.0,
        spy_ma20=535.0,
        spy_ma50=530.0,
        spy_ma200=510.0,
        spy_change_pct=0.3,
        score=score,
        top_n=top_n,
        position_multiplier=mult,
        min_strength=3,
    )


def _make_playbook_data(
    symbol="US.NTES",
    price=120.0,
    atr=3.5,
    rsi=45.0,
    pe=22.0,
    industry="Technology",
    quality="ok",
):
    """创建 PlaybookData fixture"""
    return PlaybookData(
        symbol=symbol,
        fundamental=FundamentalData(pe=pe, industry=industry, market_cap=5000, quality=quality),
        technical=TechnicalData(
            price=price, ma20=118.0, ma50=115.0, rsi_14=rsi,
            macd_signal="golden_cross", atr_14=atr, quality=quality,
        ),
        insider=InsiderData(buy_count_30d=2, cluster_score=0.7, quality=quality),
        news=NewsData(headlines=["Good news"], sentiment_score=0.5, quality=quality),
        earnings=EarningsData(days_until=45, beat_rate=0.75, quality=quality),
        sentiment=SentimentData(news_sentiment=0.3, vix=18.5, quality=quality),
    )


# ========== compute_instruction_values 测试 ==========

class TestComputeInstructionValues:
    """数值计算引擎测试 (纯确定性)"""

    def test_buy_basic_calculation(self):
        """BUY 方向: 止损在下方, 止盈在上方"""
        data = _make_playbook_data(price=100.0, atr=2.0)
        decision = {"direction": "BUY", "selected_conditions": ["above_ma20"]}

        result = compute_instruction_values(
            decision=decision,
            data=data,
            total_equity=100000,
            risk_pct=1.0,
            regime_multiplier=1.0,
            rr_ratio=2.0,
            atr_multiplier=1.5,
        )

        assert result["entry_price"] == 100.0
        # 固定 2% 止损: stop = 100 - 100*2% = 98.0
        assert result["stop_loss"] == 98.0
        # take_profit = 100 + (100-98)*2 = 104.0
        assert result["take_profit"] == 104.0
        assert result["risk_reward"] == 2.0
        # risk_amount = 82000 (risk_base) * 0.01 * 1.0 = 820
        assert result["risk_amount"] == 820.0
        # 仓位被 8% max_position_pct 截断到 65 股 ($6,500)
        assert result["position_size"] == 65

    def test_sell_short_basic_calculation(self):
        """SELL_SHORT: 止损在上方, 止盈在下方"""
        data = _make_playbook_data(price=100.0, atr=2.0)
        decision = {"direction": "SELL_SHORT", "selected_conditions": ["rsi_overbought"]}

        result = compute_instruction_values(
            decision=decision,
            data=data,
            total_equity=100000,
            risk_pct=1.0,
            regime_multiplier=1.0,
        )

        assert result["entry_price"] == 100.0
        # 固定 2% 止损: stop = 100 + 100*2% = 102.0 (SHORT 止损在上方)
        assert result["stop_loss"] == 102.0
        # take_profit = 100 - (102-100)*1 = 98.0 (SHORT 止盈在下方, 默认 rr_ratio=1.0)
        assert result["take_profit"] == 98.0

    def test_skip_returns_empty(self):
        """SKIP 方向返回空 dict"""
        data = _make_playbook_data()
        decision = {"direction": "SKIP"}
        result = compute_instruction_values(decision, data, 100000)
        assert result == {}

    def test_zero_price_returns_empty(self):
        """价格为 0 返回空"""
        data = _make_playbook_data(price=0)
        decision = {"direction": "BUY"}
        result = compute_instruction_values(decision, data, 100000)
        assert result == {}

    def test_atr_fallback_when_missing(self):
        """ATR 缺失时固定 2% 止损仍然生效"""
        data = _make_playbook_data(price=100.0, atr=None)
        # Override atr to None
        data.technical.atr_14 = None
        decision = {"direction": "BUY"}

        result = compute_instruction_values(decision, data, 100000, atr_multiplier=1.5)

        # 固定 2% 止损: stop = 100 - 100*2% = 98.0
        assert result["stop_loss"] == 98.0
        assert "FIXED_2.0%" in result["atr_source"]

    def test_regime_multiplier_affects_position(self):
        """Regime 乘数影响仓位大小"""
        data = _make_playbook_data(price=1000.0, atr=2.0)
        decision = {"direction": "BUY"}

        result_risk_on = compute_instruction_values(
            decision, data, 100000, regime_multiplier=1.0
        )
        result_cautious = compute_instruction_values(
            decision, data, 100000, regime_multiplier=0.5
        )

        # risk_amount 直接受 regime_multiplier 影响
        assert result_cautious["risk_amount"] == result_risk_on["risk_amount"] * 0.5
        # position_size 可能被 max_position_pct 截断, 但 risk_amount 是未截断的风险预算
        assert result_cautious["risk_amount"] > 0

    def test_position_size_minimum_1(self):
        """仓位至少为 1 股"""
        data = _make_playbook_data(price=100.0, atr=50.0)  # 巨大 ATR → 极小仓位
        decision = {"direction": "BUY"}
        result = compute_instruction_values(decision, data, 1000, risk_pct=0.1)
        assert result["position_size"] >= 1

    def test_entry_price_source_tracked(self):
        """入场价来源被追踪"""
        data = _make_playbook_data(price=120.0)
        decision = {"direction": "BUY"}
        result = compute_instruction_values(decision, data, 100000)
        assert "120.00" in result["entry_price_source"]
        assert "direction=BUY" in result["entry_price_source"]


# ========== build_thesis 测试 ==========

class TestBuildThesis:
    """Thesis 模板填充测试"""

    def test_trend_pullback_template(self):
        result = build_thesis("trend_pullback", ["above_ma20", "rsi_oversold"])
        assert "趋势回调" in result
        assert "above_ma20" in result
        assert "rsi_oversold" in result

    def test_skip_template(self):
        result = build_thesis("skip", [], "财报3天内")
        assert "跳过" in result
        assert "财报3天内" in result

    def test_unknown_template_defaults_to_trend_pullback(self):
        result = build_thesis("nonexistent", ["above_ma20"])
        assert "趋势回调" in result

    def test_empty_conditions(self):
        result = build_thesis("breakout", [])
        assert "无" in result

    def test_all_templates_are_valid(self):
        """所有模板都可以被填充"""
        for key in THESIS_TEMPLATES:
            if key == "skip":
                result = build_thesis(key, [], "test reason")
            else:
                result = build_thesis(key, ["test_condition"])
            assert isinstance(result, str)
            assert len(result) > 0


# ========== compute_confidence 测试 ==========

class TestComputeConfidence:
    """置信度计算测试"""

    def test_all_ok_all_verified(self):
        """6 维度 ok + 全部条件验证 → 高分"""
        data = _make_playbook_data(quality="ok")
        conditions = ["above_ma20", "rsi_oversold"]
        triggered = {"above_ma20": True, "rsi_oversold": True}

        confidence = compute_confidence(data, conditions, triggered)
        # quality: 6*12=72, condition: 2/2*28=28 → min(90, 100) = 90
        assert confidence == 90

    def test_partial_quality(self):
        """部分维度缺失 → 较低分"""
        data = _make_playbook_data(quality="ok")
        data.news.quality = "missing"
        data.sentiment.quality = "stale"

        conditions = ["above_ma20"]
        triggered = {"above_ma20": True}

        confidence = compute_confidence(data, conditions, triggered)
        # quality: 4*12=48, condition: 1/1*28=28 → 76
        assert confidence == 76

    def test_unverified_conditions(self):
        """未验证的条件降低分数"""
        data = _make_playbook_data(quality="ok")
        conditions = ["above_ma20", "fake_condition"]
        triggered = {"above_ma20": True}  # fake_condition 不在 triggered 中

        confidence = compute_confidence(data, conditions, triggered)
        # quality: 6*12=72, condition: 1/2*28=14 → 86
        assert confidence == 86

    def test_no_conditions(self):
        """无条件时 condition_score = 0"""
        data = _make_playbook_data(quality="ok")
        confidence = compute_confidence(data, [], {})
        # quality: 72, condition: 0 → 72
        assert confidence == 72

    def test_empty_triggered_factors(self):
        """triggered_factors 为 None"""
        data = _make_playbook_data(quality="ok")
        confidence = compute_confidence(data, ["above_ma20"], None)
        assert confidence == 72  # 只有 quality 分

    def test_cap_at_90(self):
        """总分不超过 90"""
        data = _make_playbook_data(quality="ok")
        conditions = ["a", "b", "c"]
        triggered = {"a": True, "b": True, "c": True}
        confidence = compute_confidence(data, conditions, triggered)
        assert confidence <= 90


# ========== InstructionGenerator 测试 ==========

class TestInstructionGeneratorValidation:
    """反幻觉验证逻辑测试"""

    def _make_generator(self):
        mock_client = MagicMock()
        return InstructionGenerator(llm_client=mock_client)

    def test_valid_decision_passes(self):
        """合法决策通过验证"""
        gen = self._make_generator()
        decisions = [{
            "symbol": "US.NTES",
            "direction": "BUY",
            "selected_conditions": ["above_ma20"],
            "thesis_template": "trend_pullback",
        }]
        factors = {"US.NTES": {"above_ma20": True}}

        result = gen._validate_decisions(decisions, factors)
        assert len(result) == 1
        assert result[0]["symbol"] == "US.NTES"
        assert result[0]["direction"] == "BUY"

    def test_unverified_condition_removed(self):
        """LLM 选择的未触发条件被移除 (核心反幻觉)"""
        gen = self._make_generator()
        decisions = [{
            "symbol": "US.NTES",
            "direction": "BUY",
            "selected_conditions": ["above_ma20", "fake_hallucinated"],
            "thesis_template": "trend_pullback",
        }]
        factors = {"US.NTES": {"above_ma20": True}}

        result = gen._validate_decisions(decisions, factors)
        assert len(result) == 1
        assert "fake_hallucinated" not in result[0]["selected_conditions"]
        assert "above_ma20" in result[0]["selected_conditions"]

    def test_invalid_direction_forced_to_skip(self):
        """无效 direction 被强制为 SKIP"""
        gen = self._make_generator()
        decisions = [{
            "symbol": "US.NTES",
            "direction": "INVALID",
            "selected_conditions": [],
            "thesis_template": "trend_pullback",
        }]
        result = gen._validate_decisions(decisions, {})
        assert result[0]["direction"] == "SKIP"

    def test_missing_fields_skipped(self):
        """缺少必要字段的决策被跳过"""
        gen = self._make_generator()
        decisions = [{"symbol": "US.NTES"}]  # 缺少 direction 等
        result = gen._validate_decisions(decisions, {})
        assert len(result) == 0

    def test_invalid_template_defaults(self):
        """无效 thesis_template 回退到 trend_pullback"""
        gen = self._make_generator()
        decisions = [{
            "symbol": "US.NTES",
            "direction": "BUY",
            "selected_conditions": [],
            "thesis_template": "nonexistent_template",
        }]
        result = gen._validate_decisions(decisions, {"US.NTES": {}})
        assert result[0]["thesis_template"] == "trend_pullback"

    def test_symbol_not_in_factors(self):
        """symbol 不在 factors 中 → 所有条件被移除"""
        gen = self._make_generator()
        decisions = [{
            "symbol": "US.UNKNOWN",
            "direction": "BUY",
            "selected_conditions": ["above_ma20", "rsi_oversold"],
            "thesis_template": "trend_pullback",
        }]
        result = gen._validate_decisions(decisions, {"US.NTES": {"above_ma20": True}})
        assert result[0]["selected_conditions"] == []


class TestInstructionGeneratorBuild:
    """指令构建测试"""

    def _make_generator(self):
        mock_client = MagicMock()
        return InstructionGenerator(llm_client=mock_client, risk_pct=1.0)

    def test_build_buy_instruction(self):
        """构建完整的 BUY 指令"""
        gen = self._make_generator()
        regime = _make_regime(MarketRegime.RISK_ON, mult=1.0)
        data = _make_playbook_data(price=100.0, atr=2.0)

        decisions = [{
            "symbol": "US.NTES",
            "direction": "BUY",
            "selected_conditions": ["above_ma20"],
            "thesis_template": "trend_pullback",
            "skip_reason": "",
        }]

        instructions = gen._build_instructions(
            decisions,
            {"US.NTES": data},
            {"US.NTES": {"above_ma20": True}},
            regime,
            100000,
        )

        assert len(instructions) == 1
        inst = instructions[0]
        assert inst.direction == "BUY"
        assert inst.entry_price == 100.0
        # 固定 2% 止损
        assert inst.stop_loss == 98.0
        # rr_ratio=1.0 (默认) → take_profit = 100 + (100-98)*1 = 102.0
        assert inst.take_profit == 102.0
        assert inst.position_size > 0
        assert inst.confidence > 0
        assert "above_ma20" in inst.conditions
        assert inst.data_quality["fundamental"] == "ok"

    def test_build_skip_instruction(self):
        """构建 SKIP 指令"""
        gen = self._make_generator()
        regime = _make_regime()

        decisions = [{
            "symbol": "US.META",
            "direction": "SKIP",
            "selected_conditions": ["earnings_approaching"],
            "thesis_template": "skip",
            "skip_reason": "财报3天内",
        }]

        instructions = gen._build_instructions(
            decisions, {"US.META": _make_playbook_data()}, {}, regime, 100000,
        )

        assert len(instructions) == 1
        assert instructions[0].direction == "SKIP"
        assert "财报" in instructions[0].skip_reason

    def test_build_with_missing_data(self):
        """数据缺失时强制 SKIP"""
        gen = self._make_generator()
        regime = _make_regime()

        decisions = [{
            "symbol": "US.NOPE",
            "direction": "BUY",
            "selected_conditions": [],
            "thesis_template": "trend_pullback",
        }]

        instructions = gen._build_instructions(
            decisions, {"US.NOPE": None}, {}, regime, 100000,
        )

        assert instructions[0].direction == "SKIP"
        assert "数据缺失" in instructions[0].skip_reason

    def test_risk_warnings_added(self):
        """数据不完整时添加风险提示 (ok_count < 4)"""
        gen = self._make_generator()
        regime = _make_regime()
        data = _make_playbook_data(quality="ok")
        # Set 3 dimensions to missing → ok_count = 3 < 4
        data.news.quality = "missing"
        data.sentiment.quality = "missing"
        data.earnings.quality = "missing"

        decisions = [{
            "symbol": "US.NTES",
            "direction": "BUY",
            "selected_conditions": ["above_ma20"],
            "thesis_template": "trend_pullback",
        }]

        instructions = gen._build_instructions(
            decisions,
            {"US.NTES": data},
            {"US.NTES": {"above_ma20": True}},
            regime,
            100000,
        )

        # 4/6 ok → should have risk warning
        assert any("数据不完整" in r for r in instructions[0].risks)


class TestInstructionGeneratorGenerate:
    """端到端生成测试 (含 LLM mock)"""

    def test_successful_generation(self):
        """正常流程: LLM 返回合法 JSON → 指令列表"""
        mock_client = MagicMock()
        llm_response = json.dumps([{
            "symbol": "US.NTES",
            "direction": "BUY",
            "selected_conditions": ["above_ma20"],
            "thesis_template": "trend_pullback",
        }])
        mock_client.call.return_value = llm_response

        gen = InstructionGenerator(llm_client=mock_client)
        regime = _make_regime(MarketRegime.RISK_ON, mult=1.0)
        data = _make_playbook_data(price=100.0, atr=2.0)

        candidates = [{
            "symbol": "US.NTES",
            "score": 8,
            "data": data,
            "factors": {"above_ma20": True},
        }]

        instructions = gen.generate(candidates, regime, [], 100000)
        assert len(instructions) == 1
        assert instructions[0].direction == "BUY"
        assert instructions[0].entry_price == 100.0

    def test_llm_invalid_json_returns_all_skip(self):
        """LLM 返回非法 JSON → 全部 SKIP"""
        mock_client = MagicMock()
        mock_client.call.return_value = "not valid json {{"

        gen = InstructionGenerator(llm_client=mock_client)
        regime = _make_regime()
        candidates = [{
            "symbol": "US.NTES",
            "score": 5,
            "data": _make_playbook_data(),
            "factors": {},
        }]

        instructions = gen.generate(candidates, regime, [], 100000)
        assert all(i.direction == "SKIP" for i in instructions)

    def test_llm_retry_on_failure(self):
        """第一次失败，第二次成功 → 重试机制"""
        mock_client = MagicMock()
        mock_client.call.side_effect = [
            "invalid json",
            json.dumps([{
                "symbol": "US.NTES",
                "direction": "BUY",
                "selected_conditions": [],
                "thesis_template": "trend_pullback",
            }]),
        ]

        gen = InstructionGenerator(llm_client=mock_client)
        regime = _make_regime()
        candidates = [{
            "symbol": "US.NTES",
            "score": 5,
            "data": _make_playbook_data(),
            "factors": {},
        }]

        instructions = gen.generate(candidates, regime, [], 100000, max_retries=2)
        assert mock_client.call.call_count == 2
        assert instructions[0].direction == "BUY"

    def test_llm_call_parameters(self):
        """验证 LLM 调用参数"""
        mock_client = MagicMock()
        mock_client.call.return_value = "[]"

        gen = InstructionGenerator(llm_client=mock_client)
        regime = _make_regime()
        gen.generate([], regime, [], 100000)

        mock_client.call.assert_called_once_with(
            feature="playbook_generator",
            prompt=mock_client.call.call_args.kwargs["prompt"],
            max_tokens=2048,
            temperature=0,
        )

    def test_no_trade_regime_in_prompt(self):
        """NO_TRADE 环境在 prompt 中体现"""
        mock_client = MagicMock()
        mock_client.call.return_value = "[]"

        gen = InstructionGenerator(llm_client=mock_client)
        regime = _make_regime(MarketRegime.NO_TRADE, score=-10, top_n=0, mult=0.0)
        candidates = [{
            "symbol": "US.NTES",
            "score": 5,
            "data": _make_playbook_data(),
            "factors": {"above_ma20": True},
        }]

        gen.generate(candidates, regime, [], 100000)
        prompt = mock_client.call.call_args.kwargs["prompt"]
        assert "NO_TRADE" in prompt

    def test_positions_included_in_prompt(self):
        """持仓信息被注入 prompt"""
        mock_client = MagicMock()
        mock_client.call.return_value = "[]"

        gen = InstructionGenerator(llm_client=mock_client)
        regime = _make_regime()
        positions = [
            {"symbol": "US.AMD", "sector": "Technology", "direction": "BUY"},
        ]
        gen.generate([], regime, positions, 100000)
        prompt = mock_client.call.call_args.kwargs["prompt"]
        assert "Technology" in prompt
