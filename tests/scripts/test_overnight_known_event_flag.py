"""Tests for scripts/overnight_known_event_flag.py (影子 ⑦).

Hermetic: fixture DB + fixture plan json, no futu/OpenD, no network.
Covers:
- 命中 / 未命中 / 计划缺失 三路分支
- 幂等重跑 (同日同 symbol 不重复)
- --report 的 cohort 统计与净额口径 (pnl_net 优先否则 realized_pnl−fees_est)
- 死线停采 (2026-12-31 之后)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import importlib
import scripts.overnight_known_event_flag as mod


@pytest.fixture(autouse=True)
def _reload():
    """Reload module to pick up patched paths."""
    importlib.reload(mod)
    yield


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """Create a hermetic sqlite DB with open_positions + trade_records."""
    db_path = tmp_path / "test_ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY,
            direction TEXT NOT NULL,
            size REAL,
            avg_entry_price REAL,
            initial_sl REAL,
            current_sl REAL,
            initial_tp REAL,
            take_profit_json TEXT,
            trail_state_json TEXT,
            entry_time TEXT,
            time_stop_hours INTEGER,
            strategy_id TEXT,
            signal_id TEXT,
            position_id TEXT,
            market_regime TEXT,
            updated_at TEXT NOT NULL,
            owner_strategy TEXT,
            mode TEXT DEFAULT 'normal',
            metadata_json TEXT,
            owner TEXT DEFAULT 'auto',
            account_type TEXT DEFAULT 'simulate'
        );
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_time TEXT,
            exit_time TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity INTEGER,
            realized_pnl REAL,
            pnl_pct REAL,
            exit_reason TEXT,
            holding_hours REAL,
            owner_strategy TEXT,
            mode TEXT DEFAULT 'normal',
            fees_est REAL,
            pnl_net REAL,
            fees_source TEXT
        );
    """)
    # Insert test positions
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, direction, size, avg_entry_price, updated_at, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("US.AAPL", "LONG", 10.0, 150.0, "2026-09-24T10:00:00-04:00", "playbook_sim"),
    )
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, direction, size, avg_entry_price, updated_at, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("US.MSFT", "LONG", 5.0, 300.0, "2026-09-24T10:00:00-04:00", "playbook_sim"),
    )
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, direction, size, avg_entry_price, updated_at, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("US.TSLA", "SHORT", 3.0, 250.0, "2026-09-24T10:00:00-04:00", "extended_hours_entry"),
    )
    # A position that should be excluded (different owner_strategy)
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, direction, size, avg_entry_price, updated_at, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("US.EXCLUDED", "LONG", 1.0, 100.0, "2026-09-24T10:00:00-04:00", "main"),
    )
    # Trade records for cohort PnL
    # Flagged trade: AAPL on 2026-09-25 (overnight from 09-24)
    conn.execute(
        "INSERT INTO trade_records "
        "(trade_date, symbol, direction, realized_pnl, pnl_net, fees_est, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-09-25", "US.AAPL", "LONG", -30.0, -35.0, 5.0, "playbook_sim"),
    )
    # Unflagged trade: MSFT on 2026-09-25
    conn.execute(
        "INSERT INTO trade_records "
        "(trade_date, symbol, direction, realized_pnl, pnl_net, fees_est, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-09-25", "US.MSFT", "LONG", 20.0, 15.0, 5.0, "playbook_sim"),
    )
    # Trade with no pnl_net → fallback to realized_pnl - fees_est
    conn.execute(
        "INSERT INTO trade_records "
        "(trade_date, symbol, direction, realized_pnl, pnl_net, fees_est, owner_strategy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-09-25", "US.TSLA", "SHORT", -50.0, None, 3.0, "extended_hours_entry"),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def fixture_plan_dir(tmp_path: Path) -> Path:
    """Create a hermetic plan directory with premarket + afterhours files."""
    plan_dir = tmp_path / "ext_entry_plan"
    plan_dir.mkdir(parents=True)
    # 2026-09-24 premarket: AAPL is planned, MSFT is NOT
    premarket = [
        {
            "symbol": "AAPL",
            "et_date": "2026-09-24",
            "session": "premarket",
            "phase": "before",
            "state": "planned",
            "habit_hint": {"phase": "bmo", "n_history": 5, "consistency": 0.8},
            "plan_hash": "abc123",
        },
    ]
    (plan_dir / "2026-09-24_premarket.json").write_text(
        json.dumps(premarket, ensure_ascii=False), encoding="utf-8"
    )
    # 2026-09-24 afterhours: empty
    (plan_dir / "2026-09-24_afterhours.json").write_text(
        json.dumps([], ensure_ascii=False), encoding="utf-8"
    )
    return plan_dir


def _patch_paths(fixture_db: Path, fixture_plan_dir: Path, tmp_path: Path):
    """Patch module-level paths to use fixtures."""
    mod.DB_PATH = fixture_db
    mod.PLAN_DIR = fixture_plan_dir
    mod.OUT = tmp_path / "overnight_known_event_flags.jsonl"


