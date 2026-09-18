#!/usr/bin/env python3
"""占位标的不得写 / 不得计入 PDT 台账 (2026-09-18 §23.5 源头修)。

事故: 生产库 data/ftseek_us.db 的 `pdt_trades` 被单测 artifact 写入 **28 行 US.TEST**
(trade_date 2026-09-17), 虚增 5 日窗口日内交易计数。PDT 计数逼近上限会 fail-closed
**拒真交易** ⇒ 假标的能间接卡住真交易, 所以写入侧和读数侧都要挡。
"""
import sqlite3

from app.risk.pdt_guard import PDTGuard
from app.utils.timezone import now_et


def _guard(tmp_path):
    db = tmp_path / "pdt.db"
    return PDTGuard(str(db)), db


def test_record_day_trade_skips_placeholder_symbols(tmp_path):
    guard, db = _guard(tmp_path)

    guard.record_day_trade("US.TEST")
    guard.record_day_trade("TEST")

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM pdt_trades").fetchone()[0] == 0
    conn.close()
    assert guard._get_day_trade_count() == 0


def test_real_symbols_still_recorded(tmp_path):
    """反面: 真标的必须照记 (守卫只挡占位, 不得削弱 PDT 合规追踪)。"""
    guard, db = _guard(tmp_path)

    guard.record_day_trade("US.FAKE")

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT symbol FROM pdt_trades").fetchall()
    conn.close()
    assert rows == [("US.FAKE",)]
    assert guard._get_day_trade_count() == 1


def test_placeholder_rows_already_in_db_do_not_count(tmp_path):
    """历史 artifact (28 行 US.TEST) 即使残留也不进计数窗口 (读数侧兜底)。"""
    guard, db = _guard(tmp_path)
    today = now_et().date().isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO pdt_trades (trade_date, symbol, timestamp) VALUES (?, 'US.TEST', 1.0)",
        (today,),
    )
    conn.execute(
        "INSERT INTO pdt_trades (trade_date, symbol, timestamp) VALUES (?, 'US.FAKE', 2.0)",
        (today,),
    )
    conn.commit()
    conn.close()

    assert guard._get_day_trade_count() == 1  # 只数真标的