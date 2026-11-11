from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.playbook import pilot_policy


def test_source_attribution_separates_mom():
    from app.review.source_attribution import playbook_source_sql, source_labels

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (raw_instruction_json TEXT, decision_id TEXT)")
    conn.execute("INSERT INTO t VALUES (?, ?)", ("{}", "MOM-ETF-V1-20261112-US.QQQ"))
    conn.execute("INSERT INTO t VALUES (?, ?)", ("{}", "DA-20261112-US.AAPL"))
    rows = conn.execute(f"SELECT ({playbook_source_sql()}) AS source FROM t ORDER BY rowid").fetchall()
    assert [row[0] for row in rows] == ["MOM-ETF-V1", "DA自选"]
    assert "MOM-ETF-V1" in source_labels()
    conn.close()


def test_live_paths_share_loop_and_keep_v1_exit_branches():
    root = Path(__file__).resolve().parents[2]
    auto_execute = (root / "app/playbook/pipeline_parts/auto_execute_flow.py").read_text()
    exit_monitor = (root / "app/playbook/pipeline_parts/exit_monitor_flow.py").read_text()
    cleanup = (root / "app/playbook/pipeline_parts/overnight_cleanup.py").read_text()
    service = (root / "app/playbook/playbook_service.py").read_text()
    assert "execute_mom_etf_v1_instructions" not in auto_execute
    assert "mom_reprice_params" in auto_execute
    assert "_reprice_quote_age_gate" in auto_execute
    assert "mom_budget_reject_reason" in auto_execute
    assert "check_mom_exit" in exit_monitor
    assert 'decision["emit_overnight_shadow"] = False' in exit_monitor
    assert "decision_id, futu_order_id" in cleanup
    assert "policy_for(_decision_id)" in cleanup
    assert "mom_final_order_reject_reason" in service
    assert service.index("gap_dive_halve_enabled") < service.index("mom_final_order_reject_reason")


def test_rfc141_sim_pnl_excludes_mom_but_keeps_null_decision_id(tmp_path, monkeypatch):
    import scripts.settle_entry_confirm_forward as settle

    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trade_records (symbol TEXT, trade_date TEXT, entry_time TEXT, "
        "exit_time TEXT, exit_reason TEXT, fees_est REAL, quantity INTEGER, "
        "entry_price REAL, exit_price REAL, pnl_net REAL, realized_pnl REAL, "
        "source_type TEXT, decision_id TEXT, metadata_json TEXT)"
    )
    rows = [
        ("US.AAA", "DA-1", None),
        ("US.MOM", "MOM-ETF-V1-20261113-US.QQQ", None),
        ("US.NULL", None, None),
        ("US.RESEARCH", "DA-2", json.dumps({"gate_mode": "research_momentum"})),
    ]
    for symbol, decision_id, meta in rows:
        conn.execute(
            "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, "2026-11-13", "2026-11-13T10:00:00-05:00",
             "2026-11-13T11:00:00-05:00", "SL_INITIAL", 1.0, 1, 100.0,
             101.0, 1.0, 1.0, "PLAYBOOK_SIM", decision_id, meta),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(settle, "DB", str(db_path))
    out = settle.load_sim_pnl("2026-11-01")
    assert {row["symbol"] for row in out} == {"US.AAA", "US.NULL"}
    fills = settle.load_fills("2026-11-01")
    assert ("US.MOM", "2026-11-13") not in fills


def _writer_instruction():
    return {
        "date": "2026-11-13",
        "symbol": "US.QQQ",
        "direction": "BUY",
        "decision_id": "MOM-ETF-V1-20261113-US.QQQ",
        "raw_instruction": {
            "source": "mom_etf_v1",
            "candidate_screening": {"candidate_id": "MOM-ETF-V1-20261113-US.QQQ"},
        },
    }


def _patch_writer(monkeypatch, db, asof="2026-11-12"):
    import scripts.mom_etf_v1_generate as writer

    monkeypatch.setattr("sys.argv", ["mom_etf_v1_generate.py"])
    bars = {symbol: [[asof, 0, 0, 0, 100.0]] for symbol in pilot_policy.MOM_ETF_V1.universe}
    monkeypatch.setattr(writer, "load_bars", lambda: bars)
    monkeypatch.setattr(writer, "is_rebalance_day", lambda today: True)
    monkeypatch.setattr(writer, "compute_mom_candidates", lambda bars, asof: [
        {"symbol": "US.QQQ", "close": 100.0, "momentum_12_1": 0.1, "asof_date": asof},
    ])
    monkeypatch.setattr(writer, "build_instruction", lambda candidate, trade_date: _writer_instruction())
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: datetime(
        2026, 11, 13, 9, 0, tzinfo=timezone(timedelta(hours=-5))))

    class Calendar:
        def prev_trading_day(self, market, today, max_days=14):
            from datetime import date
            return date(2026, 11, 12)

    monkeypatch.setattr("app.utils.holiday_calendar.get_holiday_calendar", lambda: Calendar())
    monkeypatch.setattr("app.playbook.db.route_playbook_db", lambda arm: db)
    return writer


