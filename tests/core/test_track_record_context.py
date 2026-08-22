"""_build_track_record_context 回归 — DEB-20260822 §十 个体级自战绩注入。

盘感载体: 聚合桶统计之外, 给模型看"最近N次同类判断+各自结果"的可对齐经验。
数据源 playbook_instructions.fwd_return_t{1,5} (方向带符号, 正=判断赚钱,
ic_tracker.py:289 sign 约定)。本测试用临时 sqlite 钉死渲染契约。
回滚: 删本文件即可。
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.pipeline.llm_pipeline import _build_track_record_context


def _make_db(tmp_path: Path, rows: list) -> Path:
    p = tmp_path / "sq_test.db"
    conn = sqlite3.connect(p)
    conn.execute("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT,
            confidence REAL, fwd_return_t1 REAL, fwd_return_t5 REAL)""")
    conn.executemany(
        "INSERT INTO playbook_instructions (date, symbol, direction, confidence,"
        " fwd_return_t1, fwd_return_t5) VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return p


# ── 渲染契约 ─────────────────────────────────────────────────────────────

def test_renders_rows_newest_first_with_marks(tmp_path):
    db = _make_db(tmp_path, [
        ("2026-08-21", "US.MU", "BUY", 70, -0.004, -0.04),
        ("2026-08-20", "US.CVX", "BUY", 50, 0.023, 0.036),
    ])
    out = _build_track_record_context(db_path=db)
    assert "个体级" in out and "逐条对齐" in out
    i_mu, i_cvx = out.index("US.MU"), out.index("US.CVX")
    assert i_mu < i_cvx                      # 新→旧
    assert "-4.0%" in out and "✗" in out and "✓" in out
    assert "T+5 命中 1/2" in out             # 小结统计


def test_short_signed_convention_positive_signed_value_is_win(tmp_path):
    # 方向带符号约定 (ic_tracker.py:289): sign=-1 × 价差 → 做空赚钱时 fwd 为正
    # 例: 做空后价格跌 5% → fwd_return_t5 = -1 × (-0.05) = +0.05 → ✓
    db = _make_db(tmp_path, [
        ("2026-08-14", "US.JD", "SELL_SHORT", 70, None, 0.05),
    ])
    out = _build_track_record_context(db_path=db)
    assert "✓" in out and "T+5 命中 1/1" in out and "SELL_SHORT 1/1" in out


def test_null_t1_renders_dash_not_crash(tmp_path):
    db = _make_db(tmp_path, [("2026-08-13", "US.NVDA", "BUY", 35, None, 0.02)])
    out = _build_track_record_context(db_path=db)
    assert "T+1" in out and "—" in out       # 缺 T+1 显示占位不崩
    assert "T+5 命中 1/1" in out


# ── 缺数据不编造 ─────────────────────────────────────────────────────────

def test_missing_db_reports_unavailable():
    out = _build_track_record_context(db_path="/tmp/no_such_db_xyz.db")
    assert "不可用" in out and "MU" not in out


def test_no_rows_reports_unavailable(tmp_path):
    db = _make_db(tmp_path, [])
    out = _build_track_record_context(db_path=db)
    assert "不可用" in out


def test_missing_table_reports_unavailable(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()               # 无 playbook_instructions 表
    out = _build_track_record_context(db_path=p)
    assert "不可用" in out


def test_real_db_smoke_has_summary_or_unavailable():
    """活体冒烟: 对仓库真库渲染 — 要么有小结要么明确不可用, 二者必居其一。"""
    real = PROJECT_ROOT / "data" / "ftseek_us.db"
    if not real.exists():
        return
    out = _build_track_record_context()
    assert ("小结:" in out) or ("不可用" in out)
