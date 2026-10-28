"""A0 invalidation contract: compile, validate, and audit without actions."""
import importlib.util
import json
import sqlite3
from pathlib import Path

from app.playbook.invalidation_contract import (
    ACTION_REVIEW_ONLY,
    compile_invalidation_contract,
    compile_structured_rules,
    extract_text_price,
    validate_invalidation_contract,
)


ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    path = ROOT / "scripts" / "invalidation_contract_audit.py"
    spec = importlib.util.spec_from_file_location("invalidation_contract_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load_script()


def _raw_crm():
    return json.dumps({"invalidation_triggers": [
        {"metric": "收盘价", "operator": "<", "value": 239.5, "deadline": "2026-11-10"},
        {"metric": "5日涨跌", "operator": "<", "value": -5.0, "deadline": "2026-11-10"},
    ]})


def test_crm_compiles_both_sources_and_is_review_only():
    contract = compile_invalidation_contract(
        invalidation="放量跌破243.45(MA10)且主力资金持续净流出，W底形态失效",
        raw_instruction_json=_raw_crm(),
        direction="BUY",
        entry_price=244.55,
    )
    assert contract["schema_version"] == "invalidation_contract_v1"
    assert contract["source"] == "both"
    assert contract["action"] == ACTION_REVIEW_ONLY
    assert contract["parseable"] is True
    assert contract["text_parseable"] is True
    assert contract["legacy_text_parseable"] is True
    assert contract["text_parse_status"] == "parseable"
    assert contract["text_compound_condition"] is True
    assert contract["requires_confirmation"] is True
    assert contract["deadline"] == "2026-11-10"
    assert contract["reanchor_on_reprice"] == "stale"
    assert contract["precedence"] == ["hard_sl", "structured", "text"]
    assert len(contract["rules"]) == 3
    assert [rule["rule_id"] for rule in contract["rules"]] == [
        "structured:0", "structured:1", "text:price:0"
    ]
    assert contract["rules"][-1]["value"] == 243.45
    assert contract["price_basis"] == "mixed"
    assert contract["contract_hash"]
    assert validate_invalidation_contract(contract) == []


def test_structured_only_is_machine_parseable_and_no_text_confirmation():
    contract = compile_invalidation_contract(
        invalidation="",
        raw_instruction_json=_raw_crm(),
        direction="BUY",
        entry_price=244.55,
    )
    assert contract["source"] == "structured"
    assert contract["text_parseable"] is None
    assert contract["requires_confirmation"] is False
    assert contract["action"] == ACTION_REVIEW_ONLY
    assert contract["deadline"] == "2026-11-10"


def test_unparseable_text_is_not_guessed_but_remains_review_only():
    contract = compile_invalidation_contract(
        invalidation="主力资金持续净流出且基本面恶化",
        raw_instruction_json="{}",
        direction="BUY",
    )
    assert contract["source"] == "text"
    assert contract["text_parseable"] is False
    assert contract["text_parse_status"] == "unparseable"
    assert contract["rules"] == []
    assert contract["parseable"] is False
    assert contract["action"] == ACTION_REVIEW_ONLY
    assert contract["requires_confirmation"] is True
    assert "text_unparseable" in contract["warnings"]


def test_structured_plus_unparseable_text_still_keeps_structured_rules():
    raw = json.dumps({"invalidation_triggers": [
        {"metric": "收盘价", "operator": ">", "value": 70.5, "deadline": "2026-10-28"},
    ]})
    contract = compile_invalidation_contract(
        invalidation="盘中放量突破MA5(70.10)及70.50压力位",
        raw_instruction_json=raw,
        direction="SELL_SHORT",
    )
    assert contract["source"] == "both"
    assert contract["text_parseable"] is False
    assert len(contract["rules"]) == 1
    assert contract["rules"][0]["metric"] == "收盘价"
    assert contract["deadline"] == "2026-10-28"
    assert contract["action"] == ACTION_REVIEW_ONLY


def test_invalid_structured_entries_are_reported_not_compiled():
    raw = json.dumps({"invalidation_triggers": [
        {"metric": "未知指标", "operator": "<", "value": 1},
        {"metric": "收盘价", "operator": "=", "value": 1},
        {"metric": "收盘价", "operator": "<", "value": "not-a-number"},
        "bad-entry",
        {"metric": "收盘价", "operator": "<", "value": 99, "deadline": "2026-11-03"},
    ]})
    rules, rejected, raw_count = compile_structured_rules(raw)
    assert raw_count == 5
    assert len(rules) == 1
    assert len(rejected) == 4


def test_no_inputs_compiles_to_empty_review_only_contract():
    contract = compile_invalidation_contract(
        invalidation="", raw_instruction_json="{}", direction="BUY"
    )
    assert contract["source"] == "none"
    assert contract["parseable"] is False
    assert contract["rules"] == []
    assert contract["action"] == ACTION_REVIEW_ONLY
    assert validate_invalidation_contract(contract) == []


def test_validator_rejects_any_non_review_action():
    contract = compile_invalidation_contract(
        invalidation="跌破 243.45", raw_instruction_json="{}", direction="BUY"
    )
    contract["action"] = "EXIT"
    assert "a0_action_must_be_REVIEW_ONLY" in validate_invalidation_contract(contract)


def test_extract_text_price_picks_the_near_anchor_number():
    """2026-10-28 接管修复: 原实现取第一处匹配 (10.0 → 判退化), 现取离锚价最近的
    合法价 237.23。'不猜' 的不变量改由真正的跨资产/窗口数字守 (见下一个用例)。"""
    assert extract_text_price(
        "放量跌破243.45(MA10)且资金流出", entry_price=244.55
    ) == 243.45
    assert extract_text_price(
        "跌破10-21低点237.23及MA10", entry_price=247.22
    ) == 237.23
    assert extract_text_price(
        "恒生科技指数跌破24000点", entry_price=247.22
    ) is None
    assert extract_text_price("基本面恶化", entry_price=244.55) is None
    assert extract_text_price("", entry_price=244.55) is None


def test_degenerate_date_fragment_is_not_compiled_as_a_price_rule():
    """不猜的不变量: 只有日期/窗口/跨资产数字时, 契约不得编出一个价格规则。

    原用例用 "跌破10-21低点237.23…" 断言退化 —— 那一句其实含可用价 237.23,
    2026-10-28 修复后应被采纳 (见 tests/test_invalidation_text_price_v2.py);
    真正的退化样本改用跨资产指数价。
    """
    contract = compile_invalidation_contract(
        invalidation="恒生科技指数跌破24000点，或恒指失守18000",
        raw_instruction_json="{}",
        direction="BUY",
        entry_price=247.22,
    )
    assert contract["source"] == "text"
    assert contract["legacy_text_parseable"] is True
    assert contract["text_parseable"] is False
    assert contract["text_parse_status"] == "degenerate"
    assert contract["rules"] == []
    assert contract["action"] == ACTION_REVIEW_ONLY
    assert "text_degenerate_price" in contract["warnings"]


def _make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
        "entry_time TEXT, realized_pnl REAL, pnl_net REAL, metadata_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE playbook_instructions (id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, "
        "direction TEXT, entry_price REAL, stop_loss REAL, invalidation TEXT, "
        "raw_instruction_json TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?)",
        (4796, "2026-10-27", "US.CRM", "BUY", 244.55, 232.32,
         "放量跌破243.45(MA10)且主力资金持续净流出", _raw_crm(), "{}"),
    )
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?)",
        (4800, "2026-10-27", "US.TEXT", "BUY", 100.0, 95.0,
         "跌破 96.0", "{}", "{}"),
    )
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?)",
        (4801, "2026-10-27", "US.NONE", "BUY", 50.0, 47.0, "", "{}", "{}"),
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)",
        (1729, "US.CRM", "LONG", "2026-10-27T09:51:03-04:00", -11.0, -10.0,
         json.dumps({"playbook_instruction_id": 4796, "stop_loss": 232.32})),
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)",
        (1731, "US.TEXT", "LONG", "2026-10-27T10:00:00-04:00", 4.0, 5.0,
         json.dumps({"playbook_instruction_id": 4800, "stop_loss": 95.0})),
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)",
        (1732, "US.NONE", "LONG", "2026-10-27T10:30:00-04:00", 2.0, 2.0,
         json.dumps({"playbook_instruction_id": 4801, "stop_loss": 47.0})),
    )
    conn.commit()
    conn.close()


