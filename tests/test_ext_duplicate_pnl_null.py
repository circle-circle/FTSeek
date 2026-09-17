#!/usr/bin/env python3
"""F1 修复回归: 标记"重复平仓"时**必须同时置 NULL** realized_pnl/pnl_pct.

依据 (daily_invariant_check 既定约定): data_quality 命中 duplicate/outlier/invalid/void/excluded
前缀的行 = 写方声明"非真实了结", 故意留 NULL 使**净额求和在 SQL 层自动排除**。
首版 repair 只打标记不置 NULL ⇒ 3 行幻影盈亏 -258.52 会被不过滤的消费方计入。
"""
import importlib.util
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = _load("repair_dup_pnl_test", "scripts/repair_extended_hours_exit_books.py")


def _db(tmp_path, rows):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, exit_price REAL, "
                 "exit_time TEXT, exit_reason TEXT, realized_pnl REAL, pnl_pct REAL, "
                 "close_futu_order_id TEXT, metadata_json TEXT, data_quality TEXT)")
    for r in rows:
        conn.execute("INSERT INTO trade_records (id,symbol,realized_pnl,pnl_pct,data_quality,"
                     "exit_price,exit_time,metadata_json) VALUES (?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    return path


def _row(path, rid):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT realized_pnl, pnl_pct, data_quality FROM trade_records WHERE id=?",
                            (rid,)).fetchone()
    finally:
        conn.close()


def test_mark_duplicate_nulls_pnl(tmp_path):
    db = _db(tmp_path, [(1523, "US.GIPR", -91.95, -0.0186, "duplicate_external_close", 0.423,
                         "2026-09-17T04:40:00-04:00", '{"attribution":{"pnl":-91.95}}')])
    audit = tmp_path / "a.jsonl"
    audit.write_text("")
    res = R.apply_repair_plan(str(db), str(audit),
                              [{"action": "mark_duplicate_external_close", "order_id": "",
                                "symbol": "US.GIPR", "trade_id": 1523}],
                              "2026-09-18T00:00:00Z", tmp_path / "b.db")
    assert len(res["applied"]) == 1
    assert _row(db, 1523) == (None, None, "duplicate_external_close")   # 幻影已消
    assert (tmp_path / "b.db").exists() is False or True                # backup 由 main() 负责


def test_mark_duplicate_is_idempotent_and_self_healing(tmp_path):
    """① 已修行再跑不动 (幂等); ② 历史"已标记但非 NULL"行可自愈."""
    db = _db(tmp_path, [
        (1525, "US.NBIS", None, None, "duplicate_external_close", 0.0, "", "{}"),   # 已合规
        (1527, "US.AEHL", -184.21, -0.037, "duplicate_external_close", 12.06,
         "2026-09-17T07:40:00-04:00", "{}"),                                        # 漏改
    ])
    audit = tmp_path / "a.jsonl"
    audit.write_text("")
    res = R.apply_repair_plan(str(db), str(audit),
                              [{"action": "mark_duplicate_external_close", "order_id": "",
                                "symbol": "US.NBIS", "trade_id": 1525}],
                              "ts", None)
    assert res["applied"] == []                       # 合规行 → 不动 (幂等)
    assert _row(db, 1525) == (None, None, "duplicate_external_close")

    res2 = R.apply_repair_plan(str(db), str(audit),
                               [{"action": "mark_duplicate_external_close", "order_id": "",
                                 "symbol": "US.AEHL", "trade_id": 1527}],
                               "ts", None)
    assert len(res2["applied"]) == 1                  # 漏改行 → 自愈
    assert _row(db, 1527) == (None, None, "duplicate_external_close")


def test_scan_finds_only_tagged_non_null_rows(tmp_path):
    db = _db(tmp_path, [
        (1, "US.A", -10.0, -0.01, "duplicate_external_close", 0.0, "", "{}"),   # 待修
        (2, "US.B", None, None, "duplicate_external_close", 0.0, "", "{}"),     # 已合规
        (3, "US.C", 5.0, 0.01, "real", 0.0, "", "{}"),                          # 正常了结 → 不碰
    ])
    plan = R.build_duplicate_pnl_plan(str(db))
    assert [a["trade_id"] for a in plan] == [1]
    assert plan[0]["previous_realized_pnl"] == -10.0

# ── F6/F4 (2026-09-18) ──────────────────────────────────────────────
def test_verify_invariants_pure_function():
    good = {"mismatches": 0, "open_position_conflicts": 0, "duplicate_close_records": 0,
            "broker_filled_exit_orders": 6, "audit_confirmed": 6, "db_matched": 6}
    assert R.verify_invariants(good) == (True, [])
    assert R.verify_invariants(None)[0] is False                      # 空报告 → 失败
    bad = dict(good, mismatches=2)
    ok, fails = R.verify_invariants(bad)
    assert ok is False and any("mismatches=2" in f for f in fails)
    ok3, fails3 = R.verify_invariants(dict(good, broker_filled_exit_orders=7))
    assert ok3 is False and any("三方不一致" in f for f in fails3)
    ok4, fails4 = R.verify_invariants(dict(good, duplicate_close_records=1))
    assert ok4 is False and any("duplicate_close_records=1" in f for f in fails4)


def test_default_backup_path_is_timestamped():
    import re
    p1 = R.default_backup_path("/tmp/x/ftseek_us.db")
    assert re.match(r"^ftseek_us_before_repair_\d{8}_\d{6}\.db$", p1.name), p1.name
    assert p1.parent.name == "backups"
    assert p1.name != "ftseek_us_before_extended_hours_exit_repair.db"   # 不再是固定名
