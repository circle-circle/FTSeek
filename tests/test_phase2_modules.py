"""Tests for Phase 2 modules: weight_adjuster, risk_circuit_breaker, weekly_report"""
import pytest
import sqlite3
import tempfile
import os
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.playbook.db import PlaybookDB
from app.playbook.weight_adjuster import WeightAdjuster
from app.playbook.risk_circuit_breaker import RiskCircuitBreaker
from app.playbook.weekly_report import WeeklyReportGenerator
from app.playbook.factor_analyzer import FactorAnalyzer


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def tmp_config():
    """创建临时 YAML 配置文件"""
    config = {
        "screening_factors": {
            "insider_cluster_buy": {"weight": 4, "description": "内部人集群买入"},
            "rsi_oversold": {"weight": 2, "condition": "rsi_14 < 40"},
            "above_ma20": {"weight": 2, "condition": "price > ma20"},
            "macd_golden_cross": {"weight": 1},
            "rsi_overbought": {"weight": -2, "condition": "rsi_14 > 70"},
            "insider_net_sell": {"weight": -2},
            "earnings_imminent": {"weight": -3},
        },
        "weight_adjustments": [],
    }
    tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode='w')
    yaml.dump(config, tmp, allow_unicode=True)
    tmp.close()
    yield tmp.name
    os.unlink(tmp.name)


def _save_result(db, symbol, conditions, outcome, pnl, date="2026-06-18"):
    inst_id = db.save_instruction({
        "date": date, "symbol": symbol, "direction": "BUY",
        "conditions": conditions, "entry_price": 100.0, "position_size": 50,
    })
    db.update_instruction_tracking(inst_id, {
        "executed": 1, "execution_price": 100.0, "outcome": outcome, "pnl": pnl,
    })
    return inst_id


# ========== WeightAdjuster 测试 ==========

class TestWeightAdjuster:
    def test_positive_factor_increases_weight(self, tmp_config):
        adjuster = WeightAdjuster(config_path=tmp_config)
        report = {"positive_singles": [
            {"factor_key": "rsi_oversold", "bayesian_win_rate": 0.72,
             "bayesian_lower": 0.58, "bayesian_upper": 0.84}
        ], "negative_singles": []}

        adjustments = adjuster.adjust(report)
        assert len(adjustments) == 1
        assert adjustments[0]["factor"] == "rsi_oversold"
        assert adjustments[0]["old_weight"] == 2
        assert adjustments[0]["new_weight"] == 3

    def test_negative_factor_decreases_weight(self, tmp_config):
        adjuster = WeightAdjuster(config_path=tmp_config)
        report = {"positive_singles": [], "negative_singles": [
            {"factor_key": "macd_golden_cross", "bayesian_win_rate": 0.32}
        ]}

        adjustments = adjuster.adjust(report)
        assert len(adjustments) == 1
        assert adjustments[0]["new_weight"] == 0

    def test_upper_bound(self, tmp_config):
        """权重不超过 5"""
        adjuster = WeightAdjuster(config_path=tmp_config)
        # insider_cluster_buy 已经是 4, 只能 +1 到 5
        report = {"positive_singles": [
            {"factor_key": "insider_cluster_buy", "bayesian_win_rate": 0.80,
             "bayesian_lower": 0.60, "bayesian_upper": 0.90}
        ], "negative_singles": []}

        adjustments = adjuster.adjust(report)
        assert adjustments[0]["new_weight"] == 5

        # 再调一次: 已经在 5, 不能再加
        adjustments2 = adjuster.adjust(report)
        assert len(adjustments2) == 0

    def test_lower_bound(self, tmp_config):
        """权重不低于 -5"""
        adjuster = WeightAdjuster(config_path=tmp_config)
        # earnings_imminent 已经是 -3, 调到 -4, -5
        for _ in range(3):
            report = {"positive_singles": [], "negative_singles": [
                {"factor_key": "earnings_imminent", "bayesian_win_rate": 0.30}
            ]}
            adjuster.adjust(report)

        weights = adjuster.get_current_weights()
        assert weights["earnings_imminent"] >= -5

    def test_unknown_factor_ignored(self, tmp_config):
        """不存在的因子被忽略"""
        adjuster = WeightAdjuster(config_path=tmp_config)
        report = {"positive_singles": [
            {"factor_key": "nonexistent_factor", "bayesian_win_rate": 0.80,
             "bayesian_lower": 0.60, "bayesian_upper": 0.90}
        ], "negative_singles": []}

        adjustments = adjuster.adjust(report)
        assert len(adjustments) == 0

    def test_adjustment_history_recorded(self, tmp_config):
        """调整历史被记录"""
        adjuster = WeightAdjuster(config_path=tmp_config)
        report = {"positive_singles": [
            {"factor_key": "above_ma20", "bayesian_win_rate": 0.65,
             "bayesian_lower": 0.58, "bayesian_upper": 0.72}
        ], "negative_singles": []}

        adjuster.adjust(report)
        history = adjuster.get_adjustment_history()
        assert len(history) == 1
        assert history[0]["factor"] == "above_ma20"

    def test_get_current_weights(self, tmp_config):
        adjuster = WeightAdjuster(config_path=tmp_config)
        weights = adjuster.get_current_weights()
        assert weights["insider_cluster_buy"] == 4
        assert weights["rsi_overbought"] == -2


