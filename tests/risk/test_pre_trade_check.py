# tests/risk/test_pre_trade_check.py
import pytest
from app.risk.pre_trade_check import PreTradeChecker
from app.core.models import SignalScore, Direction


@pytest.fixture(autouse=True)
def _stub_pdt_guard(monkeypatch):
    """密封 PDT 保护 (P0 2026-04-21 新增): get_pdt_guard 是全局单例,
    读真实日内交易计数 — 实盘状态会让测试随机 PDT_BLOCKED. 测试放行."""
    class _StubPDT:
        def update_equity(self, equity):
            pass

        def check_trade_permission(self, is_overnight=False):
            return True, "", "low"

    monkeypatch.setattr("app.risk.pre_trade_check.get_pdt_guard", lambda: _StubPDT())


def test_pre_trade_check_allows_good_signal():
    """测试预交易检查：好信号应该通过"""
    checker = PreTradeChecker()

    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )

    result = checker.check(signal)
    assert result.allowed == True
    assert result.reason is None


def test_pre_trade_check_rejects_low_confidence():
    """测试预交易检查：低置信度应该拒绝"""
    checker = PreTradeChecker(min_confidence=0.60)

    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=75,  # 需过 RFC058 strength 硬地板 (50 会先被 SIGNAL_TOO_WEAK 拒)
        confidence=0.40,  # 低于阈值
    )

    result = checker.check(signal)
    assert result.allowed == False
    assert "LOW_CONFIDENCE" in result.reason
