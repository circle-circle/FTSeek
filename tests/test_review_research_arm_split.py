"""日报研究臂分账: headline 总账之外, 单独显示 research_momentum 与其余人群。

10-19 事故形态: 研究臂 4 笔了结 +$53.18 混在 headline +$130.93 里, 读者无法
判断核心策略贡献。本 section 只读分账, 不改 headline/OKR 判分 SQL。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.review.bundle_builder import build_research_arm_split  # noqa: E402


def _connection():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, entry_time TEXT, "
        "exit_time TEXT, exit_reason TEXT, entry_session TEXT, strategy_id TEXT, "
        "owner_strategy TEXT, source_type TEXT, trade_mode TEXT, entry_reason TEXT, "
        "signal_id TEXT, decision_id TEXT, entry_price REAL, exit_price REAL, "
        "realized_pnl REAL, pnl_net REAL, fees_est REAL, mfe_pct REAL, mae_pct REAL, "
        "quantity INTEGER, metadata_json TEXT, data_quality TEXT)"
    )
    rows = [
        (1, "US.RESEARCH", "LONG", "2026-10-19T09:40:00-04:00",
         "2026-10-19T10:00:00-04:00", "SL_BREAKEVEN", "regular", None,
         "extended_hours_entry", "PLAYBOOK_SIM", "simulate", None, None, None,
         100.0, 101.0, 55.0, 50.0, 5.0, 1.0, 0.2, 10,
         '{"gate_mode": "research_momentum", "research_source": "research_watchlist"}',
         "real"),
        (2, "US.CORE", "LONG", "2026-10-19T09:40:00-04:00",
         "2026-10-19T10:00:00-04:00", "SL_TRAIL_LOCK", "regular", None,
         "playbook_sim", "PLAYBOOK_SIM", "simulate", None, None, None,
         100.0, 101.0, 24.0, 20.0, 4.0, 1.0, 0.2, 10, '{}', "real"),
        (3, "US.OPEN_RESEARCH", "LONG", "2026-10-19T16:30:00-04:00",
         None, None, "afterhours", None, "extended_hours_entry", "PLAYBOOK_SIM",
         "simulate", None, None, None, 100.0, 0.0, None, None, 0.0, 0.0, 0.0, 10,
         '{"gate_mode": "research_momentum", "research_source": "research_beat"}',
         "real"),
    ]
    con.executemany(
        "INSERT INTO trade_records (id, symbol, direction, entry_time, exit_time, "
        "exit_reason, entry_session, strategy_id, owner_strategy, source_type, "
        "trade_mode, entry_reason, signal_id, decision_id, entry_price, exit_price, "
        "realized_pnl, pnl_net, fees_est, mfe_pct, mae_pct, quantity, metadata_json, "
        "data_quality) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?)",
        rows,
    )
    return con


def test_research_arm_split_separates_closed_and_open_positions():
    out = build_research_arm_split(_connection(), "2026-10-19")
    assert out["research_trade_count"].value == 1
    assert out["research_net_pnl"].value == 50.0
    assert out["research_gross_pnl"].value == 55.0
    assert out["research_fees"].value == 5.0
    assert out["research_win_count"].value == 1
    assert out["research_win_rate"].value == 1.0
    assert out["core_trade_count"].value == 1
    assert out["core_net_pnl"].value == 20.0
    assert out["core_win_count"].value == 1
    assert out["research_sources"].value == "research_watchlist 1"
    assert out["open_research_count"].value == 1
    assert out["open_research_symbols"].value == ["US.OPEN_RESEARCH"]
    assert out["open_research_sources"].value == "research_beat 1"
