"""short_funnel_shadow_tag 输入链告警降噪契约 (DEB-20261028)."""

import json
import logging
import sqlite3

from scripts import short_funnel_shadow_tag as tag


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "shadow.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE playbook_instructions "
        "(symbol TEXT, direction TEXT, executed INTEGER, skip_reason TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    out = tmp_path / "short_funnel_shadow.jsonl"
    monkeypatch.setattr(tag, "DB", db)
    monkeypatch.setattr(tag, "LOG_DIR", log_dir)
    monkeypatch.setattr(tag, "OUT", out)
    monkeypatch.setattr(tag, "_INSUFFICIENT_WARNED_DATES", set())
    return log_dir, out


def _write_two_ratings(path):
    path.write_text(
        "深度分析 1/3: US.AAA\n评级: 看空 (3/10)\n"
        "深度分析 2/3: US.BBB\n评级: 观望 (5/10)\n",
        encoding="utf-8",
    )


def test_empty_current_log_is_silent(tmp_path, monkeypatch, caplog):
    log_dir, out = _setup(tmp_path, monkeypatch)
    (log_dir / "da_pipeline_stderr.log").write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="ftseek.short_funnel_shadow"):
        tag.main()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not out.exists()


def test_nonempty_short_log_warns_only_once_per_date(tmp_path, monkeypatch, caplog):
    log_dir, out = _setup(tmp_path, monkeypatch)
    log = log_dir / "da_pipeline_stderr.log-20261028"  # 文件日期为次日, 交易日=10-27
    _write_two_ratings(log)

    with caplog.at_level(logging.WARNING, logger="ftseek.short_funnel_shadow"):
        tag.main()
        first_warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        tag.main()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(first_warnings) == 1
    assert len(warnings) == 1
    assert "2026-10-27" in warnings[0].getMessage()
    assert not out.exists()


def test_three_ratings_writes_record_without_insufficient_warning(tmp_path, monkeypatch, caplog):
    log_dir, out = _setup(tmp_path, monkeypatch)
    (log_dir / "da_pipeline_stderr.log-20261028").write_text(
        "深度分析 1/3: US.AAA\n评级: 看空 (3/10)\n"
        "深度分析 2/3: US.BBB\n评级: 观望 (5/10)\n"
        "深度分析 3/3: US.CCC\n评级: 看多 (8/10)\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="ftseek.short_funnel_shadow"):
        tag.main()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert out.exists()
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert records[0]["date"] == "2026-10-27"
    assert records[0]["analyzed_n"] == 3
