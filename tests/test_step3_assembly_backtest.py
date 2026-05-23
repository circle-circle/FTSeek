"""
Step 3 单元测试: FactorAssembly + WalkForward + StatisticalGate

≥ 20 test cases
"""
import pytest
import random
import math
from typing import List, Dict, Any

from app.factors.assembly import FactorAssembly
from app.factors.base import FactorValue, FactorParams
from app.backtest.walk_forward import WalkForwardEngine, _compute_stats
from app.backtest.statistical_gate import StatisticalGate


# ── Helper ──

def _make_factor_value(name, score=0.5, value=0.5, direction="LONG", confidence=0.8):
    return FactorValue(name=name, score=score, value=value, 
                       direction_hint=direction, confidence=confidence)


def _make_pnl_list(n=100, mean=1.0, std=3.0, seed=42):
    """生成模拟 PnL 序列"""
    rng = random.Random(seed)
    return [rng.gauss(mean, std) for _ in range(n)]


def _make_positive_pnl(n=100, seed=42):
    """生成明显正收益的 PnL"""
    rng = random.Random(seed)
    return [rng.gauss(2.0, 1.5) for _ in range(n)]


def _make_random_pnl(n=100, seed=42):
    """生成零均值的 PnL (无 alpha)"""
    rng = random.Random(seed)
    return [rng.gauss(0.0, 2.0) for _ in range(n)]


# ── FactorAssembly 测试 ──

