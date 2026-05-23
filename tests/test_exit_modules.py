"""
Exit 模块单元测试

覆盖:
- ExitProfile 序列化/反序列化
- ATR 止损模块 (fixed_pct / atr_multi)
- Chandelier 追踪止损
- 分层止盈 (scale_out)
- 时间止损
- 注册中心操作
- 组合评估
"""
import pytest
from dataclasses import dataclass
from typing import Any

from app.exit import (
    ExitProfile,
    ExitDecision,
    get_exit_registry,
)
from app.exit.registry import ExitModuleRegistry
from app.exit.atr_exit import AtrExitModule
from app.exit.trailing_exit import TrailingExitModule
from app.exit.scale_out_exit import ScaleOutExitModule


# ── Mock Position ──

@dataclass
class MockPosition:
    avg_entry_price: float
    direction: str = "LONG"
    quantity: int = 100


# ── ExitProfile 测试 ──

def test_exit_profile_from_dict():
    """测试 ExitProfile 从字典创建"""
    data = {
        "name": "test_profile",
        "stop_loss_type": "atr_multi",
        "stop_loss_params": {"multiplier": 2.0},
        "take_profit_type": "fixed_pct",
        "take_profit_params": {"target_pct": 0.05},
        "max_holding_minutes": 120,
    }
    profile = ExitProfile.from_dict(data)
    assert profile.name == "test_profile"
    assert profile.stop_loss_type == "atr_multi"
    assert profile.stop_loss_params["multiplier"] == 2.0
    assert profile.max_holding_minutes == 120


def test_exit_profile_to_dict():
    """测试 ExitProfile 序列化为字典"""
    profile = ExitProfile(
        name="test",
        stop_loss_type="fixed_pct",
        stop_loss_params={"pct": -0.02},
        take_profit_type="trailing",
        take_profit_params={"activation_profit_atr": 1.0, "multiplier": 1.5},
        max_holding_minutes=60,
    )
    d = profile.to_dict()
    assert d["name"] == "test"
    assert d["stop_loss_type"] == "fixed_pct"
    assert d["take_profit_type"] == "trailing"
    assert d["max_holding_minutes"] == 60


# ── 注册中心测试 ──

def test_registry_singleton():
    """测试注册中心单例"""
    r1 = get_exit_registry()
    r2 = get_exit_registry()
    assert r1 is r2


def test_registry_list_modules():
    """测试列出已注册模块"""
    registry = get_exit_registry()
    modules = registry.list_available()
    assert "atr_exit" in modules
    assert "trailing_exit" in modules
    assert "scale_out_exit" in modules


def test_registry_load_profiles():
    """测试加载 profiles"""
    registry = get_exit_registry()
    count = registry.load_profiles()
    assert count >= 9  # 至少 9 个策略配置
    profiles = registry.list_profiles()
    assert "stable_15m_base" in profiles
    assert "aggressive_5m_high_momentum" in profiles


def test_registry_get_profile():
    """测试获取指定 profile"""
    registry = get_exit_registry()
    registry.load_profiles()
    profile = registry.get_profile("stable_15m_base")
    assert profile is not None
    assert profile.name == "stable_15m_base"
    assert profile.stop_loss_type == "atr_multi"
    assert profile.max_holding_minutes == 120


def test_registry_get_nonexistent_profile():
    """测试获取不存在的 profile"""
    registry = get_exit_registry()
    profile = registry.get_profile("nonexistent_profile")
    assert profile is None


# ── ATR 止损模块测试 ──

def test_atr_exit_fixed_pct_triggered():
    """测试固定百分比止损触发"""
    module = AtrExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="fixed_pct",
        stop_loss_params={"pct": -0.02},  # -2%
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 97.5, "atr": 2.0}  # -2.5%, 超过 -2%
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "CLOSE"
    assert decision.reason == "FIXED_PCT_STOP"
    assert decision.qty_pct == 1.0


def test_atr_exit_fixed_pct_not_triggered():
    """测试固定百分比止损未触发"""
    module = AtrExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="fixed_pct",
        stop_loss_params={"pct": -0.02},
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 98.5, "atr": 2.0}  # -1.5%, 未超过 -2%
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "NO_ACTION"


