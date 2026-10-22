"""stop/invalidation 只读审计的纯函数与 CLI 契约 (2026-10-22)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "stop_invalidation_audit", ROOT / "scripts" / "stop_invalidation_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()
PRICE = "收盘价"


def _t(op, value, metric=PRICE):
    return {"metric": metric, "operator": op, "value": value}


def test_short_stop_is_tighter_than_invalidation():
    r = M.classify_stop_vs_triggers("SELL_SHORT", 388.5, [_t(">", 388.84)])
    assert r["classification"] == M.STOP_BEFORE
    assert r["trigger"]["value"] == 388.84
    assert r["gap_pct"] > 0


def test_short_invalidation_is_tighter_than_stop():
    # ARM: invalidation 243.85, stop 246 -> thesis 先失效, stop 未到
    r = M.classify_stop_vs_triggers("SELL_SHORT", 246.0, [_t(">", 243.85)])
    assert r["classification"] == M.INVALIDATION_BEFORE
    assert r["gap_pct"] < 0


def test_short_aligned():
    r = M.classify_stop_vs_triggers("SELL_SHORT", 178.5, [_t(">", 178.5)])
    assert r["classification"] == M.ALIGNED
    assert r["gap_pct"] == 0.0


def test_long_aligned():
    r = M.classify_stop_vs_triggers("BUY", 146.0, [_t("<", 146.0)])
    assert r["classification"] == M.ALIGNED


def test_long_invalidation_is_tighter_than_stop():
    # LONG stop 140 在 invalidation 145 下方 -> 跌破 145 时 thesis 已失效, stop 未到
    r = M.classify_stop_vs_triggers("BUY", 140.0, [_t("<", 145.0)])
    assert r["classification"] == M.INVALIDATION_BEFORE


def test_long_stop_is_tighter_than_invalidation():
    r = M.classify_stop_vs_triggers("BUY", 150.0, [_t("<", 145.0)])
    assert r["classification"] == M.STOP_BEFORE


def test_multiple_price_triggers_use_first_hit():
    # SHORT 的 OR 语义: 取最小的上破位 (最先触发)
    r = M.classify_stop_vs_triggers(
        "SELL_SHORT", 388.5, [_t(">", 390.0), _t(">", 388.84)])
    assert r["trigger"]["value"] == 388.84


def test_non_price_trigger_does_not_compare():
    r = M.classify_stop_vs_triggers("BUY", 100.0, [_t(">", 2.5, metric="量比")])
    assert r["classification"] == M.NO_PRICE_TRIGGER
    assert r["trigger"] is None


def test_wrong_side_trigger_is_ignored():
    # SHORT 配了 "<" 的收盘价 trigger -> 不参与 stop 比较
    r = M.classify_stop_vs_triggers("SELL_SHORT", 100.0, [_t("<", 90.0)])
    assert r["classification"] == M.NO_PRICE_TRIGGER
    assert r["ignored_price_triggers"]


def test_missing_stop():
    r = M.classify_stop_vs_triggers("BUY", None, [_t("<", 90.0)])
    assert r["classification"] == "no_stop"


def _make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE playbook_instructions ("
        "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, "
        "entry_price REAL, stop_loss REAL, invalidation TEXT, raw_instruction_json TEXT)"
    )
    rows = [
        (1, "2026-10-22", "US.TSLA", "SELL_SHORT", 375.5, 388.5, "break 388.84",
         json.dumps({"invalidation_triggers": [_t(">", 388.84)]})),
        (2, "2026-10-22", "US.ARM", "SELL_SHORT", 225.0, 246.0, "break 243.85",
         json.dumps({"invalidation_triggers": [_t(">", 243.85)]})),
        (3, "2026-10-22", "US.SPCX", "BUY", 153.5, 146.0, "break 146",
         json.dumps({"invalidation_triggers": [_t("<", 146.0)]})),
        (4, "2026-10-22", "US.CAT", "SELL_SHORT", 728.0, 745.0, "break 745",
         json.dumps({"invalidation_triggers": [_t(">", 745.0)]})),
        (5, "2026-10-22", "US.X", "BUY", 10.0, 9.0, "volume only",
         json.dumps({"invalidation_triggers": [_t(">", 2.0, metric="量比")]})),
        (6, "2026-10-22", "US.SKIP", "SKIP", 100.0, 90.0, "skip row",
         json.dumps({"invalidation_triggers": [_t("<", 95.0)]})),
    ]
    conn.executemany(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_cli_summary_and_jsonl(tmp_path, capsys):
    db = tmp_path / "t.db"
    out = tmp_path / "audit.jsonl"
    _make_db(db)
    rc = M.main(["--date", "2026-10-22", "--db", str(db), "--out", str(out)])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.split("\n[stop-invalidation]")[0])
    assert summary["total"] == 5
    assert summary["counts"][M.STOP_BEFORE] == 1
    assert summary["counts"][M.INVALIDATION_BEFORE] == 1
    assert summary["counts"][M.ALIGNED] == 2
    assert summary["counts"][M.NO_PRICE_TRIGGER] == 1
    assert summary["invalidation_before_stop"] == [
        {"symbol": "US.ARM", "direction": "SELL_SHORT",
         "stop": 246.0, "trigger": 243.85, "gap_pct": -0.874}
    ]
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 5
    assert {line["symbol"] for line in lines} == {"US.TSLA", "US.ARM", "US.SPCX", "US.CAT", "US.X"}
    # 幂等: 同日同 instruction 再跑不重复 append
    M.main(["--date", "2026-10-22", "--db", str(db), "--out", str(out)])
    assert len(out.read_text().splitlines()) == 5


def test_cli_no_write(tmp_path, capsys):
    db = tmp_path / "t.db"
    out = tmp_path / "audit.jsonl"
    _make_db(db)
    rc = M.main(["--date", "2026-10-22", "--db", str(db), "--out", str(out), "--no-write"])
    assert rc == 0
    assert not out.exists()


def test_cli_include_skip_flag(tmp_path, capsys):
    db = tmp_path / "t.db"
    out = tmp_path / "audit.jsonl"
    _make_db(db)
    rc = M.main(["--date", "2026-10-22", "--db", str(db), "--out", str(out),
                 "--no-write", "--include-skip"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.split("\n[stop-invalidation]")[0])
    assert summary["total"] == 6
    assert any(r["symbol"] == "US.SKIP" for r in summary["invalidation_before_stop"])
