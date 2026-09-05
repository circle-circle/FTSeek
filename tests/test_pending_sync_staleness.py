"""pending_sync 超时判据的时区同尺契约 (2026-09-05, DEB-20260905 M-6/B2, REPORT D-06/G-12).

被测: app/storage/trade_store.py `TradeStore.get_pending_sync_positions` + `_parse_sync_ts`。

病灶 (机制成立): 原判据把 cutoff 交给 SQLite —
`datetime(last_sync_at) < datetime('now','-300 seconds')`, 而 `last_sync_at` 由
mark_sync_pending / mark_sync_complete / mark_anomalous 用 naive `datetime.now().isoformat()`
写 = **服务器本地(CST)墙钟**, SQLite `datetime('now')` = **UTC**。CST = UTC+8 ⇒ 库内时刻被
当成"8 小时之后", 判据在 8h 内**恒 False** — 漏报方向: 真卡住的同步不告警, 感知窗口从
5min 膨胀到 8h+。

**影响面更正 (本批复算, 与 REPORT D-06 不同)**: 该函数**全仓零消费方**, D-06 声称的调用链
`scheduler_core._position_sync_loop → get_pending_sync_positions(300)` 中 `_position_sync_loop`
在仓内**不存在** ⇒ 现实不是"告警延迟 8h", 而是**这条告警从未发出过** (又一条纸面防线;
`sync_status` 字段本身是活的, 见 position_sync 的 needs_data_fix 判定)。故本批只修判据、
不接线 (接线 = 新增告警环路, 须入 CONTROL-LOOP-LEDGER 并另行裁决)。

现语义 (同尺原则): 带 tz 的值按其偏移换算; naive 值一律按 CST 解释 = 与写入方同一把尺子;
NULL/脏格式解析不出 → **保守计入告警** (fail-loud, 宁多告不静默)。
写入方迁到 now_et() 属 M-6/B1, 本判据对两种格式都已兼容 → 迁移时无需再改此处。

回滚: 还原本函数为 SQL 侧 cutoff (`git checkout <本提交>^ -- app/storage/trade_store.py`)
+ 删本文件; 因零消费方, 回滚**零运行时行为影响**。
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.storage.trade_store import TradeStore, _parse_sync_ts
from app.utils.timezone import now_et

MAX_AGE = 300


@pytest.fixture
def store(tmp_path):
    s = TradeStore(str(tmp_path / "t.db"))
    if not getattr(s, "_initialized", False):
        s._init_db()
    return s


def _put(store, symbol, sync_status="pending_sync", last_sync_at=None):
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO open_positions "
        "(symbol, direction, size, avg_entry_price, sync_status, last_sync_at, updated_at) "
        "VALUES (?, 'LONG', 10, 100.0, ?, ?, ?)",
        (symbol, sync_status, last_sync_at, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def _naive_cst(delta_seconds: float) -> str:
    """现役写入方的格式: naive 服务器本地(CST)墙钟 isoformat."""
    return (datetime.now() - timedelta(seconds=delta_seconds)).isoformat()


# ── 1. 核心回归: CST 墙钟 10 分钟前的 pending_sync 必须被判超时 ──
def test_stale_naive_cst_row_is_reported(store):
    _put(store, "US.UBER", last_sync_at=_naive_cst(600))
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.UBER"]


def test_old_sql_predicate_was_blind_for_8h(store):
    """证伪: 同一行数据跑**旧判据**返回空 — 证明病灶真实且本修复改变了行为."""
    _put(store, "US.RIVN", last_sync_at=_naive_cst(600))
    conn = sqlite3.connect(store.db_path)
    old = conn.execute("""
        SELECT symbol FROM open_positions
        WHERE sync_status = 'pending_sync'
        AND datetime(last_sync_at) < datetime('now', '-300 seconds')
    """).fetchall()
    conn.close()
    assert old == [], "旧 SQL 判据在 8h 内恒 False (CST 墙钟被当成未来时刻) — 漏报实证"
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.RIVN"]


def test_fresh_row_not_reported(store):
    _put(store, "US.HOOD", last_sync_at=_naive_cst(30))
    assert store.get_pending_sync_positions(MAX_AGE) == []


def test_window_boundary(store):
    _put(store, "US.IN", last_sync_at=_naive_cst(MAX_AGE - 100))    # 窗内
    _put(store, "US.OUT", last_sync_at=_naive_cst(MAX_AGE + 100))   # 窗外
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.OUT"]


# ── 2. 格式兼容: B1 把写入方迁到 now_et()/UTC 后本判据无需再改 ──
def test_aware_et_value_is_same_scale(store):
    old_et = (now_et() - timedelta(seconds=600)).isoformat()
    _put(store, "US.ET", last_sync_at=old_et)
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.ET"]

    fresh_et = now_et().isoformat()
    _put(store, "US.ET", last_sync_at=fresh_et)
    assert store.get_pending_sync_positions(MAX_AGE) == []


def test_aware_utc_value_is_same_scale(store):
    old_utc = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    _put(store, "US.UTC", last_sync_at=old_utc)
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.UTC"]


def test_mixed_formats_in_one_sweep(store):
    """同一轮里 naive-CST 与 aware-ET 混存 (迁移期必然形态) 都按各自尺子判对."""
    _put(store, "US.NAIVE", last_sync_at=_naive_cst(600))
    _put(store, "US.AWARE", last_sync_at=(now_et() - timedelta(seconds=600)).isoformat())
    _put(store, "US.FRESH", last_sync_at=_naive_cst(10))
    assert sorted(store.get_pending_sync_positions(MAX_AGE)) == ["US.AWARE", "US.NAIVE"]


# ── 3. fail-loud: 解析不出必须计入告警, 不得静默 ──
def test_null_last_sync_at_fails_loud(store, caplog):
    _put(store, "US.NULL", last_sync_at=None)
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.NULL"]
    assert "从未写过 last_sync_at" in caplog.text


def test_garbage_last_sync_at_fails_loud(store, caplog):
    _put(store, "US.BAD", last_sync_at="not-a-timestamp")
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.BAD"]
    assert "不可解析" in caplog.text


def test_empty_string_last_sync_at_fails_loud(store):
    _put(store, "US.EMPTY", last_sync_at="   ")
    assert store.get_pending_sync_positions(MAX_AGE) == ["US.EMPTY"]


# ── 4. 作用域与入参守卫 ──
def test_non_pending_rows_never_reported(store):
    """sync_status != pending_sync 的行即使时间戳古老也不告警 (判据只管待同步行)."""
    _put(store, "US.SYNCED", sync_status="synced", last_sync_at=_naive_cst(99999))
    _put(store, "US.ERR", sync_status="error", last_sync_at=_naive_cst(99999))
    assert store.get_pending_sync_positions(MAX_AGE) == []


def test_max_age_zero_reports_any_past_row(store):
    _put(store, "US.Z", last_sync_at=_naive_cst(1))
    assert store.get_pending_sync_positions(0) == ["US.Z"]


def test_negative_max_age_is_clamped_not_crash(store):
    _put(store, "US.NEG", last_sync_at=_naive_cst(1))
    assert store.get_pending_sync_positions(-5) == ["US.NEG"], "负值应夹到 0 而非炸/漏"


# ── 5. 解析器单元 ──
def test_parse_sync_ts_units():
    assert _parse_sync_ts(None) is None
    assert _parse_sync_ts("") is None
    assert _parse_sync_ts("garbage") is None
    naive = _parse_sync_ts("2026-09-05T10:00:00")
    assert naive is not None and naive.utcoffset() == timedelta(hours=8), \
        "naive 必须按 CST(+08:00) 解释 = 与写入方同尺"
    aware = _parse_sync_ts("2026-09-05T10:00:00-04:00")
    assert aware is not None and aware.utcoffset() == timedelta(hours=-4)
    # 同一瞬间的两种写法必须判等 — 跨尺比较能力正是迁移期 naive/aware 混存的基础
    assert naive == _parse_sync_ts("2026-09-04T22:00:00-04:00")
    assert _parse_sync_ts("2026-09-05T10:00:00Z").utcoffset() == timedelta(0)
