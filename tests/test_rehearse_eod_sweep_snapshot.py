"""演练器快照契约 (2026-10-22): WAL 未 checkpoint 时 `cp .db` 会漏行 ⇒ 基线假绿。

实测代价: 用 shutil.copy2 取 data/ftseek_us.db 做演练库, TSLA 那条未平仓行**不在**副本里,
S0 基线因此"没复现 P0"——若当时按 S0 结果下结论, 会把"守卫拦住了"误判成"守卫破坏了正常平仓"。
修法: sqlite3 backup API (本测试钉住)。
"""
import sqlite3

import pytest

from scripts.rehearse_eod_sweep import _snapshot


def _wal_db(tmp_path):
    db = tmp_path / "src.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (symbol TEXT)")
    conn.commit()
    conn.execute("INSERT INTO t VALUES ('US.TSLA')")   # 留在 WAL, 未 checkpoint
    conn.commit()
    return db, conn                              # 保持连接打开 = WAL 不落盘


def test_snapshot_sees_uncheckpointed_wal_rows(tmp_path):
    db, conn = _wal_db(tmp_path)
    try:
        snap = _snapshot(db)
        try:
            rows = sqlite3.connect("file:%s?mode=ro" % snap, uri=True).execute(
                "SELECT symbol FROM t").fetchall()
            assert rows == [("US.TSLA",)]        # backup API 覆盖 WAL
        finally:
            import shutil
            shutil.rmtree(snap.parent, ignore_errors=True)
    finally:
        conn.close()


def test_plain_copy_would_miss_them(tmp_path):
    # 反向钉: 若有人把 _snapshot 改回 shutil.copy2, 本断言证明那不是等价实现
    import shutil
    db, conn = _wal_db(tmp_path)
    try:
        naive = tmp_path / "naive.db"
        shutil.copy2(db, naive)
        # 坑本体比"少行"更彻底: 连 schema 都在 WAL 里 ⇒ 副本读不到表
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect("file:%s?mode=ro" % naive, uri=True).execute(
                "SELECT symbol FROM t").fetchall()
    finally:
        conn.close()