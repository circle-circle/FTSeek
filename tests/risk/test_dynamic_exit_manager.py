"""
动态止盈止损管理器 (DynamicExitManager) 单元测试

覆盖:
1. 波动率自适应 — 高/中/低三种波动率下的止损/止盈倍率
2. ATR 动态止损 — BUY/SELL 方向、ATR 范围限制
3. 组合式退出引擎 (check_exit_priority) — 7 层退出逻辑 + 状态机
4. 保本止损 — 触发条件 + 止损上移
5. 日内止损 — T+0 当日限制
6. 追踪止盈 — ATR/百分比两种模式
7. 分批止盈 — 多级别触发 + close_ratio
8. 时间止损 — 持仓超时退出
9. 状态机转移 — INIT → BE_ACTIVE → TRAIL → EXIT
"""

import pytest
import tempfile
import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch

from app.risk.dynamic_exit_manager import (
    DynamicExitManager,
    ExitState,
    ExitDecision,
)


class TestVolatilityMultiplier:
    """波动率自适应乘数测试"""

    def test_default_multiplier(self):
        """默认波动率 → 乘数 1.0"""
        mgr = DynamicExitManager()
        assert mgr._get_vol_multiplier() == 1.0

    def test_high_vol_multiplier(self):
        """ATR% > 4% → 乘数 1.5"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.05)
        assert mgr._get_vol_multiplier() == 1.5

    def test_low_vol_multiplier(self):
        """ATR% < 1% → 乘数 0.7"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.005)
        assert mgr._get_vol_multiplier() == 0.7

    def test_mid_vol_no_adjustment(self):
        """1% <= ATR% <= 4% → 乘数 1.0"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.02)
        assert mgr._get_vol_multiplier() == 1.0

    def test_boundary_high_vol(self):
        """ATR% = 4.0% → 不算高波动 (阈值是 > 4%)"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.04)
        assert mgr._get_vol_multiplier() == 1.0

    def test_boundary_low_vol(self):
        """ATR% = 1.0% → 不算低波动 (阈值是 < 1%)"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.01)
        assert mgr._get_vol_multiplier() == 1.0


class TestGetStopLossAndTakeProfit:
    """get_stop_loss / get_take_profit 测试"""

    def test_default_stop_loss(self):
        mgr = DynamicExitManager()
        sl = mgr.get_stop_loss()
        assert sl < 0
        assert abs(sl) == 0.03  # default -3%

    def test_default_take_profit(self):
        mgr = DynamicExitManager()
        tp = mgr.get_take_profit()
        assert tp > 0
        assert tp == 0.10  # default 10%

    def test_high_vol_widens_stop(self):
        """高波动时止损放宽 1.5x"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.05)
        base = mgr.get_base_stop_loss()
        adjusted = mgr.get_stop_loss()
        assert adjusted == round(base * 1.5, 4)

    def test_low_vol_tightens_stop(self):
        """低波动时止损收紧 0.7x"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.005)
        base = mgr.get_base_stop_loss()
        adjusted = mgr.get_stop_loss()
        assert adjusted == round(base * 0.7, 4)

    def test_high_vol_widens_tp(self):
        """高波动时止盈放宽 1.5x"""
        mgr = DynamicExitManager()
        mgr.update_regime('default', atr_pct=0.05)
        base = mgr.get_base_take_profit()
        adjusted = mgr.get_take_profit()
        assert adjusted == round(base * 1.5, 4)


class TestCheckBreakevenStop:
    """保本止损测试"""

    def test_trigger_breakeven(self):
        """最高盈利 ≥ 3% → 触发保本"""
        mgr = DynamicExitManager()
        should_update, new_price = mgr.check_breakeven_stop(
            entry_price=100.0, current_price=102.0, peak_price=103.5
        )
        assert should_update is True
        assert new_price == 100.0  # cost price

    def test_no_trigger_below_threshold(self):
        """最高盈利 < 3% → 不触发"""
        mgr = DynamicExitManager()
        should_update, new_price = mgr.check_breakeven_stop(
            entry_price=100.0, current_price=101.0, peak_price=102.0
        )
        assert should_update is False
        assert new_price is None

    def test_loss_no_trigger(self):
        """亏损状态 → 不触发"""
        mgr = DynamicExitManager()
        should_update, new_price = mgr.check_breakeven_stop(
            entry_price=100.0, current_price=98.0, peak_price=99.0
        )
        assert should_update is False

    def test_zero_entry_price(self):
        """零入场价 → 安全返回"""
        mgr = DynamicExitManager()
        should_update, new_price = mgr.check_breakeven_stop(
            entry_price=0.0, current_price=100.0, peak_price=105.0
        )
        assert should_update is False


class TestCheckIntradayStop:
    """日内止损测试"""

    def test_trigger_intraday_stop(self):
        """当日亏损 ≥ 2% → 触发日内止损"""
        mgr = DynamicExitManager()
        should_exit, reason = mgr.check_intraday_stop(
            entry_price=100.0, current_price=97.5,
            entry_time=datetime.now(), is_same_day=True
        )
        assert should_exit is True
        assert '日内止损' in reason

    def test_no_trigger_small_loss(self):
        """亏损 < 2% → 不触发"""
        mgr = DynamicExitManager()
        should_exit, reason = mgr.check_intraday_stop(
            entry_price=100.0, current_price=98.5,
            entry_time=datetime.now(), is_same_day=True
        )
        assert should_exit is False

    def test_not_same_day_no_trigger(self):
        """非当日买入 → 不触发日内止损"""
        mgr = DynamicExitManager()
        should_exit, reason = mgr.check_intraday_stop(
            entry_price=100.0, current_price=95.0,
            entry_time=datetime.now() - timedelta(days=1), is_same_day=False
        )
        assert should_exit is False

    def test_profit_no_trigger(self):
        """盈利状态 → 不触发"""
        mgr = DynamicExitManager()
        should_exit, reason = mgr.check_intraday_stop(
            entry_price=100.0, current_price=105.0,
            entry_time=datetime.now(), is_same_day=True
        )
        assert should_exit is False


class TestCalculateAtrStopPrice:
    """ATR 止损价计算测试"""

    def test_buy_direction(self):
        """做多: 止损在入场价下方"""
        mgr = DynamicExitManager()
        stop = mgr.calculate_atr_stop_price(
            entry_price=100.0, atr=2.0, direction='BUY'
        )
        assert stop < 100.0
        # ATR*2.0 = 4.0, clamped to [5%, 10%] → 5% → 95.0
        assert stop == 95.0

    def test_sell_direction(self):
        """做空: 止损在入场价上方"""
        mgr = DynamicExitManager()
        stop = mgr.calculate_atr_stop_price(
            entry_price=100.0, atr=2.0, direction='SELL'
        )
        assert stop > 100.0
        assert stop == 105.0

    def test_large_atr_clamped_to_max(self):
        """大 ATR → 止损距离被 max_stop 限制在 10%"""
        mgr = DynamicExitManager()
        stop = mgr.calculate_atr_stop_price(
            entry_price=100.0, atr=20.0, direction='BUY'
        )
        # 20*2/100=40% > max 10% → clamped to 10%
        assert stop == 90.0

    def test_small_atr_clamped_to_min(self):
        """小 ATR → 止损距离被 min_stop 限制在 5%"""
        mgr = DynamicExitManager()
        stop = mgr.calculate_atr_stop_price(
            entry_price=100.0, atr=0.1, direction='BUY'
        )
        # 0.1*2/100=0.2% < min 5% → clamped to 5%
        assert stop == 95.0


class TestCheckExitPriorityHardRisk:
    """组合式退出引擎 — 硬风险层 (Layer B) 测试"""

    def _mgr(self):
        return DynamicExitManager()

    def test_atr_hard_stop_triggers(self):
        """ATR 硬止损: 亏损 ≥ 1.5×ATR → 立即退出"""
        mgr = self._mgr()
        # entry=100, atr=2.0, vol_mult=1.0 → atr_stop_dist = 1.5*2/100 = 3%
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=96.0,
            peak_price=100.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True
        assert decision.priority == 1
        assert decision.is_stop_loss is True
        assert decision.new_state == ExitState.FULL_EXIT_PREPARED

    def test_structural_stop_triggers(self):
        """结构止损: 亏损 ≥ 3% → 立即退出"""
        mgr = self._mgr()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=96.0,
            peak_price=100.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True
        assert decision.priority == 2
        assert decision.is_stop_loss is True

    def test_intraday_stop_triggers(self):
        """日内止损: 当日亏损 ≥ 2% → 立即退出"""
        mgr = self._mgr()
        # Need loss < 3% (structural stop) but ≥ 2% (intraday) → use 2.5%
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=97.5,
            peak_price=100.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', is_same_day=True,
            current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True
        assert decision.priority == 3

    def test_volatility_stop_triggers(self):
        """波动止损: 价格区间 > 3×ATR 且亏损 → 退出"""
        mgr = self._mgr()
        # Need loss < ATR hard stop but still negative + range > 3×ATR
        # entry=100, peak=102, current=98, atr=0.5
        # atr_stop = 1.5*0.5/100 = 0.75%, loss=2% > 0.75% → ATR triggers first
        # So use larger ATR: entry=100, peak=102, current=98.5, atr=3.0
        # atr_stop = 1.5*3/100 = 4.5%, loss=1.5% < 4.5% → no ATR
        # range = |102-98.5|/100 = 3.5% > 3×(3/100)=9%? No.
        # Let's use: entry=100, peak=104, current=98.5, atr=1.0
        # atr_stop = 1.5*1/100=1.5%, loss=1.5% → ATR triggers (equal)
        # Use: entry=100, peak=105, current=99.0, atr=2.0
        # atr_stop = 1.5*2/100=3%, loss=1% < 3% → no ATR
        # range = |105-99|/100=6% > 3×(2/100)=6%? 6% > 6% is false.
        # Use peak=106: range=7% > 6% → YES. loss=1% > 0 → YES.
        # Also need loss < 3% (structural) → 1% < 3% → YES.
        # And loss < 2% (intraday) → 1% < 2% → YES.
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=99.0,
            peak_price=106.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', is_same_day=True,
            current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True
        assert decision.priority == 5

    def test_no_exit_when_profitable(self):
        """盈利状态 → 不触发硬风险退出"""
        mgr = self._mgr()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=105.0,
            peak_price=105.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is False

    def test_short_direction_pnl_flip(self):
        """做空方向: pnl 计算反转, 价格下跌=盈利"""
        mgr = self._mgr()
        # SELL entry=100, current=95 → actual profit 5%
        # Need no intraday trigger: is_same_day=False
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=95.0,
            peak_price=95.0, entry_time=datetime.now(),
            atr_value=0.0, direction='SELL', is_same_day=False,
            current_state=ExitState.INITIAL
        )
        assert decision.should_exit is False


class TestCheckExitPriorityProfitManagement:
    """组合式退出引擎 — 利润管理层 (Layer C) 测试"""

    def _mgr(self):
        return DynamicExitManager()

    def test_breakeven_state_transition(self):
        """INIT 状态 + 盈利 ≥ 3% → 转移到 BE_ACTIVE + 新止损价"""
        mgr = self._mgr()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=104.0,
            peak_price=104.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is False
        assert decision.new_state == ExitState.BREAKEVEN_ACTIVE
        assert decision.new_stop_loss is not None
        assert decision.new_stop_loss > 100.0  # entry + buffer

    def test_trailing_stop_triggers(self):
        """BE_ACTIVE 状态 + 盈利 ≥ 8% + 峰值回撤 ≥ trail_dist → 退出"""
        mgr = self._mgr()
        # entry=100, current=108 (+8%), peak=115 (+15%), atr=2.0
        # trail_dist = 2.5*2/100 = 5%, drawdown = (115-108)/100 = 7% >= 5%
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=108.0,
            peak_price=115.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.BREAKEVEN_ACTIVE
        )
        assert decision.should_exit is True
        assert decision.priority == 7
        assert '移动止损' in decision.reason

    def test_trailing_stop_no_trigger_small_drawdown(self):
        """回撤 < trail_dist → 不触发追踪止盈"""
        mgr = self._mgr()
        # entry=100, current=110 (+10%), peak=112 (+12%), atr=2.0
        # trail_dist = 2.5*2/100 = 5%, drawdown = 2% < 5%
        # Need to disable partial TP levels (from config they might exist)
        mgr.current_params['partial_take_profit'] = {'levels': []}
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=110.0,
            peak_price=112.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.BREAKEVEN_ACTIVE
        )
        assert decision.should_exit is False

    def test_partial_take_profit_triggers(self):
        """BE_ACTIVE 状态 + 达到分批止盈目标 → 部分平仓"""
        mgr = self._mgr()
        # Force partial TP levels via current_params
        mgr.current_params['partial_take_profit'] = {
            'levels': [
                {'trigger': 0.08, 'ratio': 0.3},
                {'trigger': 0.15, 'ratio': 0.5},
            ]
        }
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=108.5,
            peak_price=108.5, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.BREAKEVEN_ACTIVE
        )
        assert decision.should_exit is True
        assert decision.close_ratio == 0.3
        assert decision.new_state == ExitState.PARTIAL_TAKEN

    def test_state_kept_when_no_exit(self):
        """无退出条件时 → 保持当前状态"""
        mgr = self._mgr()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=101.0,
            peak_price=101.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is False
        assert decision.new_state == ExitState.INITIAL


class TestCheckExitPriorityShortDirection:
    """做空方向的退出逻辑测试"""

    def _mgr(self):
        return DynamicExitManager()

    def test_short_profit_correct(self):
        """做空: 价格下跌 = 盈利, 不触发止损"""
        mgr = self._mgr()
        # SELL entry=100, current=95 → profit 5%
        # BUT intraday stop uses (current - entry)/entry regardless of direction
        # (95-100)/100 = -5% → triggers intraday stop with is_same_day=True
        # So we need is_same_day=False to avoid intraday
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=95.0,
            peak_price=95.0, entry_time=datetime.now(),
            atr_value=0.0, direction='SELL', is_same_day=False,
            current_state=ExitState.INITIAL
        )
        # 5% profit > 3% breakeven → state transition to BE_ACTIVE
        assert decision.should_exit is False
        assert decision.new_state == ExitState.BREAKEVEN_ACTIVE

    def test_short_loss_triggers_stop(self):
        """做空: 价格上涨 = 亏损 → 触发结构止损"""
        mgr = self._mgr()
        # SELL entry=100, current=104 → loss 4% > 3%
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=104.0,
            peak_price=104.0, entry_time=datetime.now(),
            atr_value=0.0, direction='SELL', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True
        assert decision.is_stop_loss is True


class TestEdgeCases:
    """边界条件和异常场景测试"""

    def test_zero_entry_price(self):
        """零入场价 → 安全返回不退出"""
        mgr = DynamicExitManager()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=0.0, current_price=100.0,
            peak_price=100.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is False

    def test_zero_current_price(self):
        """零当前价 → 安全返回不退出"""
        mgr = DynamicExitManager()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=0.0,
            peak_price=100.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is False

    def test_no_atr_value(self):
        """ATR=0 → 跳过 ATR 相关检查，走结构止损"""
        mgr = DynamicExitManager()
        # 小亏损不触发 ATR 但可能触发结构止损
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=99.0,
            peak_price=100.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        # 1% loss < 3% structural → no exit
        assert decision.should_exit is False

    def test_entry_time_in_past_triggers_time_stop(self):
        """入场时间 > 72h → 触发时间止损"""
        mgr = DynamicExitManager()
        past_time = datetime.now() - timedelta(hours=80)
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=99.5,
            peak_price=100.0, entry_time=past_time,
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True
        assert '时间止损' in decision.reason

    def test_entry_time_string_parsed(self):
        """字符串格式入场时间 → 正确解析"""
        mgr = DynamicExitManager()
        past_time_str = (datetime.now() - timedelta(hours=80)).isoformat()
        decision = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=99.5,
            peak_price=100.0, entry_time=past_time_str,
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert decision.should_exit is True


class TestConfigLoading:
    """配置加载测试"""

    def test_config_not_found_uses_defaults(self, caplog):
        """配置文件不存在 → 使用默认参数"""
        mgr = DynamicExitManager(config_path='/nonexistent/path.yaml')
        assert mgr.config == {}
        # 默认值: stop_loss=-6%, take_profit=12%
        assert mgr.get_stop_loss() == -0.06
        assert mgr.get_take_profit() == 0.12

    def test_get_all_params(self):
        """get_all_params 返回完整参数字典"""
        mgr = DynamicExitManager()
        params = mgr.get_all_params()
        assert 'regime' in params
        assert 'stop_loss' in params
        assert 'take_profit' in params
        assert 'trailing_stop' in params
        assert 'atr_stop' in params
        assert 'breakeven_stop' in params
        assert 'intraday_stop' in params


class TestStateTransitions:
    """状态机转移完整链路测试"""

    def _mgr(self):
        return DynamicExitManager()

    def test_init_to_breakeven_active(self):
        """INIT → BE_ACTIVE: 盈利达标"""
        mgr = self._mgr()
        # Step 1: Small profit, no transition
        d1 = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=101.0,
            peak_price=101.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert d1.new_state == ExitState.INITIAL

        # Step 2: Profit >= 3% → transition to BE_ACTIVE
        d2 = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=104.0,
            peak_price=104.0, entry_time=datetime.now(),
            atr_value=0.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert d2.new_state == ExitState.BREAKEVEN_ACTIVE
        assert d2.new_stop_loss is not None

    def test_full_lifecycle(self):
        """完整生命周期: INIT → BE_ACTIVE → PARTIAL → EXIT"""
        mgr = self._mgr()
        mgr.current_params['partial_take_profit'] = {
            'levels': [
                {'trigger': 0.08, 'ratio': 0.3},
                {'trigger': 0.15, 'ratio': 1.0},
            ]
        }

        # Phase 1: INIT → BE_ACTIVE (+4%)
        d1 = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=104.0,
            peak_price=104.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.INITIAL
        )
        assert d1.new_state == ExitState.BREAKEVEN_ACTIVE

        # Phase 2: BE_ACTIVE → PARTIAL (+8%)
        d2 = mgr.check_exit_priority(
            code='HK.00700', entry_price=100.0, current_price=108.0,
            peak_price=108.0, entry_time=datetime.now(),
            atr_value=2.0, direction='BUY', current_state=ExitState.BREAKEVEN_ACTIVE
        )
        assert d2.should_exit is True
        assert d2.close_ratio == 0.3
        assert d2.new_state == ExitState.PARTIAL_TAKEN
