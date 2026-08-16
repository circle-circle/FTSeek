# -*- coding: utf-8 -*-
"""TradingMode 模式判定契约测试 (2026-08-16, DEB-fullchain P0-1).

契约: 模式判定与 di_container.py broker 路由同源 (AUTO_TRADE_ACCOUNT +
ALLOW_REAL_TRADING 安全门), 不再消费已废弃的 TRADE_ENV。

背景: 6/24 重构把 broker 路由迁到 AUTO_TRADE_ACCOUNT, 但 TradingMode 仍读
TRADE_ENV (恒=1) → 切实盘后日亏熔断/限额缩放/净值 fail-close 三重失效。

修复: _detect_mode 改读 AUTO_TRADE_ACCOUNT + ALLOW_REAL_TRADING。
本测试锁定: 四态判定矩阵 + 安全门降级 + TRADE_ENV 不再影响模式。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import patch

from app.core import trading_mode as tm_mod
from app.core.trading_mode import TradingMode, TradingModeEnum


def _env_map(**kv):
    """构造 _read_env_file 的 side_effect: 从 kv dict 读取, 缺失返回默认."""
    def _fake(key, default=""):
        return kv.get(key, default)
    return _fake


@pytest.fixture
def fresh_tm():
    """每个用例独立实例 (绕过单例缓存)."""
    tm = TradingMode.__new__(TradingMode)
    tm._initialized = False
    return tm


class TestModeDetectionContract:
    """P0-1 契约: AUTO_TRADE_ACCOUNT 驱动, 与 broker 路由同源."""

    def test_sim_default(self, fresh_tm):
        """AUTO_TRADE_ACCOUNT=sim → SIMULATE (当前生产态, 零行为变化)."""
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="sim", ALLOW_REAL_TRADING="true")):
            assert fresh_tm.mode == TradingModeEnum.SIMULATE
            assert fresh_tm.is_simulate and not fresh_tm.is_live

    def test_real_with_allow_conservative(self, fresh_tm):
        """real + ALLOW_REAL_TRADING=true + LIVE_CONSERVATIVE=1 → LIVE_CONSERVATIVE."""
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="real", ALLOW_REAL_TRADING="true",
                                   LIVE_CONSERVATIVE="1")):
            assert fresh_tm.mode == TradingModeEnum.LIVE_CONSERVATIVE
            assert fresh_tm.is_live and fresh_tm.is_conservative

    def test_real_with_allow_normal(self, fresh_tm):
        """real + ALLOW_REAL_TRADING=true + LIVE_CONSERVATIVE=0 → LIVE_NORMAL."""
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="real", ALLOW_REAL_TRADING="true",
                                   LIVE_CONSERVATIVE="0")):
            assert fresh_tm.mode == TradingModeEnum.LIVE_NORMAL
            assert fresh_tm.is_live and not fresh_tm.is_conservative

    def test_real_without_allow_degrades(self, fresh_tm):
        """安全门: real 但 ALLOW_REAL_TRADING!=true → 强制 SIMULATE
        (与 di_container.py L145-152 安全拦截同源)."""
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="real", ALLOW_REAL_TRADING="false",
                                   LIVE_CONSERVATIVE="0")):
            assert fresh_tm.mode == TradingModeEnum.SIMULATE

    def test_trade_env_no_longer_affects_mode(self, fresh_tm):
        """回归保护: TRADE_ENV=0 (旧实盘信号) 不再触发 LIVE —
        TRADE_ENV 仅剩 DB 路由职责."""
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="sim", ALLOW_REAL_TRADING="true",
                                   TRADE_ENV="0", LIVE_CONSERVATIVE="0")):
            assert fresh_tm.mode == TradingModeEnum.SIMULATE

    def test_missing_env_defaults_to_sim(self, fresh_tm):
        """缺省安全: 空 env → SIMULATE (fail-safe)."""
        with patch.object(tm_mod, "_read_env_file", _env_map()):
            assert fresh_tm.mode == TradingModeEnum.SIMULATE

    def test_live_risk_thresholds_differ(self, fresh_tm):
        """实盘风控参数确实收紧 (P0-1 的资金影响面验证):
        live_conservative 日亏熔断 2% < simulate 5%; 仓位乘数 0.5 < 1.0."""
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="sim", ALLOW_REAL_TRADING="true")):
            sim_daily = fresh_tm.get_max_daily_loss_pct()
            sim_mult = fresh_tm.get_risk_multiplier()
        with patch.object(tm_mod, "_read_env_file",
                          _env_map(AUTO_TRADE_ACCOUNT="real", ALLOW_REAL_TRADING="true",
                                   LIVE_CONSERVATIVE="1")):
            live_daily = fresh_tm.get_max_daily_loss_pct()
            live_mult = fresh_tm.get_risk_multiplier()
        assert live_daily < sim_daily, "实盘日亏熔断必须严于模拟盘"
        assert live_mult < sim_mult, "实盘仓位乘数必须收紧"

    def test_db_routing_untouched(self):
        """DB 路由仍由 TRADE_ENV 独立控制 (db_utils.is_live_mode), 与模式判定解耦."""
        from app.core import db_utils
        import inspect
        src = inspect.getsource(db_utils.is_live_mode)
        assert "TRADE_ENV" in src, "db_utils.is_live_mode 必须仍读 TRADE_ENV (DB 路由职责)"
