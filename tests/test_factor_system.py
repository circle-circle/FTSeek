"""
因子系统单元测试 — Phase 1 验证

覆盖:
1. BaseFactor 接口 + FactorValue
2. FactorRegistry 注册/发现/实例化
3. SSS/GDIAN/MFFF 因子计算正确性
4. FactorAssembly 组合评估
5. ConditionEvaluator 条件解析
6. StrategyVariantLibrary YAML 加载
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.factors.base import (
    BaseFactor, FactorValue, FactorParams, FactorTimeframe,
    ConditionEvaluator,
)
from app.factors.registry import FactorRegistry
from app.factors.sss import SSSFactor
from app.factors.gdian import GDIANFactor
from app.factors.mfff import MFFFFactor
from app.factors.assembly import (
    FactorAssembly, FactorSlot, AssemblyResult,
    StrategyVariantLibrary,
)


# ============================================================
#  Fixtures
# ============================================================
@pytest.fixture(autouse=True)
def reset_registry():
    """每个测试前重置 registry"""
    FactorRegistry.reset()
    yield
    FactorRegistry.reset()


def make_market_data(**overrides):
    """生成标准测试行情数据"""
    data = {
        "symbol": "US.AAPL",
        "price": 150.0,
        "close": 150.0,
        "open": 149.0,
        "high": 151.0,
        "low": 148.0,
        "prev_close": 148.5,
        "volume": 50_000_000,
        "avg_volume": 60_000_000,
        "ma_5": 149.5,
        "ma_20": 148.0,
        "ma_50": 145.0,
        "rsi_14": 35.0,
        "macd": 0.5,
        "macd_signal": 0.3,
        "macd_hist": 0.2,
        "bollinger_upper": 155.0,
        "bollinger_lower": 142.0,
        "bollinger_mid": 148.5,
        "atr_14": 2.5,
        "bars": [
            {"close": 147.0, "high": 148.0, "low": 146.0, "volume": 55_000_000},
            {"close": 147.5, "high": 148.5, "low": 147.0, "volume": 52_000_000},
            {"close": 148.0, "high": 149.0, "low": 147.5, "volume": 58_000_000},
            {"close": 149.0, "high": 150.0, "low": 148.0, "volume": 60_000_000},
            {"close": 150.0, "high": 151.0, "low": 149.0, "volume": 50_000_000},
        ],
    }
    data.update(overrides)
    return data


# ============================================================
#  1. FactorValue 基础测试
# ============================================================
class TestFactorValue:
    def test_bullish(self):
        fv = FactorValue(name="test", score=0.5)
        assert fv.is_bullish(0.3)
        assert not fv.is_bearish()

    def test_bearish(self):
        fv = FactorValue(name="test", score=-0.6)
        assert fv.is_bearish(-0.3)
        assert not fv.is_bullish()

    def test_to_dict(self):
        fv = FactorValue(name="SSS", value=-0.5, score=-0.5, direction_hint="LONG")
        d = fv.to_dict()
        assert d["name"] == "SSS"
        assert d["score"] == -0.5
        assert d["direction_hint"] == "LONG"


# ============================================================
#  2. SSS 因子测试
# ============================================================
class TestSSSFactor:
    def test_extreme_oversold(self):
        """极端超卖: RSI=20, 价格低于布林下轨, 缩量"""
        factor = SSSFactor()
        md = make_market_data(
            rsi_14=20,
            price=140.0,
            bollinger_lower=142.0,
            bollinger_upper=155.0,
            volume=20_000_000,
            avg_volume=60_000_000,
        )
        result = factor.compute("US.AAPL", md)
        assert result.name == "SSS"
        assert result.score < -0.3, f"Expected oversold, got {result.score}"
        assert result.direction_hint == "LONG", "Oversold should hint LONG"
        assert result.confidence > 0.3

    def test_extreme_overbought(self):
        """极端超买: RSI=80, 价格高于布林上轨, 放量"""
        factor = SSSFactor()
        md = make_market_data(
            rsi_14=80,
            price=158.0,
            bollinger_lower=142.0,
            bollinger_upper=155.0,
            volume=100_000_000,
            avg_volume=60_000_000,
        )
        result = factor.compute("US.AAPL", md)
        assert result.score > 0.3, f"Expected overbought, got {result.score}"
        assert result.direction_hint == "SHORT"

    def test_neutral(self):
        """中性: RSI=50, 价格在布林中轨, 正常量"""
        factor = SSSFactor()
        md = make_market_data(
            rsi_14=50,
            price=148.5,
            bollinger_lower=142.0,
            bollinger_upper=155.0,
            volume=60_000_000,
            avg_volume=60_000_000,
        )
        result = factor.compute("US.AAPL", md)
        assert abs(result.score) < 0.3, f"Expected neutral, got {result.score}"
        assert result.direction_hint == "NEUTRAL"

    def test_param_space(self):
        """参数扫描空间生成"""
        factor = SSSFactor()
        combos = factor.get_param_combinations()
        # threshold: 7 values, w_rsi: 2, w_bb: 2, w_vol: 2 = 7*2*2*2 = 56
        assert len(combos) == 56, f"Expected 56 combinations, got {len(combos)}"

    def test_metadata_populated(self):
        """metadata 应包含所有中间值"""
        factor = SSSFactor()
        md = make_market_data()
        result = factor.compute("US.AAPL", md)
        assert "rsi" in result.metadata
        assert "bb_component" in result.metadata
        assert "vol_ratio" in result.metadata


# ============================================================
#  3. GDIAN 因子测试
# ============================================================
class TestGDIANFactor:
    def test_strong_uptrend(self):
        """强上升趋势"""
        factor = GDIANFactor()
        md = make_market_data(
            price=160.0,
            ma_5=158.0,
            ma_20=152.0,
            ma_50=145.0,
        )
        result = factor.compute("US.AAPL", md)
        assert result.name == "GDIAN"
        assert result.score > 0, f"Expected positive for uptrend, got {result.score}"
        assert result.metadata.get("trend_direction") in ("up", "neutral")

    def test_weak_trend(self):
        """弱趋势 (适合反转)"""
        factor = GDIANFactor()
        md = make_market_data(
            price=148.0,
            ma_5=148.1,
            ma_20=148.0,
            ma_50=148.2,
        )
        result = factor.compute("US.AAPL", md)
        assert result.metadata.get("is_tight") == True or result.value < 35

    def test_param_combinations(self):
        factor = GDIANFactor()
        combos = factor.get_param_combinations()
        # tight_threshold: 4, w_slope: 2, w_adx: 2, w_pos: 2 = 4*2*2*2 = 32
        assert len(combos) == 32


# ============================================================
#  4. MFFF 因子测试
# ============================================================
class TestMFFFFactor:
    def test_strong_positive_momentum(self):
        """强正向动量"""
        factor = MFFFFactor()
        md = make_market_data(
            macd=1.5,
            macd_signal=0.5,
            macd_hist=1.0,
            volume=80_000_000,
            avg_volume=60_000_000,
            net_capital_flow=2_000_000,
        )
        result = factor.compute("US.AAPL", md)
        assert result.name == "MFFF"
        assert result.score > 0.2, f"Expected positive momentum, got {result.score}"
        assert result.direction_hint == "LONG"

    def test_negative_momentum(self):
        """负向动量"""
        factor = MFFFFactor()
        md = make_market_data(
            macd=-1.0,
            macd_signal=0.2,
            macd_hist=-1.2,
            volume=30_000_000,
            avg_volume=60_000_000,
            net_capital_flow=-1_500_000,
        )
        result = factor.compute("US.AAPL", md)
        assert result.score < -0.2, f"Expected negative momentum, got {result.score}"
        assert result.direction_hint == "SHORT"

    def test_above_threshold_flag(self):
        """threshold 标记"""
        factor = MFFFFactor(FactorParams(params={"threshold": 0.5}))
        md = make_market_data(
            macd=2.0, macd_signal=0.5, macd_hist=1.5,
            volume=100_000_000, avg_volume=60_000_000,
            net_capital_flow=3_000_000,
        )
        result = factor.compute("US.AAPL", md)
        assert result.metadata.get("above_threshold") == True


# ============================================================
#  5. FactorRegistry 测试
# ============================================================
class TestFactorRegistry:
    def test_register_and_create(self):
        reg = FactorRegistry.instance()
        reg.register(SSSFactor)
        assert "SSS" in reg
        factor = reg.create("SSS")
        assert factor.name == "SSS"

    def test_auto_discover(self):
        reg = FactorRegistry.instance()
        count = reg.auto_discover()
        assert count >= 3, f"Expected >= 3 factors, discovered {count}"
        assert "SSS" in reg
        assert "GDIAN" in reg
        assert "MFFF" in reg

    def test_compute_all(self):
        reg = FactorRegistry.instance()
        reg.register(SSSFactor)
        reg.register(GDIANFactor)
        reg.register(MFFFFactor)
        md = make_market_data()
        results = reg.compute_all("US.AAPL", md)
        assert len(results) == 3
        assert "SSS" in results
        assert "GDIAN" in results
        assert "MFFF" in results

    def test_unregister(self):
        reg = FactorRegistry.instance()
        reg.register(SSSFactor)
        assert "SSS" in reg
        reg.unregister("SSS")
        assert "SSS" not in reg

    def test_create_unregistered_raises(self):
        reg = FactorRegistry.instance()
        with pytest.raises(KeyError):
            reg.create("NONEXISTENT")


# ============================================================
#  6. ConditionEvaluator 测试
# ============================================================
class TestConditionEvaluator:
    def test_pass(self):
        fv = FactorValue(name="SSS", score=-1.5)
        passed, reason = ConditionEvaluator.evaluate(
            "SSS.score >= -1.5", {"SSS": fv})
        assert passed

    def test_fail(self):
        fv = FactorValue(name="SSS", score=0.5)
        passed, reason = ConditionEvaluator.evaluate(
            "SSS.score <= -1.5", {"SSS": fv})
        assert not passed

    def test_gdian_value(self):
        fv = FactorValue(name="GDIAN", value=30.0)
        passed, _ = ConditionEvaluator.evaluate(
            "GDIAN.value <= 35", {"GDIAN": fv})
        assert passed

    def test_missing_factor(self):
        passed, reason = ConditionEvaluator.evaluate(
            "MISSING.score >= 0", {})
        assert not passed
        assert "not found" in reason

    def test_invalid_format(self):
        passed, reason = ConditionEvaluator.evaluate(
            "bad format string", {})
        assert not passed


# ============================================================
#  7. FactorAssembly 测试
# ============================================================
class TestFactorAssembly:
    def test_full_assembly_pass(self):
        """完整组合评估 — 所有条件满足"""
        reg = FactorRegistry.instance()
        reg.register(SSSFactor)
        reg.register(GDIANFactor)
        reg.register(MFFFFactor)

        assembly = FactorAssembly(
            name="test_oversold",
            timeframe="15min",
            slots=[
                FactorSlot(name="SSS", params={},
                           weight=0.4, condition="SSS.score >= -1.5"),
                FactorSlot(name="GDIAN", params={},
                           weight=0.3, condition="GDIAN.value <= 45"),
                FactorSlot(name="MFFF", params={},
                           weight=0.3, condition="MFFF.score >= -1.0"),
            ],
            direction_gate="CONSENSUS",
            min_composite_score=0.0,  # 放宽以便测试
        )

        md = make_market_data(rsi_14=30, price=143.0,
                               bollinger_lower=142.0, bollinger_upper=155.0)
        result = assembly.evaluate("US.AAPL", md, registry=reg)

        assert isinstance(result, AssemblyResult)
        assert result.assembly_name == "test_oversold"
        assert len(result.factor_values) == 3
        assert result.direction in ("LONG", "SHORT", "NEUTRAL")

    def test_condition_failure_blocks(self):
        """条件不满足 → passed=False"""
        reg = FactorRegistry.instance()
        reg.register(SSSFactor)

        assembly = FactorAssembly(
            name="strict_test",
            slots=[
                FactorSlot(name="SSS", params={},
                           weight=1.0, condition="SSS.score <= -2.0"),
            ],
            min_composite_score=0.0,
        )

        # 中性行情 → SSS 不会达到 -2.0
        md = make_market_data(rsi_14=50, price=148.5)
        result = assembly.evaluate("US.AAPL", md, registry=reg)
        assert result.passed == False

    def test_to_dict_roundtrip(self):
        assembly = FactorAssembly(
            name="test",
            slots=[FactorSlot(name="SSS", weight=1.0)],
        )
        d = assembly.to_dict()
        restored = FactorAssembly.from_dict(d)
        assert restored.name == "test"
        assert len(restored.slots) == 1


# ============================================================
#  8. StrategyVariantLibrary 测试
# ============================================================
class TestStrategyVariantLibrary:
    def test_load_from_yaml(self):
        lib = StrategyVariantLibrary()
        count = lib.load("config/strategy_variants.yaml")
        assert count >= 9, f"Expected >= 9 variants, loaded {count}"

    def test_best_variant_exists(self):
        lib = StrategyVariantLibrary()
        lib.load("config/strategy_variants.yaml")
        assert "stable_15m_relaxed_oversold_tight_trend" in lib
        variant = lib.get("stable_15m_relaxed_oversold_tight_trend")
        assert variant is not None
        assert len(variant.slots) == 3
        assert variant.timeframe == "15min"

    def test_all_variants_have_factors(self):
        lib = StrategyVariantLibrary()
        lib.load("config/strategy_variants.yaml")
        for name in lib.list_variants():
            v = lib.get(name)
            assert len(v.slots) >= 1, f"{name} should have >= 1 slots, has {len(v.slots)}"


# ============================================================
#  Run
# ============================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
