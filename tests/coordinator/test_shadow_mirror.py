"""
Shadow 1:1 对照单测 (Z 系列, 2026-05-15)
"""
import os
import sqlite3
import tempfile
from unittest.mock import patch
import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    """临时 trade_records DB (含 shadow_trades schema)"""
    db = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE shadow_trades (
            trade_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'A',
            direction TEXT NOT NULL,
            signal_score INTEGER DEFAULT 0,
            signal_detail TEXT DEFAULT '',
            entry_time TEXT, exit_time TEXT,
            entry_price REAL, exit_price REAL,
            quantity INTEGER DEFAULT 0,
            realized_pnl REAL DEFAULT 0,
            pnl_pct REAL DEFAULT 0,
            exit_reason TEXT,
            holding_minutes REAL DEFAULT 0,
            closed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db",
        lambda: db
    )
    yield db
    if os.path.exists(db):
        os.unlink(db)


# ─── Z1 ShadowMirror 入场 ─────────────────────────────────
def test_z1_mirror_entry_creates_n_variants(tmp_db):
    from app.evolution.shadow_mirror import ShadowMirror
    m = ShadowMirror()
    ids = m.mirror_entry(
        real_trace_id="trace-abc",
        symbol="NVDA",
        direction="LONG",
        entry_price=150.0,
        quantity=100,
    )
    # 2026-05-19 更新: 默认 8 变体, 但 LONG 信号中 short_only 被过滤 → 实际写 7
    # baseline / tighter_stop / no_chandelier / quick_exit (4 老)
    # tp_sl_5to1 / min_hold_6min / early_chandelier (3 新, short_only 拒 LONG)
    assert len(ids) == 7, f"LONG 入场应写 7 镜像 (8 - short_only 拒 LONG), 实际 {len(ids)}"
    # 验证 DB
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT strategy, symbol, direction, closed FROM shadow_trades"
    ).fetchall()
    assert len(rows) == 7
    variants = [r[0] for r in rows]
    assert "baseline" in variants
    assert all(r[3] == 0 for r in rows)  # 全部未关闭


def test_z1_mirror_disabled_returns_empty(tmp_db):
    from app.evolution.shadow_mirror import ShadowMirror
    m = ShadowMirror()
    m.set_enabled(False)
    ids = m.mirror_entry(
        real_trace_id="x", symbol="NVDA", direction="LONG",
        entry_price=150, quantity=100,
    )
    assert ids == []


# ─── Z1 ShadowMirror 出场 (baseline 同步用 real 价格) ──────
def test_z1_mirror_exit_baseline_uses_real_price(tmp_db):
    from app.evolution.shadow_mirror import ShadowMirror
    m = ShadowMirror()
    m.mirror_entry(
        real_trace_id="trace-xyz", symbol="NVDA", direction="LONG",
        entry_price=150.0, quantity=100,
    )
    # 实盘 +$5/股 平仓
    m.mirror_exit(
        real_trace_id="trace-xyz", symbol="NVDA",
        exit_price=155.0, exit_reason="TRAILING",
    )
    conn = sqlite3.connect(tmp_db)
    # baseline 应该已平仓, 用 $155 价格
    row = conn.execute(
        "SELECT realized_pnl, exit_price, closed FROM shadow_trades WHERE strategy='baseline'"
    ).fetchone()
    assert row[2] == 1  # closed
    assert row[1] == 155.0
    assert row[0] == pytest.approx(500.0, abs=0.1)  # ($155-$150)×100 = $500


def test_z1_mirror_exit_non_baseline_keeps_open(tmp_db):
    """非 baseline 变体不立即平仓 (等 EOD 评估)"""
    from app.evolution.shadow_mirror import ShadowMirror
    m = ShadowMirror()
    m.mirror_entry(
        real_trace_id="trace-yyy", symbol="TSLA", direction="LONG",
        entry_price=200.0, quantity=50,
    )
    m.mirror_exit(
        real_trace_id="trace-yyy", symbol="TSLA",
        exit_price=210.0, exit_reason="TP",
    )
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT strategy, closed FROM shadow_trades WHERE symbol='TSLA'"
    ).fetchall()
    closed_by_variant = {r[0]: r[1] for r in rows}
    assert closed_by_variant["baseline"] == 1
    # 其他变体保持 open
    assert closed_by_variant["tighter_stop"] == 0
    assert closed_by_variant["no_chandelier"] == 0


def test_z1_mirror_short_pnl():
    """SHORT pnl 计算正确"""
    from app.evolution.shadow_mirror import ShadowMirror
    m = ShadowMirror()
    # SHORT entry $200, exit $190 → pnl = ($200-$190) × 100 = $1000
    pnl = m._calc_pnl("SHORT", 200, 190, 100)
    assert pnl == 1000


# ─── Z3 shadow_compare CLI ──────────────────────────────
def test_z3_compare_module_importable():
    """CLI 可 import (无 syntax error)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sc", "scripts/shadow_compare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "cmd_overview")
    assert hasattr(mod, "cmd_by_symbol")
    assert hasattr(mod, "cmd_variant_diff")
    assert hasattr(mod, "cmd_recent_pairs")


# ─── Z4 daily_review 集成 ────────────────────────────────
def test_z4_daily_review_has_shadow_section():
    src = open("app/review/daily_review.py").read()
    assert "_analyze_shadow_vs_real" in src
    assert "_analyze_pnl_outliers" in src
    assert "_analyze_loss_guard" in src
    assert "shadow_comparison" in src