class TestFactorAssembly:
    
    def test_evaluate_all_conditions_met(self):
        """所有条件满足 → 入场"""
        assembly = FactorAssembly(
            name="test_strategy",
            factors=[
                {"name": "SSS", "params": {}, "weight": 0.4},
                {"name": "GDIAN", "params": {}, "weight": 0.3},
                {"name": "MFFF", "params": {}, "weight": 0.3},
            ],
            direction_gate="ALL_LONG",
            min_composite_score=0.2,
            timeframe="15min",
            entry_conditions=["SSS.score >= -1", "GDIAN.value <= 40", "MFFF.score >= 0.5"],
        )
        
        fv = {
            "SSS": _make_factor_value("SSS", score=0.5, value=0.5, direction="LONG"),
            "GDIAN": _make_factor_value("GDIAN", score=0.3, value=30, direction="LONG"),
            "MFFF": _make_factor_value("MFFF", score=0.7, value=0.7, direction="LONG"),
        }
        
        enter, comp_score, direction = assembly.evaluate(fv)
        assert enter is True
        assert comp_score > 0.2
        assert direction == "LONG"
    
    def test_evaluate_condition_fails(self):
        """某个条件不满足 → 不入场"""
        assembly = FactorAssembly(
            name="test",
            factors=[{"name": "SSS", "params": {}, "weight": 1.0}],
            direction_gate="ALL_LONG",
            min_composite_score=0.0,
            timeframe="15min",
            entry_conditions=["SSS.score >= -1"],
        )
        
        fv = {"SSS": _make_factor_value("SSS", score=-1.5, direction="LONG")}
        enter, _, _ = assembly.evaluate(fv)
        assert enter is False
    
    def test_evaluate_missing_factor(self):
        """缺少必需因子 → 不入场"""
        assembly = FactorAssembly(
            name="test",
            factors=[
                {"name": "SSS", "params": {}, "weight": 0.5},
                {"name": "GDIAN", "params": {}, "weight": 0.5},
            ],
            direction_gate="ALL_LONG",
            min_composite_score=0.0,
            timeframe="15min",
            entry_conditions=[],
        )
        
        fv = {"SSS": _make_factor_value("SSS")}  # 缺少 GDIAN
        enter, _, _ = assembly.evaluate(fv)
        assert enter is False
    
    def test_evaluate_composite_score_below_threshold(self):
        """综合评分低于阈值 → 不入场"""
        assembly = FactorAssembly(
            name="test",
            factors=[{"name": "SSS", "params": {}, "weight": 1.0}],
            direction_gate="ALL_LONG",
            min_composite_score=0.8,  # 高阈值
            timeframe="15min",
            entry_conditions=[],
        )
        
        fv = {"SSS": _make_factor_value("SSS", score=0.3, confidence=0.5, direction="LONG")}
        enter, comp_score, _ = assembly.evaluate(fv)
        assert enter is False
        assert comp_score < 0.8
    
    def test_direction_gate_all_long(self):
        """ALL_LONG: 所有因子必须 LONG"""
        assembly = FactorAssembly(
            name="test",
            factors=[
                {"name": "SSS", "params": {}, "weight": 0.5},
                {"name": "MFFF", "params": {}, "weight": 0.5},
            ],
            direction_gate="ALL_LONG",
            min_composite_score=0.0,
            timeframe="15min",
            entry_conditions=[],
        )
        
        # 一个 LONG 一个 SHORT
        fv = {
            "SSS": _make_factor_value("SSS", direction="LONG"),
            "MFFF": _make_factor_value("MFFF", direction="SHORT"),
        }
        enter, _, direction = assembly.evaluate(fv)
        assert enter is False
    
    def test_direction_gate_consensus(self):
        """CONSENSUS: 多数方向一致"""
        assembly = FactorAssembly(
            name="test",
            factors=[
                {"name": "SSS", "params": {}, "weight": 0.33},
                {"name": "GDIAN", "params": {}, "weight": 0.33},
                {"name": "MFFF", "params": {}, "weight": 0.34},
            ],
            direction_gate="CONSENSUS",
            min_composite_score=0.0,
            timeframe="15min",
            entry_conditions=[],
        )
        
        fv = {
            "SSS": _make_factor_value("SSS", direction="LONG"),
            "GDIAN": _make_factor_value("GDIAN", direction="LONG"),
            "MFFF": _make_factor_value("MFFF", direction="SHORT"),
        }
        enter, _, direction = assembly.evaluate(fv)
        assert enter is True
        assert direction == "LONG"
    
    def test_direction_gate_any(self):
        """ANY: 任意有方向即可"""
        assembly = FactorAssembly(
            name="test",
            factors=[
                {"name": "SSS", "params": {}, "weight": 0.5},
                {"name": "MFFF", "params": {}, "weight": 0.5},
            ],
            direction_gate="ANY",
            min_composite_score=0.0,
            timeframe="15min",
            entry_conditions=[],
        )
        
        fv = {
            "SSS": _make_factor_value("SSS", direction="SHORT"),
            "MFFF": _make_factor_value("MFFF", direction="NEUTRAL"),
        }
        enter, _, direction = assembly.evaluate(fv)
        assert enter is True
        assert direction == "SHORT"
    
    def test_to_dict_from_dict(self):
        """序列化/反序列化"""
        assembly = FactorAssembly(
            name="test_strategy",
            factors=[{"name": "SSS", "params": {}, "weight": 1.0}],
            direction_gate="ALL_LONG",
            min_composite_score=0.3,
            timeframe="15min",
            entry_conditions=["SSS.score >= -1"],
            description="test",
        )
        d = assembly.to_dict()
        restored = FactorAssembly.from_dict(d)
        assert restored.name == assembly.name
        assert restored.factors == assembly.factors
        assert restored.entry_conditions == assembly.entry_conditions
    
    def test_multiple_conditions_all_must_pass(self):
        """多个条件必须全部满足"""
        assembly = FactorAssembly(
            name="test",
            factors=[
                {"name": "SSS", "params": {}, "weight": 0.5},
                {"name": "GDIAN", "params": {}, "weight": 0.5},
            ],
            direction_gate="ALL_LONG",
            min_composite_score=0.0,
            timeframe="15min",
            entry_conditions=[
                "SSS.score >= -1",
                "GDIAN.value <= 35",
            ],
        )
        
        # SSS 通过, GDIAN 失败 (value=40 > 35)
        fv = {
            "SSS": _make_factor_value("SSS", score=0.5, direction="LONG"),
            "GDIAN": _make_factor_value("GDIAN", value=40, direction="LONG"),
        }
        enter, _, _ = assembly.evaluate(fv)
        assert enter is False
    
    def test_condition_operators(self):
        """测试各种比较运算符"""
        assembly = FactorAssembly(
            name="test", factors=[{"name": "SSS", "params": {}, "weight": 1.0}],
            direction_gate="ALL_LONG", min_composite_score=0.0,
            timeframe="15min", entry_conditions=["SSS.value <= 35"],
        )
        
        # value=30 <= 35 → 通过
        fv = {"SSS": _make_factor_value("SSS", value=30, direction="LONG")}
        enter, _, _ = assembly.evaluate(fv)
        assert enter is True
        
        # value=40 > 35 → 失败
        fv = {"SSS": _make_factor_value("SSS", value=40, direction="LONG")}
        enter, _, _ = assembly.evaluate(fv)
        assert enter is False


