import json
import sqlite3

from app.review.bundle_builder import (
    build,
    build_anomalies,
    build_plan_vs_actual,
    build_slices,
    exit_date_mismatches,
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
          entry_session text,
          exit_reason text,
          strategy_id text,
          owner_strategy text,
          source_type text,
          trade_mode text,
          entry_reason text,
          signal_id text,
          decision_id text,
          entry_price real,
          exit_price real,
          realized_pnl real,
          pnl_net real,
          fees_est real,
          mfe_pct real,
          mae_pct real,
          quantity integer,
          data_quality text,
          metadata_json text
        );
        create table playbook_instructions (
          id integer primary key,
          date text not null,
          symbol text not null,
          direction text not null,
          skip_reason text,
          entry_price real, raw_instruction_json text, metadata_json text,
          decision_id text
        );
        """
    )
    return connection


def _insert_trade(connection, *, symbol, direction, entry_time, exit_time,
                  session, net, gross=None, fees=0.0, mfe=1.0, quality="real",
                  decision_id=None, instruction_id=None):
    metadata = {"playbook_instruction_id": instruction_id} if instruction_id else None
    connection.execute(
        "insert into trade_records(trade_date,symbol,direction,entry_time,exit_time,"
        "entry_session,exit_reason,strategy_id,source_type,realized_pnl,pnl_net,"
        "fees_est,mfe_pct,mae_pct,quantity,data_quality,decision_id,metadata_json) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            exit_time[:10], symbol, direction, entry_time, exit_time, session,
            "TEST", "test", "PLAYBOOK_SIM", gross if gross is not None else net,
            net, fees, mfe, 0.0, 1, quality, decision_id,
            json.dumps(metadata) if metadata else None,
        ),
    )


def test_build_reconciles_headline_and_stage_slices():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T04:00:00-04:00", exit_time="2026-09-18T05:00:00-04:00",
        session="premarket", net=-180.0, mfe=0.0,
    )
    _insert_trade(
        connection, symbol="US.B", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=200.0, gross=210.0, fees=10.0, mfe=0.0,
    )
    _insert_trade(
        connection, symbol="US.C", direction="LONG",
        entry_time="2026-09-18T18:00:00-04:00", exit_time="2026-09-18T19:00:00-04:00",
        session="afterhours", net=55.0, mfe=2.0,
    )

    bundle = build(connection, "2026-09-18")
    bundle.validate()

    assert bundle.headline["net_pnl"].value == 75.0
    assert bundle.headline["gross_pnl"].value == 85.0
    assert bundle.headline["fees"].value == 10.0
    assert bundle.headline["trade_count"].value == 3
    assert bundle.headline["win_count"].value == 2
    assert bundle.headline["mfe0_count"].value == 2

    slices = build_slices(connection, "2026-09-18")
    assert sum(row["net"].value for row in slices.values()) == 75.0
    assert slices["premarket"]["net"].value == -180.0
    assert slices["regular"]["net"].value == 200.0
    assert slices["afterhours"]["net"].value == 55.0
    assert slices["regular"]["gross"].value == 210.0


def test_duplicate_external_close_is_excluded_from_result_layer():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=100.0,
    )
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=999.0, quality="duplicate_external_close",
    )

    bundle = build(connection, "2026-09-18")

    assert bundle.headline["net_pnl"].value == 100.0
    assert bundle.headline["trade_count"].value == 1


def test_plan_vs_actual_matches_direction_and_reports_unplanned():
    connection = _create_db()
    connection.executemany(
        "insert into playbook_instructions(date,symbol,direction,decision_id) values (?,?,?,?)",
        [
            ("2026-09-18", "US.A", "BUY", "DA-A"),
            ("2026-09-18", "US.B", "SELL_SHORT", "DA-B"),
            ("2026-09-18", "US.C", "BUY", "DA-C"),
        ],
    )
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=10.0, decision_id="DA-A",
    )
    _insert_trade(
        connection, symbol="US.C", direction="SHORT",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=10.0, decision_id="DA-C",
    )

    plan = build_plan_vs_actual(connection, "2026-09-18")

    assert plan["planned_count"].value == 3
    assert plan["executed_count"].value == 1
    assert plan["missed_count"].value == 2
    assert plan["unplanned_count"].value == 1
    assert plan["execution_rate"].value == 0.333333
    assert plan["direction_mismatch"].value == ["US.C(BUY/SELL_SHORT)"]


def test_plan_vs_actual_does_not_borrow_cross_channel_same_symbol_fill():
    connection = _create_db()
    connection.execute(
        "insert into playbook_instructions(date,symbol,direction,decision_id) "
        "values ('2026-11-09','US.APP','BUY','DA-APP')"
    )
    _insert_trade(
        connection, symbol="US.APP", direction="LONG",
        entry_time="2026-11-09T04:00:00-05:00", exit_time="2026-11-09T09:32:00-05:00",
        session="premarket", net=10.0, decision_id="EXT-APP",
    )

    plan = build_plan_vs_actual(connection, "2026-11-09")

    assert plan["executed_count"].value == 0
    assert plan["missed_count"].value == 1
    assert plan["unplanned_count"].value == 1
    assert plan["direction_mismatch"].value == []


def test_plan_vs_actual_does_not_call_unrelated_cross_channel_reverse_a_mismatch():
    connection = _create_db()
    connection.execute(
        "insert into playbook_instructions(date,symbol,direction,decision_id) "
        "values ('2026-11-09','US.TSLA','SELL_SHORT','DA-TSLA')"
    )
    _insert_trade(
        connection, symbol="US.TSLA", direction="LONG",
        entry_time="2026-11-09T04:00:00-05:00", exit_time="2026-11-09T09:32:00-05:00",
        session="premarket", net=10.0, decision_id="EXT-TSLA",
    )

    plan = build_plan_vs_actual(connection, "2026-11-09")

    assert plan["executed_count"].value == 0
    assert plan["missed_count"].value == 1
    assert plan["unplanned_count"].value == 1
    assert plan["direction_mismatch"].value == []


def test_plan_vs_actual_metadata_instruction_id_wins_over_ambiguous_decision_id():
    connection = _create_db()
    connection.executemany(
        "insert into playbook_instructions(id,date,symbol,direction,decision_id) values (?,?,?,?,?)",
        [
            (1, "2026-11-09", "US.APP", "BUY", "SHARED"),
            (2, "2026-11-09", "US.APP", "BUY", "SHARED"),
        ],
    )
    _insert_trade(
        connection, symbol="US.APP", direction="LONG",
        entry_time="2026-11-09T10:00:00-05:00", exit_time="2026-11-09T11:00:00-05:00",
        session="regular", net=10.0, decision_id="SHARED", instruction_id=2,
    )

    plan = build_plan_vs_actual(connection, "2026-11-09")

    # 计划单元 = 去重后的 (symbol, direction): 同标的多轮指令只占一个分母。
    assert plan["planned_count"].value == 1
    assert plan["instruction_count"].value == 2
    assert plan["executed_count"].value == 1
    assert plan["executed_instruction_count"].value == 1
    assert plan["missed_count"].value == 0
    assert plan["unplanned_count"].value == 0
    assert plan["execution_rate"].value == 1.0
    assert plan["attribution_ambiguous_count"].value == 0


def test_plan_vs_actual_duplicate_reentry_rows_do_not_inflate_missed_count():
    connection = _create_db()
    connection.executemany(
        "insert into playbook_instructions(id,date,symbol,direction,decision_id) values (?,?,?,?,?)",
        [
            (1, "2026-11-09", "US.APP", "BUY", "DA-APP-1"),
            (2, "2026-11-09", "US.APP", "BUY", "DA-APP-2"),
        ],
    )
    _insert_trade(
        connection, symbol="US.APP", direction="LONG",
        entry_time="2026-11-09T10:00:00-05:00", exit_time="2026-11-09T11:00:00-05:00",
        session="regular", net=10.0, decision_id="DA-APP-1",
    )

    plan = build_plan_vs_actual(connection, "2026-11-09")

    assert plan["planned_count"].value == 1
    assert plan["instruction_count"].value == 2
    assert plan["executed_count"].value == 1
    assert plan["missed_count"].value == 0
    assert plan["unplanned_count"].value == 0
    assert plan["execution_rate"].value == 1.0


def test_plan_vs_actual_same_day_ambiguous_decision_id_is_not_unplanned():
    connection = _create_db()
    connection.executemany(
        "insert into playbook_instructions(id,date,symbol,direction,decision_id) values (?,?,?,?,?)",
        [
            (1, "2026-11-09", "US.APP", "BUY", "SHARED"),
            (2, "2026-11-09", "US.APP", "BUY", "SHARED"),
        ],
    )
    _insert_trade(
        connection, symbol="US.APP", direction="LONG",
        entry_time="2026-11-09T10:00:00-05:00", exit_time="2026-11-09T11:00:00-05:00",
        session="regular", net=10.0, decision_id="SHARED",
    )

    plan = build_plan_vs_actual(connection, "2026-11-09")

    assert plan["executed_count"].value == 0
    assert plan["missed_count"].value == 1
    assert plan["unplanned_count"].value == 0
    assert plan["attribution_ambiguous_count"].value == 1
    assert plan["attribution_ambiguous_symbols"].value == ["US.APP"]


def test_plan_vs_actual_resolves_cross_day_duplicate_decision_id_by_trade_date():
    connection = _create_db()
    connection.executemany(
        "insert into playbook_instructions(id,date,symbol,direction,decision_id) values (?,?,?,?,?)",
        [
            (1, "2026-11-08", "US.APP", "BUY", "DA-REUSED"),
            (2, "2026-11-09", "US.APP", "BUY", "DA-REUSED"),
        ],
    )
    _insert_trade(
        connection, symbol="US.APP", direction="LONG",
        entry_time="2026-11-09T10:00:00-05:00", exit_time="2026-11-09T11:00:00-05:00",
        session="regular", net=10.0, decision_id="DA-REUSED",
    )

    plan = build_plan_vs_actual(connection, "2026-11-09")

    assert plan["planned_count"].value == 1
    assert plan["executed_count"].value == 1
    assert plan["missed_count"].value == 0
    assert plan["unplanned_count"].value == 0
    assert plan["attribution_ambiguous_count"].value == 0


def test_exit_date_mismatch_is_p0_anomaly():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T00:30:00", exit_time="2026-09-19T00:30:00",
        session="regular", net=-10.0,
    )

    mismatches = exit_date_mismatches(connection)
    anomalies = build_anomalies(connection, "2026-09-18")

    assert mismatches[0]["sqlite_date"] == "2026-09-19"
    assert mismatches[0]["et_date"] == "2026-09-18"
    assert anomalies[0]["kind"] == "exit_date_mismatch"
    assert anomalies[0]["severity"] == "P0"
    assert anomalies[0]["trade_id"] == 1


def test_empty_day_is_zero_not_fabricated():
    connection = _create_db()

    bundle = build(connection, "2026-09-18")
    bundle.validate()

    assert bundle.headline["trade_count"].value == 0
    assert bundle.headline["net_pnl"].value == 0.0
    assert bundle.sections["slices"] == {}
    assert bundle.anomalies == []
