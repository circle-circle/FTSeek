"""
SymbolDailyLossGuard 单测 (P0-X 2026-05-15)

基于 14 日 LCID 8 笔 -$634 的真实数据触发场景
"""
import pytest


def test_daily_guard_consecutive_losses_triggers():
    """连续 3 笔亏损触发 cooldown"""
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    g = SymbolDailyLossGuard(max_consecutive_losses=3)
    g.record_trade("LCID", -50)
    g.record_trade("LCID", -47)
    blocked, _ = g.is_blocked("LCID")
    assert not blocked  # 2 笔还没到
    g.record_trade("LCID", -25)
    blocked, reason = g.is_blocked("LCID")
    assert blocked
    assert "consecutive" in reason


def test_daily_guard_reset_on_win():
    """中间一笔盈利 → 连亏计数清零"""
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    # 关掉 win_rate 检查避免干扰本测试
    g = SymbolDailyLossGuard(max_consecutive_losses=3, min_trades_for_winrate_check=999)
    g.record_trade("NVDA", -10)
    g.record_trade("NVDA", -10)
    g.record_trade("NVDA", +50)  # 盈利打断
    g.record_trade("NVDA", -10)
    g.record_trade("NVDA", -10)
    blocked, _ = g.is_blocked("NVDA")
    assert not blocked  # 不连续, 不触发


def test_daily_guard_total_loss_pct_triggers():
    """累计亏损 > 1% 净值触发"""
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    g = SymbolDailyLossGuard(max_daily_loss_pct=0.01, max_consecutive_losses=999)
    g.set_equity_provider(lambda: 100000)
    g.record_trade("LCID", -500)
    blocked, _ = g.is_blocked("LCID")
    assert not blocked  # 0.5% 没到
    g.record_trade("LCID", -600)  # 累计 -1100 = 1.1%
    blocked, reason = g.is_blocked("LCID")
    assert blocked
    assert "loss_pct" in reason


def test_daily_guard_loss_rate_triggers():
    """5+ 笔且亏损率 > 70% 触发"""
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    g = SymbolDailyLossGuard(
        max_consecutive_losses=999,
        min_trades_for_winrate_check=5,
        max_loss_rate=0.7,
    )
    # 5 笔 4 亏 1 赢 → 80% 亏损率
    g.record_trade("LCID", -10)
    g.record_trade("LCID", +20)
    g.record_trade("LCID", -10)
    g.record_trade("LCID", -10)
    g.record_trade("LCID", -10)
    blocked, reason = g.is_blocked("LCID")
    assert blocked
    assert "loss_rate" in reason


def test_daily_guard_other_symbol_not_blocked():
    """LCID cooldown, NVDA 不受影响"""
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    g = SymbolDailyLossGuard(max_consecutive_losses=2)
    g.record_trade("LCID", -10)
    g.record_trade("LCID", -10)
    assert g.is_blocked("LCID")[0]
    assert not g.is_blocked("NVDA")[0]


def test_daily_guard_exempt_symbols():
    """豁免标的不被阻塞"""
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    g = SymbolDailyLossGuard(max_consecutive_losses=2)
    g.set_exempt(["LCID"])
    g.record_trade("LCID", -10)
    g.record_trade("LCID", -10)
    g.record_trade("LCID", -10)
    blocked, _ = g.is_blocked("LCID")
    assert not blocked


def test_daily_guard_snapshot():
    from app.risk.symbol_daily_loss_guard import SymbolDailyLossGuard
    g = SymbolDailyLossGuard()
    g.record_trade("NVDA", +100)
    g.record_trade("LCID", -50)
    snap = g.snapshot()
    assert len(snap) == 2
    nvda = next(s for s in snap if s["symbol"] == "NVDA")
    assert nvda["total_pnl"] == 100
    assert nvda["wins"] == 1


def test_pre_trade_check_integrates_daily_loss_guard():
    """PreTradeChecker._check 增加了 SYMBOL_DAILY_LOSS 步"""
    src = open("app/risk/pre_trade_check.py").read()
    assert "symbol_daily_loss_guard" in src
    assert "SYMBOL_DAILY_LOSS_GUARD" in src
    assert "SYMBOL_DAILY_LOSS" in src


def test_scalping_engine_integrates_hedge_guard():
    """ScalpingEngine._place_order 加了 broker hedge 检查"""
    src = open("app/scalping/engine.py").read()
    assert "hedge_guard" in src
    assert "无法同时买多" not in src or "broker.get_positions" in src
