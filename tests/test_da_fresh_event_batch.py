"""UE premarket → DA fresh-event candidate-batch contract tests."""

from __future__ import annotations

import importlib.util
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/da_fresh_event_batch.py"
SPEC = importlib.util.spec_from_file_location("da_fresh_event_batch", SCRIPT)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_select_fresh_candidates_filters_skip_and_caps_count():
    scan = {
        "batch_id": "UE:fresh",
        "surfaced": [
            {"symbol": "TENB", "direction": "SHORT", "confidence": 0.45},
            {"symbol": "BLBD", "direction": "LONG", "confidence": 0.50},
            {"symbol": "WDC", "direction": "SKIP", "confidence": 0.25},
            {"symbol": "STX", "direction": "SHORT", "confidence": 0.40},
            {"symbol": "CEG", "direction": "LONG", "confidence": 0.35},
        ],
    }
    rows = MOD.select_fresh_candidates(scan, max_candidates=3)
    assert [row["symbol"] for row in rows] == ["US.TENB", "US.BLBD", "US.STX"]
    assert [row["direction"] for row in rows] == ["SHORT", "LONG", "SHORT"]


def test_load_fresh_scan_requires_premarket_and_freshness(tmp_path):
    now = datetime.now(timezone(timedelta(hours=-4)))
    stamp = (now - timedelta(minutes=5)).isoformat()
    path = tmp_path / "scan.jsonl"
    path.write_text(json.dumps({
        "type": "scan",
        "date": now.strftime("%Y-%m-%d"),
        "scan_window": "premarket",
        "batch_id": "UE:fresh",
        "batch_status": "complete",
        "started_at": stamp,
        "scanned_at": stamp,
        "surfaced": [],
    }) + "\n", encoding="utf-8")
    scan, reason = MOD.load_fresh_scan(path, now=now, max_age_hours=1)
    assert scan is not None and reason == "complete"

    stale = (now - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps({
        "type": "scan", "date": now.strftime("%Y-%m-%d"),
        "scan_window": "da_feed", "batch_status": "complete",
        "started_at": stale, "scanned_at": stale,
    }) + "\n", encoding="utf-8")
    scan, reason = MOD.load_fresh_scan(path, now=now, max_age_hours=1)
    assert scan is None and reason == "no_premarket_attempt"


def test_command_output_dir_isolated_in_both_modes(tmp_path):
    """[2026-10-14 execute转正·clobber防护] --output-dir 两种模式恒隔离。

    病灶: execute 原实现不传 output_dir ⇒ pipeline 默认落生产 data/ 根,
    Stage 6b 的 opportunity_scan bridge/deep_sector_rotation/dynamic_blacklist
    会被 1-3 个候选的版本覆盖 (DA 主链当日文件, 15+ 消费方)。execute 的
    唯一生产触点 = DB 指令 (save_instruction 走 adapter 真 DB 路径) ⇒
    两模式命令的唯一允许差异 = --no-db-write。
    """
    shadow = MOD.build_command(
        "/venv/bin/python", Path("/skill/pipeline.py"), False,
        tmp_path / "shadow", tmp_path / "context")
    assert "--fresh-event" in shadow
    assert "--no-db-write" in shadow
    assert "--no-pool-write" in shadow
    assert shadow[shadow.index("--output-dir") + 1] == str(tmp_path / "shadow")

    execute = MOD.build_command(
        "/venv/bin/python", Path("/skill/pipeline.py"), True,
        tmp_path / "shadow", tmp_path / "context")
    assert "--no-db-write" not in execute
    assert "--no-pool-write" in execute  # pool 恒关 (合入生产池另裁, RFC-136 附录)
    assert execute[execute.index("--output-dir") + 1] == str(tmp_path / "shadow")


def test_select_fresh_candidates_excludes_same_day_db_symbols():
    """[2026-10-14] 当日已有指令标的进批前剔除 (UPSERT by date+symbol 防覆盖)。"""
    scan = {
        "batch_id": "UE:fresh",
        "surfaced": [
            {"symbol": "MU", "direction": "LONG", "confidence": 0.5},
            {"symbol": "RARE", "direction": "LONG", "confidence": 0.5},
            {"symbol": "US.NOK", "direction": "SHORT", "confidence": 0.4},
        ],
    }
    rows = MOD.select_fresh_candidates(scan, max_candidates=3,
                                       exclude_symbols=frozenset({"MU", "NOK"}))
    assert [row["symbol"] for row in rows] == ["US.RARE"]
    # 不传 exclude_symbols = 旧行为 (全进批)
    rows_all = MOD.select_fresh_candidates(scan, max_candidates=3)
    assert [row["symbol"] for row in rows_all] == ["US.MU", "US.RARE", "US.NOK"]


def test_same_day_instruction_symbols_distinguishes_query_failure(tmp_path):
    db = tmp_path / "ftseek_us.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE playbook_instructions (date TEXT, symbol TEXT)")
    connection.executemany("INSERT INTO playbook_instructions VALUES (?, ?)", [
        ("2026-10-14", "US.CAT"), ("2026-10-14", "US.RIVN"), ("2026-10-13", "US.OLD"),
    ])
    connection.commit()
    connection.close()
    got = MOD.same_day_instruction_symbols(db, "2026-10-14")
    assert got == frozenset({"CAT", "RIVN"})  # 裸化 + 只取当日
    assert MOD.same_day_instruction_symbols(tmp_path / "missing.db", "2026-10-14") is None
    assert MOD.same_day_instruction_symbols(db, "") == frozenset()


def test_execute_skips_batch_when_same_day_query_fails(tmp_path, monkeypatch):
    """execute 下查库失败必须 fail-closed, 不得绕过当日 UPSERT 防覆盖过滤。"""
    event_path = tmp_path / "events.jsonl"
    scan = {"batch_id": "UE:fresh", "surfaced": [
        {"symbol": "RARE", "direction": "LONG", "confidence": 0.5},
    ]}
    monkeypatch.setattr(MOD, "load_fresh_scan", lambda *args, **kwargs: (scan, "complete"))
    monkeypatch.setattr(MOD, "same_day_instruction_symbols", lambda *args, **kwargs: None)
    args = argparse.Namespace(
        execute=True, force=True, max_candidates=3, max_age_hours=1.0, timeout=1,
        python="python", pipeline=Path("pipeline.py"), scan_path=Path("scan.jsonl"),
        event_path=event_path, db=tmp_path / "missing.db", output_dir=tmp_path / "out",
        context_dir=tmp_path / "context",
    )
    assert MOD.run(args) == 2
    events = MOD._read_jsonl(event_path)
    assert events[-1]["status"] == "dedup_query_failed"
    assert not (tmp_path / "out").exists()
