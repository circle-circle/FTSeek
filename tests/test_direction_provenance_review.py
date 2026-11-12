import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.core.obsidian_writer import ObsidianWriter  # noqa: E402
from app.review.daily_review import DailyReviewGenerator  # noqa: E402


def test_plan_vs_actual_tags_ue_da_reverse_with_challenger_abstain(monkeypatch):
    generator = DailyReviewGenerator()

    actionable_rows = [
        {"id": 1, "symbol": "US.CAT", "direction": "BUY", "decision_id": "DA-CAT"},
        {"id": 2, "symbol": "US.TMUS", "direction": "SELL_SHORT", "decision_id": "DA-TMUS"},
    ]
    trade_rows = [
        {"symbol": "US.CAT", "direction": "LONG", "decision_id": "DA-CAT"},
        {"symbol": "US.TMUS", "direction": "SHORT", "decision_id": "DA-TMUS"},
    ]
    detailed_rows = [
        {
            "id": 1,
            "symbol": "US.CAT",
            "direction": "BUY",
            "executed": 1,
            "execution_state": "BOOKED",
            "skip_reason": "",
            "decision_id": "DA-CAT",
            "raw_instruction_json": json.dumps({
                "source": "deep_analysis",
                "debate_outcome": "方向一致",
                "ue_direction": "",
            }),
        },
        {
            "id": 2,
            "symbol": "US.TMUS",
            "direction": "SKIP",
            "executed": 0,
            "execution_state": "PENDING",
            "skip_reason": "entry_confirm_reversed",
            "decision_id": "DA-TMUS",
            "raw_instruction_json": json.dumps({
                "source": "deep_analysis",
                "debate_outcome": "挑战者弃权(SKIP)",
                "ue_direction": "LONG",
            }),
        },
    ]

    def fake_query(sql, params=(), db_path=None):
        if "raw_instruction_json" in sql:
            return detailed_rows
        if "FROM trade_records" in sql:
            return trade_rows
        if "FROM playbook_instructions" in sql:
            return actionable_rows
        return []

    monkeypatch.setattr(generator, "_query", fake_query)
    monkeypatch.setattr(
        generator,
        "_load_entry_confirm_directions",
        lambda date: {"DA-TMUS": {"direction": "SELL_SHORT", "outcome": "ABANDONED"}},
    )

    result = generator._get_plan_vs_actual("2026-10-13")
    provenance = result["direction_provenance"]
    tmus = next(row for row in result["planned_details"] if row["symbol"] == "US.TMUS")

    assert tmus["original_direction"] == "SELL_SHORT"
    assert tmus["final_direction"] == "SKIP"
    assert tmus["ue_direction"] == "LONG"
    assert tmus["ue_da_reverse"] is True
    assert tmus["challenger_abstain"] is True
    assert "UE_DA_REVERSE_CHALLENGER_ABSTAIN" in tmus["risk_tags"]
    assert provenance["original_buy"] == 1
    assert provenance["original_sell_short"] == 1
    assert provenance["entry_confirm_abandoned"] == 1
    assert provenance["ue_da_reverse_challenger_abstain"] == 1
    assert provenance["ue_da_reverse_challenger_abstain_symbols"] == ["US.TMUS"]


def test_overnight_risk_uses_exit_day_and_net_pnl():
    generator = DailyReviewGenerator()
    result = generator._analyze_overnight_risk(
        "2026-10-13",
        [
            {
                "trade_date": "2026-10-09",
                "symbol": "US.AMD",
                "exit_time": "2026-10-13T09:30:58",
                "exit_reason": "OVERNIGHT_GAP_STOP",
                "pnl_net": -357.49,
            },
            {
                "trade_date": "2026-10-13",
                "symbol": "US.META",
                "exit_time": "2026-10-13T10:11:14",
                "exit_reason": "SL_TRAIL_LOCK",
                "pnl_net": 155.10,
            },
        ],
    )

    assert result["legacy_exit_count"] == 1
    assert result["legacy_exit_net_pnl"] == -357.49
    assert result["overnight_gap_stop_count"] == 1
    assert result["worst_legacy_symbol"] == "US.AMD"
    assert result["attention_required"] is True


