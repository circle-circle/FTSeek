from app.review.bundle_builder import build
from app.review.bundle_render import render_md

from tests.review.test_bundle_builder import _create_db, _insert_trade


def test_render_contains_result_stage_plan_and_anomaly():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=125.0, gross=130.0, fees=5.0, mfe=0.0,
    )
    bundle = build(connection, "2026-09-18")

    full_md, push_text = render_md(bundle)

    assert "# FTSeek-US 每日复盘 2026-09-18" in full_md
    assert "| 净收益 | +$125.00 |" in full_md
    assert "| 盘中 | 1 | +$125.00 | +$130.00 |" in full_md
    assert "计划外交易" in full_md
    assert "证据" in full_md
    assert "2026-09-18" in push_text
    assert "净 +$125.00" in push_text


def test_render_explicitly_lists_p0_anomalies():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T00:30:00", exit_time="2026-09-19T00:30:00",
        session="regular", net=-10.0,
    )
    bundle = build(connection, "2026-09-18")

    full_md, push_text = render_md(bundle)

    assert "P0" in full_md
    assert "exit_date_mismatch" in full_md
    assert "P0 异常 1" in push_text


def test_render_reports_ready_adjudications_without_flooding_push():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=25.0,
    )
    bundle = build(connection, "2026-09-18")
    bundle.pending_adjudications = [
        {"id": f"ready-{index}", "status": "READY_TO_EVALUATE", "progress": "30/30",
         "ready": True, "ready_since": f"2026-09-1{index}", "criteria": "criteria"}
        for index in range(6)
    ] + [
        {"id": "observing", "status": "OBSERVING", "progress": "1/30", "ready": False,
         "criteria": "criteria C"},
    ]

    full_md, push_text = render_md(bundle)

    assert "待裁决" in full_md
    assert "ready-0" in full_md
    assert "ready-5" in full_md
    assert "observing" in full_md
    assert "待裁决 6" in push_text
    assert "ready-0" in push_text
    assert "ready-4" in push_text
    assert "ready-5" not in push_text
