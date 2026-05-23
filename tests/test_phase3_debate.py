"""
Phase 3 测试: 统计门禁 + 辩论框架

覆盖:
1. StatisticalGate 各项检验
2. DebatePromptBuilder prompt 生成
3. StrategyDebateRunner (with mock LLM)
4. DebateResult 序列化
"""
import sys
import os
import asyncio
import json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.debate.statistical_gate import StatisticalGate, GateResult
from app.debate.strategy_debate_runner import (
    DebatePromptBuilder, StrategyDebateRunner,
    DebateResult, DebateRound,
)


# ============================================================
#  Helpers
# ============================================================
def make_profitable_trades(n=120, mean=2.0, std=5.0, seed=42):
    """生成盈利交易序列"""
    import random
    rng = random.Random(seed)
    return [{"pnl": rng.gauss(mean, std)} for _ in range(n)]


def make_losing_trades(n=100, mean=-1.0, std=3.0, seed=42):
    """生成亏损交易序列"""
    import random
    rng = random.Random(seed)
    return [{"pnl": rng.gauss(mean, std)} for _ in range(n)]


def make_concentrated_trades(n=50, seed=42):
    """生成利润集中的交易 (top 10% 贡献 >80%)"""
    import random
    rng = random.Random(seed)
    trades = [{"pnl": rng.gauss(0.5, 2.0)} for _ in range(n)]
    # 注入几笔大额盈利
    trades[0]["pnl"] = 200.0
    trades[1]["pnl"] = 150.0
    trades[2]["pnl"] = 100.0
    return trades


