#!/usr/bin/env python3
"""W8 费用异常单守卫契约测试 (DEB-20261009-event §30, 2026-10-10 用户裁「费用护栏同意」).

钉住:
  1. >200bps 的仙股/最低佣金异常单被告警 (CTNT/GIPR 同型);
  2. 正常费率单不告警;
  3. 裁决日 (2026-10-10) 之前的存量不追溯 (CTNT/GIPR 本身不被告警);
  4. 常量被钉死 (防静默移阈值/移窗 = 改判据面)。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.daily_invariant_check import (  # noqa: E402
    _FEE_BPS_MAX,
    _FEE_BPS_WATCH_START,
    _check_fee_bps_anomaly,
)


def _mk_db(tmp_path: Path) -> Path:
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE trade_records (
            symbol TEXT, direction TEXT, entry_price REAL, quantity REAL,
            trade_date TEXT, exit_time TEXT, exit_reason TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db


def _row(db: Path, symbol: str, price: float, qty: float, day: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trade_records VALUES (?, 'LONG', ?, ?, ?, '2026-10-10T15:00:00-04:00', 'EOD')",
        (symbol, price, qty, day),
    )
    conn.commit()
    conn.close()


class _Rec:
    def __init__(self):
        self.hits = []

    def __call__(self, code, *_args):
        self.hits.append(code)


def test_constants_pinned():
    assert _FEE_BPS_WATCH_START == "2026-10-10"  # 裁决日; 移窗 = 改判据面, 须 DEB + 裁决
    assert _FEE_BPS_MAX == 200.0                 # 预注册阈值 (§28.3 回放: 拦2误杀0)


def test_penny_stock_anomaly_detected(tmp_path):
    """CTNT 同型: $0.072 × 68493 股 ⇒ 最低佣金爆炸, fee_bps >> 200。"""
    db = _mk_db(tmp_path)
    _row(db, "US.CTNT", 0.072, 68493, "2026-10-10")
    rec = _Rec()
    out = _check_fee_bps_anomaly(rec, db_path=db)
    assert out["scanned"] == 1
    assert out["anomalies"] == 1
    assert "W8_fee_bps_anomaly" in rec.hits


def test_normal_fee_not_flagged(tmp_path):
    """常规单 ($50 × 100 股, ~8bps) 不得告警。"""
    db = _mk_db(tmp_path)
    _row(db, "US.AAPL", 50.0, 100, "2026-10-10")
    rec = _Rec()
    out = _check_fee_bps_anomaly(rec, db_path=db)
    assert out["scanned"] == 1
    assert out["anomalies"] == 0
    assert rec.hits == []


def test_pre_adjudication_rows_not_retroactive(tmp_path):
    """裁决日前的存量 (含 CTNT/GIPR 9/17 本体) 不追溯 — 它们已由 EXT 地板收口。"""
    db = _mk_db(tmp_path)
    _row(db, "US.CTNT", 0.072, 68493, "2026-09-17")
    rec = _Rec()
    out = _check_fee_bps_anomaly(rec, db_path=db)
    assert out["scanned"] == 0
    assert out["anomalies"] == 0
    assert rec.hits == []


def test_open_trades_excluded(tmp_path):
    """未平仓单 (exit_reason='') 不计入。"""
    db = _mk_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trade_records VALUES ('US.X', 'LONG', 0.05, 90000, '2026-10-10', NULL, '')"
    )
    conn.commit()
    conn.close()
    rec = _Rec()
    out = _check_fee_bps_anomaly(rec, db_path=db)
    assert out["scanned"] == 0
    assert rec.hits == []
