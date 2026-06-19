"""Tests for app.playbook.condition_taxonomy — 固定条件枚举表"""
import pytest
from app.playbook.condition_taxonomy import (
    CONDITION_TAXONOMY,
    get_condition_list,
    get_condition_description,
    validate_conditions,
    get_taxonomy_for_prompt,
)


class TestConditionTaxonomy:
    """条件枚举表基础测试"""

    def test_taxonomy_has_32_conditions(self):
        """Spec 4.5.1 说 27 但实际实现有 32 (7 类别完整计数)"""
        assert len(CONDITION_TAXONOMY) == 32

    def test_all_keys_are_strings(self):
        for key in CONDITION_TAXONOMY:
            assert isinstance(key, str)
            assert len(key) > 0

    def test_all_values_are_strings(self):
        for value in CONDITION_TAXONOMY.values():
            assert isinstance(value, str)
            assert len(value) > 0

    def test_no_duplicate_keys(self):
        """确保没有重复的条件标签"""
        keys = list(CONDITION_TAXONOMY.keys())
        assert len(keys) == len(set(keys))

    def test_expected_categories_present(self):
        """验证 7 个类别的条件都存在"""
        # 趋势类
        for c in ["above_ma20", "above_ma50", "below_ma20", "below_ma50",
                   "ma_golden_cross", "ma_dead_cross"]:
            assert c in CONDITION_TAXONOMY, f"趋势类缺少: {c}"

        # 动量类
        for c in ["rsi_oversold", "rsi_overbought", "rsi_neutral_low",
                   "macd_golden_cross", "macd_dead_cross",
                   "bollinger_lower", "bollinger_upper", "bollinger_squeeze"]:
            assert c in CONDITION_TAXONOMY, f"动量类缺少: {c}"

        # 价值类
        for c in ["pe_below_industry", "pe_above_industry", "revenue_growth_high",
                   "roe_high", "near_52w_low", "near_52w_high"]:
            assert c in CONDITION_TAXONOMY, f"价值类缺少: {c}"

        # 内部人
        for c in ["insider_cluster_buy", "insider_net_buy", "insider_net_sell"]:
            assert c in CONDITION_TAXONOMY, f"内部人类别缺少: {c}"

        # 情绪/新闻
        for c in ["news_positive", "news_negative", "sentiment_bullish", "sentiment_bearish"]:
            assert c in CONDITION_TAXONOMY, f"情绪/新闻类别缺少: {c}"

        # 量价类
        for c in ["volume_expansion", "volume_contraction"]:
            assert c in CONDITION_TAXONOMY, f"量价类缺少: {c}"

        # 事件类
        for c in ["earnings_beat_streak", "post_earnings_momentum", "earnings_approaching"]:
            assert c in CONDITION_TAXONOMY, f"事件类缺少: {c}"


class TestGetConditionList:
    def test_returns_list(self):
        result = get_condition_list()
        assert isinstance(result, list)

    def test_returns_all_keys(self):
        result = get_condition_list()
        assert len(result) == 32
        assert set(result) == set(CONDITION_TAXONOMY.keys())


class TestGetConditionDescription:
    def test_valid_condition(self):
        desc = get_condition_description("above_ma20")
        assert "MA20" in desc

    def test_insider_cluster_buy(self):
        desc = get_condition_description("insider_cluster_buy")
        assert "cluster" in desc.lower() or "集群" in desc

    def test_unknown_condition(self):
        desc = get_condition_description("nonexistent_condition")
        assert "未知" in desc


class TestValidateConditions:
    def test_all_valid(self):
        conditions = ["above_ma20", "rsi_oversold", "insider_cluster_buy"]
        is_valid, invalid = validate_conditions(conditions)
        assert is_valid is True
        assert invalid == []

    def test_empty_list(self):
        is_valid, invalid = validate_conditions([])
        assert is_valid is True
        assert invalid == []

    def test_some_invalid(self):
        conditions = ["above_ma20", "fake_condition", "rsi_oversold"]
        is_valid, invalid = validate_conditions(conditions)
        assert is_valid is False
        assert invalid == ["fake_condition"]

    def test_all_invalid(self):
        conditions = ["fake1", "fake2"]
        is_valid, invalid = validate_conditions(conditions)
        assert is_valid is False
        assert len(invalid) == 2


class TestGetTaxonomyForPrompt:
    def test_returns_string(self):
        result = get_taxonomy_for_prompt()
        assert isinstance(result, str)

    def test_contains_category_headers(self):
        result = get_taxonomy_for_prompt()
        assert "趋势类" in result
        assert "动量类" in result
        assert "价值类" in result

    def test_contains_condition_names(self):
        result = get_taxonomy_for_prompt()
        assert "above_ma20" in result
        assert "insider_cluster_buy" in result

    def test_contains_descriptions(self):
        result = get_taxonomy_for_prompt()
        assert "MA20" in result
        assert "RSI" in result
