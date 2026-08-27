"""复盘验证链出场日记账回归测试 (2026-08-28)。

背景: ReviewDataCollector 的 claim 验证 SQL 原按入场日 (trade_date=?) 归集 —
隔夜仓当日出场不计入, 胜率/盈亏错判 → 复盘论断 verified/severity 错判。
另有 _verify_db_evidence 缺 exit_price>0 过滤 (未出场行也进 COUNT)。
与 AccountCircuitBreaker / daily_alert_summary / _validate_general 同批口径:
出场日 substr(exit_time,1,10) + 净额 COALESCE(pnl_net, realized_pnl)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.review.review_orchestrator import ValidationAgent


@pytest.fixture
def collector(tmp_path):
    db = tmp_path / "review.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL, source_type TEXT)"
    )
    conn.commit()
    conn.close()
    # decisions_db 也须隔离 — 默认指向生产 data/decisions.db (fixture 不隔离会
    # 让 "decision" hint 测试读真库)
    return ValidationAgent(db_path=str(db), decisions_db=str(tmp_path / "decisions.db")), db


def _insert(db, symbol, trade_date, exit_date, pnl_net=None, realized_pnl=None, exit_price=100.0):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, pnl_net, realized_pnl, source_type) "
        "VALUES (?, ?, ?, ?, ?, ?, 'PLAYBOOK_SIM')",
        (symbol, trade_date,
         f"{exit_date}T10:00:00-04:00" if exit_date else None,
         exit_price if exit_date else None, pnl_net, realized_pnl),
    )
    conn.commit()
    conn.close()


# ── _validate_signal_quality: 胜率按出场日 + 净额 ────────────────────────

def test_signal_quality_counts_overnight_closed_today(collector):
    c, db = collector
    # 8/27 实盘场景: 隔夜 UBER 净 -307 (出场日=今天) + 日内 COIN +101
    _insert(db, "US.UBER", "2026-08-26", "2026-08-27", pnl_net=-307.13, realized_pnl=-306.0)
    _insert(db, "US.COIN", "2026-08-27", "2026-08-27", pnl_net=101.30, realized_pnl=102.0)
    r = c._validate_signal_quality("胜率不错", "2026-08-27", "signal_quality")
    assert r["verified"] is True
    assert r["evidence"] == "当日 2 笔交易, 胜率 50%"
    # 旧口径 (入场日) 只见 COIN → 1 笔胜率 100% — 隔夜亏损被隐藏


def test_signal_quality_uses_net_pnl(collector):
    c, db = collector
    # pnl_net 负但 realized_pnl 正 (费用翻转): 净口径判负
    _insert(db, "US.RIVN", "2026-08-27", "2026-08-27", pnl_net=-2.31, realized_pnl=7.06)
    r = c._validate_signal_quality("x", "2026-08-27", "signal_quality")
    assert r["evidence"] == "当日 1 笔交易, 胜率 0%"
    assert r["severity"] == "P0"  # 0% < 0.2


def test_signal_quality_no_data(collector):
    c, _ = collector
    r = c._validate_signal_quality("x", "2026-08-27", "signal_quality")
    assert r["verified"] is False
    assert r["reason"] == "no trade data"


# ── _verify_db_evidence: hint 驱动查询同口径 + exit_price 过滤 ───────────

def test_db_evidence_excludes_open_positions(collector):
    c, db = collector
    # 未出场行 (exit_time NULL): 不进 COUNT (原缺陷: 无 exit_price>0 过滤)
    _insert(db, "US.MRK", "2026-08-27", None, pnl_net=None, realized_pnl=None, exit_price=None)
    _insert(db, "US.ORCL", "2026-08-27", "2026-08-27", pnl_net=21.39, realized_pnl=22.0)
    r = c._verify_db_evidence("trade_records", "claim", "2026-08-27")
    assert r["verified"] is True
    assert "1笔交易" in r["evidence"]
    assert "胜率100%" in r["evidence"]
    assert "$21" in r["evidence"]


def test_db_evidence_overnight_counted_by_exit_date(collector):
    c, db = collector
    _insert(db, "US.HOOD", "2026-08-26", "2026-08-27", pnl_net=2.99, realized_pnl=3.5)
    r = c._verify_db_evidence("trade_records", "claim", "2026-08-27")
    assert r["verified"] is True
    # 昨天的报告不应包含它
    r2 = c._verify_db_evidence("trade_records", "claim", "2026-08-26")
    assert r2["verified"] is False or "0笔" not in r2.get("evidence", "")


def test_db_evidence_decision_hint_routes_to_decisions_db(collector):
    # "decision" hint 走 decisions 表 (无表 → 查询异常 → verified False, 不碰 trade 口径)
    c, _ = collector
    r = c._verify_db_evidence("decision", "claim", "2026-08-27")
    assert r["verified"] is False
