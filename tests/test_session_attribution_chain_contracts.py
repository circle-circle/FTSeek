"""session_attribution 新增 J/K 段与 EXT 退出契约的只读口径。"""
from __future__ import annotations

import json

import scripts.session_attribution as sa


def test_segment_j_uses_price_ledger_and_strict_fill(monkeypatch):
    instruction = {
        "id": 1,
        "symbol": "US.MPWR",
        "direction": "BUY",
        "decision_id": "DA-test",
        "executed": 1,
        "execution_price": 1364.73,
        "entry_price": 1366.57,
        "stop_loss": 1298.24,
        "take_profit": 1469.06,
        "position_size": 3,
        "raw_instruction_json": json.dumps({
            "planned_entry": 1320.0,
            "planned_stop_loss": 1250.0,
            "planned_take_profit": 1550.0,
        }),
        "metadata_json": json.dumps({"reprice_audit": {"rounds": 1}}),
    }
    trade = {
        "decision_id": "DA-test",
        "entry_price": 1364.73,
        "quantity": 3,
        "metadata_json": json.dumps({"execution": {"slippage_bps": 10.49}}),
    }

    def fake_rows(sql, args=()):
        if "playbook_instructions" in sql:
            return [instruction]
        return [trade]

    monkeypatch.setattr(sa, "_rows", fake_rows)
    report = sa.segment_j("2026-11-10")
    assert report["n_instructions"] == 1
    assert report["n_strict_fills"] == 1
    assert report["n_executed_latch"] == 1
    row = report["rows"][0]
    assert row["strict_fill"] is True
    assert row["planned"]["entry_price"] == 1320.0
    assert row["effective"]["entry_price"] == 1366.57
    assert row["fill"]["entry_price"] == 1364.73
    assert row["planned"]["risk_reward"] == 3.2857
    assert row["effective"]["risk_reward"] == 1.4999
    assert row["reprice_rounds"] == 1
    assert row["fill_slippage_bps"] == 10.49


def test_segment_g_discloses_ext_exit_contract(monkeypatch):
    position = {
        "symbol": "US.AMZN",
        "direction": "LONG",
        "size": 18.0,
        "avg_entry_price": 271.16,
        "initial_sl": 257.6,
        "current_sl": 257.6,
        "initial_tp": None,
        "strategy_id": "extended_hours_entry",
        "sync_status": "pending_sync",
        "entry_time": "2026-11-10T16:42:52-05:00",
        "metadata_json": json.dumps({
            "entry_grade": "A",
            "gate_mode": "research_momentum",
            "cert_lock_pct": 0.0,
        }),
    }

    def fake_rows(sql, args=()):
        if "initial_tp IS NULL" in sql:
            return [position]
        return []

    monkeypatch.setattr(sa, "_rows", fake_rows)
    report = sa.segment_g("2026-11-10")
    row = report["no_tp_positions"][0]
    assert row["entry_grade"] == "A"
    assert row["cert_lock_pct"] == 0.0
    assert row["initial_risk_usd"] == 244.08
    assert row["profit_protection"] == "BE/trail/RTH (条件触发)"


def test_segment_g_initial_risk_ignores_trailing_stop(monkeypatch):
    position = {
        "symbol": "US.AMZN",
        "direction": "LONG",
        "size": 18.0,
        "avg_entry_price": 271.16,
        "initial_sl": 257.6,
        "current_sl": 265.0,
        "initial_tp": None,
        "strategy_id": "extended_hours_entry",
        "sync_status": "synced",
        "entry_time": "2026-11-10T16:42:52-05:00",
        "metadata_json": json.dumps({"entry_grade": "A", "cert_lock_pct": 0.0}),
    }

    def fake_rows(sql, args=()):
        if "initial_tp IS NULL" in sql:
            return [position]
        return []

    monkeypatch.setattr(sa, "_rows", fake_rows)
    row = sa.segment_g("2026-11-10")["no_tp_positions"][0]
    assert row["initial_sl"] == 257.6
    assert row["sl"] == 265.0
    assert row["initial_risk_usd"] == 244.08


