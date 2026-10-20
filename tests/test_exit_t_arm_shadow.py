"""RFC-154 T 臂 (adaptive trailing min DD 0.4→0.8) 影子契约。

钉住:
  ① replay_trade 的可选 adaptive_cfg 真的走 adaptive trailing 公式 (默认 None 不变);
  ② T 臂在「峰值 1% 后回撤 0.7%」路径上不被 0.4% 地板扫出, baseline 被扫出;
  ③ 只读 bar 缓存加载 (缺 entry day / 不足 5 根 ⇒ None);
  ④ delta / 误杀 / gate 口径。
"""
from __future__ import annotations

import json
from datetime import datetime

import scripts.exit_replay_backtest as erb
import scripts.exit_t_arm_shadow as arm


def _bars():
    et = erb.ET
    return [
        {"t": et.localize(datetime(2026, 10, 20, 9, 51)), "o": 100.0, "h": 101.0,
         "l": 100.0, "c": 100.5},
        {"t": et.localize(datetime(2026, 10, 20, 9, 52)), "o": 100.5, "h": 100.6,
         "l": 100.3, "c": 100.4},
        {"t": et.localize(datetime(2026, 10, 20, 9, 53)), "o": 100.4, "h": 101.2,
         "l": 100.4, "c": 101.0},
        {"t": et.localize(datetime(2026, 10, 20, 9, 54)), "o": 101.0, "h": 101.0,
         "l": 100.7, "c": 100.9},
    ]


def _tr():
    et = erb.ET
    return {
        "id": 1, "symbol": "US.TEST", "direction": "LONG",
        "entry_et": et.localize(datetime(2026, 10, 20, 9, 50)),
        "exit_et": et.localize(datetime(2026, 10, 20, 16, 0)),
        "entry": 100.0, "live_exit": 100.0, "qty": 100.0, "live_pnl": 0.0,
        "live_reason": "", "live_mfe": 0.0, "config_version": "",
        "decision_id": "", "meta_config_version": "",
        "initial_sl": 98.0, "take_profit": 0.0, "conditions": [],
    }


def _params():
    return {
        "fee_cushion_pct": 0.15,
        "profit_override_threshold": 1.5,
        "profit_override_dd": 0.5,
        "cooldown_sl_pct": 4.0,
        "overnight_gap_stop_pct": 3.0,
        "stall_minutes": 150,
        "stall_mfe_pct": 0.5,
        "min_tp_rr": 1.5,
    }


def _exit_cfg():
    return {
        "breakeven_at_pct": 0.4,
        "trailing_enabled": True,
        "drawdown_from_peak_pct": 0.8,
        "time_exit_hours": 6.0,
        "time_exit_only_if_loss": True,
    }


def test_t_arm_holds_through_retrace_baseline_stops():
    """峰值 1% 后回撤 0.7%: baseline 0.4% 地板被扫, T 0.8% 地板不被扫。"""
    base = erb.replay_trade(
        _tr(), _bars(), _exit_cfg(), {"name": "adaptive_live"}, _params(), "pess", 3.0,
        adaptive_cfg=arm.BASE_ADAPTIVE)
    t_arm = erb.replay_trade(
        _tr(), _bars(), _exit_cfg(), {"name": "t_arm"}, _params(), "pess", 3.0,
        adaptive_cfg=arm.T_ADAPTIVE)
    assert base["reason"] == "SL_TRAIL_LOCK"
    assert t_arm["reason"] == "REPLAY_EOD"
    assert t_arm["exit_price"] > base["exit_price"], (base, t_arm)


def test_default_adaptive_cfg_is_none_and_preserves_static_path():
    """现有调用不传 adaptive_cfg ⇒ 走原静态 dd; 不因 T 臂改动而换语义。"""
    default = erb.replay_trade(
        _tr(), _bars(), _exit_cfg(), {"name": "live_baseline"}, _params(), "pess", 3.0)
    adaptive = erb.replay_trade(
        _tr(), _bars(), _exit_cfg(), {"name": "adaptive_live"}, _params(), "pess", 3.0,
        adaptive_cfg=arm.BASE_ADAPTIVE)
    # 本路径静态 dd=0.8 与 adaptive min 0.4 在峰值 1% 时分别给出 0.8/0.4
    assert default["exit_price"] != adaptive["exit_price"]


def test_load_cached_bars_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(arm, "BAR_DIR", tmp_path)
    (tmp_path / "US_TEST_2026-10-20.json").write_text(json.dumps([
        ["2026-10-20 09:51:00", 100, 101, 100, 100.5],
        ["2026-10-20 09:52:00", 100.5, 100.6, 100.3, 100.4],
        ["2026-10-20 09:53:00", 100.4, 101.2, 100.4, 101.0],
        ["2026-10-20 09:54:00", 101.0, 101.0, 100.7, 100.9],
        ["2026-10-20 09:55:00", 100.9, 101.1, 100.8, 101.0],
    ]))
    et = erb.ET
    entry = et.localize(datetime(2026, 10, 20, 9, 50))
    exit_et = et.localize(datetime(2026, 10, 20, 16, 0))
    bars = arm._load_cached_bars("US.TEST", entry, exit_et)
    assert bars is not None and len(bars) == 5
    assert bars[0]["t"] > entry
    missing = arm._load_cached_bars("US.MISSING", entry, exit_et)
    assert missing is None


def test_compare_harm_and_save():
    rows = [
        {"day": "2026-10-01", "base_net_bps": -50.0, "t_net_bps": 10.0},
        {"day": "2026-10-02", "base_net_bps": 20.0, "t_net_bps": 5.0},
        {"day": "2026-10-03", "base_net_bps": 30.0, "t_net_bps": 30.0},
    ]
    out = arm._compare(rows)
    assert out["n"] == 3
    assert out["save_from_losers_bps"] == 60.0
    assert out["harm_to_winners_bps"] == 15.0
    assert out["total_delta_bps"] == 45.0


def test_stats_and_gate():
    rows = [
        {"net_bps": 20.0, "reason": "SL_TRAIL_LOCK", "capture_pct": 50.0},
        {"net_bps": -10.0, "reason": "SL_INITIAL", "capture_pct": None},
        {"net_bps": 30.0, "reason": "PRE_CLOSE_EXIT", "capture_pct": 70.0},
    ]
    stats = arm._stats(rows)
    assert stats["n"] == 3 and stats["win_rate_pct"] == 66.7
    assert stats["sl_initial_n"] == 1
    sample = {"n": 30, "distinct_days": 10}
    base = {"sl_initial_n": 5}
    t_arm = {"sl_initial_n": 4}
    delta = {"mean_delta_bps": 12.0, "day_clustered": {"t": 2.5},
             "harm_to_winners_bps": 100.0, "save_from_losers_bps": 200.0}
    assert arm._gate(sample, base, t_arm, delta, fidelity_ok=True)["pass"] is True
    assert arm._gate({**sample, "n": 29}, base, t_arm, delta)["pass"] is False
    assert arm._gate(sample, base, {"sl_initial_n": 6}, delta)["pass"] is False
    frozen = arm._gate(sample, base, t_arm, delta, fidelity_ok=False)
    assert frozen["pass"] is False and frozen["frozen_by_fidelity"] is True
