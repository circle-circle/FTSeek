import json
import sqlite3

from app.review.decision_diagnostics import (
    build_decision_diagnostics,
    build_price_band_fees,
    build_repeated_loss_clusters,
    build_sim_real_bridge,
    build_stage_direction_reason,
)


def _create_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        create table trade_records (
          id integer primary key,
          trade_date text,
          symbol text not null,
          direction text not null,
          entry_time text,
          exit_time text,
          entry_price real,
          exit_price real,
          entry_session text,
          exit_reason text,
          strategy_id text,
          owner_strategy text,
          source_type text,
          trade_mode text,
          entry_reason text,
          signal_id text,
          decision_id text,
          realized_pnl real,
          pnl_net real,
          fees_est real,
          mfe_pct real,
          mae_pct real,
          quantity integer,
          data_quality text
        );
        create table playbook_instructions (
          id integer primary key,
          date text not null,
          symbol text not null,
          direction text not null,
          entry_price real,
          raw_instruction_json text,
          metadata_json text,
          decision_id text
        );
        """
    )
    return connection


def _trade(connection, *, symbol, direction, entry_time, exit_time, session,
           entry_price, net, mfe, reason, signal_id, decision_id,
           source_type="PLAYBOOK_SIM", trade_mode="simulate", quantity=100):
    connection.execute(
        "insert into trade_records(trade_date,symbol,direction,entry_time,exit_time,"
        "entry_price,exit_price,entry_session,exit_reason,strategy_id,owner_strategy,"
        "source_type,trade_mode,entry_reason,signal_id,decision_id,realized_pnl,"
        "pnl_net,fees_est,mfe_pct,mae_pct,quantity,data_quality) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            exit_time[:10], symbol, direction, entry_time, exit_time, entry_price,
            entry_price, session, reason, "playbook_sim", "playbook_sim",
            source_type, trade_mode, "playbook", signal_id, decision_id, net,
            net, 2.0, mfe, 0.5, quantity, "real",
        ),
    )


def _instruction(connection, symbol, direction, decision_id, planned_entry,
                 chase_cap=None, strategy_type="pullback_limit"):
    raw = {
        "strategy_type": strategy_type,
        "planned_entry": planned_entry,
        "chase_cap_price": chase_cap,
    }
    metadata = {"reprice_audit": {"reprice_from": planned_entry, "reprice_to": 25.0}}
    connection.execute(
        "insert into playbook_instructions(date,symbol,direction,entry_price,"
        "raw_instruction_json,metadata_json,decision_id) values (?,?,?,?,?,?,?)",
        (
            "2026-09-18", symbol, direction, planned_entry,
            json.dumps(raw, sort_keys=True), json.dumps(metadata, sort_keys=True),
            decision_id,
        ),
    )


def test_stage_direction_reason_and_price_band_are_grouped():
    connection = _create_db()
    _trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", entry_price=12.0, net=-100.0, mfe=0.0,
        reason="SL_INITIAL", signal_id="sig-a", decision_id="dec-a",
    )
    _trade(
        connection, symbol="US.B", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:30:00-04:00",
        session="regular", entry_price=12.0, net=50.0, mfe=1.0,
        reason="SL_TRAIL_LOCK", signal_id="sig-b", decision_id="dec-b",
    )

    groups = build_stage_direction_reason(connection, "2026-09-18")
    price_bands = build_price_band_fees(connection, "2026-09-18")

    assert groups["regular"]["做多"]["SL_INITIAL"]["n"].value == 1
    assert groups["regular"]["做多"]["SL_INITIAL"]["net"].value == -100.0
    assert price_bands["$10-$20"]["n"].value == 2
    assert price_bands["$10-$20"]["net"].value == -50.0
    assert price_bands["$10-$20"]["fees"].value == 4.0


def test_mfe0_and_chase_are_attributed_without_claiming_a_verdict():
    connection = _create_db()
    _instruction(connection, "US.A", "LONG", "dec-a", 22.0, chase_cap=24.0)
    _trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", entry_price=25.0, net=-120.0, mfe=0.0,
        reason="SL_INITIAL", signal_id="sig-a", decision_id="dec-a",
    )

    diagnostics = build_decision_diagnostics(connection, "2026-09-18")
    questions = diagnostics["three_questions"]

    assert questions["should_not_fire_count"].value == 1
    assert questions["should_not_fire_cases"].value[0]["symbol"] == "US.A"
    assert questions["should_not_fire_cases"].value[0]["verdict"] == "candidate_not_verdict"
    assert questions["chase_entry_count"].value == 1
    case = questions["chase_entry_cases"].value[0]
    assert case["entry_premium_pct"] == round((25.0 - 22.0) / 22.0, 6)
    assert case["above_chase_cap"] is True
    assert case["reprice_from"] == 22.0
    assert case["initial_planned_entry"] == 22.0
    assert case["effective_planned_entry"] == 22.0


def test_repeated_loss_cluster_requires_all_dimensions():
    connection = _create_db()
    rows = [
        ("2026-09-16", "US.A", -350.0),
        ("2026-09-17", "US.B", -400.0),
        ("2026-09-18", "US.C", -300.0),
    ]
    for index, (day, symbol, net) in enumerate(rows):
        _trade(
            connection, symbol=symbol, direction="LONG",
            entry_time=f"{day}T10:00:00-04:00", exit_time=f"{day}T11:00:00-04:00",
            session="regular", entry_price=20.0, net=net, mfe=0.0,
            reason="SL_INITIAL", signal_id=f"sig-{index}", decision_id=f"dec-{index}",
        )

    clusters = build_repeated_loss_clusters(connection, "2026-09-18", lookback_days=30)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["prompt_class"] == "playbook"
    assert cluster["n"].value == 3
    assert cluster["distinct_days"].value == 3
    assert cluster["distinct_symbols"].value == 3
    assert cluster["current_loss_streak"].value == 3
    assert cluster["current_streak_loss"].value == -1050.0
    assert cluster["qualifies"].value is True


def test_sim_real_bridge_reports_pairs_and_normalized_difference():
    connection = _create_db()
    _trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", entry_price=10.0, net=100.0, mfe=1.0,
        reason="EXIT", signal_id="same-signal", decision_id="same-decision",
        source_type="PLAYBOOK_SIM", trade_mode="simulate", quantity=100,
    )
    _trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:05:00-04:00",
        session="regular", entry_price=10.0, net=175.0, mfe=1.0,
        reason="EXIT", signal_id="same-signal", decision_id="same-decision",
        source_type="PLAYBOOK_LIVE", trade_mode="live", quantity=100,
    )

    bridge = build_sim_real_bridge(connection, "2026-09-18", lookback_days=30)

    assert bridge["paired_signal_count"].value == 1
    assert bridge["rolling_20_cumulative_difference"].value == 75.0
    assert bridge["rolling_20_exceeds_500"].value is False
    assert bridge["pairs"].value[0]["real_minus_sim"] == 75.0
    assert bridge["pairs"].value[0]["normalized_per_5000"] == 375.0