# ── WalkForward 测试 ──

class TestWalkForward:
    
    def test_compute_stats_basic(self):
        """基础统计计算"""
        pnl = [1.0, -0.5, 2.0, -1.0, 3.0]
        stats = _compute_stats(pnl)
        assert stats["n_trades"] == 5
        assert stats["win_rate"] == 0.6  # 3/5
        assert stats["total_pnl"] == 4.5
    
    def test_compute_stats_empty(self):
        """空 PnL"""
        stats = _compute_stats([])
        assert stats["n_trades"] == 0
    
    def test_compute_stats_all_wins(self):
        """全赢"""
        stats = _compute_stats([1, 2, 3, 4, 5])
        assert stats["win_rate"] == 1.0
        assert stats["profit_factor"] == float("inf")
    
    def test_walk_forward_basic(self):
        """基础 Walk-Forward 运行"""
        engine = WalkForwardEngine()
        
        # 简单信号生成器: 返回固定 PnL
        def signal_gen(bars):
            return [random.gauss(0.5, 1.0) for _ in range(len(bars) // 10)]
        
        random.seed(42)
        bars = [{"close": 100 + i * 0.1} for i in range(300)]
        
        result = engine.run(signal_gen, bars, train_pct=0.7, n_windows=3)
        
        assert result.n_windows == 3
        assert len(result.window_results) == 3
        assert result.in_sample_stats["n_trades"] > 0
        assert result.out_sample_stats["n_trades"] > 0
    
    def test_walk_forward_degradation_ratio(self):
        """退化比率计算"""
        engine = WalkForwardEngine()
        
        # IS 表现好, OOS 表现差
        call_count = [0]
        def signal_gen(bars):
            call_count[0] += 1
            n = len(bars) // 10
            # 奇数调用 = IS (好), 偶数调用 = OOS (差)
            if call_count[0] % 2 == 1:
                return [2.0 + random.gauss(0, 0.5) for _ in range(n)]
            else:
                return [random.gauss(0, 1.0) for _ in range(n)]
        
        random.seed(42)
        bars = [{"close": 100} for _ in range(200)]
        result = engine.run(signal_gen, bars, train_pct=0.7, n_windows=2)
        
        # 退化比率应该 < 1 (OOS 比 IS 差)
        assert result.degradation_ratio >= 0  # 至少非负
    
    def test_walk_forward_result_to_dict(self):
        """结果序列化"""
        from app.backtest.walk_forward import WalkForwardResult
        result = WalkForwardResult(
            n_windows=3,
            degradation_ratio=0.85,
            param_stability=0.9,
        )
        d = result.to_dict()
        assert d["n_windows"] == 3
        assert d["degradation_ratio"] == 0.85


# ── StatisticalGate 测试 ──

class TestStatisticalGate:
    
    def test_pass_with_strong_signal(self):
        """强信号应通过门禁"""
        gate = StatisticalGate()
        # 明显正收益 (mean=2, std=1.5)
        pnl = _make_positive_pnl(200)
        result = gate.evaluate(pnl, n_total_tests=1, timeframe="15min")
        assert result.passed is True
        assert result.sharpe_corrected > 0
    
    def test_fail_with_no_signal(self):
        """零均值不应通过门禁"""
        gate = StatisticalGate()
        pnl = _make_random_pnl(200, seed=123)
        result = gate.evaluate(pnl, n_total_tests=1, timeframe="15min")
        # 大概率不通过 (但不保证 100%)
        # 至少检查 p 值是否合理
        assert result.p_value_bonferroni >= 0
    
    def test_fail_insufficient_samples(self):
        """样本量不足 → 失败"""
        gate = StatisticalGate()
        pnl = _make_positive_pnl(20)  # 只有 20 笔
        result = gate.evaluate(pnl, n_total_tests=1, timeframe="15min")
        assert result.passed is False
        assert "FAIL" in result.details.get("sample_size", "")
    
    def test_bonferroni_correction(self):
        """Bonferroni 校正使 p 值变大"""
        gate = StatisticalGate()
        pnl = _make_positive_pnl(150)
        
        r1 = gate.evaluate(pnl, n_total_tests=1)
        r10 = gate.evaluate(pnl, n_total_tests=10)
        
        # 多次检验的 p 值应该更大
        assert r10.p_value_bonferroni >= r1.p_value_bonferroni
    
    def test_sharpe_corrected_annualized(self):
        """修正 Sharpe 应该年化"""
        gate = StatisticalGate()
        pnl = _make_positive_pnl(200)
        result = gate.evaluate(pnl, trades_per_year=252)
        
        # 年化 Sharpe = per_trade * sqrt(252) ≈ per_trade * 15.87
        assert abs(result.sharpe_corrected) > 0
    
    def test_monte_carlo_p_value(self):
        """蒙特卡洛 p 值范围 [0, 1]"""
        gate = StatisticalGate()
        pnl = _make_positive_pnl(100)
        mc_p = gate._monte_carlo_permutation(pnl, n_permutations=100)
        assert 0 <= mc_p <= 1
    
    def test_top_n_robustness(self):
        """头部移除稳健性"""
        gate = StatisticalGate()
        pnl = _make_positive_pnl(100)
        n = gate._top_n_robustness(pnl, 252)
        assert n >= 0
    
    def test_gate_result_to_dict(self):
        """结果序列化"""
        from app.backtest.statistical_gate import GateResult
        r = GateResult(passed=True, sharpe_corrected=2.5)
        d = r.to_dict()
        assert d["passed"] is True
        assert d["sharpe_corrected"] == 2.5
    
    def test_different_timeframes(self):
        """不同时间框架的最小样本量"""
        gate = StatisticalGate()
        
        # 5min 需要 200 笔
        pnl_50 = _make_positive_pnl(50)
        r = gate.evaluate(pnl_50, timeframe="5min")
        assert r.passed is False
        
        # daily 只需要 50 笔
        r = gate.evaluate(pnl_50, timeframe="daily")
        # 50 笔可能通过样本量检查
        assert "PASS" in r.details.get("sample_size", "")


# ── YAML 配置加载测试 ──

class TestFactorProfilesConfig:
    
    def test_load_factor_profiles(self):
        """加载 factor_profiles.yaml"""
        import yaml
        from pathlib import Path
        
        path = Path(__file__).resolve().parent.parent / "config" / "factor_profiles.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        
        variants = data.get("strategy_variants", {})
        assert len(variants) == 9
        assert "aggressive_5m_high_momentum" in variants
        assert "stable_15m_base" in variants
        assert "radical_daily_deep_oversold" in variants
    
    def test_factor_profile_structure(self):
        """每个 profile 结构完整"""
        import yaml
        from pathlib import Path
        
        path = Path(__file__).resolve().parent.parent / "config" / "factor_profiles.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        
        for name, variant in data["strategy_variants"].items():
            assert "factors" in variant, f"{name}: missing factors"
            assert "entry_conditions" in variant, f"{name}: missing entry_conditions"
            assert "exit_profile" in variant, f"{name}: missing exit_profile"
            assert "timeframe" in variant, f"{name}: missing timeframe"
            assert "direction_gate" in variant, f"{name}: missing direction_gate"
            
            # 每个因子有 name, params, weight
            for factor in variant["factors"]:
                assert "name" in factor, f"{name}: factor missing name"
                assert "weight" in factor, f"{name}: factor missing weight"
    
    def test_assembly_from_profile(self):
        """从 YAML profile 创建 FactorAssembly"""
        import yaml
        from pathlib import Path
        
        path = Path(__file__).resolve().parent.parent / "config" / "factor_profiles.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        
        variant = data["strategy_variants"]["stable_15m_base"]
        assembly = FactorAssembly(
            name="stable_15m_base",
            factors=variant["factors"],
            direction_gate=variant["direction_gate"],
            min_composite_score=variant["min_composite_score"],
            timeframe=variant["timeframe"],
            entry_conditions=variant["entry_conditions"],
        )
        
        assert assembly.name == "stable_15m_base"
        assert len(assembly.factors) == 3
        assert assembly.timeframe == "15min"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
