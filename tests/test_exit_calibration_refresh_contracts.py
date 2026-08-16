"""scripts/exit_calibration_refresh.py 契约测试 (DEB-20260816-long-expectancy L-E3 前置)

锁定三件口径 (本案两次翻车都在单位/量纲上):
1. mfe_pct/mae_pct 列是百分数 (4.854=4.85%), 加载必须归一为分数再与 2%/2.0x 阈值比较
2. 分类语义沿用 2026-06-06 旧版: noise_shakeout / profit_giveback / well_calibrated
3. 输出 schema 与历史 config/exit_calibration.json 可比 (avg_* 为分数)
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.exit_calibration_refresh import (  # noqa: E402
    _bucket_conf, _classify, _rule_table, build_report,
)


def _t(**kw):
    base = {
        "trade_date": "2026-08-01", "symbol": "US.TEST", "direction": "LONG",
        "source_type": "PLAYBOOK_SIM", "exit_reason": "TIME_EXIT",
        "realized_pnl": -100.0, "pnl_net": -105.0, "fees_est": 5.0,
        "pnl_pct": -0.01, "mfe_pct": 0.005, "mae_pct": 0.004,
        "signal_confidence": 0.5, "holding_hours": 3.0,
    }
    base.update(kw)
    return base


class TestUnitNormalization:
    def test_percent_columns_normalized_to_fraction(self, tmp_path):
        """归一契约: 4.854 (百分数) → 0.04854 (分数), 否则 2% 阈值分类全歪"""
        import sqlite3
        from scripts.exit_calibration_refresh import _load_trades
        db = str(tmp_path / "t.db")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE trade_records (trade_date TEXT, symbol TEXT,
            direction TEXT, source_type TEXT, exit_reason TEXT, realized_pnl REAL,
            pnl_net REAL, fees_est REAL, pnl_pct REAL, mfe_pct REAL, mae_pct REAL,
            signal_confidence REAL, holding_hours REAL, exit_price REAL)""")
        conn.execute("INSERT INTO trade_records VALUES ('2026-08-15','US.A','LONG',"
                     "'PLAYBOOK_SIM','TIME_EXIT',-100,-105,5,-0.01,4.854,0.5,0.9,3,101.0)")
        conn.commit()
        rows = _load_trades(db, days=999, source_filter=True)
        assert rows and rows[0]["mfe_pct"] == 0.04854
        assert rows[0]["mae_pct"] == 0.005

    def test_rule_table_reports_percent_units(self):
        out = _rule_table([_t(mfe_pct=0.014854, mae_pct=0.005)])
        assert out[("TIME_EXIT", "LONG")]["avg_mfe_pct"] == 1.485


class TestClassification:
    def test_noise_shakeout(self):
        """MAE>2% 且最终盈利 → 止损太紧"""
        t = _t(realized_pnl=200, mae_pct=0.025, mfe_pct=0.01)
        assert _classify(t) == "noise_shakeout"

    def test_profit_giveback(self):
        """MFE > 2× 捕获利润 (盈利单) → 追踪太松"""
        t = _t(realized_pnl=100, pnl_pct=0.01, mfe_pct=0.03, mae_pct=0.002)
        assert _classify(t) == "profit_giveback"

    def test_well_calibrated(self):
        t = _t(realized_pnl=100, pnl_pct=0.02, mfe_pct=0.025, mae_pct=0.004)
        assert _classify(t) == "well_calibrated"

    def test_loss_with_small_mae_not_noise(self):
        """亏损单无论 MAE 多大都不算 noise_shakeout (定义要求最终盈利)"""
        t = _t(realized_pnl=-300, mae_pct=0.04, mfe_pct=0.001)
        assert _classify(t) == "well_calibrated"


class TestReportSchema:
    def test_schema_compatible_with_202606_snapshot(self):
        r = build_report([_t(), _t(realized_pnl=150, pnl_pct=0.012, mfe_pct=0.03),
                          _t(symbol="US.B", direction="SHORT", exit_reason="SL_TRAIL_LOCK",
                             realized_pnl=-50)], days=77)
        for key in ("generated_at", "period_days", "total_trades", "analyzed_trades",
                    "noise_shakeout_count", "profit_giveback_count", "well_calibrated_count",
                    "noise_shakeout_pct", "avg_mae_pct", "avg_mfe_pct", "avg_pnl_pct",
                    "win_rate", "recommendations"):
            assert key in r, key
        assert r["total_trades"] == 3
        assert 0.0 <= r["avg_mae_pct"] < 0.05  # 分数量纲
        assert "exit_reason_review" in r["recommendations"]
        assert "confidence_tier_stats" in r["recommendations"]

    def test_negative_rule_flagged_for_review(self):
        """净期望为负且 n≥3 的规则必须进 review 名单 (L-E3 供给)"""
        trades = [_t() for _ in range(4)]  # TIME_EXIT 全亏
        r = build_report(trades, days=30)
        bad = r["recommendations"]["exit_reason_review"]["bad_reasons"]
        assert any(b["reason"] == "TIME_EXIT" and b["n"] == 4 for b in bad)

    def test_conf_buckets(self):
        assert _bucket_conf(0.35) == "low"
        assert _bucket_conf(0.5) == "mid"
        assert _bucket_conf(0.9) == "high"
        assert _bucket_conf(0) == "unknown"
