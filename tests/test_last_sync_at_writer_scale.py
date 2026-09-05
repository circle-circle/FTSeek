r"""open_positions.last_sync_at 写方尺度契约 (2026-09-05, DEB-20260905 M-6/B1 第三阶).

被测: `app/storage/trade_store.py` 里写 `last_sync_at` 的**全部 6 个**写方 —
`upsert_open_position` (最热路径, 16 个调用点) / `mark_sync_pending` / `mark_sync_complete` /
`mark_anomalous` / `unmark_anomalous` / `update_position_field`。

病灶: 6 处原先都写 naive `datetime.now().isoformat()` = 服务器本地 (CST) 墙钟, 而交易口径是 ET。
读方 `get_pending_sync_positions` 曾把这个 CST 墙钟交给 SQLite `datetime('now')`(= UTC) 比较
⇒ 判据 8h 内恒 False (REPORT D-06, 已在 B2/第二阶修成 Python 侧同尺比较)。本阶把**写方**也统一到
`now_et()`, 使该列读写同尺闭环。

盘点更正 (复算义务): 第一阶记的"写方 3 处"是**漏盘**。除 3 个 `mark_*` 外还有
`upsert_open_position` / `unmark_anomalous` / `update_position_field` — 其中 `upsert_open_position`
是最热路径, **只翻 3 处会让该列永久混存**(而非过渡期混存), 把 ISO 字典序≠时间序的错序风险固化进稳态。

该列可一次翻完的实证 (全部消费方审计):
  - 全仓**零** `ORDER BY last_sync_at`、零 `substr(last_sync_at,…)`、零字符串比较;
  - 唯一语义读方 `_parse_sync_ts` 已双格式兼容 (naive→CST 兜底 / aware→按偏移);
  - 唯一展示读方 `app/pipeline/position_monitor.py:458` 把它塞进 `metadata["_close_blocked_at"]`,
    而该 key **全仓零读取点** (3 处写 + `order_executor.py:581` 一处 pop) ⇒ 纯遥测;
  - `web_dashboard` 两条 INSERT (`order/executor.py:42` SQL_TAKEOVER、`order/audit.py:473` stub)
    的列清单里**都不含** `last_sync_at` ⇒ 6 处即完整写方集合;
  - 现库 3 行 (UBER/RIVN/HOOD) 该列**全为 NULL** (活行由 `upsert_position_simple` 写, 其列清单
    不含本列) ⇒ 无历史 naive 行需归一, **本阶零数据迁移**。

刻意**不动**的相邻尺度债 (B1 **第五阶**, 须单独 DEB; 原第三阶稿件误称"第四阶", 而第四阶已用于
别名裸 now 清剿 ⇒ 2026-09-05 第四阶批内统一改名, 见 DEB §12.6): 同一条 INSERT 里的 `updated_at`
仍写 naive —
它有真消费方做字符串排序/比较 (`position_monitor.py:1229` `ORDER BY updated_at DESC LIMIT 1`、
`position_monitor.py:1164` `updated_at>=?`), 且**已经在混存** (`upsert_open_position` naive vs
`upsert_position_simple` 的 `now_et()`) ⇒ 归一方案必须先于翻转。见
`test_updated_at_adjacent_scale_debt_is_documented`。

回滚: `git revert <本阶提交>` + 删本文件 ⇒ 6 处回到 naive CST。因该列现库全 NULL 且唯一语义读方
`_parse_sync_ts` 对 naive 仍按 CST 正确解释, 回滚**零运行时行为影响**、无需改库。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.storage.trade_store import TradeStore, _parse_sync_ts
from app.utils.timezone import now_et


@pytest.fixture
def store(tmp_path):
    s = TradeStore(str(tmp_path / "t.db"))
    if not getattr(s, "_initialized", False):
        s._init_db()
    return s


def _raw(store, symbol):
    """直读库里的 last_sync_at 原文 (不经任何解析) — 契约测的是**写进去的字符串**."""
    conn = sqlite3.connect(store.db_path)
    row = conn.execute(
        "SELECT last_sync_at FROM open_positions WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    return row[0] if row else None


def _assert_aware_et(raw, who):
    assert raw, f"{who}: last_sync_at 未写入 (NULL)"
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None, (
        f"{who}: last_sync_at 仍是 **naive** (无 tz 偏移) = 写方回潮: {raw!r}。"
        f"naive 落库会被读成服务器本地 CST 墙钟, 与 ET/UTC 口径比较即 8~12h 错位 (D-06 病灶)")
    assert dt.utcoffset() == now_et().utcoffset(), (
        f"{who}: 偏移 {dt.utcoffset()} ≠ 当前 ET 偏移 {now_et().utcoffset()} — 写的是别的尺: {raw!r}"
        f" (注意不得硬编码 -04:00: 冬令时是 -05:00)")
    assert abs((dt - now_et()).total_seconds()) < 120, (
        f"{who}: 写入时刻与 now_et() 相差 {(dt - now_et()).total_seconds():.0f}s, 不像'刚刚': {raw!r}")


def _seed(store, symbol="US.UBER", sync_status="synced", last_sync_at=None):
    """种一行持仓 (绕开写方, 用于测单个写方的效果)."""
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO open_positions "
        "(symbol, direction, size, avg_entry_price, sync_status, last_sync_at, updated_at) "
        "VALUES (?, 'LONG', 10, 100.0, ?, ?, ?)",
        (symbol, sync_status, last_sync_at, now_et().isoformat()))
    conn.commit()
    conn.close()


# ── 1~5. 五个只吃 symbol 的写方: 逐个验尺度 ────────────────────────────────
def test_mark_sync_pending_writes_et(store):
    _seed(store, "US.UBER")
    store.mark_sync_pending("US.UBER", trace_id="t-1")
    _assert_aware_et(_raw(store, "US.UBER"), "mark_sync_pending")


def test_mark_sync_complete_writes_et(store):
    _seed(store, "US.RIVN", sync_status="pending_sync")
    store.mark_sync_complete("US.RIVN")
    _assert_aware_et(_raw(store, "US.RIVN"), "mark_sync_complete")


def test_mark_anomalous_writes_et(store):
    _seed(store, "US.HOOD")
    store.mark_anomalous("US.HOOD", reason="test")
    _assert_aware_et(_raw(store, "US.HOOD"), "mark_anomalous")


def test_unmark_anomalous_writes_et(store):
    _seed(store, "US.HOOD")
    store.mark_anomalous("US.HOOD", reason="test")     # 先置 is_anomalous=1
    assert store.unmark_anomalous("US.HOOD", reason="recovered") is True
    _assert_aware_et(_raw(store, "US.HOOD"), "unmark_anomalous")


def test_update_position_field_writes_et(store):
    _seed(store, "US.UBER")
    assert store.update_position_field("US.UBER", "current_sl", 95.0, action="test_sl") is True
    _assert_aware_et(_raw(store, "US.UBER"), "update_position_field")


# ── 6. 最热路径: upsert_open_position (第一阶漏盘的那一处) ──────────────────
def _mk_position(symbol="US.AAPL"):
    from app.core.models import Position, Direction, MarketRegime
    pos = Position(symbol=symbol, direction=Direction.LONG, size=10.0, avg_entry_price=150.0,
                   initial_sl=145.0, current_sl=145.0, initial_tp=160.0,
                   market_regime=MarketRegime.RANGE)
    pos.owner_strategy = "playbook_sim"
    return pos


def test_upsert_open_position_writes_et(store):
    store.upsert_open_position(_mk_position(), source="scheduler", action="open")
    _assert_aware_et(_raw(store, "US.AAPL"), "upsert_open_position")


def test_upsert_open_position_overwrites_legacy_naive_value(store):
    """闭环的关键: 最热路径必须能**覆盖掉**历史 naive 值, 否则混存永不收敛."""
    _seed(store, "US.AAPL", last_sync_at=(datetime.now() - timedelta(hours=9)).isoformat())
    legacy = _raw(store, "US.AAPL")
    assert datetime.fromisoformat(legacy).tzinfo is None, "前置条件: 种的是 naive 历史值"
    store.upsert_open_position(_mk_position(), source="scheduler", action="open")
    _assert_aware_et(_raw(store, "US.AAPL"), "upsert_open_position(覆盖 naive)")


# ── 7. 读写同尺闭环: 写方产物必须被读方判对 ────────────────────────────────
def test_roundtrip_writer_to_staleness_reader(store):
    """写方 → 读方全链路: 刚写完不告警, 老化后必告警 (D-06 的 8h 盲区已闭合)."""
    _seed(store, "US.UBER")
    store.mark_sync_pending("US.UBER", trace_id="t-round")
    assert _parse_sync_ts(_raw(store, "US.UBER")) is not None, "读方必须能解析写方产物"
    assert store.get_pending_sync_positions(max_age_seconds=300) == [], \
        "刚标记的 pending_sync 不应立刻判超时"
    assert store.get_pending_sync_positions(max_age_seconds=0) == ["US.UBER"], \
        "max_age=0 时同一行必须判超时 (同尺比较生效)"


def test_mixed_legacy_and_new_rows_judged_together(store):
    """混存窗口 (回滚期 / 手工改库 / 别名兜底) 下, 一轮调用必须把两种格式都判对."""
    _seed(store, "US.LEGACY", sync_status="pending_sync",
          last_sync_at=(datetime.now() - timedelta(minutes=10)).isoformat())   # naive CST, 10min 前
    _seed(store, "US.NEW", sync_status="pending_sync")
    store.mark_sync_pending("US.NEW", trace_id="t-new")                        # aware ET, 刚刚
    stale = store.get_pending_sync_positions(max_age_seconds=300)
    assert "US.LEGACY" in stale, "naive 历史行 (10min 前) 必须判超时 — 旧 SQL 判据在此恒 False"
    assert "US.NEW" not in stale, "aware 新行 (刚刚) 不应判超时"


# ── 8. 全列闭合: 6 个写方跑一遍后, 该列不得残留任何 naive 值 ────────────────
def test_all_six_writers_leave_no_naive_value_in_column(store):
    _seed(store, "US.A", last_sync_at=datetime.now().isoformat())   # 先污染成 naive
    _seed(store, "US.B", last_sync_at=datetime.now().isoformat())
    store.mark_sync_pending("US.A", trace_id="t")
    store.mark_sync_complete("US.A")
    store.mark_anomalous("US.B", reason="t")
    store.unmark_anomalous("US.B", reason="t")
    store.update_position_field("US.B", "current_sl", 95.0, action="t")
    store.upsert_open_position(_mk_position("US.C"), source="scheduler", action="open")
    conn = sqlite3.connect(store.db_path)
    rows = conn.execute("SELECT symbol, last_sync_at FROM open_positions").fetchall()
    conn.close()
    naive = [(s, v) for s, v in rows
             if v and datetime.fromisoformat(v).tzinfo is None]
    assert not naive, f"该列仍残留 naive 值 (写方未闭合): {naive}"
    assert len(rows) == 3, f"应有 3 行, 实测 {len(rows)}"


# ── 9. 相邻尺度债的显式留痕 (第五阶, 本阶刻意不动) ──────────────────────────
def test_updated_at_adjacent_scale_debt_is_documented(store):
    """同一条 INSERT 里 last_sync_at=ET 而 updated_at=naive — 这是**已知债**, 不是疏漏.

    钉住现状的目的: 第五阶落地时本测试会变红, 逼迫执行者去改测试并出归一方案,
    而不是让"两列不同尺"这件事被遗忘。updated_at 有 `ORDER BY updated_at DESC` 与
    `updated_at>=?` 的字符串消费方 (position_monitor:1229 / :1164), 且已在混存 ⇒ 不可顺手翻。
    """
    store.upsert_open_position(_mk_position("US.DEBT"), source="scheduler", action="open")
    conn = sqlite3.connect(store.db_path)
    row = conn.execute(
        "SELECT last_sync_at, updated_at, entry_time FROM open_positions WHERE symbol='US.DEBT'"
    ).fetchone()
    conn.close()
    last_sync_at, updated_at, entry_time = row
    assert datetime.fromisoformat(last_sync_at).tzinfo is not None, "last_sync_at 应为 ET (本阶已翻)"
    assert datetime.fromisoformat(updated_at).tzinfo is None, (
        f"updated_at 已变成带尺 ({updated_at!r}) ⇒ 说明有人在本阶之外翻了第五阶的账本列。"
        f"请先确认 `ORDER BY updated_at DESC` / `updated_at>=?` 两个字符串消费方已改成解析后比较, "
        f"并更新本测试与 DEB 第五阶条目 (§11.5 / §11.7)")
    assert datetime.fromisoformat(entry_time).tzinfo is not None, \
        "entry_time 应仍为 ET (R-12 已统一, 本阶不动)"