def test_entry_confirm_direction_mapping_normalizes_legacy_values(tmp_path):
    ledger = tmp_path / "entry_confirm_forward.jsonl"
    ledger.write_text(
        json.dumps({
            "date": "2026-10-13",
            "decision_id": "DA-TMUS",
            "direction": "SHORT",
            "outcome": "ABANDONED",
        }) + "\n",
        encoding="utf-8",
    )
    generator = DailyReviewGenerator()
    result = generator._load_entry_confirm_directions(
        "2026-10-13", ledger_path=ledger
    )

    assert result["DA-TMUS"]["direction"] == "SELL_SHORT"


def test_plan_vs_actual_survives_when_all_plans_entry_confirm_abandoned(monkeypatch):
    generator = DailyReviewGenerator()
    skip_row = {
        "id": 1,
        "symbol": "US.TEST",
        "direction": "SKIP",
        "decision_id": "DA-TEST",
    }
    detailed_row = {
        "id": 1,
        "symbol": "US.TEST",
        "direction": "SKIP",
        "executed": 0,
        "execution_state": "PENDING",
        "skip_reason": "entry_confirm_reversed",
        "decision_id": "DA-TEST",
        "raw_instruction_json": json.dumps({"source": "deep_analysis"}),
    }

    def fake_query(sql, params=(), db_path=None):
        if "raw_instruction_json" in sql:
            return [detailed_row]
        if "FROM playbook_instructions" in sql:
            return [skip_row]
        return []

    monkeypatch.setattr(generator, "_query", fake_query)
    monkeypatch.setattr(
        generator,
        "_load_entry_confirm_directions",
        lambda date: {"DA-TEST": {"direction": "BUY", "outcome": "ABANDONED"}},
    )

    result = generator._get_plan_vs_actual("2026-10-14")
    provenance = result["direction_provenance"]

    assert result["plan_available"] is True
    assert result["planned_targets"] == ["US.TEST"]
    assert result["execution_rate"] == 0
    assert provenance["original_buy"] == 1
    assert provenance["entry_confirm_abandoned"] == 1
    assert provenance["original_direction_unknown"] == 0


def test_direction_provenance_excludes_existing_position_from_execution(monkeypatch):
    generator = DailyReviewGenerator()
    instruction_rows = [
        {"id": 1, "symbol": "US.AMD", "direction": "BUY", "decision_id": "DA-AMD"},
        {"id": 2, "symbol": "US.GOOG", "direction": "BUY", "decision_id": "DA-GOOG"},
    ]
    detailed_rows = [
        {
            "id": 1,
            "symbol": "US.AMD",
            "direction": "BUY",
            "executed": 1,
            "execution_state": "BOOKED",
            "outcome": "filled",
            "execution_price": 250.0,
            "skip_reason": "",
            "decision_id": "DA-AMD",
            "raw_instruction_json": json.dumps({"source": "deep_analysis"}),
        },
        {
            "id": 2,
            "symbol": "US.GOOG",
            "direction": "BUY",
            "executed": 1,
            "execution_state": "SKIPPED_EXISTING_POSITION",
            "outcome": "already_held",
            "execution_price": 0.0,
            "skip_reason": "",
            "decision_id": "DA-GOOG",
            "raw_instruction_json": json.dumps({"source": "deep_analysis"}),
        },
    ]

    def fake_query(sql, params=(), db_path=None):
        if "raw_instruction_json" in sql:
            assert "outcome" in sql
            assert "execution_price" in sql
            return detailed_rows
        if "FROM trade_records" in sql:
            return [{
                "id": 10,
                "symbol": "US.AMD",
                "direction": "LONG",
                "decision_id": "DA-AMD",
                "metadata_json": "{}",
            }]
        if "FROM playbook_instructions" in sql:
            return instruction_rows
        return []

    monkeypatch.setattr(generator, "_query", fake_query)
    monkeypatch.setattr(generator, "_load_entry_confirm_directions", lambda date: {})

    result = generator._get_plan_vs_actual("2026-11-10")
    provenance = result["direction_provenance"]
    goog = next(row for row in result["planned_details"] if row["symbol"] == "US.GOOG")

    assert result["executed_count"] == 1
    assert provenance["executed"] == result["executed_count"]
    assert provenance["existing_position_skipped"] == 1
    assert provenance["other_blocked"] == 0
    assert goog["final_state"] == "SKIPPED_EXISTING_POSITION"


