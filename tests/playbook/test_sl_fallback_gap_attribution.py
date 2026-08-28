"""止损兜底的跳空归因 (2026-08-28 全链路复查).

缺陷: monitor_playbook_sim_exits 的 SL 兜底跑在 _check_playbook_exit_rules 规则2
(OVERNIGHT_GAP_STOP) 之前, 于是跳空打穿止损的隔夜仓全部记成 SL_INITIAL —
生产实测 OVERNIGHT_GAP_STOP 全史 0 笔, 而 30 天内 9 笔 SL_INITIAL 有 5 笔是隔夜仓。
影响全在观测/学习侧: 持夜跳空风险被算进"止损纪律", 标的档案据此注入
"LONG+SL_INITIAL 亏钱模式"教训, DEB 的持夜盈亏分解少一整类。

本组测试锁: ①归因判据与规则2 同尺 (隔夜 + 达 horizon overnight_gap_stop_pct);
②行为中性 — 两个 reason 同在再入场禁入集合 _HARD_STOP_REASONS。

回滚: 把 pipeline 兜底处改回字面量 "SL_INITIAL" 即恢复旧归因 ( wiring 测试会红)。
"""

import inspect
import os
import sys
from datetime import timedelta

sys.path.insert(0, str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))))

from app.playbook import pipeline as pb  # noqa: E402
from app.playbook.intraday_reentry import _HARD_STOP_REASONS  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402


def _entry_str(days_ago: int) -> str:
    return (now_et() - timedelta(days=days_ago)).isoformat()


def _patch_gap(monkeypatch, value_by_horizon):
    def _fake(horizon):
        return {"overnight_gap_stop_pct": float(value_by_horizon.get(horizon, 3.0))}
    monkeypatch.setattr(pb, "_load_horizon_config", _fake)


def test_overnight_gap_through_stop_is_not_sl_initial(monkeypatch):
    """隔夜仓 -5% 打穿 SL (阈值 3%) → 归因跳空, 不再冒充"初始止损被打到"."""
    _patch_gap(monkeypatch, {"short": 3.0})
    assert pb._sl_fallback_exit_reason(-5.0, _entry_str(2), "short") == "OVERNIGHT_GAP_STOP"


def test_intraday_stop_hit_stays_sl_initial(monkeypatch):
    """当日入场即使 -5% 也不是跳空 (规则2 同样不触发) → 保持 SL_INITIAL."""
    _patch_gap(monkeypatch, {"short": 3.0})
    assert pb._sl_fallback_exit_reason(-5.0, _entry_str(0), "short") == "SL_INITIAL"


def test_shallow_overnight_loss_stays_sl_initial(monkeypatch):
    """隔夜但亏损未达跳空线 (-1% < 3%) → 就是普通止损被扫, 保持 SL_INITIAL."""
    _patch_gap(monkeypatch, {"short": 3.0})
    assert pb._sl_fallback_exit_reason(-1.0, _entry_str(1), "short") == "SL_INITIAL"


def test_threshold_follows_horizon_config(monkeypatch):
    """尺子与规则2 同源: long horizon 阈值 5% 时, 隔夜 -4% 不算跳空桶."""
    _patch_gap(monkeypatch, {"short": 3.0, "long": 5.0})
    assert pb._sl_fallback_exit_reason(-4.0, _entry_str(1), "long") == "SL_INITIAL"
    assert pb._sl_fallback_exit_reason(-4.0, _entry_str(1), "short") == "OVERNIGHT_GAP_STOP"


def test_boundary_exactly_at_threshold_is_gap(monkeypatch):
    """边界: 恰好 -3.0% 按规则2 的 <= 语义归跳空桶."""
    _patch_gap(monkeypatch, {"short": 3.0})
    assert pb._sl_fallback_exit_reason(-3.0, _entry_str(1), "short") == "OVERNIGHT_GAP_STOP"


def test_config_failure_falls_back_to_old_label(monkeypatch):
    """读配置失败不得让兜底链抛错或凭空造新桶 → 退回 SL_INITIAL."""
    def _boom(horizon):
        raise RuntimeError("config down")
    monkeypatch.setattr(pb, "_load_horizon_config", _boom)
    assert pb._sl_fallback_exit_reason(-9.0, _entry_str(3), "short") == "SL_INITIAL"


def test_bad_entry_time_falls_back_to_old_label(monkeypatch):
    _patch_gap(monkeypatch, {"short": 3.0})
    assert pb._sl_fallback_exit_reason(-9.0, "", "short") == "SL_INITIAL"
    assert pb._sl_fallback_exit_reason(-9.0, "not-a-date", "short") == "SL_INITIAL"


def test_reentry_gate_treats_both_reasons_identically():
    """行为中性契约: 换标签不能改再入场结论 (两值必须同在禁入集合)."""
    assert "SL_INITIAL" in _HARD_STOP_REASONS
    assert "OVERNIGHT_GAP_STOP" in _HARD_STOP_REASONS


def test_fallback_call_site_uses_the_attribution_helper():
    """防"抽了函数忘了接线": 兜底记账必须走 _sl_fallback_exit_reason."""
    src = inspect.getsource(pb.monitor_playbook_sim_exits)
    assert "_sl_fallback_exit_reason(" in src
    assert '_close_trade_record(conn, r["id"], _exit_price, "SL_INITIAL"' not in src, \
        "兜底处仍硬编码 SL_INITIAL — 跳空归因未接线"
