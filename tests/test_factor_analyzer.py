"""Tests for app.playbook.factor_analyzer — 双层因子分析 + 贝叶斯"""
import pytest
import sqlite3
import math

from app.playbook.db import PlaybookDB
from app.playbook.factor_analyzer import (
    bayesian_binomial,
    classify_factor,
    compute_trade_stats,
    hash_conditions,
    FactorAnalyzer,
    BayesianEstimate,
    TradeStats,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def analyzer(db):
    return FactorAnalyzer(playbook_db=db)


def _save_result(db, symbol, conditions, outcome, pnl, date="2026-06-18"):
    """保存一条有结果的指令"""
    inst_id = db.save_instruction({
        "date": date,
        "symbol": symbol,
        "direction": "BUY",
        "conditions": conditions,
        "entry_price": 100.0,
        "position_size": 50,
    })
    db.update_instruction_tracking(inst_id, {
        "executed": 1,
        "execution_price": 100.0,
        "outcome": outcome,
        "pnl": pnl,
    })
    return inst_id


# ========== bayesian_binomial 测试 ==========

class TestBayesianBinomial:
    def test_uniform_prior_no_data(self):
        """无数据时返回先验均值 0.5"""
        est = bayesian_binomial(0, 0)
        assert est.mean == 0.5
        assert est.lower_95 == 0.0
        assert est.upper_95 == 1.0

    def test_small_sample_shrinkage(self):
        """小样本: 后验均值被先验收缩 (shrinkage)"""
        # 20笔16胜: naive=80%, posterior=(1+16)/(2+20)=77.3%
        est = bayesian_binomial(16, 20)
        assert 0.75 < est.mean < 0.80
        # 区间应该较宽
        assert est.upper_95 - est.lower_95 > 0.2

    def test_large_sample_precision(self):
        """大样本: 后验均值接近 naive, 区间窄"""
        # 100笔72胜: naive=72%, posterior=(1+72)/(2+100)=71.6%
        est = bayesian_binomial(72, 100)
        assert 0.70 < est.mean < 0.73
        # 区间应该较窄
        assert est.upper_95 - est.lower_95 < 0.2

    def test_all_wins(self):
        """全胜: 均值高, 下界合理"""
        est = bayesian_binomial(10, 10)
        assert est.mean > 0.8
        assert est.lower_95 > 0.5

    def test_all_losses(self):
        """全败: 均值低, 上界合理"""
        est = bayesian_binomial(0, 10)
        assert est.mean < 0.2
        assert est.upper_95 < 0.5

    def test_half_half(self):
        """50/50: 均值接近 0.5"""
        est = bayesian_binomial(50, 100)
        assert 0.48 < est.mean < 0.52

    def test_confidence_interval_contains_mean(self):
        """CI 包含均值"""
        for wins, total in [(3, 10), (7, 10), (50, 100), (1, 5)]:
            est = bayesian_binomial(wins, total)
            assert est.lower_95 <= est.mean <= est.upper_95


# ========== classify_factor 测试 ==========

class TestClassifyFactor:
    def test_positive_factor(self):
        """高胜率 + 高 profit_factor → positive"""
        bayesian = BayesianEstimate(mean=0.72, lower_95=0.58, upper_95=0.84)
        stats = TradeStats(total=20, wins=15, losses=5, gross_profit=1500, gross_loss=500)
        assert classify_factor(bayesian, stats) == "positive"

    def test_negative_factor_low_wr(self):
        """低胜率 → negative"""
        bayesian = BayesianEstimate(mean=0.32, lower_95=0.15, upper_95=0.42)
        stats = TradeStats(total=20, wins=6, losses=14, gross_profit=400, gross_loss=1200)
        assert classify_factor(bayesian, stats) == "negative"

    def test_negative_factor_low_pf(self):
        """低 profit_factor → negative"""
        bayesian = BayesianEstimate(mean=0.48, lower_95=0.30, upper_95=0.60)
        stats = TradeStats(total=20, wins=10, losses=10, gross_profit=200, gross_loss=800)
        assert classify_factor(bayesian, stats) == "negative"

    def test_observing_factor(self):
        """不确定区间 → observing"""
        bayesian = BayesianEstimate(mean=0.55, lower_95=0.35, upper_95=0.70)
        stats = TradeStats(total=8, wins=5, losses=3, gross_profit=500, gross_loss=300)
        assert classify_factor(bayesian, stats) == "observing"


# ========== compute_trade_stats 测试 ==========

class TestComputeTradeStats:
    def test_basic_stats(self):
        trades = [
            {"pnl": 100, "outcome": "win"},
            {"pnl": 200, "outcome": "win"},
            {"pnl": -50, "outcome": "loss"},
        ]
        stats = compute_trade_stats(trades)
        assert stats.total == 3
        assert stats.wins == 2
        assert stats.losses == 1
        assert stats.total_pnl == 250
        assert stats.gross_profit == 300
        assert stats.gross_loss == 50
        assert stats.max_win == 200
        assert stats.max_loss == -50

    def test_empty_trades(self):
        stats = compute_trade_stats([])
        assert stats.total == 0
        assert stats.win_rate == 0
        assert stats.profit_factor == 0

    def test_all_wins(self):
        trades = [{"pnl": 100, "outcome": "win"}, {"pnl": 200, "outcome": "win"}]
        stats = compute_trade_stats(trades)
        assert stats.profit_factor == float('inf')

    def test_profit_factor_calculation(self):
        trades = [
            {"pnl": 300, "outcome": "win"},
            {"pnl": -100, "outcome": "loss"},
        ]
        stats = compute_trade_stats(trades)
        assert stats.profit_factor == pytest.approx(3.0)

    def test_none_pnl_handled(self):
        trades = [{"pnl": None, "outcome": "win"}, {"pnl": 100, "outcome": "win"}]
        stats = compute_trade_stats(trades)
        assert stats.total == 2
        assert stats.total_pnl == 100


# ========== hash_conditions 测试 ==========

class TestHashConditions:
    def test_deterministic(self):
        h1 = hash_conditions(["a", "b", "c"])
        h2 = hash_conditions(["a", "b", "c"])
        assert h1 == h2

    def test_order_independent(self):
        h1 = hash_conditions(["a", "b", "c"])
        h2 = hash_conditions(["c", "a", "b"])
        assert h1 == h2

    def test_different_conditions_different_hash(self):
        h1 = hash_conditions(["a", "b"])
        h2 = hash_conditions(["a", "c"])
        assert h1 != h2

    def test_hash_length(self):
        h = hash_conditions(["a", "b"])
        assert len(h) == 12


# ========== FactorAnalyzer 测试 ==========

class TestFactorAnalyzerUpdate:
    def test_update_single_factors(self, analyzer, db):
        """单因子分析正确"""
        # 5 个指令都有 above_ma20, 4 赢 1 亏
        for i, (outcome, pnl) in enumerate([
            ("win", 100), ("win", 150), ("win", 200), ("win", 80), ("loss", -50)
        ]):
            _save_result(db, f"US.S{i}", ["above_ma20"], outcome, pnl,
                         date=f"2026-06-{14+i:02d}")

        result = analyzer.update_all_stats(days_back=90)
        assert result["single"] >= 1

        stats = db.get_factor_stats(level="single")
        above_ma20 = [s for s in stats if s["factor_key"] == "above_ma20"]
        assert len(above_ma20) == 1
        assert above_ma20[0]["total_trades"] == 5
        assert above_ma20[0]["wins"] == 4

    def test_update_pair_factors(self, analyzer, db):
        """条件对分析正确"""
        # 3 个指令都有 [above_ma20, insider_cluster_buy]
        for i, (outcome, pnl) in enumerate([
            ("win", 100), ("win", 200), ("loss", -80)
        ]):
            _save_result(db, f"US.P{i}",
                         ["above_ma20", "insider_cluster_buy"],
                         outcome, pnl,
                         date=f"2026-06-{14+i:02d}")

        result = analyzer.update_all_stats()
        assert result["pair"] >= 1

        stats = db.get_factor_stats(level="pair")
        pair_key = "above_ma20|insider_cluster_buy"
        pairs = [s for s in stats if s["factor_key"] == pair_key]
        assert len(pairs) == 1
        assert pairs[0]["total_trades"] == 3

    def test_pair_factors_min_3_samples(self, analyzer, db):
        """条件对少于 3 个样本 → 跳过"""
        _save_result(db, "US.X1", ["a", "b"], "win", 100)
        _save_result(db, "US.X2", ["a", "b"], "win", 200)
        # 只有 2 个, 不够 3

        result = analyzer.update_all_stats()
        stats = db.get_factor_stats(level="pair")
        assert len(stats) == 0

    def test_exact_combos_min_5_samples(self, analyzer, db):
        """精确组合少于 5 个 → 跳过"""
        for i in range(4):
            _save_result(db, f"US.C{i}", ["a", "b", "c"], "win", 100,
                         date=f"2026-06-{14+i:02d}")
        # 只有 4 个

        result = analyzer.update_all_stats()
        assert result["combo"] == 0

    def test_exact_combos_5_plus(self, analyzer, db):
        """精确组合 ≥5 个 → 创建 pattern"""
        for i in range(6):
            _save_result(db, f"US.C{i}", ["a", "b", "c"], "win", 100,
                         date=f"2026-06-{12+i:02d}")

        result = analyzer.update_all_stats()
        assert result["combo"] >= 1

    def test_no_results_returns_zero(self, analyzer):
        """无结果时返回 0"""
        result = analyzer.update_all_stats()
        assert result == {"single": 0, "pair": 0, "combo": 0}


class TestFactorReport:
    def test_generate_report_structure(self, analyzer, db):
        """报告结构正确"""
        _save_result(db, "US.R1", ["above_ma20"], "win", 100)
        analyzer.update_all_stats()

        report = analyzer.generate_factor_report()
        assert "positive_singles" in report
        assert "negative_singles" in report
        assert "positive_pairs" in report
        assert "negative_pairs" in report
        assert "summary" in report

    def test_report_sorting(self, analyzer, db):
        """报告按 total_trades 降序排列"""
        for i in range(10):
            _save_result(db, f"US.S{i}", ["high_freq"], "win", 100,
                         date=f"2026-06-{8+i:02d}")
        for i in range(3):
            _save_result(db, f"US.L{i}", ["low_freq"], "win", 100,
                         date=f"2026-06-{8+i:02d}")

        analyzer.update_all_stats()
        report = analyzer.generate_factor_report()

        all_singles = (report["positive_singles"] +
                       report["negative_singles"] +
                       report["observing_singles"])
        if len(all_singles) >= 2:
            assert all_singles[0]["total_trades"] >= all_singles[1]["total_trades"]

    # 2026-09-21: test_get_patterns_for_prompt 随方法退役同批删除 (DEB-20260921 局限9, 用户裁「请修复」)
