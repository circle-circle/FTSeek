"""DEB-20260816-fullchain 批次C 契约测试: P2-17 (手动平仓 exit_time 写 ET) +
#26 (admission stop_loss 键契约 + playbook_service 执行复核接线)."""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from app.web_dashboard.order import audit  # noqa: E402

_FIXED_ET = datetime(2026, 8, 14, 20, 30, 0)  # ET 盘后 20:30 → 北京次日 08:30


class _FakeService:
    def __init__(self, conn):
        self._conn = conn

    def _db(self):
        return self._conn

    def _get_mode_label(self):
        return "sim"

    def _is_live(self):
        return False


def _make_db(tmp_path):
    # 文件库: record_manual_close 内部 conn.close(), 内存库会被关掉
    conn = sqlite3.connect(tmp_path / "audit.db")
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, symbol TEXT, direction TEXT,
            entry_time TEXT, exit_time TEXT,
            entry_price REAL, exit_price REAL, quantity INTEGER,
            realized_pnl REAL, pnl_pct REAL,
            exit_reason TEXT, entry_reason TEXT,
            strategy_id TEXT, source_type TEXT, mode TEXT, trade_mode TEXT,
            data_quality TEXT, created_at TEXT,
            futu_order_id TEXT, close_futu_order_id TEXT,
            signal_strength REAL, signal_id TEXT, signal_confidence REAL,
            owner_strategy TEXT,
            -- 2026-09-21 全链路复查修复 (P1-1): 对齐生产 lazy-ALTER 清单
            -- (trade_store.py "P0 2026-07-26 扫尾"), 否则 audit 写入路径报
            -- "no column named config_version" 使本组测试存量失败。
            config_version TEXT, entry_session TEXT, entry_vix REAL,
            mae_pct REAL, mfe_pct REAL
        )
    """)
    conn.execute("CREATE TABLE open_positions (symbol TEXT, owner TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE manual_order_audit (symbol TEXT, action TEXT, timestamp TEXT)")
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _fixed_et(monkeypatch):
    from app.utils import timezone as tz
    monkeypatch.setattr(tz, "now_et", lambda: _FIXED_ET)
    monkeypatch.setattr(tz, "get_trade_date", lambda: "2026-08-14")


def test_p2_17_update_path_writes_et_exit_time(tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, exit_price, source_type) "
        "VALUES ('XOM', 'LONG', 0, 'MANUAL')"
    )
    conn.commit()
    audit.record_manual_close(
        _FakeService(conn), "XOM", "LONG",
        entry_price=100.0, exit_price=110.0, quantity=10,
        reason="test", order_id="oid-1",
    )
    conn2 = sqlite3.connect(tmp_path / "audit.db")
    try:
        row = conn2.execute("SELECT exit_time, realized_pnl FROM trade_records WHERE exit_price>0").fetchone()
    finally:
        conn2.close()
    # 修复前: 北京时间 → 次日日期, date(exit_time)≠ET 交易日, 日回撤 SUM 漏记
    assert row[0] == _FIXED_ET.isoformat()
    assert row[1] == pytest.approx(100.0)


def test_p2_17_insert_path_writes_et_exit_time(tmp_path):
    conn = _make_db(tmp_path)
    audit.record_manual_close(
        _FakeService(conn), "CVX", "SHORT",
        entry_price=100.0, exit_price=90.0, quantity=5,
        reason="test", order_id="oid-2",
    )
    conn2 = sqlite3.connect(tmp_path / "audit.db")
    try:
        row = conn2.execute("SELECT exit_time FROM trade_records WHERE symbol='CVX'").fetchone()
    finally:
        conn2.close()
    assert row[0] == _FIXED_ET.isoformat()


# ── #26: admission stop_loss 键契约 ──────────────────────────────────────

def test_26_admission_sl_check_active_only_with_key(monkeypatch):
    from app.playbook import admission
    monkeypatch.setattr(admission, "get_admission_config",
                        lambda refresh=False: {"enforce": True, "max_sl_pct": 5.0})
    base = {"symbol": "XOM", "direction": "SELL_SHORT",
            "entry_price": 100.0, "take_profit": 0, "position_size": 10,
            "confidence": 60, "date": "2026-08-14"}
    with_sl = admission.evaluate({**base, "stop_loss": 106.09})
    assert with_sl["verdict"] == "REJECT"
    assert any(r.startswith("SL_STOP_TOO_WIDE") for r in with_sl["reasons"])  # 6.09% > 5%
    # 缺键 (修复前 playbook_service 执行复核的实际入参): SL 判据静默空转
    without_sl = admission.evaluate(dict(base))
    assert not any(r.startswith("SL_STOP_TOO_WIDE") for r in without_sl["reasons"])


def test_26_playbook_service_exec_recheck_passes_stop_loss():
    # 接线契约: playbook_service.py 执行时复核的 admission 入参必须含 stop_loss 键
    src = (PROJECT_ROOT / "app" / "playbook" / "playbook_service.py").read_text(encoding="utf-8")
    assert '"stop_loss": stop_loss' in src, "执行时复核 admission 入参缺 stop_loss 键 (#26 回归)"
