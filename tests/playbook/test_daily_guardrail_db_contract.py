from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
EXECUTION_FLOW = REPO / "app" / "playbook" / "pipeline_parts" / "execution_flow.py"


def test_daily_guardrail_uses_routed_db_and_fails_closed():
    source = EXECUTION_FLOW.read_text(encoding="utf-8")
    segment = source[source.index("def load_budget_and_guardrails"):]

    assert "/opt/ftseek/FTSeek-US/data/ftseek_us.db" not in segment
    assert "get_trade_records_db" in segment
    assert "safe_sqlite_connect" in segment
    assert "daily_guardrail_query_failed" in segment
