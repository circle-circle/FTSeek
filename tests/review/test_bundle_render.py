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


# ─────────────────────────────────────────────────────────────────────────────
# 推送白话注释 (2026-09-22 用户令「需要推送 添加容易理解的注释」)
# 推送面必须自解释: 收货人不在终端, 术语就地翻译 + 数字就地给口径.
# ─────────────────────────────────────────────────────────────────────────────


def test_push_carries_glossary_with_caliber_numbers():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=125.0, gross=130.0, fees=5.0, mfe=0.0,
    )
    bundle = build(connection, "2026-09-18")

    _full, push_text = render_md(bundle)

    assert "白话：" in push_text, "推送必须带白话注释行"
    assert "「净」=毛利−费用" in push_text
    assert "毛 +$130.00" in push_text and "费 +$5.00" in push_text, (
        "白话行必须就地给出本日毛/费 (口径可见, 不是要读者自己算)")
    assert "（已平仓·按退出日·美元）" in push_text, "抬头须标口径 (窗口/口径/单位)"
    assert "「计划外」=系统自己加的单" in push_text
    assert "「错过」" in push_text and "「P0 异常」" in push_text


def test_glossary_never_invents_numbers_when_headline_lacks_gross_or_fees():
    """缺失即省: headline 没给毛/费时只保留口径文字, 不得编造数字 (禁自造叙事)."""
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=25.0,
    )
    bundle = build(connection, "2026-09-18")
    payload = bundle.to_dict()
    payload["headline"].pop("gross_pnl", None)
    payload["headline"].pop("fees", None)
    from app.review.bundle_render import _push_glossary
    text = _push_glossary(payload["headline"], [])
    assert "「净」=毛利−费用" in text
    assert "$" not in text.split("「净」=毛利−费用")[1][:20], "无数据时不得出现金额"
    assert "「待裁决」" not in text, "无 ready 行时不该解释待裁决"


def test_glossary_explains_pending_and_points_to_plain_list():
    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=25.0,
    )
    bundle = build(connection, "2026-09-18")
    bundle.pending_adjudications = [
        {"id": "ready-0", "status": "READY_TO_EVALUATE", "progress": "30/30",
         "ready": True, "ready_since": "2026-09-15", "criteria": "c"},
    ]
    _full, push_text = render_md(bundle)
    assert "「待裁决」" in push_text and "样本够 ≠ 判据通过" in push_text, (
        "必须点破 ready 只代表样本量够 (否则读者以为判据已过)")
    assert "最久 2026-09-15 起" in push_text, "须给出最久等待日期 (urgency)"
    assert "影子验收清单" in push_text, "须指向白话清单出处"
