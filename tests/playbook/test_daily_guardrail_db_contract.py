from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PIPELINE = REPO / "app" / "playbook" / "pipeline.py"


def test_daily_guardrail_uses_routed_db_and_fails_closed():
    source = PIPELINE.read_text(encoding="utf-8")
    start = source.index("# 2026-07-09: 从DB查当日已开仓notional/count")
    end = source.index("# 按来源优先级 + confidence 降序", start)
    segment = source[start:end]

    assert "/opt/ftseek/FTSeek-US/data/ftseek_us.db" not in segment
    assert "get_trade_records_db" in segment
    assert "safe_sqlite_connect" in segment
    assert "daily_guardrail_query_failed" in segment