def test_direction_provenance_reports_unknown_when_ledger_is_missing(monkeypatch):
    generator = DailyReviewGenerator()
    instruction_rows = [
        {"id": 1, "symbol": "US.EXEC", "direction": "BUY", "decision_id": "DA-EXEC"},
        {"id": 2, "symbol": "US.SKIP", "direction": "SKIP", "decision_id": "DA-SKIP"},
    ]
    detailed_rows = [
        {
            "id": 1,
            "symbol": "US.EXEC",
            "direction": "BUY",
            "executed": 1,
            "execution_state": "BOOKED",
            "skip_reason": "",
            "decision_id": "DA-EXEC",
            "raw_instruction_json": json.dumps({"source": "deep_analysis"}),
        },
        {
            "id": 2,
            "symbol": "US.SKIP",
            "direction": "SKIP",
            "executed": 0,
            "execution_state": "PENDING",
            "skip_reason": "entry_confirm_reversed",
            "decision_id": "DA-SKIP",
            "raw_instruction_json": json.dumps({"source": "deep_analysis"}),
        },
    ]

    def fake_query(sql, params=(), db_path=None):
        if "raw_instruction_json" in sql:
            return detailed_rows
        if "FROM playbook_instructions" in sql:
            return instruction_rows
        return []

    monkeypatch.setattr(generator, "_query", fake_query)
    monkeypatch.setattr(
        generator,
        "_load_entry_confirm_directions",
        lambda date: {},
    )

    result = generator._get_plan_vs_actual("2026-10-14")
    provenance = result["direction_provenance"]

    assert provenance["original_buy"] == 1
    assert provenance["original_direction_unknown"] == 1
    assert provenance["original_direction_unknown_symbols"] == ["US.SKIP"]
    assert provenance["entry_confirm_abandoned"] == 1


def test_postmarket_renders_direction_and_overnight_sections(tmp_path):
    writer = ObsidianWriter(vault_path=str(tmp_path))
    path = writer.write_daily_postmarket({
        "date": "2026-10-13",
        "market": "US",
        "total_trades": 8,
        "win_rate": 1.0,
        "total_pnl": 235.40,
        "trades": [],
        "direction_provenance": {
            "original_buy": 11,
            "original_sell_short": 5,
            "executed": 5,
            "entry_confirm_abandoned": 8,
            "existing_position_skipped": 1,
            "other_blocked": 3,
            "original_direction_unknown": 1,
            "original_direction_unknown_symbols": ["US.ORCL"],
            "ue_da_reverse_challenger_abstain": 1,
            "ue_da_reverse_challenger_abstain_symbols": ["US.TMUS"],
        },
        "overnight_risk": {
            "legacy_exit_count": 2,
            "legacy_exit_net_pnl": -308.98,
            "overnight_gap_stop_count": 1,
            "overnight_gap_stop_net_pnl": -357.49,
            "worst_legacy_symbol": "US.AMD",
            "worst_legacy_net_pnl": -357.49,
        },
    })
    text = Path(path).read_text(encoding="utf-8")

    assert "## 方向产出与执行状态" in text
    assert "BUY 11 | SELL_SHORT 5" in text
    assert "入场确认放弃 8" in text
    assert "**已有持仓跳过**: 1" in text
    assert "**原始方向缺失**: 1 (US.ORCL)" in text
    assert "**UE-DA 反向且挑战者弃权**: 1" in text
    assert "(US.TMUS)" in text
    assert "## 隔夜遗留仓风险" in text
    assert "**OVERNIGHT_GAP_STOP**: 1 笔" in text
    assert "US.AMD" in text


