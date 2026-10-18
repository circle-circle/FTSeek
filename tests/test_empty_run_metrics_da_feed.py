"""UE da_feed → DA → 最终指令 漏斗 (只读指标) 契约."""
import json
import sqlite3

import scripts.empty_run_metrics as m


def test_parse_da_feed_injection_reads_first_ue_segment(monkeypatch, tmp_path):
    path = tmp_path / "da_pipeline_stderr.log-20261017"
    path.write_text(
        "[UE-FEED] EBS borrow_feasible=False, 跳过\n"
        "[UE-FEED] US.RGNX borrow_feasible=False, 跳过\n"
        "[UE-FEED] X streak=4 > 3, 跳过\n"
        "[Pipeline] UE候选注入: +2 (['US.SKHY', 'US.RYM']) | "
        "UE∩DA 重叠归因回填: 0 ([])\n"
        "[Pipeline] UE候选注入: +1 (['US.TSM']) | UE∩DA 重叠归因回填: 0 ([])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_da_feed_stderr_path", lambda date: path)
    out = m._parse_da_feed_injection("2026-10-16")
    assert out["injected"] == ["SKHY", "RYM"]       # 只吃第一段 da_feed
    assert out["injected_count"] == 2
    assert out["borrow_dropped"] == ["EBS", "RGNX"]
    assert out["streak_dropped"] == ["X"]
    assert out["overlap"] == 0


def test_ue_da_feed_funnel_joins_scan_db_and_stderr(monkeypatch, tmp_path):
    scan = tmp_path / "scan.jsonl"
    scan.write_text(json.dumps({
        "type": "scan", "date": "2026-10-16", "scan_window": "da_feed",
        "batch_id": "UE:da", "scanned_at": "2026-10-16T08:02:41-04:00",
        "surfaced": [
            {"symbol": "SKHY", "direction": "SHORT"},
            {"symbol": "EBS", "direction": "SHORT"},
            {"symbol": "RGNX", "direction": "SHORT"},
            {"symbol": "RYM", "direction": "LONG"},
        ],
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
        ("2026-10-16", "US.SKHY", "BUY", "no_trigger", 0, 0,
         json.dumps({"ue_batch_id": "UE:da", "ue_direction": "SHORT",
                     "origin": "ue_candidate"}),
         "2026-10-16T12:46:42+00:00", "2026-10-16T20:11:34+00:00"),
    )
    conn.commit()
    conn.close()
    stderr = tmp_path / "da_pipeline_stderr.log-20261017"
    stderr.write_text(
        "[UE-FEED] EBS borrow_feasible=False\n"
        "[UE-FEED] RGNX borrow_feasible=False\n"
        "[Pipeline] UE候选注入: +2 (['US.SKHY', 'US.RYM']) | "
        "UE∩DA 重叠归因回填: 0 ([])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "UE_SCAN_PATH", scan)
    monkeypatch.setattr(m, "DB", db_path)
    monkeypatch.setattr(m, "_da_feed_stderr_path", lambda date: stderr)

    funnel = m._ue_da_feed_funnel(3650)
    assert len(funnel) == 1
    item = funnel[0]
    assert [x["symbol"] for x in item["surfaced"]] == ["SKHY", "EBS", "RGNX", "RYM"]
    assert item["da_injected"] == ["SKHY", "RYM"]
    assert item["borrow_dropped"] == ["EBS", "RGNX"]
    assert [(r["symbol"], r["direction"]) for r in item["final_rows"]] == [("SKHY", "BUY")]
    assert item["flipped"] == ["SKHY"]
    assert item["no_final_symbols"] == ["EBS", "RGNX", "RYM"]
