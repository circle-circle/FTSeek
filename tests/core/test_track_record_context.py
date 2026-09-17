"""_build_track_record_context 回归 — DEB-20260822 §十 个体级自战绩注入。

盘感载体: 聚合桶统计之外, 给模型看"最近N次同类判断+各自结果"的可对齐经验。
数据源 playbook_instructions.fwd_return_t{1,5} (方向带符号, 正=判断赚钱,
ic_tracker.py:289 sign 约定)。本测试用临时 sqlite 钉死渲染契约。
回滚: 删本文件即可。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 2026-09-14 修: 本文件在 tests/core/ 下, parent.parent 只到 tests/ ⇒ 活体冒烟用例
# 拼出的 "tests/data/ftseek_us.db" 恒不存在, `if not real.exists(): return` 让它
# **从未真跑过一行断言** (假绿, 与本仓"只能报健康的守卫"同族)。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import app.pipeline.llm_pipeline as llm_pipeline
from app.pipeline.llm_pipeline import _build_track_record_context


# ── 冻结时钟: 根治夹具日期定时炸弹 (2026-09-14) ────────────────────────────
# 病灶: 夹具日期是硬编码常量 (08-13/08-14/08-20/08-21), 而
#   _build_track_record_context 的窗口是 `datetime.now()-30d` 的**滑动窗**
#   (llm_pipeline.py:827)。2026-09-13 起 cutoff=08-14 ⇒ 08-13/08-14 两行整体出窗,
#   3 例恒红 (handoff ⚠3 已定性"不是回归, 是时钟"); 08-20/08-21 两例会在 09-20 接力变红。
#   红着的守卫 = 没有守卫, 且会把真回归淹在噪声里。
# 修法与同族先例一致 (tests/test_stale_bridge_guard.py 的 _run_invariant(frozen_now=)、
#   tests/test_invariant_i10_exit_time.py 的 _FROZEN_NOW): 把 now() 钉在夹具窗口内,
#   判据只由夹具数据决定, 不由翻日历决定。
# 刻意不冻结的: 活体冒烟 —— 它要验的正是"今天这张真库能不能渲染"。
_FROZEN_NOW = datetime(2026, 8, 22, 12, 0)
_OLDEST_FIXTURE_DATE = "2026-08-13"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(_FROZEN_NOW.year, _FROZEN_NOW.month, _FROZEN_NOW.day,
                   _FROZEN_NOW.hour, _FROZEN_NOW.minute)


@pytest.fixture(autouse=True)
def _freeze_clock(request, monkeypatch):
    if request.node.name.startswith("test_real_db_smoke"):
        return
    monkeypatch.setattr(llm_pipeline, "datetime", _FrozenDatetime)
    # 防空转钉 (放 harness 不放用例, 新增用例想忘也忘不了): 冻结日必须真把最老夹具日
    # 留在窗内, 否则用例会退化成"根本没扫到行"的假绿。
    cutoff = (_FrozenDatetime.now() - timedelta(days=30)).date().isoformat()
    assert cutoff <= _OLDEST_FIXTURE_DATE, (
        f"冻结日 {_FROZEN_NOW:%Y-%m-%d} 使 cutoff={cutoff} 晚于最老夹具日 "
        f"{_OLDEST_FIXTURE_DATE} ⇒ 夹具行整体出窗, 用例空转")


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
    assert "✓" in out and "T+5 命中 1/1" in out and "SELL_SHORT T+5 1/1" in out


def test_null_t1_renders_dash_not_crash(tmp_path):
    db = _make_db(tmp_path, [("2026-08-13", "US.NVDA", "BUY", 35, None, 0.02)])
    out = _build_track_record_context(db_path=db)
    assert "T+1" in out and "—" in out       # 缺 T+1 显示占位不崩
    assert "T+5 命中 1/1" in out


def test_t5_pending_row_visible_but_excluded_from_t5_stats(tmp_path):
    """2026-08-22b 新鲜度修正: 双段渲染 — 已结算段保住 T+5 判决记录,
    待定段让近一周(T+5未到期, 实测105笔曾被静默丢掉)的判断可见。"""
    db = _make_db(tmp_path, [
        ("2026-08-21", "US.AMD", "BUY", 60, 0.010, None),   # 仅 T+1 已回填 → 待定段
        ("2026-08-14", "US.CVX", "BUY", 50, 0.023, 0.036),  # 已结算段
    ])
    out = _build_track_record_context(db_path=db)
    i_amd = out.index("US.AMD")
    line_amd = out[out.rfind("\n", 0, i_amd) + 1: out.index("\n", i_amd)]
    assert "待定" in line_amd                 # 行内可见但明确标注未结算
    assert "未结算" in out                    # 分段标题
    assert "T+5 命中 1/1" in out              # 小结只统计已结算子集
    assert "独立试验数" in out                # 独立性脚注
    # T+1 统计覆盖两段 (待定行的 T+1 计入)
    assert "T+1 命中 2/2" in out


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
