"""
ExitMonitor 止损止盈逻辑单元测试

P0/P1/P2 修复 (2026-04-27):
  - Scale Out 分批止盈
  - 盘末强制退出 (Pre-Close Exit)
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.risk.position_exit_monitor import ExitMonitor, get_exit_monitor


# ============================================================
#  Fixtures
# ============================================================

class FakePosition:
    """最小 Position 桩，提供 ExitMonitor 所需的所有属性"""
    def __init__(self, symbol, direction, entry_price, size,
                 take_profit_levels=None, trail_state=None,
                 entry_time=None, metadata=None):
        self.symbol = symbol
        self.direction = MagicMock(value=direction)
        self.avg_entry_price = entry_price
        self.size = float(size)
        self.take_profit_levels = take_profit_levels or []
        self.trail_state = trail_state or {}
        self.entry_time = entry_time or datetime.now()
        self.metadata = metadata or {}
        self.current_sl = 0.0
        self.initial_sl = 0.0
        self.signal_id = f"sig_{symbol}"
        self.strategy_id = "test"
        self.trace_id = "trace_test"


@pytest.fixture
def monitor():
    return ExitMonitor()


@pytest.fixture
def long_position():
    """做多持仓，入场价 $100"""
    return FakePosition("US.NVDA", "LONG", 100.0, 100)


@pytest.fixture
def short_position():
    """做空持仓，入场价 $100"""
    return FakePosition("US.NVDA", "SHORT", 100.0, 100)


# ============================================================
#  P0: Scale Out 分批止盈
# ============================================================
class TestScaleOut:
    """分批止盈逻辑测试 (Task #62 语义: yaml pct tiers, take_profit_levels 路径已退役).

    2026-07: exit_policy.yaml 已停用 scale_out (enabled:false, tiers:[]),
    此处注入 pct 模式配置密封验证引擎逻辑 (触发/去重/顺序/兜底).
    """

    # 注入配置: 等价旧 take_profit_levels 108/115/125 (entry=100)
    PCT_CFG = {
        "tier_mode": "pct",
        "trailing_remainder": True,
        "tiers": [
            {"trigger_pct": 0.08, "close_pct": 0.30, "new_stop_pct": 0.0},
            {"trigger_pct": 0.15, "close_pct": 0.30, "new_stop_pct": 0.05},
            {"trigger_pct": 0.25, "close_pct": 0.40, "new_stop_pct": 0.10},
        ],
    }

    def _cfg(self, cfg=None):
        return patch(
            "app.risk.exit_rules.scale_out._load_scale_out_config",
            return_value=cfg if cfg is not None else self.PCT_CFG,
        )

    def test_yaml_empty_tiers_falls_back_to_defaults(self, monitor, long_position):
        """yaml tiers 为空 → 回退 DEFAULT_TIERS (1.5%/3%/5%, close 25%)"""
        with self._cfg({"tier_mode": "pct", "tiers": []}):
            result = monitor._check_scale_out(long_position, 110.0, {}, "LONG")
        assert result is not None
        assert result["close_ratio"] == 0.25
        assert result["metadata"]["tier"] == 1

    def test_long_hits_tier1(self, monitor, long_position):
        """做多触及 Tier 1 (+8%) → 返回部分平仓指令"""
        with self._cfg():
            result = monitor._check_scale_out(long_position, 110.0, {}, "LONG")
        assert result is not None
        assert result["reason"] == "SCALE_OUT"
        assert result["close_qty"] == 30  # 30% of 100 shares
        assert result["exit_price"] == 110.0

    def test_long_below_tier1_no_trigger(self, monitor, long_position):
        """做多涨幅未到 Tier 1 (8%) → 不触发"""
        with self._cfg():
            result = monitor._check_scale_out(long_position, 105.0, {}, "LONG")
        assert result is None

    def test_short_hits_tier1(self, monitor, short_position):
        """做空盈利 (价格下跌 10% ≥ 8%) → 返回部分平仓指令"""
        with self._cfg():
            result = monitor._check_scale_out(short_position, 90.0, {}, "SHORT")
        assert result is not None
        assert result["reason"] == "SCALE_OUT"
        assert result["close_qty"] == 30

    def test_short_above_tier1_no_trigger(self, monitor, short_position):
        """做空跌幅未到 Tier 1 → 不触发"""
        with self._cfg():
            result = monitor._check_scale_out(short_position, 95.0, {}, "SHORT")
        assert result is None

    def test_tier_already_triggered_skips(self, monitor, long_position):
        """已触发的 Tier 不会重复触发"""
        trail_state = {"triggered_tiers": [0]}  # Tier 1 已触发

        # 价格同时超过 Tier 1 和 Tier 2 → 应触发 Tier 2 而不是 Tier 1
        with self._cfg():
            result = monitor._check_scale_out(long_position, 120.0, trail_state, "LONG")
        assert result is not None
        assert result["close_qty"] == 30  # Tier 2's 30%
        assert result["metadata"]["tier"] == 2
        assert trail_state["triggered_tiers"] == [0, 1]

    def test_sequential_tiers(self, monitor, long_position):
        """顺序触发所有三层"""
        trail_state = {}

        with self._cfg():
            # Tier 1 (+10% ≥ 8%)
            r1 = monitor._check_scale_out(long_position, 110.0, trail_state, "LONG")
            assert r1["close_qty"] == 30
            assert trail_state["triggered_tiers"] == [0]

            # Tier 2 (+20% ≥ 15%, 30% of **original** 100)
            r2 = monitor._check_scale_out(long_position, 120.0, trail_state, "LONG")
            assert r2["close_qty"] == 30
            assert trail_state["triggered_tiers"] == [0, 1]

            # Tier 3 (+30% ≥ 25%, 40% of **original** 100)
            r3 = monitor._check_scale_out(long_position, 130.0, trail_state, "LONG")
            assert r3["close_qty"] == 40
            assert trail_state["triggered_tiers"] == [0, 1, 2]
            assert r3["trailing_remainder"] is True  # 最后一档 → 余仓交 trailing

            # All done
            r4 = monitor._check_scale_out(long_position, 200.0, trail_state, "LONG")
            assert r4 is None

    def test_close_qty_minimum_1(self, monitor, long_position):
        """最小平仓量为 1 股"""
        long_position.size = 5.0
        with self._cfg():
            result = monitor._check_scale_out(long_position, 110.0, {}, "LONG")
        assert result["close_qty"] >= 1

    def test_invalid_tiers_filtered(self, monitor, long_position):
        """trigger_pct<=0 的脏 tier 被过滤 (回退默认或跳过)"""
        cfg = {
            "tier_mode": "pct",
            "tiers": [
                {"trigger_pct": 0, "close_pct": 0.30},       # 无效 → 过滤
                {"trigger_pct": 0.15, "close_pct": 0.30},    # 有效
            ],
        }
        with self._cfg(cfg):
            result = monitor._check_scale_out(long_position, 120.0, {}, "LONG")
        assert result is not None
        assert result["close_qty"] == 30
        assert result["metadata"]["total_tiers"] == 1  # 无效层被过滤


# ============================================================
#  P2: Pre-Close Exit 盘末强制退出
# ============================================================

class TestPreCloseExit:
    """盘末强制退出逻辑测试 (实现已迁移至 exit_rules/pre_close.py, patch 目标同步)"""

    def test_weekend_no_exit(self, monitor, long_position):
        """周末不触发盘末退出"""
        from pytz import timezone
        ET = timezone("US/Eastern")
        with patch("app.risk.exit_rules.pre_close.datetime") as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 4, 25, 15, 50, 0))  # Saturday
            result = monitor._check_pre_close_exit(long_position, 101.0, 2.0, {}, "LONG")
            assert result is None

    def test_within_window_low_profit_half_exit(self, monitor, long_position):
        """窗口内 (距收盘<15min) 且微利 (<0.5ATR) → 盘末减仓 50% (2026-06 分层语义)"""
        from pytz import timezone
        ET = timezone("US/Eastern")
        long_position.entry_time = datetime(2026, 4, 24, 10, 0, 0)  # 持仓已超最小持有时间

        with patch("app.risk.exit_rules.pre_close.datetime") as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 4, 24, 15, 50, 0))  # Friday 15:50 ET
            # 浮盈很小 (profit_atr = 0.5/2.5 = 0.2 < half_exit 0.5)
            result = monitor._check_pre_close_exit(long_position, 100.5, 2.5, {}, "LONG")

        assert result is not None
        assert result["reason"] == "PRE_CLOSE_EXIT_PARTIAL"
        assert result["close_ratio"] == 0.5
        assert "距收盘" in result["reason_cn"]

    def test_within_window_good_profit_holds(self, monitor, long_position):
        """窗口内但浮盈处于分层之上 (≥1 ATR) → 允许持仓过夜"""
        from pytz import timezone
        ET = timezone("US/Eastern")
        long_position.entry_time = datetime(2026, 4, 24, 10, 0, 0)

        with patch("app.risk.exit_rules.pre_close.datetime") as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 4, 24, 15, 50, 0))
            # 浮盈充足: ATR=2.0, entry=100, price=104 → profit_atr = 2.0
            result = monitor._check_pre_close_exit(long_position, 104.0, 2.0, {}, "LONG")
        assert result is None

    def test_before_window_no_trigger(self, monitor, long_position):
        """距收盘 > 窗口 → 不触发"""
        from pytz import timezone
        ET = timezone("US/Eastern")
        long_position.entry_time = datetime(2026, 4, 24, 10, 0, 0)

        with patch("app.risk.exit_rules.pre_close.datetime") as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 4, 24, 14, 0, 0))  # 2 PM
            result = monitor._check_pre_close_exit(long_position, 100.5, 2.0, {}, "LONG")
        assert result is None

    def test_after_close_no_trigger(self, monitor, long_position):
        """收盘后 → 不触发"""
        from pytz import timezone
        ET = timezone("US/Eastern")
        long_position.entry_time = datetime(2026, 4, 24, 10, 0, 0)

        with patch("app.risk.exit_rules.pre_close.datetime") as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 4, 24, 16, 30, 0))
            result = monitor._check_pre_close_exit(long_position, 100.5, 2.0, {}, "LONG")
        assert result is None

    def test_no_atr_fallback(self, monitor, long_position):
        """ATR=0 → 退化为不触发"""
        from pytz import timezone
        ET = timezone("US/Eastern")
        long_position.entry_time = datetime(2026, 4, 24, 10, 0, 0)

        with patch("app.risk.exit_rules.pre_close.datetime") as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 4, 24, 15, 50, 0))
            result = monitor._check_pre_close_exit(long_position, 101.0, 0.0, {}, "LONG")
        assert result is None


# ============================================================
#  P1: Scorer Stop-Loss 接入校验
# ============================================================

class TestScorerStopLossWiring:
    """IntradayScorer 止损接入位置校验"""

    def test_stop_loss_pct_in_vote_result(self):
        """验证 IntradaySignal.to_vote_result() 包含 stop_loss_pct.

        P0 (2026-06-04): 止损倍数对齐 exit_policy — stop = max(4.0×ATR/price, 2%),
        原 [0.01, 0.10] 区间断言随之更新为精确值校验.
        """
        from app.core.intraday_scorer import get_intraday_scorer

        ctx = {
            "symbol": "US.NVDA",
            "price": 450.0,
            "change_percent": 1.5,
            "rsi": 55.0, "rsi_prev": 52.0,
            "macd": 0.85, "macd_signal": 0.60,
            "ma20": 440.0, "ma50": 420.0, "ma200": 380.0,
            "atr": 12.5,
            "volume_ratio": 1.2, "volume": 50000000,
            "turnover": 2.25e8, "amplitude": 2.5,
            "lv3_bid": 449.80, "lv3_ask": 450.20,
            "lv3_bid_vol": 15000, "lv3_ask_vol": 12000,
            "order_flow_imbalance": 0.11,
            "vix": 20.0, "spy_price": 520.0, "spy_ma50": 510.0,
        }
        scorer = get_intraday_scorer()
        signal = scorer.score(ctx)
        vr = signal.to_vote_result()

        sl_pct = vr.get("position_suggestion", {}).get("stop_loss_pct", 0)
        assert sl_pct > 0, f"Expected positive stop_loss_pct, got {sl_pct}"
        # stop = round(max(4.0 × 12.5/450, 0.02), 3) = 0.111 (VIX 20 < 35 无恐慌放宽)
        expected = round(max(4.0 * 12.5 / 450.0, 0.02), 3)
        assert sl_pct == pytest.approx(expected, abs=1e-9), (
            f"stop_loss_pct={sl_pct:.3f} != 4×ATR 对齐值 {expected:.3f}"
        )

    @patch("app.risk.position_exit_monitor.datetime")
    def test_high_vix_wider_stop_in_vote_result(self, mock_dt):
        """高 VIX → stop_loss_pct 更大"""
        from app.core.intraday_scorer import get_intraday_scorer

        base_ctx = {
            "symbol": "US.NVDA", "price": 450.0,
            "change_percent": 1.5,
            "rsi": 55.0, "rsi_prev": 52.0,
            "macd": 0.85, "macd_signal": 0.60,
            "ma20": 440.0, "ma50": 420.0, "ma200": 380.0,
            "atr": 12.5, "volume_ratio": 1.2,
            "volume": 50000000, "turnover": 2.25e8, "amplitude": 2.5,
            "lv3_bid": 449.80, "lv3_ask": 450.20,
            "lv3_bid_vol": 15000, "lv3_ask_vol": 12000,
            "order_flow_imbalance": 0.11,
            "spy_price": 520.0, "spy_ma50": 510.0,
        }
        scorer = get_intraday_scorer()

        normal = scorer.score({**base_ctx, "vix": 18.0})
        panic = scorer.score({**base_ctx, "vix": 32.0})

        normal_sl = normal.to_vote_result()["position_suggestion"]["stop_loss_pct"]
        panic_sl = panic.to_vote_result()["position_suggestion"]["stop_loss_pct"]

        if normal.direction != "NEUTRAL" and panic.direction != "NEUTRAL":
            assert panic_sl >= normal_sl, \
                f"高VIX止损应该更宽: VIX=18→{normal_sl:.3f} vs VIX=32→{panic_sl:.3f}"




# ============================================================
#  ExitReason 枚举完整性
# ============================================================

class TestExitReasonEnum:
    """ExitReason 包含新增的 SCALE_OUT 和 PRE_CLOSE_EXIT"""

    def test_scale_out_exists(self):
        from app.core.models import ExitReason
        assert hasattr(ExitReason, 'SCALE_OUT')
        assert ExitReason.SCALE_OUT == "SCALE_OUT"

    def test_pre_close_exit_exists(self):
        from app.core.models import ExitReason
        assert hasattr(ExitReason, 'PRE_CLOSE_EXIT')
        assert ExitReason.PRE_CLOSE_EXIT == "PRE_CLOSE_EXIT"