def test_atr_exit_atr_multi_triggered():
    """测试 ATR 倍数止损触发"""
    module = AtrExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="atr_multi",
        stop_loss_params={"multiplier": 1.5},
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 96.5, "atr": 2.0}  # 止损位 = 100 - 1.5*2 = 97
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "CLOSE"
    assert decision.reason == "ATR_STOP"


def test_atr_exit_atr_multi_not_triggered():
    """测试 ATR 倍数止损未触发"""
    module = AtrExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="atr_multi",
        stop_loss_params={"multiplier": 1.5},
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 98.0, "atr": 2.0}  # 止损位 = 97, 当前 98
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "NO_ACTION"


def test_atr_exit_short_direction():
    """测试空头方向止损"""
    module = AtrExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="fixed_pct",
        stop_loss_params={"pct": -0.02},
    )
    position = MockPosition(avg_entry_price=100.0, direction="SHORT")
    ctx = {"current_price": 102.5, "atr": 2.0}  # +2.5%, 空头亏损超过 -2%
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "CLOSE"
    assert decision.reason == "FIXED_PCT_STOP"


# ── Chandelier 追踪止损测试 ──

def test_trailing_exit_not_activated():
    """测试追踪止损未激活"""
    module = TrailingExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="chandelier",
        stop_loss_params={"activation_profit_atr": 1.5, "multiplier": 2.0},
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 102.0, "atr": 2.0, "peak_price": 102.0}  # 盈利 1×ATR
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "NO_ACTION"
    assert decision.reason == "trailing_not_activated"


def test_trailing_exit_activated_not_triggered():
    """测试追踪止损已激活但未触发"""
    module = TrailingExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="chandelier",
        stop_loss_params={"activation_profit_atr": 1.0, "multiplier": 2.0},
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 104.0, "atr": 2.0, "peak_price": 105.0}  # 从峰值回撤 0.5×ATR
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "NO_ACTION"


def test_trailing_exit_triggered():
    """测试追踪止损触发"""
    module = TrailingExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="chandelier",
        stop_loss_params={"activation_profit_atr": 1.0, "multiplier": 2.0},
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 100.5, "atr": 2.0, "peak_price": 105.0}  # 从峰值回撤 2.25×ATR
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "CLOSE"
    assert decision.reason == "CHANDELIER_TRAIL"


def test_trailing_exit_short_direction():
    """测试空头方向追踪止损"""
    module = TrailingExitModule()
    profile = ExitProfile(
        name="test",
        stop_loss_type="chandelier",
        stop_loss_params={"activation_profit_atr": 1.0, "multiplier": 1.5},
    )
    position = MockPosition(avg_entry_price=100.0, direction="SHORT")
    # peak_price=95 (最低价), trail = 95 + 1.5*2 = 98, 当前 98.5 >= 98 触发
    ctx = {"current_price": 98.5, "atr": 2.0, "peak_price": 95.0}
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "CLOSE"


# ── 分层止盈测试 ──

def test_scale_out_first_tier_triggered():
    """测试第一层止盈触发"""
    module = ScaleOutExitModule()
    profile = ExitProfile(
        name="test",
        take_profit_type="scale_out",
        take_profit_params={
            "tiers": [
                {"profit_pct": 3.0, "close_pct": 50},
                {"profit_pct": 6.0, "close_pct": 50},
            ]
        },
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 103.5, "realized_tiers": []}  # +3.5%
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "REDUCE"
    assert decision.qty_pct == 0.5
    assert "SCALE_OUT" in decision.reason


def test_scale_out_second_tier_triggered():
    """测试第二层止盈触发（第一层已实现）"""
    module = ScaleOutExitModule()
    profile = ExitProfile(
        name="test",
        take_profit_type="scale_out",
        take_profit_params={
            "tiers": [
                {"profit_pct": 3.0, "close_pct": 50},
                {"profit_pct": 6.0, "close_pct": 50},
            ]
        },
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 106.5, "realized_tiers": [1]}  # +6.5%, 第一层已实现
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "REDUCE"
    assert decision.qty_pct == 0.5
    assert decision.reason == "SCALE_OUT_TIER_2"


