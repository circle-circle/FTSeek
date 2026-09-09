# -*- coding: utf-8 -*-
"""RFC-080 S4: 实盘周报 auto/manual 分臂契约测试.

钉锁:
  1. roundtrip_section 返回 arms dict (auto=PLAYBOOK_LIVE / manual=其余, 含 NULL)
  2. 两臂 n 之和 = 总 n (无重复无遗漏 — NULL source_type 归 manual)
  3. 渲染层: 有数据时输出"分臂"行; state != OK 时不输出 (不伪装空)
"""
import sqlite3
from pathlib import Path

import pytest

import scripts.live_weekly_review as lwr


@pytest.fixture
def fake_live_db(tmp_path, monkeypatch):
    db = tmp_path / "ftseek_us_live.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE trade_records (
        symbol TEXT, direction TEXT, exit_reason TEXT, trade_mode TEXT, source_type TEXT,
        entry_price REAL, exit_price REAL, entry_time TEXT, exit_time TEXT, quantity REAL,
        realized_pnl REAL, pnl_net REAL)""")
    rows = [
        # auto-live 臂 (cutover 后的 PLAYBOOK_LIVE)
        ("US.AAA", "LONG", "SL_TRAIL_LOCK", "live", "PLAYBOOK_LIVE",
         100, 105, "2026-09-02T10:00:00", "2026-09-03T11:00:00", 10, 45.0, 40.0),
        ("US.BBB", "SHORT", "SL_INITIAL", "live", "PLAYBOOK_LIVE",
         200, 210, "2026-09-02T10:00:00", "2026-09-03T12:00:00", 5, -55.0, -60.0),
        # manual 臂 (MANUAL / POSITION_SYNC / NULL 全归 manual)
        ("US.CCC", "LONG", "MANUAL_CLOSE", "live", "MANUAL",
         50, 52, "2026-09-01T10:00:00", "2026-09-04T11:00:00", 20, 40.0, None),
        ("US.DDD", "LONG", "MANUAL_CLOSE", "live", None,
         30, 29, "2026-09-01T10:00:00", "2026-09-05T11:00:00", 10, -10.0, -12.0),
        # 非 live 行不得混入
        ("US.SIM", "LONG", "SL", "simulate", "PLAYBOOK_SIM",
         10, 11, "2026-09-01T10:00:00", "2026-09-05T11:00:00", 100, 100.0, 99.0),
    ]
    conn.executemany("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(lwr, "_db_path", lambda: db)
    return db


class TestArmsSplit:
    def test_arms_present_and_split_correct(self, fake_live_db):
        rt = lwr.roundtrip_section("2026-09-01", "2026-09-07")
        assert rt["state"] == "OK"
        arms = rt["arms"]
        assert arms["auto"]["n"] == 2
        assert arms["auto"]["net_pnl"] == pytest.approx(-20.0)   # 40 - 60 (净口径 pnl_net 优先)
        assert arms["manual"]["n"] == 2                           # MANUAL + NULL 都归 manual
        assert arms["manual"]["net_pnl"] == pytest.approx(28.0)   # 40(realized, pnl_net NULL) - 12
        # 两臂之和 = 总 n, 无重复无遗漏
        assert arms["auto"]["n"] + arms["manual"]["n"] == rt["n"] == 4
        # 模拟行不混入
        assert rt["n"] == 4

    def test_render_shows_arms_line(self, fake_live_db):
        rt = lwr.roundtrip_section("2026-09-01", "2026-09-07")
        lines = lwr._render_market("US", {"state": "NO_DATA"}, {"state": "NO_DATA"},
                                   {"state": "NONE"}, rt, None)
        text = "\n".join(lines)
        assert "分臂" in text
        assert "auto(PLAYBOOK_LIVE) 2笔" in text
        assert "manual 2笔" in text

    def test_no_arms_line_when_window_empty(self, fake_live_db):
        """窗口内无已了结行 → state != OK → 不输出分臂行 (不伪装空)."""
        rt = lwr.roundtrip_section("2026-10-01", "2026-10-07")
        assert rt["state"] != "OK"
        lines = lwr._render_market("US", {"state": "NO_DATA"}, {"state": "NO_DATA"},
                                   {"state": "NONE"}, rt, None)
        assert "分臂" not in "\n".join(lines)