# ========== RiskCircuitBreaker 测试 ==========

class TestRiskCircuitBreaker:
    def test_no_trades_all_ok(self, db):
        breaker = RiskCircuitBreaker(playbook_db=db)
        status = breaker.check_status(total_equity=100000)
        assert status["daily_loss_ok"] is True
        assert status["consecutive_ok"] is True
        assert status["trading_allowed"] is True
        assert status["position_multiplier"] == 1.0

    def test_daily_loss_within_limit(self, db):
        """日内亏损在限额内"""
        _save_result(db, "US.A", ["a"], "loss", -500)  # $500 亏损
        breaker = RiskCircuitBreaker(playbook_db=db, daily_loss_limit_pct=1.0)
        status = breaker.check_status(total_equity=100000)
        assert status["daily_loss_ok"] is True  # $500 < $1000 limit
        assert status["daily_loss"] == -500

    def test_daily_loss_exceeds_limit(self, db):
        """日内亏损超限 → 禁止交易"""
        _save_result(db, "US.A", ["a"], "loss", -1500)
        breaker = RiskCircuitBreaker(playbook_db=db, daily_loss_limit_pct=1.0)
        status = breaker.check_status(total_equity=100000)
        assert status["daily_loss_ok"] is False
        assert status["trading_allowed"] is False
        assert "日内亏损" in status["stop_reason"]

    def test_consecutive_3_losses_half_position(self, db):
        """连续 3 笔亏损 → 仓位减半"""
        from datetime import datetime, timedelta
        base = datetime(2026, 6, 18, 10, 0)
        for i in range(3):
            inst_id = _save_result(db, f"US.L{i}", ["a"], "loss", -100,
                                   date=f"2026-06-{18-i:02d}")
            db.update_instruction_tracking(inst_id, {
                "execution_time": (base - timedelta(hours=i)).isoformat()
            })

        breaker = RiskCircuitBreaker(playbook_db=db, consecutive_loss_reduce=3)
        status = breaker.check_status(total_equity=100000)
        assert status["consecutive_losses"] == 3
        assert status["position_multiplier"] == 0.5

    def test_consecutive_5_losses_stop_trading(self, db):
        """连续 5 笔亏损 → 停止交易"""
        from datetime import datetime, timedelta
        base = datetime(2026, 6, 18, 10, 0)
        for i in range(5):
            inst_id = _save_result(db, f"US.L{i}", ["a"], "loss", -100,
                                   date=f"2026-06-{18-i:02d}")
            db.update_instruction_tracking(inst_id, {
                "execution_time": (base - timedelta(hours=i)).isoformat()
            })

        breaker = RiskCircuitBreaker(playbook_db=db, consecutive_loss_stop=5)
        status = breaker.check_status(total_equity=100000)
        assert status["consecutive_losses"] == 5
        assert status["trading_allowed"] is False
        assert status["position_multiplier"] == 0.0

    def test_win_resets_consecutive(self, db):
        """一笔盈利重置连续亏损计数"""
        from datetime import datetime, timedelta
        base = datetime(2026, 6, 18, 10, 0)

        # 2 losses, then 1 win, then 1 loss
        _save_result(db, "US.L1", ["a"], "loss", -100, date="2026-06-16")
        db.update_instruction_tracking(1, {"execution_time": base.isoformat()})

        _save_result(db, "US.W1", ["a"], "win", 200, date="2026-06-17")
        db.update_instruction_tracking(2, {"execution_time": (base + timedelta(hours=1)).isoformat()})

        _save_result(db, "US.L2", ["a"], "loss", -100, date="2026-06-18")
        db.update_instruction_tracking(3, {"execution_time": (base + timedelta(hours=2)).isoformat()})

        breaker = RiskCircuitBreaker(playbook_db=db)
        status = breaker.check_status(total_equity=100000)
        # 最近一笔是 loss, 往前数是 1 (win 打断了连续)
        assert status["consecutive_losses"] == 1

    def test_reset_clears_stop(self, db):
        """reset() 清除停止期"""
        _save_result(db, "US.A", ["a"], "loss", -1500)
        breaker = RiskCircuitBreaker(playbook_db=db, daily_loss_limit_pct=1.0)
        breaker.check_status(total_equity=100000)  # triggers stop

        breaker.reset()
        assert breaker._stop_until is None

    def test_alerts_populated(self, db):
        """告警信息被填充"""
        _save_result(db, "US.A", ["a"], "loss", -1500)
        breaker = RiskCircuitBreaker(playbook_db=db, daily_loss_limit_pct=1.0)
        status = breaker.check_status(total_equity=100000)
        assert len(status["alerts"]) > 0


