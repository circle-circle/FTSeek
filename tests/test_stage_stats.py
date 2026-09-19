"""阶段统计 SSOT + 延长时段阶段日报 契约测试 (2026-09-18 用户裁「日报/周报加阶段维度 + 独立阶段日报」)。

覆盖:
  1. 阶段归一: 落库 entry_session 优先 → ET 时钟回退 → 空串(未标注, 不臆造)
  2. 阶段聚合: 净额/胜率/MFE=0; 阶段之间不互相污染
  3. 渲染: 含 阶段×方向×出场原因 三维 + 延长时段单列; 样本 <5 标注"仅方向性"
  4. 空区间/无窗口 → 空串 (报告不臆造区间)
  5. 延长时段日报: 无样本不推送 (零噪音) —— 由 has_sample 语义保证
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.review.stage_stats import (  # noqa: E402
    MIN_N_FOR_READING, normalize_stage, render_stage_section, stage_breakdown, window_of,
    rows_for_closed_trades,
)


def _row(symbol, stage, dir_cn, reason, net, mfe=1.0):
    return {"symbol": symbol, "stage": stage, "dir_cn": dir_cn, "exit_reason": reason,
            "net": net, "gross": net, "mfe_pct": mfe, "direction": "LONG" if dir_cn == "做多" else "SHORT"}


# ── 1. 阶段归一 ──────────────────────────────────────────────────────────────

def test_entry_session_wins_over_clock():
    """落库 session 与时钟冲突时以落库为准 (app 自己的标签优先于推断)。"""
    # 09:41 ET 时钟判盘中, 但落库说 premarket ⇒ 取落库
    assert normalize_stage("premarket", "2026-09-18T09:41:00-04:00") == "premarket"


def test_falls_back_to_clock_when_session_empty():
    assert normalize_stage("", "2026-09-18T18:35:00-04:00") == "afterhours"
    assert normalize_stage(None, "2026-09-18T05:30:00-04:00") == "premarket"
    assert normalize_stage("", "2026-09-18T11:00:00-04:00") == "regular"


def test_offhours_never_invented():
    assert normalize_stage("", "2026-09-18T22:30:00-04:00") == ""      # 盘外
    assert normalize_stage("", None) == ""
    assert normalize_stage("garbage", "2026-09-18T22:30:00-04:00") == ""


# ── 2. 聚合 ─────────────────────────────────────────────────────────────────

def test_breakdown_keeps_stages_separate():
    rows = [_row("A", "premarket", "做多", "tighten", -180.0),
            _row("B", "premarket", "做多", "breakeven_lock", -150.0),
            _row("C", "regular", "做多", "SL_TRAIL_LOCK", +200.0, mfe=0.0),
            _row("D", "regular", "做多", "SL_INITIAL", -100.0, mfe=0.0),
            _row("E", "afterhours", "做多", "SL_TRAIL_LOCK", +55.0)]
    bd = stage_breakdown(rows)
    assert list(bd.keys()) == ["premarket", "regular", "afterhours"]
    assert bd["premarket"]["n"] == 2 and bd["premarket"]["net"] == -330.0
    assert bd["premarket"]["wins"] == 0
    assert bd["regular"]["n"] == 2 and bd["regular"]["net"] == +100.0
    assert bd["regular"]["mfe0"] == 2
    # 关键: 混合口径下"略亏", 拆开才看见盘前在放血 —— 这正是做阶段统计的理由
    assert sum(b["net"] for b in bd.values()) == -175.0


def test_breakdown_drops_empty_stage_buckets():
    bd = stage_breakdown([_row("A", "regular", "做多", "SL_TRAIL_LOCK", 10.0)])
    assert list(bd.keys()) == ["regular"]


def test_stage_x_direction_x_reason_rendered():
    rows = [_row("A", "premarket", "做多", "tighten", -352.21),
            _row("B", "premarket", "做多", "tighten", -99.37),
            _row("C", "regular", "做空", "SL_INITIAL", -599.33)]
    txt = render_stage_section(rows)
    assert "阶段 × 方向 × 出场原因" in txt
    assert "| 盘前 | 做多 | tighten | 2 | -$451.58 | 0% |" in txt
    assert "| 盘中 | 做空 | SL_INITIAL | 1 | -$599.33 | 0% |" in txt


def test_small_sample_is_flagged_directional_only():
    rows = [_row("A", "premarket", "做多", "tighten", -100.0)]
    txt = render_stage_section(rows)
    assert "样本少" in txt and "仅作方向性参考" in txt
    assert MIN_N_FOR_READING == 5


def test_extended_hours_column_present():
    rows = [_row("A", "premarket", "做多", "tighten", -100.0),
            _row("B", "afterhours", "做多", "SL_TRAIL_LOCK", +50.0),
            _row("C", "regular", "做多", "SL_TRAIL_LOCK", +10.0)]
    txt = render_stage_section(rows)
    assert "延长时段 (盘前+盘后) 单列" in txt and "2 笔" in txt and "-$50.00" in txt


def test_empty_rows_render_placeholder_not_fabrication():
    txt = render_stage_section([])
    assert "无已平仓样本" in txt
    assert "盘前" not in txt and "盘中" not in txt


# ── 3. 报告窗口 (不臆造区间) ────────────────────────────────────────────────

def test_window_of_uses_exit_time():
    assert window_of([{"exit_time": "2026-09-18T19:29:49-04:00"},
                      {"exit_time": "2026-09-16T10:00:00-04:00"}]) == ("2026-09-16", "2026-09-18")
    assert window_of([]) is None
    assert window_of([{"symbol": "X"}]) is None


def test_rows_for_closed_trades_returns_empty_without_window(tmp_path):
    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.execute("create table trade_records (symbol text, exit_time text, entry_time text, "
                 "pnl_net real, realized_pnl real, mfe_pct real, direction text, exit_reason text, "
                 "entry_session text, strategy_id text, source_type text, fees_est real, quantity int)")
    conn.commit()
    conn.close()
    assert rows_for_closed_trades([], str(db)) == []          # 无窗口
    assert rows_for_closed_trades([{"exit_time": "2026-09-18T19:29:49-04:00"}], None) == []
    assert rows_for_closed_trades([{"exit_time": "2026-09-18T19:29:49-04:00"}], str(db)) == []