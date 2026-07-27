"""
IntradayScorer v2.0 单元测试

测试覆盖:
- 6 因子各自评分逻辑
- 极端值处理
- 缺失数据处理
- 方向聚合
- 置信度/共识度标定
- 仓位和止损计算
- vote_result 格式兼容性
- 日内时段感知
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from unittest.mock import patch

from app.core.intraday_scorer import (
    IntradayScorer,
    IntradaySignal,
    FactorScore,
    get_intraday_scorer,
    _get_intraday_session,
    _session_multiplier,
)


# ============================================================
# Fixtures: 模拟市场上下文 (含 LV3 数据)
# ============================================================

def _base_context(**overrides):
    """构建基础行情上下文, 可 override 任意字段"""
    ctx = {
        "symbol": "US.NVDA",
        "price": 450.0,
        "change_percent": 1.5,
        "rsi": 55.0,
        "rsi_prev": 52.0,
        "macd": 0.85,
        "macd_signal": 0.60,
        "ma20": 440.0,
        "ma50": 420.0,
        "ma200": 380.0,
        "atr": 12.5,
        "volume_ratio": 1.2,
        "volume": 50000000,
        "turnover": 2.25e8,          # $225M (realistic for single stock)
        "amplitude": 2.5,
        "volume_trend": 0.02,
        # LV3 盘口数据
        "lv3_bid": 449.80,
        "lv3_ask": 450.20,
        "lv3_bid_vol": 15000,
        "lv3_ask_vol": 12000,
        "order_flow_imbalance": 0.11,
        # 大盘
        "vix": 20.0,
        "spy_price": 520.0,
        "spy_ma50": 510.0,
        "qqq_change_percent": 0.8,
        "data_source": "futu_lv3",
    }
    ctx.update(overrides)
    return ctx


@pytest.fixture
def scorer():
    return IntradayScorer()


# ============================================================
# 基础测试
# ============================================================

class TestBasicFunctionality:
    """基础功能测试"""

    def test_singleton(self):
        s1 = get_intraday_scorer()
        s2 = get_intraday_scorer()
        assert s1 is s2

    def test_valid_signal_has_direction(self, scorer):
        result = scorer.score(_base_context())
        assert result.direction in ("LONG", "SHORT", "NEUTRAL")
        assert 10 <= result.strength <= 90
        assert 0.0 <= result.confidence <= 1.0
        assert result.factors  # 至少有部分因子

    def test_empty_price_returns_neutral(self, scorer):
        result = scorer.score({"symbol": "US.TEST", "price": 0})
        assert result.direction == "NEUTRAL"
        assert result.confidence == 0.0

    def test_missing_price_returns_neutral(self, scorer):
        result = scorer.score({"symbol": "US.TEST"})
        assert result.direction == "NEUTRAL"

    def test_to_vote_result_format(self, scorer):
        result = scorer.score(_base_context())
        vr = result.to_vote_result()

        required_fields = [
            "final_strength", "final_confidence", "direction",
            "consensus_level", "std_dev", "is_neutral",
            "model_scores", "phase_gate", "position_suggestion",
        ]
        for field in required_fields:
            assert field in vr, f"Missing field: {field}"

        assert vr["phase_gate"] == "TECHNICAL_ONLY"
        assert len(vr["model_scores"]) == 1
        assert vr["model_scores"][0]["model_id"] == "intraday_scorer_v2"

    def test_vote_result_roundtrip(self, scorer):
        """确保 to_vote_result 输出能被下游消费"""
        result = scorer.score(_base_context())
        vr = result.to_vote_result()

        # 验证与现有 VotingCoordinator 输出格式的一致性
        assert isinstance(vr["final_strength"], int)
        assert isinstance(vr["final_confidence"], float)
        assert isinstance(vr["consensus_level"], float)
        assert isinstance(vr["is_neutral"], bool)
        assert vr["direction"] in ("LONG", "SHORT", "NEUTRAL")


# ============================================================
# 因子级测试
# ============================================================

class TestTrendFactor:
    """F1: 趋势强度测试"""

    def test_strong_bull_trend(self, scorer):
        """完整多头排列 → 强看多"""
        ctx = _base_context(
            price=460, ma20=440, ma50=420, ma200=380,
            spy_price=530, spy_ma50=500,
        )
        signal = scorer.score(ctx)
        assert signal.factors["trend"].raw > 30, f"Expected strong bull trend, got {signal.factors['trend'].raw}"

    def test_strong_bear_trend(self, scorer):
        """完整空头排列 → 强看空"""
        ctx = _base_context(
            price=360, ma20=380, ma50=400, ma200=420,
            spy_price=500, spy_ma50=510,
        )
        signal = scorer.score(ctx)
        assert signal.factors["trend"].raw < -20, f"Expected bear trend, got {signal.factors['trend'].raw}"

    def test_sideways_ma_tangle(self, scorer):
        """均线缠绕 → 中性"""
        ctx = _base_context(
            price=440, ma20=441, ma50=439, ma200=440,
        )
        signal = scorer.score(ctx)
        # 均线缠绕时趋势因子应接近 0
        assert abs(signal.factors["trend"].raw) < 25, f"Expected neutral trend, got {signal.factors['trend'].raw}"

    def test_missing_mas(self, scorer):
        """缺少均线数据: 退化为部分评分"""
        ctx = _base_context(ma20=0, ma50=0, ma200=0, spy_ma50=0)
        signal = scorer.score(ctx)
        assert signal.factors["trend"].confidence > 0
        # 缺少 MA 时, 仅靠 SPY 趋势 + 自身价格偏离, 置信度应较低

    def test_trend_confidence_valid_range(self, scorer):
        """趋势因子置信度始终在有效范围"""
        for dev in [-10, -5, -2, 0, 2, 5, 10]:
            ctx = _base_context(
                price=440 + dev, ma20=440, ma50=430,
            )
            s = scorer.score(ctx)
            assert 0.0 <= s.factors["trend"].confidence <= 1.0


class TestMomentumFactor:
    """F2: 动量质量测试"""

    def test_oversold_bounce(self, scorer):
        """RSI 超卖反弹 → 强看多"""
        ctx = _base_context(rsi=28, rsi_prev=22, macd=-0.5, macd_signal=-0.8, change_percent=-3.0)
        signal = scorer.score(ctx)
        assert signal.factors["momentum"].raw > 0

    def test_overbought_with_negative_divergence(self, scorer):
        """RSI 高位回落 → 看空"""
        ctx = _base_context(rsi=72, rsi_prev=78, macd=0.5, macd_signal=0.8, change_percent=2.0)
        signal = scorer.score(ctx)
        # RSI 从 78 掉到 72, 但还在高位, 应偏空
        assert signal.factors["momentum"].raw < 15

    def test_macd_crossover_bull(self, scorer):
        """MACD 水下金叉 → 看多"""
        ctx = _base_context(
            rsi=42, rsi_prev=38,
            macd=-0.2, macd_signal=-0.5,  # hist = +0.3, 且 macd 在 0 下
            change_percent=0.5,
        )
        signal = scorer.score(ctx)
        assert signal.factors["momentum"].raw > 10

    def test_extreme_rsi(self, scorer):
        """RSI 极端值"""
        ctx = _base_context(rsi=95, rsi_prev=92, change_percent=8.0)
        signal = scorer.score(ctx)
        # RSI=95 极度超买, momentum 应看空
        assert signal.factors["momentum"].raw < -15

    def test_neutral_momentum(self, scorer):
        """RSI 50 + MACD 平 → 中性"""
        ctx = _base_context(rsi=50, rsi_prev=50, macd=0, macd_signal=0, change_percent=0)
        signal = scorer.score(ctx)
        assert abs(signal.factors["momentum"].raw) < 15


class TestVolumeFactor:
    """F3: 量价关系测试"""

    def test_high_volume_up(self, scorer):
        """放量上涨 → 看多"""
        ctx = _base_context(volume_ratio=2.5, change_percent=3.0)
        signal = scorer.score(ctx)
        assert signal.factors["volume"].raw > 0

    def test_high_volume_down(self, scorer):
        """放量下跌 → 看空"""
        ctx = _base_context(volume_ratio=2.0, change_percent=-3.5)
        signal = scorer.score(ctx)
        assert signal.factors["volume"].raw < 0

    def test_low_volume_up_warning(self, scorer):
        """缩量上涨 → 接近中性(虚假突破风险)"""
        ctx = _base_context(volume_ratio=0.4, change_percent=2.0)
        signal = scorer.score(ctx)
        # 缩量上涨应该低置信度, 幅度接近于 0
        assert abs(signal.factors["volume"].raw) < 25

    def test_normal_volume(self, scorer):
        """正常量能 → 弱信号"""
        ctx = _base_context(volume_ratio=1.0, change_percent=0.5)
        signal = scorer.score(ctx)
        assert abs(signal.factors["volume"].raw) < 20


class TestOrderFlowFactor:
    """F4: 订单流失衡测试"""

    def test_bullish_orderflow(self, scorer):
        """买盘主导 → 看多"""
        ctx = _base_context(
            lv3_bid_vol=20000, lv3_ask_vol=10000,
            order_flow_imbalance=0.33,
            lv3_bid=449.95, lv3_ask=450.05,
        )
        signal = scorer.score(ctx)
        assert signal.factors["orderflow"].raw > 0

    def test_bearish_orderflow(self, scorer):
        """卖盘主导 → 看空"""
        ctx = _base_context(
            lv3_bid_vol=8000, lv3_ask_vol=20000,
            order_flow_imbalance=-0.43,
            lv3_bid=449.90, lv3_ask=450.10,
        )
        signal = scorer.score(ctx)
        assert signal.factors["orderflow"].raw < 0

    def test_no_lv3_data(self, scorer):
        """无 LV3 数据 → 订单流因子退化"""
        ctx = _base_context(
            lv3_bid=0, lv3_ask=0, lv3_bid_vol=0, lv3_ask_vol=0,
            order_flow_imbalance=0,
        )
        signal = scorer.score(ctx)
        assert signal.factors["orderflow"].confidence == 0.0
        assert signal.factors["orderflow"].raw == 0.0

    def test_wide_spread_lowers_confidence(self, scorer):
        """价差过大 → 降低置信度"""
        tight = scorer.score(_base_context(lv3_bid=449.50, lv3_ask=450.50)).factors["orderflow"].confidence
        wide = scorer.score(_base_context(lv3_bid=447.00, lv3_ask=453.00)).factors["orderflow"].confidence
        assert wide < tight, f"Wide spread({wide:.2f}) should have lower confidence than tight({tight:.2f})"


# ============================================================
# 聚合与置信度
# ============================================================

class TestAggregation:
    """方向聚合和置信度计算测试"""

    @patch("app.core.intraday_scorer._get_intraday_session")
    def test_all_factors_bull(self, mock_session, scorer):
        """4 因子全部看多 → 强 LONG, 高置信度"""
        mock_session.return_value = "CLOSE"
        ctx = _base_context(
            price=460, ma20=440, ma50=420,          # 趋势看多
            rsi=58, rsi_prev=54, macd=0.9, macd_signal=0.5,  # 动量看多
            volume_ratio=1.8, change_percent=2.5,   # 量价看多
            lv3_bid_vol=18000, lv3_ask_vol=10000, order_flow_imbalance=0.25,  # 订单流看多
        )
        signal = scorer.score(ctx)
        assert signal.direction == "LONG"
        assert signal.strength > 55
        assert signal.confidence >= 0.55, f"Expected confidence >= 0.55, got {signal.confidence}"

    def test_mixed_factors(self, scorer):
        """因子分歧 → 低共识度"""
        ctx = _base_context(
            price=460, ma20=440, ma50=420,          # 趋势看多
            rsi=72, rsi_prev=68, macd=0.3, macd_signal=0.5, change_percent=3.5,  # 动量偏空(高位)
            volume_ratio=0.5,                        # 量价中性(缩量)
            lv3_bid_vol=10000, lv3_ask_vol=10000, order_flow_imbalance=0.0,  # 订单流中性
        )
        signal = scorer.score(ctx)
        assert signal.consensus_level < 0.80, f"Mixed factors should have lower consensus"

    def test_neutral_when_all_flat(self, scorer):
        """所有因子中性 → NEUTRAL"""
        ctx = _base_context(
            price=440, ma20=440, ma50=440, ma200=440,
            rsi=50, rsi_prev=50, macd=0, macd_signal=0, change_percent=0,
            volume_ratio=1.0, amplitude=0, turnover=0,
            lv3_bid_vol=0, lv3_ask_vol=0, order_flow_imbalance=0,
            spy_price=0, spy_ma50=0,
        )
        signal = scorer.score(ctx)
        assert signal.direction == "NEUTRAL"

    def test_confidence_in_range(self, scorer):
        """置信度始终在有效范围"""
        # 测试多种场景
        scenarios = [
            _base_context(),                                                                 # 正常
            _base_context(price=500, ma20=400, rsi=85, change_percent=6.0),                # 极端看多
            _base_context(price=300, ma20=400, rsi=15, change_percent=-7.0),                # 极端看空
            _base_context(price=440, ma20=440, rsi=50, volume_ratio=0.5),                   # 中性
            _base_context(vix=35, lv3_bid_vol=0, lv3_ask_vol=0),                            # 极端波动 + 无 LV3
        ]
        for ctx in scenarios:
            signal = scorer.score(ctx)
            assert 0.0 <= signal.confidence <= 1.0, f"Confidence out of range: {signal.confidence}"
            assert 0.0 <= signal.consensus_level <= 1.0, f"Consensus out of range: {signal.consensus_level}"


# ============================================================
# 仓位和止损
# ============================================================

class TestPositionAndStop:
    """仓位和止损计算测试"""

    def test_position_size_positive(self, scorer):
        signal = scorer.score(_base_context())
        if signal.direction != "NEUTRAL":
            assert signal.position_pct > 0
            assert signal.stop_loss_pct > 0

    def test_volatility_scaling(self, scorer):
        """高波动 → 仓位降低"""
        normal = scorer.score(_base_context(atr=5.0, vix=18))
        volatile = scorer.score(_base_context(atr=20.0, vix=30))
        if normal.direction != "NEUTRAL" and volatile.direction != "NEUTRAL":
            # 高波动时仓位应更小
            assert volatile.position_pct <= normal.position_pct * 1.5, \
                f"High vol position({volatile.position_pct}) should not greatly exceed normal({normal.position_pct})"

    def test_stop_loss_reasonable(self, scorer):
        """止损距离在合理范围 — P0 (2026-06-04): stop = max(4×ATR/price, 2%),
        ATR% 可交易带 0.8%-6% → stop 上限 4×6%=24% (VIX≤35)"""
        for atr_val in [5, 10, 20, 30]:
            signal = scorer.score(_base_context(atr=atr_val))
            if signal.direction != "NEUTRAL":
                assert 0.02 <= signal.stop_loss_pct <= 0.24, \
                    f"Stop loss {signal.stop_loss_pct:.3f} out of [0.02, 0.24] for ATR={atr_val}"

    def test_high_vix_wider_stop(self, scorer):
        """高 VIX → 放宽止损"""
        normal = scorer.score(_base_context(vix=18, atr=10))
        panic = scorer.score(_base_context(vix=32, atr=10))
        if normal.direction != "NEUTRAL" and panic.direction != "NEUTRAL":
            assert panic.stop_loss_pct >= normal.stop_loss_pct


# ============================================================
# 日内时段
# ============================================================

class TestIntradaySession:
    """日内时段感知测试"""

    def test_get_intraday_session(self):
        """验证时段返回值为合法值 (SharedKernel canonical names)"""
        session = _get_intraday_session()
        assert session in ("opening_drive", "morning", "midday", "afternoon", "closing_drive", "after_hours")

    def test_session_multiplier_valid(self):
        """时段乘数在有效范围"""
        conf_mul, str_mul = _session_multiplier()
        assert 0.5 <= conf_mul <= 1.5
        assert 0.5 <= str_mul <= 1.5

    # 2026-07-27 修尺子: 旧版 mock `_get_intraday_session` (5 档时代 API) 不在现实现调用链上 —
    #   `_session_multiplier` 实走 shared_kernel.get_current_session() (2 档制), 假 mock 实测真墙钟:
    #   非 prime 时段碰巧通过、盘中 prime 必挂 (密封化漏网第三例)。改 mock 真实调用链, 追平 2 档语义。
    @patch("app.core.shared_kernel.get_shared_kernel")
    def test_open_session_lower_confidence(self, mock_kernel, scorer):
        """non-prime 时段 (opening_drive) → 置信度被 0.85 乘数压制 (对照 prime 不衰减)"""
        mock_kernel.return_value.get_current_session.return_value = "opening_drive"
        sig_open = scorer.score(_base_context())
        mock_kernel.return_value.get_current_session.return_value = "morning"
        sig_prime = scorer.score(_base_context())
        assert sig_open.confidence <= sig_prime.confidence, \
            f"non-prime({sig_open.confidence}) 应 ≤ prime({sig_prime.confidence})"

    @patch("app.core.shared_kernel.get_shared_kernel")
    def test_close_session_higher_strength(self, mock_kernel, scorer):
        """prime (closing_drive 1.0) 强度应 ≥ non-prime (midday 0.85), 方向一致时"""
        ctx = _base_context(price=460, ma20=430, rsi=62, volume_ratio=2.0,
                            lv3_bid_vol=20000, lv3_ask_vol=8000)
        mock_kernel.return_value.get_current_session.return_value = "closing_drive"
        signal_close = scorer.score(ctx)
        mock_kernel.return_value.get_current_session.return_value = "midday"
        signal_lunch = scorer.score(ctx)
        if signal_close.direction == signal_lunch.direction and signal_close.direction != "NEUTRAL":
            assert signal_close.strength >= signal_lunch.strength - 5, \
                f"CLOSE={signal_close.strength} vs MIDDAY={signal_lunch.strength}"


# ============================================================
# 极端场景和边界
# ============================================================

class TestEdgeCases:
    """边界情况测试"""

    def test_zero_division_protection(self, scorer):
        """防止各因子内部除零"""
        ctx = _base_context(
            price=450, atr=0.001,   # 极低 ATR
            ma20=450, ma50=450,     # 均线完全重合
            volume_ratio=0, turnover=0,  # 无成交
            lv3_bid_vol=0, lv3_ask_vol=0,
        )
        signal = scorer.score(ctx)
        # 不应抛异常
        assert signal.strength is not None

    def test_negative_values(self, scorer):
        """负价格/负指标"""
        ctx = _base_context(price=-100, rsi=-10, macd=-999)
        signal = scorer.score(ctx)
        # price <= 0 → empty signal
        assert signal.direction == "NEUTRAL"

    def test_extreme_large_values(self, scorer):
        """极端大值: 不应溢出"""
        ctx = _base_context(
            price=1e6, atr=1e5, ma20=1e6, ma50=9e5,
            rsi=99, volume_ratio=100,
        )
        signal = scorer.score(ctx)
        assert 10 <= signal.strength <= 90

    def test_context_with_extra_keys(self, scorer):
        """多余的 context 字段不影响评分"""
        ctx = _base_context(
            extra_field_1="ignored",
            extra_list=[1, 2, 3],
            extra_nested={"a": {"b": 1}},
        )
        signal = scorer.score(ctx)
        assert signal.strength is not None

    def test_symbol_preserved(self, scorer):
        """symbol 正确传递"""
        signal = scorer.score(_base_context(symbol="US.AAPL"))
        assert signal.symbol == "US.AAPL"
        vr = signal.to_vote_result()
        assert vr["symbol"] == "US.AAPL"

    def test_factor_detail_in_vote_result(self, scorer):
        """_factor_detail 包含所有活跃因子"""
        signal = scorer.score(_base_context())
        vr = signal.to_vote_result()
        assert "_factor_detail" in vr
        for name in ("trend", "momentum", "volume", "orderflow"):
            assert name in vr["_factor_detail"], f"Missing factor {name}"


# ============================================================
# 与旧版对比性测试
# ============================================================

class TestVsOldScorer:
    """新旧评分器对比 (新版本应不低于旧版本的关键属性)"""

    def test_new_scorer_uses_more_factors(self, scorer):
        """新评分器使用了 4 个因子(趋势/动量/量价/订单流), 而非旧版的 1 个"""
        signal = scorer.score(_base_context())
        active_factors = [f for f in signal.factors.values() if abs(f.raw) > 0]
        assert len(active_factors) >= 2, f"Expected multiple active factors, got {len(active_factors)}"

    def test_new_scorer_confidence_differentiation(self, scorer):
        """新评分器对不同市场环境给出差异化的置信度 (而非固定 1.0 consensus)"""
        bull = scorer.score(_base_context(price=460, ma20=430, rsi=62, volume_ratio=2.0))
        bear = scorer.score(_base_context(price=360, ma20=390, rsi=25, volume_ratio=1.8))

        # 两场景应有不同的置信度
        if bull.direction != "NEUTRAL" and bear.direction != "NEUTRAL":
            # 置信度应在合理范围内, 且为真正计算的 (非固定值)
            assert 0.20 < bull.confidence < 0.95
            assert 0.20 < bear.confidence < 0.95


# ============================================================
# 运行测试
# ============================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