# ========== WeeklyReportGenerator 测试 ==========

class TestWeeklyReportGenerator:
    def test_generate_report(self, db):
        """生成报告不崩溃"""
        analyzer = FactorAnalyzer(playbook_db=db)
        gen = WeeklyReportGenerator(playbook_db=db, factor_analyzer=analyzer)
        report = gen.generate("2026-06-18")

        assert "date" in report
        assert "week_stats" in report
        assert "factor_report" in report
        assert "adjustments" in report

    def test_format_report_basic(self, db):
        """格式化报告基本正确"""
        analyzer = FactorAnalyzer(playbook_db=db)
        gen = WeeklyReportGenerator(playbook_db=db, factor_analyzer=analyzer)
        report = gen.generate("2026-06-18")

        text = gen.format_report(report)
        assert "2026-06-18" in text
        assert "周度因子复盘" in text

    def test_format_report_with_positive_factors(self, db):
        """有正收益因子时显示"""
        for i in range(5):
            _save_result(db, f"US.P{i}", ["insider_cluster_buy"], "win", 200,
                         date=f"2026-06-{14+i:02d}")

        analyzer = FactorAnalyzer(playbook_db=db)
        analyzer.update_all_stats()

        gen = WeeklyReportGenerator(playbook_db=db, factor_analyzer=analyzer)
        report = gen.generate("2026-06-18")
        text = gen.format_report(report)

        assert "insider_cluster_buy" in text

    def test_week_stats_calculation(self, db):
        """本周统计正确"""
        _save_result(db, "US.A", ["a"], "win", 100, date="2026-06-16")
        _save_result(db, "US.B", ["a"], "win", 200, date="2026-06-17")
        _save_result(db, "US.C", ["a"], "loss", -50, date="2026-06-18")

        gen = WeeklyReportGenerator(playbook_db=db)
        stats = gen._get_week_stats("2026-06-18")

        assert stats["total"] == 3
        assert stats["wins"] == 2
        assert stats["net_pnl"] == 250

    def test_skip_analysis(self, db):
        """用户跳过分析"""
        db.save_instruction({
            "date": "2026-06-18",
            "symbol": "US.META",
            "direction": "SKIP",
            "skip_reason": "财报",
        })

        gen = WeeklyReportGenerator(playbook_db=db)
        analysis = gen._analyze_user_skips("2026-06-18")
        assert analysis["total_skips"] >= 0