def test_segment_k_flags_missing_asof_without_gating(tmp_path, monkeypatch):
    day_dir = tmp_path / "2026-11-10"
    day_dir.mkdir()
    raw = {
        "collected": {
            "_quality": {"ok": 19, "total": 19},
            "_source_status": {"news": "ok"},
            "snapshot": {"session": "premarket", "update_time": "2026-11-10 08:00:00"},
            "kline": [{"time": "2026-11-09", "close": 1.0}],
            "capital_flow": {"net": 1.0},
            "market_context": {"vix": {"as_of_basis": "prev_close", "current": 15.48}},
        }
    }
    (day_dir / "US.AMZN.raw.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(sa, "CKPT_DIR", tmp_path)
    report = sa.segment_k("2026-11-10")
    assert report["raw_count"] == 1
    assert report["quality_counts"] == {"19/19": 1}
    assert report["vix_as_of_basis"] == {"prev_close": 1}
    missing = {row["dimension"] for row in report["missing_top_level_asof"]}
    assert "capital_flow" in missing
    assert "market_context" in missing
    assert "snapshot" not in missing
    assert "kline" not in missing
    assert report["decision_use"] == "display_only_not_a_trading_gate"


def test_print_report_passes_freshness_segment_without_loop_shadowing(capsys):
    empty_a = {
        "n_closed_by_open": 0, "net_by_open": 0.0, "n_by_close": 0, "net_by_close": 0.0,
        "open_rows": [], "open_positions_table": [],
    }
    empty_b = {"by_strategy": [], "carry_over": []}
    empty_c = {"by_reason": [], "be_exit_class": []}
    empty_d = {
        "n_instructions": 0, "n_executed": 0, "skip_buckets": {},
        "gate_audit_rows": 0, "gate_audit_by_action": {"skipped_chase_cap": 1},
    }
    empty_e = {"da_executed": {}, "da_all": {}, "ext_entries": [], "conflicts": []}
    empty_f = {
        "threshold_pct": 1.0, "keep": {"n": 0, "net": 0.0, "symbols": []},
        "drop": {"n": 0, "net": 0.0, "symbols": []}, "n_da_trades": 0, "gap_coverage": 0,
    }
    empty_g = {"no_tp_positions": [], "same_day_reentry": []}
    empty_h = {"n_shadow_rows": 0, "da_available_rate": None, "conflict": {}, "clean": {}, "rows": []}
    price_ledger = {"n_instructions": 0, "n_strict_fills": 0, "n_executed_latch": 0, "rows": []}
    freshness = {
        "raw_count": 0, "quality_counts": {}, "vix_as_of_basis": {}, "snapshot_sessions": {},
        "source_status_keys": {}, "missing_top_level_asof": [],
    }
    sa.print_report(
        "2026-11-10", empty_a, empty_b, empty_c, empty_d, empty_e, empty_f,
        empty_g, empty_h, None, price_ledger, freshness,
    )
    output = capsys.readouterr().out
    assert "J. DA planned/effective/fill" in output
    assert "K. DA 输入新鲜度审计" in output


def test_segment_l_reports_tca_coverage_and_legacy_hygiene(monkeypatch):
    def fake_rows(sql, args=()):
        if "execution.slippage_bps" in sql:
            return [
                {"strategy_id": "playbook_sim", "exec_slip": 5.0, "top_slip": None,
                 "entry_price_source": "broker_deal_confirm"},
                {"strategy_id": "playbook_sim", "exec_slip": None, "top_slip": None,
                 "entry_price_source": "broker_deal_confirm"},
                {"strategy_id": "extended_hours_entry", "exec_slip": None, "top_slip": None,
                 "entry_price_source": None},
            ]
        if "stop_loss_price IS NULL" in sql:
            return [{"symbol": "US.GOOG", "initial_sl": 277.52, "current_sl": 277.52}]
        if "pnl_net IS NULL OR fees_est IS NULL" in sql:
            return [{"n": 5}]
        if "decision_id IS NULL OR decision_id=''" in sql:
            return [{"n": 293}]
        if "HAVING COUNT(*)>1" in sql:
            return [{"n": 6}]
        return []

    monkeypatch.setattr(sa, "_rows", fake_rows)
    report = sa.segment_l("2026-11-10")
    assert report["tca"]["n"] == 3
    assert report["tca"]["slippage_present"] == 1
    assert report["tca"]["entry_price_source_present"] == 2
    assert report["tca"]["by_strategy"]["extended_hours_entry"]["slippage_present"] == 0
    assert report["open_positions_stop_loss_price_null"][0]["symbol"] == "US.GOOG"
    assert report["legacy_hygiene"]["closed_missing_pnl_or_fees"] == 5
    assert report["decision_use"] == "display_only_not_a_trading_gate"
