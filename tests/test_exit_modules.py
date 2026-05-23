"""
退出模块系统单元测试 — Phase 2 验证

覆盖:
1. ExitProfile 序列化/反序列化
2. ExitModuleRegistry 注册/加载/评估
3. AtrExitModule 止损触发逻辑
4. TrailingExitModule 追踪止损
5. ScaleOutExitModule 分层止盈
6. 组合评估 evaluate_best
7. YAML 配置加载
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from types import SimpleNamespace

from app.exit.base import ExitProfile, ExitDecision, BaseExitModule
from app.exit.registry import ExitModuleRegistry, get_exit_registry
from app.exit.atr_exit import AtrExitModule
from app.exit.trailing_exit import TrailingExitModule
from app.exit.scale_out_exit import ScaleOutExitModule


# ============================================================
#  Fixtures
# ============================================================
@pytest.fixture(autouse=True)
def reset_registry():
    ExitModuleRegistry.reset_singleton()
    yield
    ExitModuleRegistry.reset_singleton()


def make_position(entry_price=150.0, direction="LONG", quantity=100):
    """模拟持仓对象"""
    return SimpleNamespace(
        avg_entry_price=entry_price,
        direction=direction,
        quantity=quantity,
        peak_price=entry_price,
    )


def make_ctx(current_price=150.0, atr=2.5, holding_minutes=30, peak_price=None):
    """模拟上下文"""
    ctx = {
        "current_price": current_price,
        "atr": atr,
        "holding_minutes": holding_minutes,
    }
    if peak_price is not None:
        ctx["peak_price"] = peak_price
    return ctx


# ============================================================
#  1. ExitProfile 测试
# ============================================================
class TestExitProfile:
    def test_to_dict_roundtrip(self):
        p = ExitProfile(
            name="test",
            stop_loss_type="atr_multi",
            stop_loss_params={"multiplier": 2.0},
            take_profit_type="fixed_pct",
            take_profit_params={"target_pct": 0.05},
            max_holding_minutes=120,
        )
        d = p.to_dict()
        restored = ExitProfile.from_dict(d)
        assert restored.name == "test"
        assert restored.stop_loss_params["multiplier"] == 2.0
        assert restored.max_holding_minutes == 120

    def test_defaults(self):
        p = ExitProfile(name="minimal")
        assert p.stop_loss_type == "fixed_pct"
        assert p.pre_close_behavior == "hold"


# ============================================================
#  2. ExitModuleRegistry 测试
# ============================================================
class TestExitModuleRegistry:
    def test_register_and_get(self):
        reg = get_exit_registry()
        reg.register(AtrExitModule)
        assert "atr_exit" in reg.list_available()
        mod = reg.get_module("atr_exit")
        assert isinstance(mod, AtrExitModule)

    def test_unregistered_raises(self):
        reg = get_exit_registry()
        with pytest.raises(KeyError):
            reg.get_module("nonexistent")

    def test_load_profiles(self):
        reg = get_exit_registry()
        count = reg.load_profiles("config/exit_profiles.yaml")
        assert count >= 9, f"Expected >= 9 profiles, got {count}"
        assert "stable_15m_base" in reg.list_profiles()
        assert "radical_daily_deep_oversold" in reg.list_profiles()

    def test_get_profile(self):
        reg = get_exit_registry()
        reg.load_profiles("config/exit_profiles.yaml")
        p = reg.get_profile("stable_15m_base")
        assert p is not None
        assert p.stop_loss_type == "atr_multi"
        assert p.stop_loss_params["multiplier"] == 1.5

    def test_evaluate_best_no_action(self):
        """价格远离止损止盈 → NO_ACTION"""
        reg = get_exit_registry()
        reg.register(AtrExitModule)
        reg.register(TrailingExitModule)
        reg.register(ScaleOutExitModule)
        reg.load_profiles("config/exit_profiles.yaml")

        pos = make_position(entry_price=150.0, direction="LONG")
        ctx = make_ctx(current_price=151.0, atr=2.5, holding_minutes=30)
        decision = reg.evaluate_best(pos, ctx, "stable_15m_base")
        assert decision.action == "NO_ACTION"


# ============================================================
#  3. AtrExitModule 测试
# ============================================================
class TestAtrExitModule:
    def test_long_stop_triggered(self):
        """LONG 方向 ATR 止损触发"""
        mod = AtrExitModule()
        profile = ExitProfile(
            name="test",
            stop_loss_type="atr_multi",
            stop_loss_params={"multiplier": 2.0, "max_loss_pct": -0.15},
        )
        pos = make_position(entry_price=150.0, direction="LONG")
        # stop_price = 150 - 2.0*2.5 = 145.0
        ctx = make_ctx(current_price=144.0, atr=2.5)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action == "CLOSE"
        assert "ATR_STOP" in decision.reason

    def test_long_stop_not_triggered(self):
        """LONG 方向未触发"""
        mod = AtrExitModule()
        profile = ExitProfile(
            name="test",
            stop_loss_type="atr_multi",
            stop_loss_params={"multiplier": 2.0, "max_loss_pct": -0.15},
        )
        pos = make_position(entry_price=150.0, direction="LONG")
        ctx = make_ctx(current_price=148.0, atr=2.5)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action == "NO_ACTION"

    def test_short_stop_triggered(self):
        """SHORT 方向止损触发"""
        mod = AtrExitModule()
        profile = ExitProfile(
            name="test",
            stop_loss_type="atr_multi",
            stop_loss_params={"multiplier": 2.0, "max_loss_pct": -0.15},
        )
        pos = make_position(entry_price=150.0, direction="SHORT")
        # stop_price = 150 + 2.0*2.5 = 155.0
        ctx = make_ctx(current_price=156.0, atr=2.5)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action == "CLOSE"

    def test_fixed_pct_stop(self):
        """固定百分比止损"""
        mod = AtrExitModule()
        profile = ExitProfile(
            name="test",
            stop_loss_type="fixed_pct",
            stop_loss_params={"pct": -0.015, "max_loss_pct": -0.15},
        )
        pos = make_position(entry_price=100.0, direction="LONG")
        # stop_price = 100 * (1 - 0.015) = 98.5
        ctx = make_ctx(current_price=98.0, atr=2.0)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action == "CLOSE"
        assert "FIXED_PCT_STOP" in decision.reason

    def test_max_loss_pct(self):
        """最大亏损百分比触发"""
        mod = AtrExitModule()
        profile = ExitProfile(
            name="test",
            stop_loss_type="atr_multi",
            stop_loss_params={"multiplier": 5.0, "max_loss_pct": -0.05},
        )
        pos = make_position(entry_price=100.0, direction="LONG")
        # ATR 止损价很远 (100-5*2.5=87.5), 但亏损超 -5%
        ctx = make_ctx(current_price=94.0, atr=2.5)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action == "CLOSE"
        assert "MAX_LOSS" in decision.reason


# ============================================================
#  4. TrailingExitModule 测试
# ============================================================
class TestTrailingExitModule:
    def test_trailing_activated_and_triggered(self):
        """追踪止损激活后触发"""
        mod = TrailingExitModule()
        profile = ExitProfile(
            name="test",
            take_profit_type="trailing",
            stop_loss_params={
                "activation_profit_atr": 1.0,
                "multiplier": 1.5,
            },
        )
        pos = make_position(entry_price=100.0, direction="LONG")
        # 盈利 > 1*ATR(2.5) = 2.5 → 激活
        # peak=108, trail = 108 - 1.5*2.5 = 104.25
        ctx = make_ctx(current_price=103.0, atr=2.5, peak_price=108.0)
        decision = mod.evaluate(pos, ctx, profile)
        # 103 < 104.25 → 触发
        assert decision.action == "CLOSE"

    def test_trailing_not_activated(self):
        """利润不足未激活"""
        mod = TrailingExitModule()
        profile = ExitProfile(
            name="test",
            take_profit_type="trailing",
            stop_loss_params={
                "activation_profit_atr": 2.0,
                "multiplier": 1.5,
            },
        )
        pos = make_position(entry_price=100.0, direction="LONG")
        # 盈利 1.0 < 2*ATR(2.5)=5.0 → 未激活
        ctx = make_ctx(current_price=101.0, atr=2.5, peak_price=101.0)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action == "NO_ACTION"


# ============================================================
#  5. ScaleOutExitModule 测试
# ============================================================
class TestScaleOutExitModule:
    def test_first_tier_triggered(self):
        """第一层止盈触发"""
        mod = ScaleOutExitModule()
        profile = ExitProfile(
            name="test",
            take_profit_type="scale_out",
            take_profit_params={
                "tiers": [
                    {"profit_pct": 3.0, "close_pct": 50},
                    {"profit_pct": 6.0, "close_pct": 50},
                ],
            },
        )
        pos = make_position(entry_price=100.0, direction="LONG")
        # 盈利 3.5% > 第一层 3%
        ctx = make_ctx(current_price=103.5, atr=2.5)
        decision = mod.evaluate(pos, ctx, profile)
        assert decision.action in ("REDUCE", "CLOSE")
        assert decision.qty_pct > 0


# ============================================================
#  6. 时间止损 (registry 内置)
# ============================================================
class TestTimeStop:
    def test_time_stop_triggered(self):
        """持仓超时触发"""
        reg = get_exit_registry()
        reg.register(AtrExitModule)
        reg.register(TrailingExitModule)
        reg.register(ScaleOutExitModule)

        profile = ExitProfile(
            name="test_time",
            stop_loss_type="atr_multi",
            stop_loss_params={"multiplier": 2.0, "max_loss_pct": -0.15},
            take_profit_type="fixed_pct",
            take_profit_params={"target_pct": 0.10},
            max_holding_minutes=30,
        )
        reg.add_profile(profile)

        pos = make_position(entry_price=100.0, direction="LONG")
        ctx = make_ctx(current_price=101.0, atr=2.5, holding_minutes=35)
        decisions = reg.evaluate_all(pos, ctx, "test_time")

        time_decisions = [d for d in decisions if "TIME_STOP" in d.reason]
        assert len(time_decisions) >= 1
        assert time_decisions[0].action == "CLOSE"


# ============================================================
#  Run
# ============================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
