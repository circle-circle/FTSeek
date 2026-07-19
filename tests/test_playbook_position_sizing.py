"""
F1: Playbook sizing SSOT 公共函数测试 (DEB-20260719-round2 Ω.3-4, β焦点4)

验收清单 (β焦点4):
  ①兜底现状公式锁定 (risk_amount/sl_distance 核心数学)
  ②主路径乘数链锁定 (regime_mult × water_mult)
  ③max_position_pct 截断
  ④分歧降仓 50% (conf_factor=0.5)
  ⑤统一后兜底含乘数的新行为 (F1 修复: YELLOW/ORANGE 水位缩仓)
  + 边界: sl_distance<=0 / 价格非法 → 0
"""
import pytest

from app.playbook.instruction_generator import compute_position_size


class TestComputePositionSize:
    """F1 sizing SSOT 公共函数确定性测试。"""

    def test_basic_formula_lockdown(self):
        """①兜底现状公式锁定: risk_amount/sl_distance 核心数学。
        risk_base=82000, risk_pct=0.5 → risk_amount=410; sl_distance=|100-98|=2 → size=205。
        """
        size = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=1.0,  # 放宽不触发截断
        )
        assert size == 205  # int(410 / 2)

    def test_main_path_mult_chain(self):
        """②主路径乘数链锁定: regime_mult × water_mult 缩放 risk_amount。
        risk_amount = 82000×0.005×0.8(regime)×0.5(water) = 164; size = int(164/2) = 82。
        """
        size = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=1.0,
            regime_mult=0.8, water_mult=0.5,
        )
        assert size == 82  # int(164 / 2)

    def test_max_position_pct_truncation(self):
        """③max_position_pct 截断: size×entry 超上限时缩至 max(1, int(max_val/entry))。
        无截断时 size=205 (×$100=$20500); max_position_pct=0.04 → max_val=$3280 → int(3280/100)=32。
        """
        size = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=0.04,
        )
        assert size == 32  # int(82000×0.04 / 100)

    def test_truncation_floor_one_share(self):
        """③补充: 截断后至少 1 股 (max(1, ...))。仓位过大被截断至不足 1 股时保底 1 股。
        risk_amount=100000×0.005=500; sl_distance=1 → size=500;
        max_position_pct=0.004 → max_val=$400; 500×$500=$250000>$400 → int(400/500)=0 → max(1,0)=1。
        """
        size = compute_position_size(
            risk_base=100000.0, risk_pct=0.5,
            entry_price=500.0, stop_loss=499.0,
            max_position_pct=0.004,  # max_val=$400 → int(400/500)=0 → max(1,0)=1
        )
        assert size == 1

    def test_conf_factor_disagreement(self):
        """④分歧降仓 50%: conf_factor=0.5 使 risk_amount 减半。
        risk_amount = 82000×0.005×0.5 = 205; size = int(205/2) = 102。
        """
        size = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=1.0,
            conf_factor=0.5,
        )
        assert size == 102  # int(205 / 2)

    def test_fallback_with_water_mult_fix(self):
        """⑤F1 修复核心: 兜底路径补传 water_mult 后, YELLOW/ORANGE 水位仓位缩小。
        修复前兜底无 water_mult → ORANGE 仓位与 GREEN 相同 (反身性放大)。
        修复后 ORANGE water_mult=0.25 → risk_amount=82000×0.005×0.25=102.5 → size=int(102.5/2)=51。
        对比 GREEN(无缩) size=205, ORANGE 缩至 1/4。
        """
        green = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=1.0, water_mult=1.0,
        )
        orange = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=1.0, water_mult=0.25,
        )
        assert green == 205
        assert orange == 51  # int(102.5 / 2)
        assert orange < green  # 水位减仓防御生效

    def test_zero_sl_distance_returns_zero(self):
        """边界: entry==stop_loss → sl_distance=0 → 返回 0。"""
        size = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=100.0,
            max_position_pct=0.04,
        )
        assert size == 0

    def test_invalid_price_returns_zero(self):
        """边界: entry_price<=0 或 stop_loss<=0 → 返回 0。"""
        assert compute_position_size(82000.0, 0.5, 0.0, 98.0, 0.04) == 0
        assert compute_position_size(82000.0, 0.5, 100.0, 0.0, 0.04) == 0
        assert compute_position_size(82000.0, 0.5, -5.0, 98.0, 0.04) == 0

    def test_combined_mults_multiplicative(self):
        """组合: regime×water×conf 三者相乘 (主路径+兜底合并语义)。
        risk_amount = 82000×0.005×0.8×0.5×0.5 = 82; size = int(82/2) = 41。
        """
        size = compute_position_size(
            risk_base=82000.0, risk_pct=0.5,
            entry_price=100.0, stop_loss=98.0,
            max_position_pct=1.0,
            regime_mult=0.8, water_mult=0.5, conf_factor=0.5,
        )
        assert size == 41  # int(82 / 2)
