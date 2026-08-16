"""DEB-20260816-fullchain 批次C 契约测试 (P1-12): 回撤自动重置只允许入金方向.

修复前: max(cur/base, base/cur) 对称判定 → 大亏损 (>80%) 也触发 set_baseline →
dd 双清零 + 熔断复位 (fail-open 背门)。修复后: 仅 current > baseline×5 自动重置。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.risk import drawdown_control  # noqa: E402


@pytest.fixture()
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(drawdown_control, "_DB_PATH", str(tmp_path / "dd_state.db"))
    return drawdown_control.DrawdownController(
        max_daily_drawdown=0.03, max_total_drawdown=0.08, initial_baseline=100_000.0
    )


def test_p1_12_catastrophic_loss_does_not_reset(controller):
    # 亏损 85% (base/cur ≈ 6.7x > 5x): 修复前会自动重置基准拆熔断
    controller.update_assets(15_000.0)
    assert controller.baseline == 100_000.0
    assert controller.current_total_dd == pytest.approx(0.85)
    assert controller.block_reason is not None  # 熔断保持触发, 不被自动重置拆除


def test_p1_12_large_deposit_resets(controller):
    controller.update_assets(600_000.0)  # 入金 6x > 5x
    assert controller.baseline == 600_000.0
    assert controller.current_total_dd == 0.0


def test_p1_12_moderate_loss_keeps_normal_accounting(controller):
    controller.update_assets(95_000.0)
    assert controller.baseline == 100_000.0
    assert controller.current_daily_dd == pytest.approx(0.05)
    assert controller.current_total_dd == pytest.approx(0.05)
