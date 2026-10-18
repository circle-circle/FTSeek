"""UE 09:10 → DA fresh-event → 最终指令 漏斗 (只读指标) 契约."""
import json
import sqlite3

import scripts.empty_run_metrics as m


def test_direction_kept_mapping():
    assert m._direction_kept("LONG", "BUY") is True
    assert m._direction_kept("SHORT", "SELL_SHORT") is True
    assert m._direction_kept("SHORT", "BUY") is False
    assert m._direction_kept("LONG", "SKIP") is False
    assert m._direction_kept("", "BUY") is False


def test_parse_borrow_dropped():
    text = "[UE-FEED] ERAS borrow_feasible=False, 跳过 (不可借券SHORT)\n" \
           "[UE-FEED] US.AN borrow_feasible=False, 跳过\n" \
           "[UE-FEED] TSM borrow_feasible=True, 保留\n"
    assert m._parse_borrow_dropped(text) == ["AN", "ERAS"]


def test_overwrite_detection_uses_created_before_scan():
    # TSM: 08:46 ET 创建, 09:12 ET 被 fresh-event 批 UPSERT
    assert m._is_overwrite("2026-10-16T12:46:42+00:00",
                           "2026-10-16T09:12:33-04:00") is True
    # 本批新建行: 创建晚于扫描
    assert m._is_overwrite("2026-10-16T13:16:42+00:00",
                           "2026-10-16T09:12:33-04:00") is False


def test_funnel_exposes_wrapper_vs_pipeline_mismatch(monkeypatch, tmp_path):
    scan_path = tmp_path / "scan.jsonl"
    scan_path.write_text(json.dumps({
        "type": "scan", "date": "2026-10-16", "scan_window": "premarket",
        "batch_id": "UE:test", "scanned_at": "2026-10-16T09:12:33-04:00",
        "surfaced": [
            {"symbol": "TSM", "direction": "SHORT"},
            {"symbol": "ERAS", "direction": "SHORT"},
            {"symbol": "AN", "direction": "SHORT"},
        ],
    }) + "\n", encoding="utf-8")
    pool_path = tmp_path / "pool.jsonl"
    pool_path.write_text(json.dumps({
        "type": "fresh_event_batch", "date": "2026-10-16", "mode": "execute",
        "status": "complete", "ue_batch_id": "UE:test",
        "candidates": [
            {"symbol": "US.ERAS", "direction": "SHORT"},
            {"symbol": "US.AN", "direction": "SHORT"},
        ],
        "same_day_db_excluded": ["TSM"],
        "stderr_tail": "[UE-FEED] ERAS borrow_feasible=False\n"
                       "[UE-FEED] AN borrow_feasible=False\n",
    }) + "\n", encoding="utf-8")
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE playbook_instructions ("
        "date TEXT, symbol TEXT, direction TEXT, skip_reason TEXT, "
        "triggered INTEGER, executed INTEGER, raw_instruction_json TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-10-16", "US.TSM", "BUY", "no_trigger", 0, 0,
         json.dumps({"origin": "ue_fresh_event", "ue_batch_id": "UE:test",
                     "ue_direction": "SHORT"}),
         "2026-10-16T12:46:42+00:00", "2026-10-16T20:11:34+00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(m, "UE_SCAN_PATH", scan_path)
    monkeypatch.setattr(m, "FRESH_EVENT_POOL", pool_path)
    monkeypatch.setattr(m, "DB", db_path)

    funnel = m._ue_fresh_funnel(3650)
    assert len(funnel) == 1
    row = funnel[0]
    assert [x["symbol"] for x in row["surfaced"]] == ["TSM", "ERAS", "AN"]
    assert [x["symbol"] for x in row["wrapper_selected"]] == ["ERAS", "AN"]
    assert row["same_day_excluded"] == ["TSM"]
    assert row["borrow_dropped"] == ["AN", "ERAS"]
    assert [(r["symbol"], r["direction"]) for r in row["final_rows"]] == [("TSM", "BUY")]
    assert row["flipped"] == ["TSM"]
    assert row["overwrites"] == ["TSM"]
    assert row["no_final_symbols"] == ["AN", "ERAS"]
    assert row["actionable"] == 1 and row["triggered"] == 0 and row["executed"] == 0
