"""交易模式回归测试 - 验证模拟/实盘切换安全性

2026 更新:
- TradingMode 已演进为从 .env.us 热加载 (环境变量仅作 fallback),
  测试通过 monkeypatch _ENV_FILE 指向不存在路径, 强制走 os.environ 分支, 保持密封.
- scripts/live_readiness_check.py 已退役, TestLiveReadinessCheck 随之删除.
"""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def isolated_trading_mode(monkeypatch):
    """隔离 .env.us 热加载, 让 TradingMode 只读 os.environ; 前后重置单例"""
    from app.core import trading_mode
    monkeypatch.setattr(trading_mode, "_ENV_FILE", Path("/nonexistent/.env.us"))
    trading_mode.TradingMode._instance = None
    yield trading_mode
    trading_mode.TradingMode._instance = None


class TestTradingMode:
    """测试 TradingMode 管理器"""

    def test_default_is_simulate(self, isolated_trading_mode, monkeypatch):
        """默认应为模拟模式"""
        trading_mode = isolated_trading_mode
        monkeypatch.setenv('TRADE_ENV', '1')
        tm = trading_mode.TradingMode()
        assert tm.is_simulate
        assert not tm.is_live
        assert tm.mode_label == '模拟盘'

    def test_risk_multiplier_simulate(self, isolated_trading_mode, monkeypatch):
        """模拟盘风控倍数应为1.0"""
        trading_mode = isolated_trading_mode
        monkeypatch.setenv('TRADE_ENV', '1')
        tm = trading_mode.TradingMode()
        assert tm.get_risk_multiplier() == 1.0

    def test_conservative_mode_restricts(self, isolated_trading_mode, monkeypatch):
        """保守模式应限制仓位

        [DEB-fullchain 2026-08-16] P0-1 (commit 2b82de00) 后判定源 TRADE_ENV →
        AUTO_TRADE_ACCOUNT+ALLOW_REAL_TRADING; 本测试遗留旧语义 → 全量回归恒红。
        更新为 P0-1 契约 (与 §十 契约测试 8/8 同口径)。"""
        trading_mode = isolated_trading_mode
        monkeypatch.setenv('AUTO_TRADE_ACCOUNT', 'real')
        monkeypatch.setenv('ALLOW_REAL_TRADING', 'true')
        monkeypatch.setenv('LIVE_CONSERVATIVE', '1')
        tm = trading_mode.TradingMode()

        assert tm.is_live
        assert tm.is_conservative
        assert tm.get_risk_multiplier() == 0.5
        assert tm.get_max_position_size(10000) == 5000
