"""DrawdownController 日内日亏门测试 (DEB-20260823 重写).

原文件测的总回撤门/回撤重置已随死机制删除 (用户裁"不值得", 见模块历史注记);
现只测存活面: record_trade_pnl 记账 + 日限熔断 + SQLite 重启恢复。
"""
import pytest
from app.risk import drawdown_control
from app.risk.drawdown_control import DrawdownController


@pytest.fixture(autouse=True)
def _seal_mode_aware_limit(monkeypatch):
    """密封模式感知日亏损限额: _check_limits 现从 TradingMode YAML 读 effective
    daily limit (simulate=5%), 会覆盖构造参数 max_daily=3%. 测试还原为用构造值."""
    monkeypatch.setattr(
        DrawdownController, "_get_effective_max_daily",
        lambda self: self.max_daily,
    )


@pytest.fixture()
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(drawdown_control, "_DB_PATH", str(tmp_path / "dd_state.db"))
    return DrawdownController(max_daily_drawdown=0.03, initial_baseline=100_000.0)


def test_no_baseline_skips_accounting(tmp_path, monkeypatch):
    """daily_start_assets<=0 (无人播种基准) 时记账跳过, 不误触发."""
    monkeypatch.setattr(drawdown_control, "_DB_PATH", str(tmp_path / "dd_state.db"))
    c = DrawdownController(max_daily_drawdown=0.03, initial_baseline=0.0)
    assert c.is_trading_allowed()
    c.record_trade_pnl(-5000.0, source="test")
    assert c.current_daily_dd_abs == 0.0
    assert c.is_trading_allowed()


def test_daily_drawdown_limit():
    """亏损累计到 4% (>3%) 触发日限熔断"""
    c = DrawdownController.__new__(DrawdownController)  # 绕过 DB 初始化
    c.max_daily = 0.03
    c.baseline = c.current_assets = c.daily_start_assets = 100_000.0
    c.current_daily_dd_abs = 0.0
    c.current_daily_dd = 0.0
    c.last_reset_date = None
    c.block_reason = None

    assert c.is_trading_allowed()

    # 亏 $2k (2%): 放行
    c.record_trade_pnl(-2000.0, source="test")
    assert c.is_trading_allowed()

    # 再亏 $2k (累计 4%): 熔断
    c.record_trade_pnl(-2000.0, source="test")
    assert not c.is_trading_allowed()
    assert c.get_block_reason() == "DAILY_DRAWDOWN"
    assert c.is_daily_limit_breached()


def test_profit_offsets_loss(controller):
    """盈利对冲同日先前亏损 (净额口径)"""
    controller.record_trade_pnl(-2000.0, source="test")
    controller.record_trade_pnl(+500.0, source="test")
    assert controller.current_daily_dd_abs == pytest.approx(1500.0)
    assert controller.current_daily_dd == pytest.approx(0.015)


def test_state_survives_restart(tmp_path, monkeypatch):
    """P0-fix 契约: 已实现亏损跨进程重启不归零 (防重启绕过日限)."""
    db = str(tmp_path / "dd_state.db")
    monkeypatch.setattr(drawdown_control, "_DB_PATH", db)

    c1 = DrawdownController(max_daily_drawdown=0.03, initial_baseline=100_000.0)
    c1.record_trade_pnl(-2500.0, source="test")
    assert c1.current_daily_dd_abs == pytest.approx(2500.0)

    # 模拟重启: 新实例从同一 DB 恢复
    c2 = DrawdownController(max_daily_drawdown=0.03, initial_baseline=100_000.0)
    assert c2.daily_start_assets == pytest.approx(100_000.0)
    assert c2.current_daily_dd_abs == pytest.approx(2500.0)


def test_deleted_machinery_stays_deleted():
    """契约守卫 (DEB-20260823): 死总回撤机制不得复活 — 复活须先过毕业门禁 checklist."""
    import inspect

    src = inspect.getsource(drawdown_control)
    for banned in ("def update_assets", "def set_baseline", "def reset_daily_baseline",
                   "def update_drawdown", "def get_status", "def get_baseline_history",
                   "class DrawdownBaselineRecord", '"TOTAL_DRAWDOWN"',
                   "max_total_drawdown="):
        assert banned not in src, f"死机制 {banned} 重新出现于 drawdown_control.py"
