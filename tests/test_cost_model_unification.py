"""决策侧成本模型统一金标准测试 (2026-08-24, A2 复查修复).

背景: cost_model 原自带幻想费率参数 (commission_per_share=0.0 / fixed_fee_bps=0.3),
完全缺席富途 $1.99 佣金地板 — 对 <$10k 订单低估费用一个数量级, 与实收金标准
app/execution/fee_model (order_fee_query 实盘逐位验证) 脱节两个来源.
统一后: 股票侧佣金+监管费全部委托 fee_model 往返双腿估算, 本类锁住一致性.

连带回归: trailing_stop 的调用签名 bug (dict 塞进 symbol 参数位 → TypeError 被
except 吞成 cost_bps=0), 源码锚点防止回退.
"""
import math
from pathlib import Path

import pytest

from app.decision.cost_model import CostBreakdown, CostModel, CostModelConfig
from app.execution.fee_model import estimate_round_trip

ROOT = Path(__file__).resolve().parent.parent


class TestStockFeeUnification:
    """cost_model 股票侧费用必须与 fee_model 金标准逐位一致."""

    def test_small_order_hits_199_floor(self):
        """地板场景: SNDK 式高价小单 — 名义 $158k 但 10 股佣金应 $1.99/腿."""
        m = CostModel(CostModelConfig())
        bd = m.estimate("US.SNDK", "LONG", 10, 15800.0)
        rt = estimate_round_trip(10, 15800.0, 15800.0, "LONG")
        assert bd.notional_usd == pytest.approx(158000.0)
        # 双腿佣金 $3.98 → bps
        assert bd.commission_bps == pytest.approx(3.98 / 158000.0 * 1e4, rel=1e-3)
        assert bd.total_bps == pytest.approx(
            bd.commission_bps + bd.fixed_fee_bps + bd.borrow_bps
            + 2.0 * (0.5 * bd.spread_bps + bd.impact_bps), rel=1e-6)

    def test_commission_matches_fee_model_cap_and_floor(self):
        """佣金三段逻辑 (per-share/cap/floor) 必须与金标准一致 (往返双腿口径)."""
        m = CostModel()
        for qty, price in [(4, 50.0),        # 地板: max(min(0.04, ...), 1.99)=1.99
                           (1000, 5.0),      # 中段: 0.01×1000=10 < 0.3%×5000=15
                           (2000, 5.0)]:     # 上限段: min(20, 30)=20 → 无地板影响
            bd = m.estimate("US.TEST", "LONG", qty, price)
            rt = estimate_round_trip(qty, price, price, "LONG")
            two_legs = rt["entry"]["commission"] + rt["exit"]["commission"]
            assert bd.commission_bps == pytest.approx(
                two_legs / (qty * price) * 1e4, rel=1e-3)

    def test_regulatory_symmetric_short_vs_long_borrow_asymmetric(self):
        """往返口径不变量: LONG/SHORT 各恰含一条卖出腿 → 监管费对称;
        真正的方向不对称项是借券 (仅 SHORT 计提)."""
        m = CostModel()
        long_bd = m.estimate("US.TEST", "LONG", 100, 50.0)
        short_bd = m.estimate("US.TEST", "SHORT", 100, 50.0,
                              {"borrow_rate": 0.02})
        assert long_bd.fixed_fee_bps == pytest.approx(short_bd.fixed_fee_bps)
        assert long_bd.borrow_bps == 0.0
        assert short_bd.borrow_bps > 0.0
        assert short_bd.total_bps > long_bd.total_bps

    def test_total_usd_no_double_count(self):
        """total_usd 必须由 total_bps 单一换算 (修复原佣金 bps/usd 双计)."""
        m = CostModel()
        bd = m.estimate("US.TEST", "LONG", 37, 123.45, {"spread_bps": 4.0})
        assert bd.total_usd == pytest.approx(bd.total_bps * bd.notional_usd / 1e4)

    def test_removed_fantasy_params_are_gone(self):
        """幻想参数不得回流: CostModelConfig 不再持有 commission_per_share/fixed_fee_bps."""
        cfg = CostModelConfig()
        assert not hasattr(cfg, "commission_per_share")
        assert not hasattr(cfg, "fixed_fee_bps")


class TestOptionPathUntouched:
    def test_option_spread_rejection_still_works(self):
        m = CostModel()
        # 3.6% 宽度 < 5% 上限 → 放行
        ok = m.estimate("AAPL241220P00170000", "LONG", 2, 1.10,
                        {"bid": 1.08, "ask": 1.12}, asset="option")
        assert not ok.rejected
        assert ok.commission_bps == pytest.approx(
            2 * 0.65 / (2 * 1.10 * 100) * 1e4, rel=1e-3)
        # 54.5% 宽度 > 上限 → 拒绝
        wide = m.estimate("AAPL241220P00170000", "LONG", 2, 1.10,
                          {"bid": 0.80, "ask": 1.40}, asset="option")
        assert wide.rejected


class TestTrailingStopCallSiteFixed:
    def test_call_site_passes_positional_contract(self):
        """源码锚点: trailing_stop 必须 按 (symbol, side, qty, price, market_ctx, asset)
        传参 — 原 estimate(dict, dict) 签名使 TypeError 被吞、cost_bps 恒 0."""
        src = (ROOT / "app" / "risk" / "exit_rules" / "trailing_stop.py").read_text(
            encoding="utf-8")
        # 全文件不得再存在 dict 整体塞参调用形态
        assert "estimate(fake_signal" not in src
        idx = src.index("cost_model.estimate(")
        call_block = src[idx:idx + 400]
        assert "position.symbol" in call_block
        assert "getattr(position" in call_block       # size
        assert "float(current_price)" in call_block   # price

    def test_realistic_small_order_cost_buffer_nonzero(self):
        """行为级: 修复调用签名后 cost_bps 必须真正非零且量级合理
        (小单高价股: 地板佣金+监管费主导 ≈ 数十 bps, 幻想参数时代恒 0)."""
        m = CostModel()
        bd = m.estimate("US.SNDK", "LONG", 50, 300.0)   # $15k 名义额
        assert bd.total_bps > 1.0
        assert bd.total_usd == pytest.approx(bd.total_bps * 15000.0 / 1e4, rel=1e-6)
        # 地板佣金 $3.98/往返 在 $15k 上 ≈ 2.65bps, 监管费另计
        assert bd.commission_bps == pytest.approx(3.98 / 15000.0 * 1e4, rel=1e-3)
