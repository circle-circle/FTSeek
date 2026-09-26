"""Tests for scripts/premarket_peer_shadow.py 非交易日守卫.

2026-09-26 实测缺口回归: cron `5 21 * * 1-5` 只按星期排, **不含美股假日** ⇒
2026-09-07 (Labor Day, 休市) 仍采集并落盘 1 行陈旧快照 (last/prev_close/volume_ratio
与 9/8 逐只完全相同 — 休市日不可能有成交量比)。注册表计数轴是 `distinct:date`,
该假行虚增一个"交易日" ⇒ 污染峰群一致性统计与采样进度。

Hermetic: 不连 futu/OpenD、不落生产 jsonl、不推送 (只测守卫与判定函数)。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import scripts.premarket_peer_shadow as mod


@pytest.fixture(autouse=True)
def _reload():
    importlib.reload(mod)
    yield


class TestTradingDayGuard:
    """_is_trading_day_et: 假日/周末 False, 交易日 True, 日历不可用 fail-open."""

    def test_labor_day_is_not_trading_day(self):
        """2026-09-07 = Labor Day (实测已污染生产 jsonl 的那一天)."""
        assert mod._is_trading_day_et("2026-09-07") is False

    def test_weekend_is_not_trading_day(self):
        assert mod._is_trading_day_et("2026-09-26") is False  # Sat
        assert mod._is_trading_day_et("2026-09-27") is False  # Sun

    def test_normal_weekday_is_trading_day(self):
        assert mod._is_trading_day_et("2026-09-25") is True   # Fri
        assert mod._is_trading_day_et("2026-09-08") is True   # Tue (节后首日)

    def test_thanksgiving_is_not_trading_day(self):
        assert mod._is_trading_day_et("2026-11-26") is False

    def test_fail_open_when_calendar_broken(self, caplog):
        """日历抛异常 ⇒ 继续采集 (宁多采一天, 不漏采交易日), 且**必须留痕**.

        fail-open 静默吞异常 = 影子/采集静默死亡的根因: 日历挂掉时所有采集脚本会
        无声地在假日照采, 又回到本守卫要修的污染态。故断言 warning 落日志。
        """
        import logging
        with patch("app.utils.holiday_calendar.get_holiday_calendar",
                   side_effect=RuntimeError("calendar down")):
            with caplog.at_level(logging.WARNING, logger="ftseek.trading_calendar"):
                assert mod._is_trading_day_et("2026-09-25") is True
        assert "判定失败" in caplog.text, "fail-open 未留痕 ⇒ 日历挂了会无声污染"


class TestMainSkipsNonTradingDay:
    """main() 源头拦: 非交易日不采集、不落盘、不推送."""

    def test_main_skips_and_writes_nothing(self, tmp_path, capsys):
        out = tmp_path / "premarket_peer_shadow.jsonl"
        with patch.object(mod, "SHADOW_FILE", out), \
             patch.object(mod, "_today_et", return_value="2026-09-07"), \
             patch.object(mod, "_collect") as m_collect, \
             patch.object(mod, "_upsert") as m_upsert, \
             patch.object(mod, "_report", return_value="REPORT"), \
             patch("app.notifier.feishu.send_text") as m_send:
            mod.main(push=True)

        m_collect.assert_not_called()
        m_upsert.assert_not_called()
        m_send.assert_not_called()
        assert not out.exists(), "非交易日仍落盘 ⇒ 假样本会污染 distinct:date 计数轴"
        assert "非美股交易日" in capsys.readouterr().out

    def test_main_proceeds_on_trading_day(self, tmp_path, capsys):
        """阳性对照: 交易日必须照常采集 (守卫不得一刀切封杀整条链路)."""
        out = tmp_path / "premarket_peer_shadow.jsonl"
        fake_rec = {"date": "2026-09-25", "group_verdict": "NO_CONFIRM_LOWVOL_UP"}
        with patch.object(mod, "SHADOW_FILE", out), \
             patch.object(mod, "_today_et", return_value="2026-09-25"), \
             patch.object(mod, "_collect", return_value=fake_rec) as m_collect, \
             patch.object(mod, "_upsert") as m_upsert, \
             patch.object(mod, "_report", return_value="REPORT"):
            mod.main(push=False)

        m_collect.assert_called_once_with("2026-09-25")
        m_upsert.assert_called_once_with(fake_rec)
