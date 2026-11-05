"""阶段统计 SSOT + 延长时段阶段日报 契约测试 (2026-09-18 用户裁「日报/周报加阶段维度 + 独立阶段日报」)。

覆盖:
  1. 阶段归一: 落库 entry_session 优先 → ET 时钟回退 → 空串(未标注, 不臆造)
  2. 阶段聚合: 净额/胜率/MFE=0; 阶段之间不互相污染
  3. 渲染: 含 阶段×方向×出场原因 三维 + 延长时段单列; 样本 <30 标注"仅方向性"
  4. 空区间/无窗口 → 空串 (报告不臆造区间)
  5. 延长时段日报: 无样本不推送 (零噪音) —— 由 has_sample 语义保证
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.review.stage_stats import (  # noqa: E402
    MIN_N_FOR_READING, load_trades, normalize_stage, render_stage_section, rows_by_exit_stage,
    stage_breakdown, window_of, rows_for_closed_trades,
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


def test_naive_clock_fallback_uses_cst():
    assert normalize_stage("", "2026-09-18T23:30:00") == "regular"
    assert normalize_stage("", "2026-09-18T18:30:00") == "premarket"
    assert normalize_stage("", "2026-09-18T09:00:00") == ""


def test_offhours_never_invented():
    assert normalize_stage("", "2026-09-18T22:30:00-04:00") == ""      # 盘外
    assert normalize_stage("", None) == ""
    assert normalize_stage("garbage", "2026-09-18T22:30:00-04:00") == ""


def test_load_trades_uses_et_exit_date_and_excludes_duplicate():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      create table trade_records (
        id integer primary key, symbol text, direction text, entry_time text,
        exit_time text, exit_reason text, entry_session text, strategy_id text,
        source_type text, realized_pnl real, pnl_net real, fees_est real,
        mfe_pct real, mae_pct real, quantity integer, data_quality text
      );
    """)
    rows = [
      ("US.B", "LONG", "2026-09-18T23:20:00", "2026-09-18T23:30:00", None),
      ("US.C", "LONG", "2026-09-18T14:00:00+08:00", "2026-09-19T15:00:00+08:00", None),
      ("US.A", "LONG", "2026-09-18T18:30:00-04:00", "2026-09-18T18:40:00-04:00", None),
      ("US.D", "LONG", "2026-09-18T18:35:00-04:00", "2026-09-18T18:41:00-04:00", "duplicate_external_close"),
    ]
    conn.executemany(
      "insert into trade_records(symbol,direction,entry_time,exit_time,exit_reason,"
      "entry_session,strategy_id,source_type,realized_pnl,pnl_net,fees_est,mfe_pct,"
      "mae_pct,quantity,data_quality) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      [(s, d, e, x, "TEST", "", "test", "TEST", 10.0, 10.0, 0.0, 1.0, 0.0, 1, q)
       for s, d, e, x, q in rows],
    )
    got = load_trades(conn, "2026-09-18", "2026-09-18")
    assert [r["symbol"] for r in got] == ["US.A", "US.B"]
    assert got[0]["stage"] == "afterhours"
    assert got[1]["stage"] == "regular"


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
    assert MIN_N_FOR_READING == 30


def test_small_sample_boundary_is_thirty():
    rows29 = [_row(f"A{index}", "premarket", "做多", "test", 1.0) for index in range(29)]
    rows30 = rows29 + [_row("A29", "premarket", "做多", "test", 1.0)]
    assert "样本少" in render_stage_section(rows29)
    assert "样本少" not in render_stage_section(rows30)


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


# ── 4. 出场时段对照臂 (2026-10-25 周日复盘: 入场口径把盘前兑现的利润记进盘中) ──────

def test_rows_by_exit_stage_only_relabels_stage():
    """出场口径只换 stage 标签: 净额/退出原因/方向一律不动 ⇒ 两臂必然对账。"""
    rows = [{"symbol": "US.TSLA", "stage": "regular", "dir_cn": "做空",
             "exit_reason": "SL_TRAIL_LOCK", "net": 236.81, "gross": 241.0,
             "mfe_pct": 6.35, "direction": "SHORT",
             "entry_time": "2026-10-21T16:45:23-04:00",
             "exit_time": "2026-10-22T06:12:46-04:00"}]
    out = rows_by_exit_stage(rows)
    assert out[0]["stage"] == "premarket"                 # 06:12 ET 出场 = 盘前
    assert out[0]["net"] == 236.81
    assert out[0]["exit_reason"] == "SL_TRAIL_LOCK"
    assert rows[0]["stage"] == "regular"                   # 原行不被就地改写


def test_rows_by_exit_stage_offhours_is_unlabeled():
    """盘外 20:00–03:59 ET 归未标注 (不臆造阶段), 与入场口径同律。"""
    out = rows_by_exit_stage([{"stage": "regular", "net": 1.0,
                               "exit_time": "2026-10-22T01:30:00-04:00"}])
    assert out[0]["stage"] == ""


def test_render_stage_section_caliber_override():
    """出场臂必须能覆盖口径行, 否则会把 exit 口径印成 entry 口径 (误导读者)。"""
    rows = [_row("US.X", "premarket", "做多", "SL_BREAKEVEN", 10.0)]
    txt = render_stage_section(rows, "出场臂", caliber="阶段=按 exit_time 的 ET 时钟判定")
    assert "按 exit_time 的 ET 时钟判定" in txt
    assert "entry_session" not in txt


def test_two_arms_reconcile_on_same_rows():
    """同批成交两臂合计必须相等 (入场 vs 出场只换维度, 不改账)。"""
    rows = [_row("US.A", "regular", "做多", "SL_TRAIL_LOCK", 100.0),
            _row("US.B", "regular", "做空", "SL_INITIAL", -50.0)]
    entry_bd = stage_breakdown(rows)
    exit_bd = stage_breakdown(rows_by_exit_stage(rows))
    assert sum(b["n"] for b in entry_bd.values()) == sum(b["n"] for b in exit_bd.values()) == 2
    assert round(sum(b["net"] for b in entry_bd.values()), 2) == \
           round(sum(b["net"] for b in exit_bd.values()), 2) == 50.0
