"""Bucket lineage pure-function and offline-enrichment contracts (2026-10-28)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from app.playbook.bucket_lineage import (
    BUCKET_LINEAGE_SCHEMA,
    build_bucket_lineage,
    lineage_needs_update,
    select_vol_bucket,
)


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "playbook_horizon.yaml"


def _load_script():
    path = ROOT / "scripts" / "enrich_bucket_lineage.py"
    spec = importlib.util.spec_from_file_location("enrich_bucket_lineage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load_script()


def _buckets():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return cfg["short_horizon"]["vol_buckets"]


def _crm_rows():
    instruction = {
        "id": 4796,
        "direction": "BUY",
        "entry_price": 244.55,
        "stop_loss": 232.32,
        "vol_bucket": "medium",
        "atr_pct": 0.03457,
        "horizon": "short",
        "invalidation": "放量跌破243.45(MA10)且主力资金持续净流出，W底形态失效",
        "metadata_json": json.dumps({
            "reprice_audit": {
                "reprice_from": 245.90,
                "reprice_to": 244.55,
                "reprice_original_sl": 234.83,
                "reprice_sl_pct": 5.0,
                "vol_bucket": "high",
                "atr_pct": 0.035003,
            }
        }),
    }
    trade = {
        "id": 1729,
        "direction": "LONG",
        "entry_price": 244.80,
        "metadata_json": json.dumps({"stop_loss": 232.32}),
    }
    return instruction, trade


def test_crm_lineage_reproduces_fill_sl_cap_and_text_distance():
    instruction, trade = _crm_rows()
    lineage = build_bucket_lineage(
        instruction=instruction,
        trade=trade,
        vol_buckets=_buckets(),
        bucket_schema_version="short_v1",
        invalidation_text_price=243.45,
    )
    assert lineage["lineage_schema"] == BUCKET_LINEAGE_SCHEMA
    assert lineage["bucket_schema_version"] == "short_v1"
    assert lineage["bucket_generated"] == "medium"
    assert lineage["bucket_repriced"] == "high"
    assert lineage["bucket_effective"] == "high"
    assert lineage["bucket_effective_source"] == "reprice"
    assert lineage["bucket_generated_alignment"] == "match"
    assert lineage["bucket_repriced_alignment"] == "match"
    assert lineage["sl_anchor_price"] == 244.55
    assert lineage["sl_anchor_type"] == "reprice"
    assert lineage["sl_pct_from_fill"] == 5.098
    assert lineage["bucket_sl_cap_pct"] == 5.0
    assert lineage["sl_cap_breach_bps"] == 9.8
    assert lineage["invalidation_pct_from_anchor"] == -0.45
    assert lineage["invalidation_atr_multiple"] == pytest.approx(-0.1285, abs=1e-4)
    assert lineage["input_complete"] is True
    assert len(lineage["source_hash"]) == 64


def test_short_uses_bucket_widen_in_applicable_cap():
    instruction = {
        "entry_price": 100.0,
        "stop_loss": 105.0,
        "vol_bucket": "high",
        "atr_pct": 0.055,
        "metadata_json": "{}",
    }
    trade = {
        "direction": "SHORT",
        "entry_price": 100.0,
        "metadata_json": json.dumps({"stop_loss": 105.0}),
    }
    lineage = build_bucket_lineage(
        instruction=instruction,
        trade=trade,
        vol_buckets=_buckets(),
        bucket_schema_version="short_v1",
    )
    assert lineage["bucket_effective"] == "high"
    assert lineage["bucket_sl_ceil_pct"] == 5.0
    assert lineage["bucket_sl_widen_pct"] == 0.5
    assert lineage["bucket_sl_cap_pct"] == 5.5
    assert lineage["sl_pct_from_fill"] == 5.0
    assert lineage["sl_cap_breach_bps"] == -50.0


def test_no_reprice_uses_instruction_anchor_and_generated_bucket():
    instruction = {
        "entry_price": 100.0,
        "stop_loss": 96.0,
        "vol_bucket": "medium",
        "atr_pct": 0.032,
        "metadata_json": "{}",
    }
    trade = {
        "direction": "LONG",
        "entry_price": 100.5,
        "metadata_json": json.dumps({"stop_loss": 96.0}),
    }
    lineage = build_bucket_lineage(
        instruction=instruction,
        trade=trade,
        vol_buckets=_buckets(),
        bucket_schema_version="short_v1",
    )
    assert lineage["bucket_repriced"] is None
    assert lineage["bucket_effective"] == "medium"
    assert lineage["bucket_effective_source"] == "generated"
    assert lineage["sl_anchor_price"] == 100.0
    assert lineage["sl_anchor_type"] == "instruction"


def test_missing_fill_and_atr_are_not_guessed():
    instruction = {"vol_bucket": None, "atr_pct": None, "metadata_json": "{}"}
    trade = {"direction": "LONG", "entry_price": None, "metadata_json": "{}"}
    lineage = build_bucket_lineage(
        instruction=instruction,
        trade=trade,
        vol_buckets=_buckets(),
        bucket_schema_version="short_v1",
    )
    assert lineage["bucket_effective"] is None
    assert lineage["sl_pct_from_fill"] is None
    assert lineage["input_complete"] is False
    assert {"fill_price", "stop_loss", "bucket_generated", "bucket_effective",
            "atr_pct_effective", "sl_anchor_price"} <= set(lineage["missing_fields"])


def test_bucket_boundaries_are_half_open_and_stable():
    buckets = _buckets()
    assert select_vol_bucket(0.0149, buckets) == "low"
    assert select_vol_bucket(0.015, buckets) == "medium"
    assert select_vol_bucket(0.034999, buckets) == "medium"
    assert select_vol_bucket(0.035, buckets) == "high"
    assert select_vol_bucket(0.08, buckets) == "extreme"


def test_source_hash_changes_only_when_inputs_change_and_idempotency_detects_it():
    instruction, trade = _crm_rows()
    first = build_bucket_lineage(
        instruction=instruction, trade=trade, vol_buckets=_buckets(),
        bucket_schema_version="short_v1", invalidation_text_price=243.45,
    )
    second = build_bucket_lineage(
        instruction=instruction, trade=trade, vol_buckets=_buckets(),
        bucket_schema_version="short_v1", invalidation_text_price=243.45,
    )
    assert first == second
    assert lineage_needs_update(first, second) is False
    trade["entry_price"] = 244.81
    changed = build_bucket_lineage(
        instruction=instruction, trade=trade, vol_buckets=_buckets(),
        bucket_schema_version="short_v1", invalidation_text_price=243.45,
    )
    assert lineage_needs_update(first, changed) is True


def _make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
        "entry_price REAL, entry_time TEXT, trade_date TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE playbook_instructions (id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, "
        "direction TEXT, entry_price REAL, stop_loss REAL, horizon TEXT, vol_bucket TEXT, "
        "atr_pct REAL, invalidation TEXT, raw_instruction_json TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (4796, "2026-10-27", "US.CRM", "BUY", 244.55, 232.32, "short",
         "medium", 0.03457, "放量跌破243.45(MA10)且主力资金持续净流出",
         "{}", json.dumps({"reprice_audit": {
             "reprice_to": 244.55, "vol_bucket": "high", "atr_pct": 0.035003,
         }})),
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)",
        (1729, "US.CRM", "LONG", 244.80, "2026-10-27T09:51:03-04:00",
         "2026-10-27", json.dumps({
             "playbook_instruction_id": 4796,
             "stop_loss": 232.32,
             "thesis": "preserve-me",
         })),
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)",
        (1730, "US.MANUAL", "LONG", 10.0, "2026-10-27T10:00:00-04:00",
         "2026-10-27", json.dumps({"stop_loss": 9.0})),
    )
    conn.commit()
    conn.close()


def _run(db, audit_dir, *extra):
    return M.main([
        "--db", str(db), "--config", str(CONFIG),
        "--since", "2026-10-27", "--until", "2026-10-27",
        "--audit-dir", str(audit_dir), *extra,
    ])


def test_cli_dry_run_does_not_write_and_excludes_unjoined(tmp_path, capsys):
    db = tmp_path / "lineage.db"
    _make_db(db)
    before = sqlite3.connect(str(db)).execute(
        "SELECT metadata_json FROM trade_records WHERE id=1729"
    ).fetchone()[0]
    assert _run(db, tmp_path / "audits") == 0
    out = capsys.readouterr().out
    summary = json.loads(out.split("\n[bucket-lineage]")[0])
    assert summary["joined"] == 1
    assert summary["unjoined"] == 1
    assert summary["computed"] == 1
    assert summary["written"] == 0
    assert summary["effective_bucket_counts"] == {"high": 1}
    assert summary["reprice_bucket_switches"] == 1
    assert summary["sl_cap_breaches"] == 1
    after = sqlite3.connect(str(db)).execute(
        "SELECT metadata_json FROM trade_records WHERE id=1729"
    ).fetchone()[0]
    assert json.loads(after) == json.loads(before)
    assert Path(summary["audit_path"]).exists()


def test_cli_write_is_idempotent_and_preserves_other_metadata(tmp_path, capsys):
    db = tmp_path / "lineage.db"
    _make_db(db)
    assert _run(db, tmp_path / "audits", "--write") == 0
    capsys.readouterr()
    meta = json.loads(sqlite3.connect(str(db)).execute(
        "SELECT metadata_json FROM trade_records WHERE id=1729"
    ).fetchone()[0])
    assert meta["thesis"] == "preserve-me"
    assert meta["bucket_lineage"]["bucket_effective"] == "high"
    assert meta["bucket_lineage"]["sl_pct_from_fill"] == 5.098
    assert meta["bucket_lineage"]["sl_cap_breach_bps"] == 9.8

    assert _run(db, tmp_path / "audits", "--write") == 0
    out = capsys.readouterr().out
    second = json.loads(out.split("\n[bucket-lineage]")[0])
    assert second["written"] == 0
    assert second["unchanged"] == 1
