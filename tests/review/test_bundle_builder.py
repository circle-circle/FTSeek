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
          source_type text,
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
          direction text not null
        );
        """
    )
    return connection


def _insert_trade(connection, *, symbol, direction, entry_time, exit_time,
                  session, net, gross=None, fees=0.0, mfe=1.0, quality="real"):
    connection.execute(
        "insert into trade_records(trade_date,symbol,direction,entry_time,exit_time,"
        "entry_session,exit_reason,strategy_id,source_type,realized_pnl,pnl_net,"
        "fees_est,mfe_pct,mae_pct,quantity,data_quality) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            exit_time[:10], symbol, direction, entry_time, exit_time, session,
            "TEST", "test", "PLAYBOOK_SIM", gross if gross is not None else net,
            net, fees, mfe, 0.0, 1, quality,
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
        "insert into playbook_instructions(date,symbol,direction) values (?,?,?)",
        [
            ("2026-09-18", "US.A", "BUY"),
            ("2026-09-18", "US.B", "SELL_SHORT"),
            ("2026-09-18", "US.C", "BUY"),
        ],
    )
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=10.0,
    )
    _insert_trade(
        connection, symbol="US.C", direction="SHORT",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=10.0,
    )

    plan = build_plan_vs_actual(connection, "2026-09-18")

    assert plan["planned_count"].value == 3
    assert plan["executed_count"].value == 1
    assert plan["missed_count"].value == 2
    assert plan["unplanned_count"].value == 1
    assert plan["execution_rate"].value == 0.333333
    assert plan["direction_mismatch"].value == ["US.C(BUY/SELL_SHORT)"]


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
