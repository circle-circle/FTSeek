"""A10 现役时代口径 (2026-10-14 用户裁「后续统计和数据筛选过滤旧数据」) 回归钉子.

钉四件事:
1. SSOT 谓词/常量在 app/core/db_utils (纯 SQL 片段; 边界与 long_expectancy_ledger.ERA_START 一致);
2. 过滤语义: 旧时代行 / 脏 data_quality 行 / POSITION_SYNC 行被剔, 现役 clean/real 保留;
3. 缺列 schema fail-open (返回空串, 不崩报表 — 与 a9_clean_where 同取舍);
4. 消费方接线: entry_window_cohort / shadow_board 走探测函数, rth_fade 大小写不敏感剔外部平仓。

改 CURRENT_ERA_START / 谓词语义 = 重新裁决 (docs/ADJUDICATED-CRITERIA.md A10)。
"""
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from app.core.db_utils import (  # noqa: E402
    CURRENT_ERA_START,
    ERA_CLEAN_PERFORMANCE_WHERE,
    era_clean_where,
)


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT,"
        " direction TEXT, entry_time TEXT, pnl_net REAL, realized_pnl REAL,"
        " data_quality TEXT, source_type TEXT, metadata_json TEXT, exit_reason TEXT)"
    )
    yield conn
    conn.close()


def _row(conn, trade_date, dq="clean", st="PLAYBOOK_SIM", net=10.0):
    conn.execute(
        "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, pnl_net,"
        " realized_pnl, data_quality, source_type) VALUES (?,?,?,?,?,?,?,?)",
        (trade_date, "US.TEST", "LONG", trade_date + "T10:00:00-04:00", net, net + 1.0, dq, st),
    )


def test_ssot_constant_matches_adjudicated_boundary():
    assert CURRENT_ERA_START == "2026-07-01"  # 改边界 = 重裁 (A10)
    # 与 long_expectancy_ledger 既有边界一致 (防两处 SSOT 漂移)
    led = (PROJECT_ROOT / "scripts" / "long_expectancy_ledger.py").read_text(encoding="utf-8")
    assert 'ERA_START = "2026-07-01"' in led
    # 纯 SQL 片段 (可直接拼 WHERE), 无 Python 占位符, 含前导 AND
    assert "{" not in ERA_CLEAN_PERFORMANCE_WHERE
    assert ERA_CLEAN_PERFORMANCE_WHERE.lstrip().startswith("AND")


def test_era_predicate_filters_old_era_and_dirty_rows(db):
    _row(db, "2026-04-24", dq="outlier_external", st="MAIN_PIPELINE", net=17843.0)   # ORCL 人工单原型
    _row(db, "2026-06-02", dq="direction_mismatch", st="MAIN_PIPELINE", net=8544.0)  # XLF 原型
    _row(db, "2026-06-15", dq="real", st="SCALPING", net=500.0)      # 旧时代 dq 干净行也剔 (时代过滤)
    _row(db, "2026-09-10", dq="real", st="POSITION_SYNC", net=-5.0)  # 同步行
    _row(db, "2026-09-10", dq="duplicate_external_close", st="PLAYBOOK_SIM", net=7.0)
    _row(db, "2026-09-10", dq=None, st="PLAYBOOK_SIM", net=3.0)      # NULL dq 按既定清洗口径剔
    _row(db, "2026-09-10", dq="clean", st="PLAYBOOK_SIM", net=42.0)  # 唯一该保留
    rows = db.execute(
        "SELECT pnl_net FROM trade_records WHERE 1=1 " + era_clean_where(db)
    ).fetchall()
    assert [r[0] for r in rows] == [42.0]


def test_era_probe_failopen_on_missing_columns(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, trade_date TEXT)")
    assert era_clean_where(conn) == ""  # 缺 data_quality/source_type 列 → 空串, 不崩报表
    conn.close()


def test_consumers_wired_to_ssot():
    ewc = (PROJECT_ROOT / "scripts" / "entry_window_cohort.py").read_text(encoding="utf-8")
    assert "from app.core.db_utils import" in ewc and "era_clean_where(conn)" in ewc
    sb = (PROJECT_ROOT / "scripts" / "shadow_board.py").read_text(encoding="utf-8")
    assert "era_clean_where(_c)" in sb and "a9_clean_where(_c)" in sb
    rtf = (PROJECT_ROOT / "scripts" / "rth_fade_graduation_check.py").read_text(encoding="utf-8")
    # 大小写修复: 旧过滤形态不得回潮 (SQLite IN 大小写敏感 → 漏剔 'EXTERNAL_CLOSE' 大写行)
    assert "exit_reason NOT IN ('P3_DUP_CLEANUP', 'external_close')" not in rtf
    assert "UPPER(exit_reason) NOT IN ('P3_DUP_CLEANUP', 'EXTERNAL_CLOSE')" in rtf
    assert "era_clean_where" in rtf
