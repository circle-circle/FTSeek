import json
from datetime import date, datetime

from scripts import us_overnight_shadow as collector
from scripts import us_overnight_shadow_eval as evaluator


def test_window_skips_weekend_and_holiday_without_futu():
    holidays = {date(2026, 11, 26)}
    assert collector.in_window(datetime(2026, 11, 15, 20, 0), "20:00", holidays) is True
    assert collector.in_window(datetime(2026, 11, 13, 20, 0), "20:00", holidays) is False
    assert collector.in_window(datetime(2026, 11, 14, 2, 0), "20:00", holidays) is False
    assert collector.in_window(datetime(2026, 11, 16, 2, 0), "20:00", holidays) is True
    assert collector.in_window(datetime(2026, 11, 25, 20, 0), "20:00", holidays) is False
    assert collector.in_window(datetime(2026, 11, 26, 2, 0), "20:00", holidays) is False


def test_cross_midnight_uses_one_session_date():
    assert collector._session_date(datetime(2026, 11, 25, 21, 0), "20:00") == date(2026, 11, 25)
    assert collector._session_date(datetime(2026, 11, 26, 2, 0), "20:00") == date(2026, 11, 25)


def test_evaluator_dedupes_latest_and_counts_bad_rows(tmp_path):
    path = tmp_path / "2026-11-25.jsonl"
    rows = [
        {"symbol": "US.SPY", "ts": "2026-11-25T21:00:00-05:00", "overnight_price": 100},
        {"symbol": "US.SPY", "ts": "2026-11-25T22:00:00-05:00", "overnight_price": 101},
        {"symbol": "US.QQQ", "ts": "2026-11-25T21:30:00-05:00", "overnight_price": 200},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot-json\n", encoding="utf-8")
    result = evaluator.evaluate_file(path)
    assert result["status"] == "OK" and result["exit_code"] == 0
    assert result["n_rows"] == 3 and result["n_symbols"] == 2 and result["bad_rows"] == 1
    spy = next(row for row in result["latest_by_symbol"] if row["symbol"] == "US.SPY")
    assert spy["overnight_price"] == 101


def test_evaluator_missing_file_is_nonzero(tmp_path, capsys):
    code = evaluator.main(["--dir", str(tmp_path), "--date", "2026-11-25", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["status"] == "NO_DATA"


def test_overnight_shadow_is_registered_as_permanent_measure():
    from app.playbook.shadow_registry import REGISTRY
    item = next(row for row in REGISTRY if row.id == "us_overnight_shadow")
    assert item.data_path == "data/shadow/us_overnight"
    assert item.eval_script == "scripts/us_overnight_shadow_eval.py"
    assert item.eval_on_demand is True
    assert "PERMANENT_MEASURE" in item.no_adjudication_reason
