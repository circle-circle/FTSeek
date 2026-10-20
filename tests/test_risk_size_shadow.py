"""RFC-155 前瞻记录器契约: 只记录不改交易, 旗标/乘数/收口。"""
from __future__ import annotations

import json
from datetime import datetime

import app.playbook.risk_size_shadow as rss


def _inst(**overrides):
    inst = {
        "id": 1, "symbol": "US.AAPL", "direction": "BUY",
        "decision_id": "DA-20261020-1", "entry_price": 100.0,
        "raw_instruction_json": json.dumps({"entry_archetype": "PULLBACK_LIMIT"}),
    }
    inst.update(overrides)
    return inst


def _now():
    return datetime(2026, 10, 20, 9, 40, 0)


def test_flags_multipliers_shared():
    flags = rss.flags_for({"atr_pct": 6.0, "session_min": 5, "source": "DA"})
    assert flags == {"atr5": True, "open15": True, "ue": False}
    assert rss.multiplier("atr5", flags) == 0.5
    assert rss.multiplier("ge2", flags) == 0.5
    assert rss.multiplier("ue", flags) == 1.0
    assert rss.multiplier("product", flags) == 0.25


def test_build_row_records_but_keeps_actual_size():
    row = rss.build_row(
        inst=_inst(), direction="BUY", atr_pct=6.0, size=100.0,
        notional=10000.0, entry_price=100.0, now_et=_now(),
        cfg={"enabled": True})
    assert row["actual_size"] == 100.0
    assert row["actual_notional"] == 10000.0
    assert row["r1_multiplier"] == 0.5
    assert row["r4_multiplier"] == 0.5
    assert row["cf_sizes"]["atr5"] == 50.0
    assert row["session_min"] == 10
    assert row["source"] == "DA"
    assert row["archetype"] == "PULLBACK_LIMIT"


def test_build_row_test_symbol_and_disabled_are_noop():
    assert rss.build_row(inst=_inst(symbol="US.TESTX"), direction="BUY",
                         atr_pct=6.0, size=10, notional=1000,
                         entry_price=100, now_et=_now(),
                         cfg={"enabled": True}) is None
    assert rss.build_row(inst=_inst(), direction="BUY", atr_pct=6.0,
                         size=10, notional=1000, entry_price=100,
                         now_et=_now(), cfg={"enabled": False}) is None


def test_finalize_success_clears_stale_skip_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(rss, "_FORWARD_PATH", tmp_path / "fwd.jsonl")
    row = rss.build_row(inst=_inst(skip_reason="old_block"), direction="BUY",
                        atr_pct=6.0, size=100, notional=10000,
                        entry_price=100, now_et=_now(), cfg={"enabled": True})
    assert rss.finalize_and_record(
        row, result={"success": True, "execution_price": 101.0}) is True
    saved = json.loads((tmp_path / "fwd.jsonl").read_text().splitlines()[0])
    assert saved["success"] is True and saved["executed"] is True
    assert saved["fill_price"] == 101.0
    assert saved["skip_reason"] is None
    assert saved["prior_skip_reason"] == "old_block"


def test_finalize_failure_and_exception_never_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(rss, "_FORWARD_PATH", tmp_path / "fwd.jsonl")
    row = rss.build_row(inst=_inst(), direction="BUY", atr_pct=6.0,
                        size=100, notional=10000, entry_price=100,
                        now_et=_now(), cfg={"enabled": True})
    assert rss.finalize_and_record(
        row, result={"success": False, "message": "no fill"}) is True
    saved = json.loads((tmp_path / "fwd.jsonl").read_text().splitlines()[0])
    assert saved["success"] is False and saved["message"] == "no fill"
    monkeypatch.setattr(rss, "_FORWARD_PATH", tmp_path / "no_dir" / "x.jsonl")
    (tmp_path / "no_dir").write_text("not a dir")
    assert rss.finalize_and_record(row, error="boom") is False
