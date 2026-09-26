"""非交易日采集守卫回归 — borrow_rate_snapshot / sector_pulse.

2026-09-26 实证: cron 只按星期排 (`5 21 * * 1-5` / `*/30 22-23 * * 1-5`), **不含美股假日** ⇒
2026-09-07 (Labor Day, 休市) 两脚本各落了 36 / 12 行陈旧快照:
  - borrow_rate_series: 36 行中 **35 行费率与 9/4 完全相同** (休市无新报价), 纯 append ⇒
    永久留在时序里, 被 `load_borrow_rates()` 当"最近一条"返回给 UE 预览。
  - sector_pulse_history: 12 行 changes 全为 ±0.01 陈旧报价, 污染 ③板块 veto 攒料。
守卫走 SSOT `is_trading_day_str` (不在脚本内复制判定块), 源头拦 ⇒ 零落盘。

Hermetic: 不连 futu/OpenD、不落生产 jsonl、不推送。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import scripts.borrow_rate_snapshot as brs
import scripts.sector_pulse as sp


@pytest.fixture(autouse=True)
def _reload():
    importlib.reload(brs)
    importlib.reload(sp)
    yield


# ── borrow_rate_snapshot ───────────────────────────────────────────────

class TestBorrowRateGuard:
    """非交易日不采集不落盘; 交易日照常 (阳性对照)."""

    def _run(self, tmp_path, fake_date, monkeypatch_target="scripts.borrow_rate_snapshot.now_et"):
        out = tmp_path / "borrow_rate_series.jsonl"
        # 脚本读 BASE_DIR/"data"/"daily_active_pool.json" — fixture 必须同层级
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        pool = tmp_path / "data" / "daily_active_pool.json"
        pool.write_text('{"entries": [{"code": "US.AAPL"}]}', encoding="utf-8")
        fake_now = MagicMock()
        fake_now.strftime.return_value = fake_date
        with patch(monkeypatch_target, return_value=fake_now), \
             patch.object(brs, "OUT_PATH", out), \
             patch.object(brs, "BASE_DIR", tmp_path), \
             patch("app.execution.dual_broker.get_dual_broker") as m_broker, \
             patch.object(brs, "setup_logger", return_value=MagicMock()):
            rc = brs.main()
        return rc, out, m_broker

    def test_labor_day_skips_without_fetch(self, tmp_path):
        """9/7 Labor Day: 不得调券商接口, 不得落盘 (已实证污染的那一天)."""
        rc, out, m_broker = self._run(tmp_path, "2026-09-07")
        assert rc == 0
        m_broker.assert_not_called()
        assert not out.exists(), "假日仍落盘 ⇒ 陈旧费率永久进时序, 被 load_borrow_rates 当最近一条"

    def test_saturday_skips(self, tmp_path):
        rc, out, m_broker = self._run(tmp_path, "2026-09-26")
        assert rc == 0
        m_broker.assert_not_called()
        assert not out.exists()

    def test_trading_day_proceeds(self, tmp_path):
        """阳性对照: 交易日必须照常取费率并落盘 (守卫不得封杀整条链)."""
        rc, out, m_broker = self._run(tmp_path, "2026-09-25")
        m_broker.assert_called_once()          # 走到了取数
        assert m_broker.return_value.get_short_pool.called


# ── sector_pulse ───────────────────────────────────────────────────────

class TestSectorPulseGuard:
    """非交易日不取行情不落盘; 交易日照常 (阳性对照)."""

    def _run(self, tmp_path, fake_date):
        hist = tmp_path / "sector_pulse_history.jsonl"
        fake_now = MagicMock()
        fake_now.strftime.side_effect = lambda f: fake_date if "%Y" in f else "10:00"
        with patch.object(sp, "now_et", return_value=fake_now), \
             patch.object(sp, "HIST", hist), \
             patch("app.data.market_data_router.MarketDataRouter") as m_router:
            rc = sp.main()
        return rc, hist, m_router

    def test_labor_day_skips_without_quote(self, tmp_path):
        """9/7: 不得实例化行情 router (休市日报价是陈旧的 ±0.01)."""
        rc, hist, m_router = self._run(tmp_path, "2026-09-07")
        assert rc == 0
        m_router.assert_not_called()
        assert not hist.exists(), "假日仍落盘 ⇒ 陈旧板块脉冲污染 veto 攒料"

    def test_saturday_skips(self, tmp_path):
        rc, hist, m_router = self._run(tmp_path, "2026-09-26")
        assert rc == 0
        m_router.assert_not_called()
        assert not hist.exists()

    def test_trading_day_proceeds(self, tmp_path):
        """阳性对照: 交易日必须照常取行情."""
        rc, hist, m_router = self._run(tmp_path, "2026-09-25")
        m_router.assert_called_once()
