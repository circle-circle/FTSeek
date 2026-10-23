"""尾部归因报告的 A10 口径 (只读) 回归."""
import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tail_attribution_report", ROOT / "scripts" / "tail_attribution_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()


def _db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT,
            entry_time TEXT, entry_price REAL, quantity REAL, realized_pnl REAL,
            fees_est REAL, pnl_net REAL, mfe_pct REAL, mae_pct REAL,
            holding_hours REAL, exit_reason TEXT, decision_id TEXT,
            market_regime TEXT, metadata_json TEXT, source_type TEXT,
            data_quality TEXT, exit_price REAL)
    """)
    rows = [
        # clean 行, 应入样本
        (1, "2026-10-20", "US.A", "LONG", "2026-10-20T09:35:00", 100, 1, 10, 1, 9,
         1.0, -0.5, 1, "SL_TRAIL_LOCK", "DA-1", "RISK_ON", "{}",
         "PLAYBOOK_SIM", "clean", 101),
        # 旧时代前, 日期谓词排除
        (2, "2026-06-30", "US.B", "LONG", "2026-06-30T09:35:00", 100, 1, 10, 1, 9,
         1.0, -0.5, 1, "SL_TRAIL_LOCK", "DA-2", "RISK_ON", "{}",
         "PLAYBOOK_SIM", "clean", 101),
        # A9 首跑批排除
        (3, "2026-10-20", "US.C", "LONG", "2026-10-20T09:35:00", 100, 1, 10, 1, 9,
         1.0, -0.5, 1, "SL_TRAIL_LOCK", "DA-3", "RISK_ON",
         '{"exclusion_reason":"first_run_test_2026-09-17"}',
         "PLAYBOOK_SIM", "clean", 101),
        # A10 坏质量行排除
        (4, "2026-10-20", "US.D", "LONG", "2026-10-20T09:35:00", 100, 1, 10, 1, 9,
         1.0, -0.5, 1, "SL_TRAIL_LOCK", "DA-4", "RISK_ON", "{}",
         "PLAYBOOK_SIM", "suspect", 101),
        # A10 账户同步行排除
        (5, "2026-10-20", "US.E", "LONG", "2026-10-20T09:35:00", 100, 1, 10, 1, 9,
         1.0, -0.5, 1, "SL_TRAIL_LOCK", "DA-5", "RISK_ON", "{}",
         "PLAYBOOK_SIM", "real", 101),
    ]
    conn.executemany("""
        INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.execute("UPDATE trade_records SET source_type='POSITION_SYNC' WHERE id=5")
    conn.commit()
    conn.close()


def test_load_applies_a9_a10_caliber(tmp_path):
    db = tmp_path / "t.db"
    _db(db)
    rows = M._load(str(db))
    assert [r["id"] for r in rows] == [1]
    assert rows[0]["net"] == 9.0


def test_a10_predicate_matches_ssot_literal():
    # 不 import db_utils (会 monkey-patch sqlite3.connect); 直接读源文件里的常量文本.
    src = (ROOT / "app" / "core" / "db_utils.py").read_text(encoding="utf-8")
    assert "COALESCE(data_quality,'') IN ('clean','real')" in src
    assert "COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')" in src