def test_writer_reports_saved_only_after_confirm_saved(monkeypatch, capsys):
    instruction = _writer_instruction()

    class DB:
        def save_instruction(self, inst):
            return 7

        def get_instruction_by_id(self, instruction_id):
            return dict(instruction, id=instruction_id, status="PENDING", executed=0, outcome=None)

    writer = _patch_writer(monkeypatch, DB())
    assert writer.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "saved"
    assert payload["saved"][0]["decision_id"] == instruction["decision_id"]


def test_writer_rejects_da_overwrite_and_stale_bars(monkeypatch, capsys):
    class OverwriteDB:
        def save_instruction(self, inst):
            return 9

        def get_instruction_by_id(self, instruction_id):
            return {"id": instruction_id, "decision_id": "DA-20261113-US.QQQ",
                    "symbol": "US.QQQ", "direction": "BUY", "raw_instruction": {}}

    writer = _patch_writer(monkeypatch, OverwriteDB())
    assert writer.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"

    writer = _patch_writer(monkeypatch, OverwriteDB(), asof="2020-01-02")
    assert writer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "fail_closed"
    assert payload["reason"] == "bars_not_previous_trading_day"


class _FakePBConfig:
    def get_horizon_params(self, horizon):
        return {}

    def get(self, key, default=None):
        return default


def _run_auto_execute(monkeypatch, instruction, now_et_dt, now_sequence=None):
    from app.playbook.pipeline_parts import auto_execute_flow as aef
    import datetime as datetime_module

    times = list(now_sequence or [])

    class FrozenDateTime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            if times:
                return times.pop(0) if len(times) > 1 else times[0]
            return now_et_dt

    monkeypatch.setattr(datetime_module, "datetime", FrozenDateTime)

    et = timezone(timedelta(hours=-5))
    executed = []

    class FakeDB:
        def get_instructions_by_date(self, today):
            return [instruction]

        def update_instruction_tracking(self, instruction_id, payload):
            instruction.update(payload)

        def mark_instruction_guardrail_skip(self, instruction_id, reason):
            instruction["skip_reason"] = reason

        def merge_instruction_metadata(self, instruction_id, payload):
            instruction.setdefault("metadata_json", {}).update(payload)
            return True

    class FakeService:
        def execute_instruction(self, **kwargs):
            executed.append(kwargs)
            return {"success": True}

    class FakeRouter:
        def get_full_market_context(self, symbol):
            return {"price": 100.0, "last": 100.0, "lv3_bid": 99.9,
                    "lv3_ask": 100.0, "lv3_quote_age_ms": 100}

    class FakeScheduler:
        data_router = FakeRouter()

    class FakeBroker:
        def get_positions(self, target_env):
            return []

    fake_db = FakeDB()
    service = FakeService()
    budget_ctx = {
        "max_deploy": 0.0,
        "playbook_config": _FakePBConfig(),
        "budget_limit": 100000.0,
        "risk_base_capital": 100000.0,
        "daily_notional_cap": 100000.0,
        "daily_notional_cap_pct": 0.8,
        "max_daily_positions": 16,
        "max_new_per_cycle": 3,
        "max_open_positions": 999,
        "today_opened_notional": 0.0,
        "today_opened_count": 0,
        "open_positions_count": 0,
    }
    monkeypatch.setattr("app.playbook.db.route_playbook_db", lambda arm: fake_db)
    monkeypatch.setattr("app.playbook.playbook_service.PlaybookService", lambda db: service)
    monkeypatch.setattr("app.execution.dual_broker.get_dual_broker", lambda: FakeBroker())
    monkeypatch.setattr(aef, "progress_ping", lambda: None)
    monkeypatch.setattr(pilot_policy, "entries_enabled", lambda: True)
    monkeypatch.setattr(pilot_policy, "is_rebalance_day", lambda today, policy=None: True)
    monkeypatch.setattr(pilot_policy, "mom_budget_snapshot", lambda now, policy=None: (0, 0.0))
    monkeypatch.setattr("app.execution.fee_model.estimate_round_trip",
                        lambda quantity, entry, exit_price, direction, date: {"total": 4.0})
    monkeypatch.setattr("app.playbook.entry_routing.chase_cap_enforce_enabled", lambda cfg=None: False)
    monkeypatch.setattr("app.playbook.execution_tca_shadow.tca_config", lambda cfg: {"enabled": False})
    monkeypatch.setattr("app.playbook.risk_size_shadow.risk_shadow_enabled", lambda cfg: False)

    def playbook_cfg(key, default=None):
        return {"auto_execute": True,
                "same_day_hard_stop_cooldown": False,
                "premarket_invalidation_hard_gate": False}.get(key, default)

    result = aef.run_playbook_auto_execute(
        FakeScheduler(),
        force=True,
        datetime_cls=FrozenDateTime,
        _now_et=lambda: et,
        _today_et_str=lambda: now_et_dt.date().isoformat(),
        module_file=__file__,
        logger=logging.getLogger("test-mom-auto"),
        INVALIDATED_SKIP_REASONS=set(),
        _CC_NORM_LOGGED=set(),
        _CC_NORM_LOGGED_MAX=100,
        _auto_execute_preflight=lambda today, logger=None: None,
        _chase_cap_round_bump=lambda instruction_id: 0,
        _check_bridge_health_and_alert=lambda source: None,
        _check_intraday_trigger=lambda scheduler, inst: True,
        _check_same_day_hard_stop_cooldown=lambda symbol, direction, today: None,
        _compute_volatility_based_sl_tp=lambda *args: None,
        _emit_exec_gate_audit=lambda *args, **kwargs: None,
        _is_trading_day_today=lambda: True,
        _live_engineering_probe_context=lambda: None,
        _load_execution_budget=lambda today: (budget_ctx, None),
        _mirror_instruction_to_live_if_allowed=lambda *args: (None, {"allowed": False}),
        _normalize_regime_str=lambda value: value,
        _playbook_cfg=playbook_cfg,
        _positive_finite_atr=lambda value: value if isinstance(value, (int, float)) and value > 0 else None,
        _resolve_sizing_base=lambda cfg: 100000.0,
        _shadow_dedup_claim=lambda *args: True,
        _shadow_dedup_commit=lambda *args: None,
        _vol_bucket_sl_bounds=lambda *args: None,
    )
    return result, executed, instruction