def _fake_plan_query(generator, monkeypatch, *, instructions, identity_rows, trades):
    def fake_query(sql, params=(), db_path=None):
        if "raw_instruction_json" in sql:
            return []
        if "FROM trade_records" in sql:
            return trades
        if "WHERE date = ?" in sql:
            return instructions
        if "FROM playbook_instructions" in sql:
            return identity_rows
        return []

    monkeypatch.setattr(generator, "_query", fake_query)
    monkeypatch.setattr(generator, "_load_entry_confirm_directions", lambda date: {})


def test_plan_vs_actual_dedupes_same_symbol_direction_reentry_rows(monkeypatch):
    """同 (date,symbol,direction) 多轮指令不得重复进分母/漏执数 (2026-11-12 回归)。"""
    generator = DailyReviewGenerator()
    instructions = [
        {"id": 1, "symbol": "US.APP", "direction": "BUY", "decision_id": "DA-APP-1"},
        {"id": 2, "symbol": "US.APP", "direction": "BUY", "decision_id": "DA-APP-2"},
    ]
    trades = [{
        "id": 10, "symbol": "US.APP", "direction": "LONG",
        "trade_date": "2026-11-10", "decision_id": "DA-APP-1", "metadata_json": None,
    }]
    _fake_plan_query(
        generator, monkeypatch,
        instructions=instructions,
        identity_rows=[{**row, "date": "2026-11-10"} for row in instructions],
        trades=trades,
    )

    result = generator._get_plan_vs_actual("2026-11-10")

    assert result["planned_count"] == 1
    assert result["planned_instruction_count"] == 2
    assert result["executed_count"] == 1
    assert result["executed_targets"] == ["US.APP"]
    assert result["missed_opportunities"] == []
    assert result["unplanned_trades"] == []
    assert result["execution_rate"] == 1.0


def test_plan_vs_actual_ambiguous_decision_id_is_not_unplanned(monkeypatch):
    generator = DailyReviewGenerator()
    instructions = [
        {"id": 1, "symbol": "US.APP", "direction": "BUY", "decision_id": "SHARED"},
        {"id": 2, "symbol": "US.APP", "direction": "BUY", "decision_id": "SHARED"},
    ]
    trades = [{
        "id": 10, "symbol": "US.APP", "direction": "LONG",
        "trade_date": "2026-11-10", "decision_id": "SHARED", "metadata_json": None,
    }]
    _fake_plan_query(
        generator, monkeypatch,
        instructions=instructions,
        identity_rows=[{**row, "date": "2026-11-10"} for row in instructions],
        trades=trades,
    )

    result = generator._get_plan_vs_actual("2026-11-10")

    assert result["attribution_ambiguous_count"] == 1
    assert result["attribution_ambiguous_symbols"] == ["US.APP"]
    assert result["unplanned_trades"] == []


def test_plan_vs_actual_resolves_cross_day_duplicate_decision_id(monkeypatch):
    generator = DailyReviewGenerator()
    instructions = [
        {"id": 2, "symbol": "US.APP", "direction": "BUY", "decision_id": "DA-REUSED"},
    ]
    identity_rows = [
        {"id": 1, "date": "2026-11-09", "symbol": "US.APP", "direction": "BUY",
         "decision_id": "DA-REUSED"},
        {"id": 2, "date": "2026-11-10", "symbol": "US.APP", "direction": "BUY",
         "decision_id": "DA-REUSED"},
    ]
    trades = [{
        "id": 10, "symbol": "US.APP", "direction": "LONG",
        "trade_date": "2026-11-10", "decision_id": "DA-REUSED", "metadata_json": None,
    }]
    _fake_plan_query(
        generator, monkeypatch,
        instructions=instructions, identity_rows=identity_rows, trades=trades,
    )

    result = generator._get_plan_vs_actual("2026-11-10")

    assert result["executed_targets"] == ["US.APP"]
    assert result["unplanned_trades"] == []
    assert result["attribution_ambiguous_count"] == 0
