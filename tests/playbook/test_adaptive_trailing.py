import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.adaptive_trailing import (
    adaptive_trailing_drawdown_pct,
    adaptive_trailing_snapshot,
)
from app.playbook.exit_engine import (
    _check_playbook_exit_rules,
    _compute_dynamic_effective_sl,
)
from app.utils.timezone import now_et
from scripts.extended_hours_profit_exit_rules import (
    decide_original_profit_exit,
    persist_profit_exit_state,
)


CONFIG = {
    "adaptive_trailing_enabled": True,
    "adaptive_trailing_target_capture_pct": 60.0,
    "adaptive_trailing_min_drawdown_pct": 0.4,
    "adaptive_trailing_max_drawdown_pct": 1.2,
}


@pytest.mark.parametrize("mfe,expected", [
    (0.5, 0.4),
    (1.0, 0.4),
    (2.0, 0.8),
    (3.0, 1.2),
    (5.0, 1.2),
])
def test_drawdown_scales_with_mfe_and_is_clamped(mfe, expected):
    assert adaptive_trailing_drawdown_pct(mfe, CONFIG, "PLAYBOOK_SIM") == expected


def test_adaptive_trailing_is_sim_only_and_config_gated():
    assert adaptive_trailing_drawdown_pct(2.0, CONFIG, "PLAYBOOK_LIVE") is None
    assert adaptive_trailing_drawdown_pct(2.0, {
        **CONFIG, "adaptive_trailing_enabled": False,
    }, "PLAYBOOK_SIM") is None


@pytest.mark.parametrize("invalid", [
    {"adaptive_trailing_target_capture_pct": 100.0},
    {"adaptive_trailing_target_capture_pct": 0.0},
    {"adaptive_trailing_min_drawdown_pct": 0.0},
    {"adaptive_trailing_max_drawdown_pct": 0.2},
])
def test_invalid_configuration_disables_adaptive_trailing(invalid):
    assert adaptive_trailing_drawdown_pct(
        2.0, {**CONFIG, **invalid}, "PLAYBOOK_SIM"
    ) is None


def test_snapshot_reports_mfe_drawdown_and_target_capture():
    assert adaptive_trailing_snapshot(2.0, CONFIG, "PLAYBOOK_SIM") == {
        "mfe_pct": 2.0,
        "drawdown_pct": 0.8,
        "target_capture_pct": 60.0,
    }


def test_compute_effective_sl_uses_adaptive_drawdown_and_keeps_net_floor():
    exit_cfg = {
        "breakeven_at_pct": 0.5,
        "trailing_enabled": True,
        "drawdown_from_peak_pct": 0.5,
    }
    static_sl = _compute_dynamic_effective_sl(
        "LONG", 100.0, 95.0, 2.0, exit_cfg, 0.2, 0.0
    )
    adaptive_sl = _compute_dynamic_effective_sl(
        "LONG", 100.0, 95.0, 2.0, exit_cfg, 0.2, 0.0, 0.8
    )
    assert static_sl == pytest.approx(101.49)
    assert adaptive_sl == pytest.approx(101.184, abs=0.0001)


def test_intraday_engine_enables_adaptive_trailing_for_sim_only(monkeypatch):
    from app.playbook import exit_engine
    monkeypatch.setattr(
        exit_engine,
        "_get_factor_exit_config",
        lambda *args, **kwargs: {
            "trailing_enabled": True,
            "breakeven_at_pct": 0.4,
            "drawdown_from_peak_pct": 2.0,
        },
    )

    def run(source_type):
        meta = {
            "_log_tag": "adaptive_test",
            "_symbol": "TEST",
            "cohort": "A",
            "horizon": "short",
            "_source_type": source_type,
            "_skip_allowed": False,
            "stop_loss": 95.0,
            "take_profit": 0,
        }
        return _check_playbook_exit_rules(
            "LONG", 100.0, 101.75, 95.0, 103.0, meta, 30, 30, 20,
            now_et().isoformat(),
        ), meta

    sim_result, sim_meta = run("PLAYBOOK_SIM")
    live_result, live_meta = run("PLAYBOOK_LIVE")
    assert sim_result[0] == "SL_TRAIL_LOCK"
    assert sim_meta["_adaptive_trailing"]["drawdown_pct"] == 1.2
    assert live_result[0] == ""
    assert "_adaptive_trailing" not in live_meta


def test_extended_hours_exit_uses_adaptive_trailing_for_sim_only(monkeypatch):
    from app.playbook import exit_engine
    monkeypatch.setattr(
        exit_engine,
        "_get_factor_exit_config",
        lambda *args, **kwargs: {
            "trailing_enabled": True,
            "breakeven_at_pct": 0.4,
            "drawdown_from_peak_pct": 2.0,
        },
    )
    metadata = {
        "stop_loss": 95.0,
        "take_profit": 0,
        "conditions": [],
        "peak_price": 103.0,
        "breakeven_triggered": True,
    }
    sim = decide_original_profit_exit(
        "LONG", 100.0, 101.7, 95.0, 103.0, metadata, 0.15, "PLAYBOOK_SIM"
    )
    live = decide_original_profit_exit(
        "LONG", 100.0, 101.7, 95.0, 103.0, metadata, 0.15, "PLAYBOOK_LIVE"
    )
    assert sim["action"] == "SL_TRAIL_LOCK"
    assert sim["effective_sl"] == pytest.approx(101.764)
    assert sim["adaptive_trailing_drawdown_pct"] == 1.2
    assert live["action"] == "hold"
    assert live["adaptive_trailing_drawdown_pct"] is None


def test_extended_hours_state_persists_adaptive_snapshot(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1, ?)",
        (json.dumps({"peak_price": 100.0}),),
    )
    conn.commit()
    conn.close()

    decision = decide_original_profit_exit(
        "LONG", 100.0, 102.0, 95.0, 103.0,
        {"stop_loss": 95.0, "take_profit": 0, "conditions": []},
        0.15, "PLAYBOOK_SIM",
    )
    assert persist_profit_exit_state(db_path, 1, decision, "LONG")

    conn = sqlite3.connect(db_path)
    metadata = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])
    conn.close()
    assert metadata["_adaptive_trailing"] == {
        "mfe_pct": 3.0,
        "drawdown_pct": 1.2,
        "target_capture_pct": 60.0,
    }