# ---------------------------------------------------------------------------
# 命中 / 未命中 / 计划缺失 三路分支
# ---------------------------------------------------------------------------

class TestThreeBranches:
    """Test the three flag branches: hit, miss, plan_missing."""

    def test_hit_flag(self, fixture_db, fixture_plan_dir, tmp_path):
        """AAPL is in plan → overnight_into_known_event."""
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-24")
        aapl_flags = [f for f in flags if f["symbol"] == "AAPL"]
        assert len(aapl_flags) == 1
        assert aapl_flags[0]["flag"] == "overnight_into_known_event"
        assert aapl_flags[0]["position_direction"] == "LONG"
        assert aapl_flags[0]["position_qty"] == 10.0
        assert aapl_flags[0]["event_phase"] == "before"
        assert aapl_flags[0]["event_session"] == "premarket"
        assert aapl_flags[0]["habit_hint"] is not None

    def test_miss_flag(self, fixture_db, fixture_plan_dir, tmp_path):
        """MSFT not in plan → overnight_no_known_event."""
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-24")
        msft_flags = [f for f in flags if f["symbol"] == "MSFT"]
        assert len(msft_flags) == 1
        assert msft_flags[0]["flag"] == "overnight_no_known_event"
        assert msft_flags[0]["event_phase"] is None

    def test_excluded_owner(self, fixture_db, fixture_plan_dir, tmp_path):
        """US.EXCLUDED (owner_strategy='main') should not appear."""
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-24")
        syms = {f["symbol"] for f in flags}
        assert "EXCLUDED" not in syms

    def test_plan_missing(self, fixture_db, tmp_path):
        """Both plan files missing → plan_missing flag."""
        plan_dir = tmp_path / "empty_plans"
        plan_dir.mkdir()
        _patch_paths(fixture_db, plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-25")
        assert len(flags) == 1
        assert flags[0]["flag"] == "plan_missing"
        assert flags[0]["symbol"] is None

    def test_tsla_ext_owner(self, fixture_db, fixture_plan_dir, tmp_path):
        """TSLA (extended_hours_entry owner) also gets flagged."""
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-24")
        tsla_flags = [f for f in flags if f["symbol"] == "TSLA"]
        assert len(tsla_flags) == 1
        assert tsla_flags[0]["flag"] == "overnight_no_known_event"
        assert tsla_flags[0]["position_direction"] == "SHORT"
        assert tsla_flags[0]["owner_strategy"] == "extended_hours_entry"


# ---------------------------------------------------------------------------
# 幂等重跑
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Same date+symbol should not produce duplicate rows."""

    def test_idempotent_rerun(self, fixture_db, fixture_plan_dir, tmp_path):
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        # First run
        flags1 = mod.build_flags("2026-09-24")
        n1 = mod.append_flags(flags1)
        assert n1 == 3  # AAPL + MSFT + TSLA

        # Second run — should produce 0 new rows
        flags2 = mod.build_flags("2026-09-24")
        n2 = mod.append_flags(flags2)
        assert n2 == 0

        # Verify jsonl has exactly 3 rows
        lines = [l for l in mod.OUT.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# --report cohort PnL + 净额口径
# ---------------------------------------------------------------------------

class TestReport:
    """Test --report mode: cohort stats, net PnL convention, sampling progress."""

    def test_report_cohort_stats(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        # Build and append flags
        flags = mod.build_flags("2026-09-24")
        mod.append_flags(flags)

        # Run report
        rc = mod.report()
        assert rc == 0
        out = capsys.readouterr().out
        # Should contain sampling progress
        assert "采样进度" in out
        assert "3/20" in out
        # Should contain cohort PnL
        assert "flagged" in out.lower() or "flagged" in out
        assert "unflagged" in out.lower() or "unflagged" in out

    def test_net_pnl_prefers_pnl_net(self, fixture_db, fixture_plan_dir, tmp_path):
        """_net_pnl should prefer pnl_net over realized_pnl - fees_est."""
        row = {"pnl_net": -35.0, "realized_pnl": -30.0, "fees_est": 5.0}
        assert mod._net_pnl(row) == -35.0

    def test_net_pnl_fallback(self, fixture_db, fixture_plan_dir, tmp_path):
        """When pnl_net is None, fall back to realized_pnl - fees_est."""
        row = {"pnl_net": None, "realized_pnl": -50.0, "fees_est": 3.0}
        assert mod._net_pnl(row) == -53.0

    def test_net_pnl_both_none(self, fixture_db, fixture_plan_dir, tmp_path):
        """When both are None, return None."""
        row = {"pnl_net": None, "realized_pnl": None, "fees_est": None}
        assert mod._net_pnl(row) is None

    def test_report_no_flags_file(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        """Report with no jsonl file → graceful message."""
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        rc = mod.report()
        assert rc == 0
        out = capsys.readouterr().out
        assert "台账不存在" in out


# ---------------------------------------------------------------------------
# 死线停采
# ---------------------------------------------------------------------------

class TestDeadline:
    """After 2026-12-31, script should stop collecting and exit 0."""

    def test_past_deadline(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        rc = mod.main(["--date", "2027-01-01"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "死线已过" in out
        # No file should be created
        assert not mod.OUT.exists()

    def test_on_deadline_ok(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        """On the deadline date itself, collection should still work."""
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        # 2026-12-31 is within deadline; but we need plan files for that date
        plan_dir = tmp_path / "deadline_plans"
        plan_dir.mkdir()
        premarket = [{"symbol": "AAPL", "et_date": "2026-12-31", "session": "premarket",
                       "phase": "before", "state": "planned"}]
        (plan_dir / "2026-12-31_premarket.json").write_text(
            json.dumps(premarket), encoding="utf-8"
        )
        mod.PLAN_DIR = plan_dir
        rc = mod.main(["--date", "2026-12-31"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "死线" not in out


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

class TestMainIntegration:
    """Integration tests for main() with --dry and normal modes."""

    def test_dry_mode(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        rc = mod.main(["--date", "2026-09-24", "--dry"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--dry" in out
        assert not mod.OUT.exists()

    def test_normal_mode_appends(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        rc = mod.main(["--date", "2026-09-24"])
        assert rc == 0
        assert mod.OUT.exists()
        lines = [l for l in mod.OUT.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 3

    def test_report_mode(self, fixture_db, fixture_plan_dir, tmp_path, capsys):
        _patch_paths(fixture_db, fixture_plan_dir, tmp_path)
        # First populate
        mod.main(["--date", "2026-09-24"])
        # Then report
        rc = mod.main(["--report"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "采样进度" in out

    def test_no_positions_message(self, tmp_path, capsys):
        """When no positions match, print message and exit 0."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE open_positions (
                symbol TEXT PRIMARY KEY, direction TEXT NOT NULL, size REAL,
                avg_entry_price REAL, entry_time TEXT, updated_at TEXT NOT NULL,
                owner_strategy TEXT,
                mode TEXT DEFAULT 'normal', metadata_json TEXT, owner TEXT DEFAULT 'auto'
            );
        """)
        conn.close()
        plan_dir = tmp_path / "plans"
        plan_dir.mkdir()
        # Provide a plan file so we don't hit plan_missing branch
        (plan_dir / "2026-09-24_premarket.json").write_text(
            json.dumps([{"symbol": "XYZ", "et_date": "2026-09-24",
                          "session": "premarket", "phase": "before", "state": "planned"}]),
            encoding="utf-8",
        )
        _patch_paths(db_path, plan_dir, tmp_path)
        rc = mod.main(["--date", "2026-09-24"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "无持仓" in out


# ---------------------------------------------------------------------------
# --next-day 模式 (cron T+1 盘前窗)
# ---------------------------------------------------------------------------

class TestNextDayMode:
    """Tests for --next-day flag: date calc, premarket-only, narrowed plan_missing."""

    def test_next_day_date_calculation(self, fixture_db, fixture_plan_dir, tmp_path):
        """et_next_day() returns ET today + 1."""
        from datetime import timedelta
        expected = (mod.datetime.now(mod.ET).date() + timedelta(days=1)).isoformat()
        assert mod.et_next_day() == expected

    def test_next_day_no_false_plan_missing(self, fixture_db, tmp_path):
        """premarket file exists, afterhours absent → NOT plan_missing (no false alarm).

        This is the core fix: cron at CST 08:45 runs before afterhours plan is generated,
        so afterhours file legitimately doesn't exist. Must not trigger plan_missing.
        """
        plan_dir = tmp_path / "premarket_only_plans"
        plan_dir.mkdir()
        # Only premarket file exists (afterhours legitimately absent at cron time)
        premarket = [{"symbol": "AAPL", "et_date": "2026-09-25",
                       "session": "premarket", "phase": "before", "state": "planned"}]
        (plan_dir / "2026-09-25_premarket.json").write_text(
            json.dumps(premarket, ensure_ascii=False), encoding="utf-8"
        )
        # No afterhours file created
        _patch_paths(fixture_db, plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-25", premarket_only=True)
        # Should NOT be plan_missing — premarket exists
        assert len(flags) > 0
        for f in flags:
            assert f["flag"] != "plan_missing", \
                "False plan_missing: afterhours absent should not trigger in --next-day mode"
            assert f["plan_scope"] == "premarket"

    def test_next_day_plan_missing_premarket_absent(self, fixture_db, tmp_path):
        """premarket file missing in --next-day mode → plan_missing (true outage).

        CST 08:30 generation failed → premarket file absent → real outage to record.
        """
        plan_dir = tmp_path / "empty_plans"
        plan_dir.mkdir()
        # Neither file exists
        _patch_paths(fixture_db, plan_dir, tmp_path)
        flags = mod.build_flags("2026-09-25", premarket_only=True)
        assert len(flags) == 1
        assert flags[0]["flag"] == "plan_missing"
        assert flags[0]["plan_scope"] == "premarket"
        # missing_files should list only premarket (not afterhours)
        assert len(flags[0]["missing_files"]) == 1
        assert "premarket" in flags[0]["missing_files"][0]