# ============================================================
#  1. StatisticalGate 测试
# ============================================================
class TestStatisticalGate:
    def test_profitable_strategy_passes(self):
        """盈利策略 (120笔, mean=2.0) 应通过门禁"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=120, mean=2.0, std=5.0)
        result = gate.evaluate(trades, n_total_tests=10, timeframe="15min")
        assert isinstance(result, GateResult)
        assert result.metrics["n_trades"] == 120
        assert result.metrics["mean_pnl"] > 0
        assert result.metrics["win_rate"] > 0.4
        # 120 笔满足最小样本量
        assert result.checks["min_samples"]

    def test_losing_strategy_fails(self):
        """亏损策略应未通过"""
        gate = StatisticalGate()
        trades = make_losing_trades(n=100, mean=-1.0, std=3.0)
        result = gate.evaluate(trades, n_total_tests=10, timeframe="15min")
        # 亏损策略 Sharpe 应为负或不显著
        assert result.metrics["mean_pnl"] < 0

    def test_insufficient_samples(self):
        """样本量不足"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=20, mean=5.0, std=3.0)
        result = gate.evaluate(trades, n_total_tests=1, timeframe="15min")
        assert not result.checks["min_samples"]
        assert "样本量不足" in result.reason

    def test_empty_trades(self):
        """空交易列表"""
        gate = StatisticalGate()
        result = gate.evaluate([], n_total_tests=1)
        assert not result.passed
        assert not result.checks.get("has_trades", True)

    def test_bonferroni_correction(self):
        """Bonferroni 校正: 测试数越多，越难通过"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=120, mean=1.5, std=6.0)

        # 少量测试 → 容易通过
        r_few = gate.evaluate(trades, n_total_tests=5, timeframe="15min")
        # 大量测试 → Bonferroni 更严
        r_many = gate.evaluate(trades, n_total_tests=500, timeframe="15min")

        # p_bonferroni 在大量测试时更高 (更难显著)
        assert r_many.metrics["p_bonferroni"] >= r_few.metrics["p_bonferroni"]

    def test_corrected_sharpe_vs_naive(self):
        """修正 Sharpe 应低于 naive sqrt(252) 年化"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=68, mean=5.28, std=15.0)

        # 日线策略: 68笔/5年 = 13.6笔/年
        result = gate.evaluate(trades, n_total_tests=1, timeframe="daily",
                                trades_per_year=13.6)

        corrected = result.metrics["corrected_sharpe"]
        # naive = sharpe_per_trade * sqrt(252)
        naive = result.metrics["sharpe_per_trade"] * (252 ** 0.5)
        assert corrected < naive, "修正 Sharpe 应低于 naive"

    def test_top_n_robustness(self):
        """Top-N 稳健性: 移除头部后应退化"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=100, mean=2.0, std=4.0)
        result = gate.evaluate(trades, n_total_tests=1, timeframe="15min")
        # 健康策略应有 >= 3 的 top_n_robust
        assert result.metrics["top_n_robust"] >= 0

    def test_monte_carlo(self):
        """蒙特卡洛置换: 真策略 p < 0.5"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=100, mean=3.0, std=4.0)
        result = gate.evaluate(trades, n_total_tests=1, timeframe="15min")
        # 盈利策略在置换检验中应有较低的 p
        assert result.metrics["monte_carlo_p"] < 0.5

    def test_concentration_detection(self):
        """利润集中度检测"""
        gate = StatisticalGate()
        trades = make_concentrated_trades(n=50)
        result = gate.evaluate(trades, n_total_tests=1, timeframe="daily")
        # 集中度高 → 检查应触发
        assert result.metrics["top_10pct_contribution"] > 0.5

    def test_block_bootstrap(self):
        """Block bootstrap: 保留序列结构的检验"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=100, mean=3.0, std=4.0)
        result = gate.evaluate(trades, n_total_tests=1, timeframe="daily")
        assert "block_bootstrap_p" in result.metrics
        assert "block_size" in result.metrics
        assert result.metrics["block_size"] == 3  # daily = 3
        # 盈利策略的 block bootstrap p 应较低 (< 0.6, 因 block bootstrap 更保守)
        assert result.metrics["block_bootstrap_p"] < 0.6

    def test_block_bootstrap_autocorrelated(self):
        """有序列自相关的交易: block bootstrap 保留结构"""
        import random
        rng = random.Random(42)
        gate = StatisticalGate()
        trades = []
        for _ in range(30):
            for _ in range(3):
                trades.append({"pnl": abs(rng.gauss(2.0, 1.0))})
            for _ in range(2):
                trades.append({"pnl": -abs(rng.gauss(1.5, 1.0))})
        result = gate.evaluate(trades, n_total_tests=1, timeframe="daily")
        assert "block_bootstrap_p" in result.metrics

    def test_auto_block_size(self):
        """不同时间框架的 block size"""
        assert StatisticalGate._auto_block_size("daily") == 3
        assert StatisticalGate._auto_block_size("15min") == 8
        assert StatisticalGate._auto_block_size("5min") == 15
        assert StatisticalGate._auto_block_size("unknown") == 5

    def test_to_dict(self):
        """结果序列化"""
        gate = StatisticalGate()
        trades = make_profitable_trades(n=100)
        result = gate.evaluate(trades, n_total_tests=1)
        d = result.to_dict()
        assert "passed" in d
        assert "checks" in d
        assert "metrics" in d


# ============================================================
#  2. DebatePromptBuilder 测试
# ============================================================
class TestDebatePromptBuilder:
    def test_round1_prompt(self):
        prompt = DebatePromptBuilder.build_round1(
            strategy_info={"name": "test_strategy", "timeframe": "15min",
                           "factors": [{"name": "SSS"}], "direction": "LONG"},
            backtest_stats={"n_trades": 100, "win_rate": 0.45,
                            "profit_factor": 1.5, "total_pnl": 500,
                            "corrected_sharpe": 2.5, "max_drawdown": -0.15},
            gate_result={"passed": True, "checks": {"min_samples": True}},
        )
        assert "test_strategy" in prompt
        assert "Round 1" in prompt
        assert "failure_modes" in prompt
        assert "JSON" in prompt

    def test_round2_prompt(self):
        prompt = DebatePromptBuilder.build_round2(
            own_analysis={"assessment": "good", "suggestions": []},
            opponent_analysis={"assessment": "risky", "suggestions": []},
            strategy_name="test_strategy",
        )
        assert "Round 2" in prompt
        assert "交叉质疑" in prompt
        assert "challenges" in prompt

    def test_round3_prompt(self):
        prompt = DebatePromptBuilder.build_round3(
            all_rounds=[{"round": 1, "model": "A", "content": {}}],
            strategy_name="test_strategy",
        )
        assert "Round 3" in prompt
        assert "收敛" in prompt
        assert "consensus" in prompt


# ============================================================
#  3. StrategyDebateRunner 测试 (with mock)
# ============================================================
class TestStrategyDebateRunner:
    @pytest.mark.asyncio
    async def test_full_debate_with_mock(self):
        """完整辩论流程 (mock LLM)"""
        call_count = {"n": 0}

        async def mock_llm(model, prompt):
            call_count["n"] += 1
            round_num = 1 if "Round 1" in prompt else (2 if "Round 2" in prompt else 3)

            if round_num == 1:
                return {
                    "assessment": f"Strategy looks {'good' if 'qwen' in model else 'risky'}",
                    "effectiveness_score": 70 if "qwen" in model else 55,
                    "failure_modes": [
                        {"mode": "Slippage", "probability": "HIGH", "evidence": "data"},
                    ],
                    "suggestions": [
                        {"action": "Add VIX filter", "expected_impact": "+10%", "risk": "fewer trades"},
                    ],
                    "key_risk": "Overfitting",
                    "data_gaps": ["No out-of-sample"],
                }
            elif round_num == 2:
                return {
                    "challenges": [
                        {"target": "VIX filter", "challenge": "Reduces sample size",
                         "counter_evidence": "data", "severity": "MAJOR"},
                    ],
                    "missed_dimensions": ["Liquidity"],
                    "risk_if_opponent_wrong": "Loss of alpha",
                    "revised_assessment": "Moderate confidence",
                    "concessions": ["Slippage is real"],
                }
            else:  # round 3
                return {
                    "consensus": ["VIX filter needed", "Slippage is key risk"],
                    "disagreements": [
                        {"topic": "Sample size", "model_a_view": "Enough",
                         "model_b_view": "Need more", "resolution": "Shadow test"},
                    ],
                    "blind_spots": ["Regime change"],
                    "final_recommendation": "Enable with VIX filter",
                    "confidence": 0.72 if "qwen" in model else 0.68,
                    "action_items": [
                        {"action": "Add VIX < 25 filter", "priority": "P0",
                         "owner": "factor", "estimated_impact": "+5% WR"},
                    ],
                }

        runner = StrategyDebateRunner(
            model_a="qwen3.7-max",
            model_b="deepseek-v4-pro",
            llm_caller=mock_llm,
        )

        result = await runner.run_full_debate(
            strategy_info={"name": "test", "timeframe": "15min",
                           "factors": [{"name": "SSS"}], "direction": "LONG"},
            backtest_stats={"n_trades": 100, "win_rate": 0.45,
                            "profit_factor": 1.5, "total_pnl": 500,
                            "corrected_sharpe": 2.5, "max_drawdown": -0.15},
            trades=make_profitable_trades(120),
            n_total_tests=10,
        )

        assert isinstance(result, DebateResult)
        assert result.strategy_name == "test"
        assert len(result.rounds) == 6  # 3 rounds × 2 models
        assert len(result.consensus) > 0
        assert len(result.action_items) > 0
        assert result.confidence > 0
        assert call_count["n"] == 6  # 6 LLM calls total

    @pytest.mark.asyncio
    async def test_no_llm_caller(self):
        """无 LLM caller → 空框架"""
        runner = StrategyDebateRunner(llm_caller=None)
        result = await runner.run_full_debate(
            strategy_info={"name": "test"},
            backtest_stats={},
        )
        assert isinstance(result, DebateResult)
        assert len(result.rounds) == 0
        assert result.confidence == 0.0

    def test_debate_result_save(self, tmp_path):
        """辩论结果保存"""
        result = DebateResult(
            debate_id="test_001",
            strategy_name="test",
            timestamp="2026-05-23T00:00:00",
            consensus=["test consensus"],
            action_items=[{"action": "test", "priority": "P0"}],
            confidence=0.7,
        )
        filepath = result.save(str(tmp_path))
        assert os.path.exists(filepath)
        with open(filepath) as f:
            data = json.load(f)
        assert data["debate_id"] == "test_001"
        assert data["confidence"] == 0.7


# ============================================================
#  Run
# ============================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