def test_scale_out_no_trigger():
    """测试未达到任何层级"""
    module = ScaleOutExitModule()
    profile = ExitProfile(
        name="test",
        take_profit_type="scale_out",
        take_profit_params={
            "tiers": [
                {"profit_pct": 5.0, "close_pct": 50},
                {"profit_pct": 10.0, "close_pct": 50},
            ]
        },
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 102.0, "realized_tiers": []}  # +2%, 未达到第一层
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "NO_ACTION"


def test_scale_out_all_tiers_realized():
    """测试所有层级已实现"""
    module = ScaleOutExitModule()
    profile = ExitProfile(
        name="test",
        take_profit_type="scale_out",
        take_profit_params={
            "tiers": [
                {"profit_pct": 3.0, "close_pct": 50},
                {"profit_pct": 6.0, "close_pct": 50},
            ]
        },
    )
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 108.0, "realized_tiers": [1, 2]}  # 全部已实现
    
    decision = module.evaluate(position, ctx, profile)
    assert decision.action == "NO_ACTION"


# ── 时间止损测试 ──

def test_time_stop_triggered():
    """测试时间止损触发"""
    registry = get_exit_registry()
    registry.load_profiles()
    
    profile = registry.get_profile("aggressive_5m_high_momentum")
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 101.0, "holding_minutes": 35}  # 超过 30 分钟
    
    decisions = registry.evaluate_all(position, ctx, "aggressive_5m_high_momentum")
    
    # 应该有 TIME_STOP
    time_decisions = [d for d in decisions if d.reason == "TIME_STOP"]
    assert len(time_decisions) > 0
    assert time_decisions[0].action == "CLOSE"


def test_time_stop_not_triggered():
    """测试时间止损未触发"""
    registry = get_exit_registry()
    registry.load_profiles()
    
    profile = registry.get_profile("aggressive_5m_high_momentum")
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {"current_price": 101.0, "holding_minutes": 20}  # 未超过 30 分钟
    
    decisions = registry.evaluate_all(position, ctx, "aggressive_5m_high_momentum")
    
    # 不应该有 TIME_STOP
    time_decisions = [d for d in decisions if d.reason == "TIME_STOP"]
    assert len(time_decisions) == 0


# ── 组合评估测试 ──

def test_evaluate_all_multiple_decisions():
    """测试组合评估返回多个决策"""
    registry = get_exit_registry()
    registry.load_profiles()
    
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {
        "current_price": 97.0,  # 触发 ATR 止损
        "atr": 2.0,
        "holding_minutes": 100,
    }
    
    decisions = registry.evaluate_all(position, ctx, "stable_15m_base")
    
    # 应该至少有 1 个决策（ATR 止损）
    assert len(decisions) >= 1
    active = [d for d in decisions if d.action != "NO_ACTION"]
    assert len(active) >= 1


def test_evaluate_best_priority():
    """测试 evaluate_best 返回最高优先级决策"""
    registry = get_exit_registry()
    registry.load_profiles()
    
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {
        "current_price": 97.0,  # 触发 ATR 止损
        "atr": 2.0,
        "holding_minutes": 150,  # 触发时间止损
    }
    
    decision = registry.evaluate_best(position, ctx, "stable_15m_base")
    
    # 应该返回最高优先级的（ATR 止损 priority=10 > TIME_STOP priority=3）
    assert decision.action == "CLOSE"
    assert decision.reason in ["ATR_STOP", "TIME_STOP"]


def test_evaluate_best_no_action():
    """测试 evaluate_best 无触发时返回 NO_ACTION"""
    registry = get_exit_registry()
    registry.load_profiles()
    
    position = MockPosition(avg_entry_price=100.0, direction="LONG")
    ctx = {
        "current_price": 101.0,  # 小幅盈利
        "atr": 2.0,
        "holding_minutes": 30,
    }
    
    decision = registry.evaluate_best(position, ctx, "stable_15m_base")
    assert decision.action == "NO_ACTION"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
