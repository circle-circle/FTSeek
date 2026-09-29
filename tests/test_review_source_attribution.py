import sqlite3

from app.review.daily_review import DailyReviewGenerator
from app.review.source_attribution import aggregate_source_direction, playbook_source_sql
from app.review.weekly_review import WeeklyReviewGenerator
from app.core.scheduler_triggers import _format_daily_review, _format_weekly_review
from scripts.direction_hit_scorecard import src_expr


RAW_SOURCES = [
    ('{"source":"deep_analysis"}', "DA自选"),
    ('{"source":"deep_analysis","origin":"ue_candidate"}', "DA-UE确认"),
    ('{"source":"universe_expansion"}', "UE直写"),
    ('{"source":"topup_pool"}', "topup"),
]


def test_playbook_source_sql_separates_da_ue_confirmation():
    connection = sqlite3.connect(":memory:")
    # [rfc117_v2] decision_id 列: UE SKIP 行 raw_json 无 universe_expansion 字面量,
    #   仅靠 decision_id 前缀归因 (真实 DB 9 条历史行实证)。
    connection.execute("CREATE TABLE t (raw_instruction_json TEXT, decision_id TEXT)")
    connection.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(raw, f"other:{i}") for i, (raw, _) in enumerate(RAW_SOURCES)]
        + [('{"admission": {"verdict": "PASS"}}', "universe_expansion:2026-09-02:ADBT")],
    )
    rows = connection.execute(
        f"SELECT ({playbook_source_sql()}) AS source FROM t ORDER BY rowid"
    ).fetchall()
    assert [row[0] for row in rows] == [expected for _, expected in RAW_SOURCES] + ["UE直写"]
    assert src_expr() == playbook_source_sql()


def test_aggregate_source_direction_includes_execution_state():
    rows = [
        {"source": "DA-UE确认", "direction": "BUY", "executed": 1, "cnt": 2},
        {"source": "DA-UE确认", "direction": "SELL_SHORT", "executed": 0, "cnt": 1},
    ]
    assert aggregate_source_direction(rows) == {
        "DA-UE确认·BUY": 2,
        "DA-UE确认·SELL_SHORT(未执行)": 1,
    }


def test_daily_signal_funnel_reports_source_direction(tmp_path, monkeypatch):
    db_path = tmp_path / "ftseek_us.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE playbook_instructions "
        "(date TEXT, direction TEXT, executed INTEGER, raw_instruction_json TEXT, "
        "decision_id TEXT)"
    )
    connection.execute(
        "INSERT INTO playbook_instructions VALUES "
        "('2026-09-29', 'BUY', 1, '{\"source\":\"deep_analysis\",\"origin\":\"ue_candidate\"}', 'DA-x')"
    )
    connection.execute(
        "INSERT INTO playbook_instructions VALUES "
        "('2026-09-29', 'SELL_SHORT', 0, '{\"source\":\"universe_expansion\"}', 'universe_expansion:2026-09-29:XYZ')"
    )
    # [rfc117_v2] UE SKIP 行: raw_json 无源字面量, 仅 decision_id 前缀可归因
    connection.execute(
        "INSERT INTO playbook_instructions VALUES "
        "('2026-09-29', 'SKIP', 0, '{\"admission\": {\"verdict\": \"PASS\"}}', 'universe_expansion:2026-09-29:ADBT')"
    )
    connection.commit()
    connection.close()
    generator = DailyReviewGenerator()
    monkeypatch.setattr(generator, "db_path", db_path)
    funnel = generator._get_signal_funnel("2026-09-29")
    assert funnel["source_attribution_version"] == "rfc117_v2"
    assert funnel["by_source_direction"] == {
        "DA-UE确认·BUY": 1,
        "UE直写·SELL_SHORT(未执行)": 1,
        "UE直写·SKIP(未执行)": 1,
    }


def test_weekly_signal_funnel_reports_source_direction(tmp_path, monkeypatch):
    db_path = tmp_path / "ftseek_us.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE playbook_instructions "
        "(date TEXT, direction TEXT, executed INTEGER, raw_instruction_json TEXT, "
        "decision_id TEXT)"
    )
    connection.execute(
        "INSERT INTO playbook_instructions VALUES "
        "('2026-09-28', 'BUY', 1, '{\"source\":\"deep_analysis\",\"origin\":\"ue_candidate\"}', 'DA-y')"
    )
    connection.commit()
    connection.close()
    generator = WeeklyReviewGenerator()
    monkeypatch.setattr(generator, "db_path", db_path)
    monkeypatch.setattr(generator, "output_dir", tmp_path)
    funnel = generator._get_weekly_signal_funnel("2026-09-28", "2026-09-29")
    assert funnel["source_attribution_version"] == "rfc117_v2"
    assert funnel["by_source_direction"] == {"DA-UE确认·BUY": 1}


def test_daily_and_weekly_push_include_source_breakdown():
    funnel = {
        "source": "playbook_instructions",
        "total_decisions": 2,
        "by_source_direction": {"DA-UE确认·BUY": 1, "UE直写·SELL_SHORT(未执行)": 1},
    }
    daily = _format_daily_review({
        "report_date": "2026-09-29",
        "report_version": "2.0",
        "summary": {},
        "signal_funnel": funnel,
    })
    weekly = _format_weekly_review({
        "period_start": "2026-09-28",
        "period_end": "2026-09-29",
        "summary": {},
        "signal_funnel": funnel,
    })
    expected = "来源: DA-UE确认·BUY:1, UE直写·SELL_SHORT(未执行):1"
    assert expected in daily
    assert expected in weekly
