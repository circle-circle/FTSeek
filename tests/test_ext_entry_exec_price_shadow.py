#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_executor():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec_price_shadow", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shadow_threshold_is_session_specific(monkeypatch):
    module = _load_executor()
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW_MIN_PREMARKET", 50.0)
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW_MIN_AFTERHOURS", 75.0)

    assert module._execution_price_shadow_threshold("premarket") == 50.0
    assert module._execution_price_shadow_threshold("afterhours") == 75.0


def test_shadow_is_disabled_by_default_and_writes_nothing(tmp_path, monkeypatch):
    module = _load_executor()
    output = tmp_path / "exec-price-shadow.jsonl"
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW", output)
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW_ON", False)

    module._record_execution_price_shadow(
        "premarket", [{"symbol": "US.AAPL", "px": 200.0, "chg": 3.0, "vol": 100000.0}]
    )

    assert not output.exists()


def test_shadow_records_counterfactual_without_changing_current_gate(tmp_path, monkeypatch, capsys):
    module = _load_executor()
    output = tmp_path / "exec-price-shadow.jsonl"
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW", output)
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW_ON", True)
    monkeypatch.setattr(module, "MIN_PRICE", 5.0)
    monkeypatch.setattr(module, "POSITION_USD", 5000.0)
    monkeypatch.setattr(module, "EXEC_PRICE_SHADOW_MIN_PREMARKET", 50.0)
    candidates = [
        {"symbol": "US.LOW", "px": 4.0, "chg": 3.0, "vol": 100000.0},
        {"symbol": "US.MID", "px": 20.0, "chg": 3.0, "vol": 100000.0},
        {"symbol": "US.HIGH", "px": 60.0, "chg": 3.0, "vol": 100000.0},
        {"symbol": "US.TOO_HIGH", "px": 6000.0, "chg": 3.0, "vol": 100000.0},
    ]

    module._record_execution_price_shadow("premarket", candidates)

    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["mode"] == "record_only"
    assert record["price_threshold"] == 50.0
    assert record["price_unit"] == "USD"
    assert record["volume_unit"] == "shares"
    assert record["candidates_n"] == 4
    assert record["current_price_pool_n"] == 2
    assert record["shadow_execution_pool_n"] == 1
    assert record["prefiltered_out_n"] == 1
    assert [item["symbol"] for item in record["prefiltered_out"]] == ["US.MID"]
    assert [item["symbol"] for item in record["shadow_execution_pool"]] == ["US.HIGH"]
    assert "现行价池=2 影子价池=1" in capsys.readouterr().out


def test_executor_has_shadow_hooks_in_dry_and_live_paths():
    source = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text(
        encoding="utf-8"
    )

    assert source.count("_record_execution_price_shadow(session, movers)") == 3