def test_auto_execute_mom_uses_shared_loop_and_v1_geometry(monkeypatch):
    now = datetime(2026, 11, 13, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
    instruction = {
        "id": 1,
        "decision_id": "MOM-ETF-V1-20261113-US.XLE",
        "symbol": "US.XLE",
        "direction": "BUY",
        "entry_price": 95.0,
        "stop_loss": 92.0,
        "take_profit": 110.0,
        "position_size": 0,
        "executed": 0,
        "triggered": 1,
        "horizon": "long",
        "raw_instruction_json": {"source": "mom_etf_v1"},
    }
    result, executed, _ = _run_auto_execute(monkeypatch, instruction, now)
    assert result["executed"] == 1
    params = executed[0]["custom_params"]
    assert params["entry_price"] == 100.0
    assert params["stop_loss"] == pytest.approx(97.0)
    assert params["take_profit"] == 0.0
    assert params["position_size"] == 49


def test_auto_execute_mom_expires_after_cutoff(monkeypatch):
    now = datetime(2026, 11, 13, 14, 1, tzinfo=timezone(timedelta(hours=-5)))
    instruction = {
        "id": 1,
        "decision_id": "MOM-ETF-V1-20261113-US.XLE",
        "symbol": "US.XLE",
        "direction": "BUY",
        "entry_price": 95.0,
        "stop_loss": 92.0,
        "executed": 0,
        "triggered": 1,
        "raw_instruction_json": {"source": "mom_etf_v1"},
    }
    result, executed, updated = _run_auto_execute(monkeypatch, instruction, now)
    assert result["executed"] == 0
    assert executed == []
    assert updated["skip_reason"] == "MOM_SAME_DAY_EXPIRED"


def test_auto_execute_mom_rechecks_cutoff_before_order(monkeypatch):
    et = timezone(timedelta(hours=-5))
    start = datetime(2026, 11, 13, 13, 59, tzinfo=et)
    late = datetime(2026, 11, 13, 14, 1, tzinfo=et)
    instruction = {
        "id": 1,
        "decision_id": "MOM-ETF-V1-20261113-US.XLE",
        "symbol": "US.XLE",
        "direction": "BUY",
        "entry_price": 95.0,
        "stop_loss": 92.0,
        "executed": 0,
        "triggered": 1,
        "raw_instruction_json": {"source": "mom_etf_v1"},
    }
    result, executed, updated = _run_auto_execute(
        monkeypatch, instruction, start, now_sequence=[start, late])
    assert result["executed"] == 0
    assert executed == []
    assert updated["skip_reason"] == "MOM_SAME_DAY_EXPIRED"