def test_cli_audit_reproduces_structured_class_and_writes_jsonl(tmp_path, capsys):
    db = tmp_path / "inv.db"
    _make_db(db)
    rc = M.main([
        "--db", str(db), "--since", "2026-10-27", "--until", "2026-10-27",
        "--audit-dir", str(tmp_path / "audits"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out.split("\n[invalidation-contract]")[0])
    assert summary["trade_rows_joined"] == 3
    assert summary["contract"]["source_counts"] == {
        "both": 1, "text": 1, "none": 1,
    }
    assert summary["contract"]["action_counts"] == {"REVIEW_ONLY": 3}
    assert summary["contract"]["structured_trades"] == 1
    assert summary["contract"]["structured_with_close_trigger"] == 1
    assert summary["stop_vs_invalidation"]["structured_sample"] == 1
    assert summary["stop_vs_invalidation"]["counts"] == {
        "invalidation_before_stop": 1
    }
    assert summary["stop_vs_invalidation"]["pnl_net_sum"] == {
        "invalidation_before_stop": -10.0
    }
    assert summary["a0_action_is_review_only"] is True
    assert summary["contract"]["validation_errors"] == {}
    assert Path(summary["audit_path"]).exists()


def test_cli_no_write_does_not_create_audit_file(tmp_path, capsys):
    db = tmp_path / "inv.db"
    _make_db(db)
    audit_dir = tmp_path / "audits"
    rc = M.main([
        "--db", str(db), "--since", "2026-10-27", "--until", "2026-10-27",
        "--audit-dir", str(audit_dir), "--no-write",
    ])
    assert rc == 0
    capsys.readouterr()
    assert not audit_dir.exists()
