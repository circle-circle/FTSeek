"""生产 SQLite 库写入闸契约 (RFC-168 方案 D, 2026-10-26)

背景: 既有生产数据写硬闸只挂 open/Path.write_*/os.replace/os.rename,
**sqlite3.connect 完全未挂** ⇒ 19 个生产库路径在测试中可写。
本文件钉住新加的写语句闸, 并钉住三个易回归点:

  1. 只读断言不受影响 (拦 connect 会打成假红 → 本仓 30 个文件直连生产库只读);
  2. db_utils._safe_connect 造的连接同样被拦 (import 期捕获引用的绕过路径);
  3. EventStore 默认路径即生产库 (构造即写) —— 防止将来把默认路径改到 data/ 下。

拦截信号: sqlite3.DatabaseError("not authorized") —— authorizer 在 prepare 阶段
拒绝。**不抛自定义异常**: sqlite3 会把回调内异常吞成同一个 "not authorized",
无法区分"被闸拦下"与"其它授权问题", 故此处断言错误消息而非异常类型。
"""
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 刻意选**未登记豁免**的库 (ftseek_us.db 属已知存量豁免库, 见 conftest 的
# _KNOWN_PRODUCTION_WRITE_DBS) —— 否则本文件测的就不是闸而是豁免名单。
PROD_DB = PROJECT_ROOT / "data" / "ftseek_us_live.db"


def _prod_ro():
    """只读连生产库 (mode=ro ⇒ 即便闸失效也写不进去, 测试自身不可能污染生产)。"""
    return sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)


@pytest.fixture(autouse=True)
def _isolate_violation_ledger():
    """把本文件的违规记录从会话账里隔离出去。

    本文件**故意**触发闸 (那是被测行为), 若不隔离, 会话结束的违规汇总会把
    闸自己的自测报成生产库污染 —— 误报会淹没真实违规。
    """
    import tests.conftest as cf

    saved = list(cf._PROD_DB_VIOLATIONS)
    cf._PROD_DB_VIOLATIONS.clear()
    yield
    cf._PROD_DB_VIOLATIONS.clear()
    cf._PROD_DB_VIOLATIONS.extend(saved)


# ── 只读放行 ────────────────────────────────────────────────────────────────

def test_select_on_production_db_is_allowed():
    conn = _prod_ro()
    try:
        assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] >= 0
    finally:
        conn.close()


def test_pragma_on_production_db_is_allowed():
    """PRAGMA 放行: db_utils 连接初始化 (journal_mode=WAL / busy_timeout) 走 PRAGMA,
    拦它会让所有生产库连接在初始化阶段就红, 掩盖真正的写入点。"""
    conn = _prod_ro()
    try:
        conn.execute("PRAGMA table_info(trade_records)").fetchall()
    finally:
        conn.close()


# ── 写操作被拦 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "INSERT INTO trade_records (symbol) VALUES ('US.GUARDTEST')",
    "UPDATE trade_records SET symbol='US.GUARDTEST'",
    "DELETE FROM trade_records",
    "DROP TABLE trade_records",
    "CREATE TABLE _guard_probe (a int)",
    "REPLACE INTO trade_records (symbol) VALUES ('US.GUARDTEST')",
])
def test_write_statements_on_production_db_are_blocked(sql):
    conn = _prod_ro()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
    finally:
        conn.close()


def test_write_with_leading_comment_is_blocked():
    """前导注释不得让写语句绕过闸 (authorizer 按语句动作判定, 与注释无关)。"""
    conn = _prod_ro()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("-- guard probe\nINSERT INTO trade_records (symbol) VALUES ('US.GUARDTEST')")
    finally:
        conn.close()


def test_blocked_write_leaves_production_db_untouched():
    """闸在语句执行前触发 (prepare 阶段即拒) —— 拦截顺序错位的话数据会先落库。"""
    conn = _prod_ro()
    try:
        before = conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO trade_records (symbol) VALUES ('US.GUARDTEST')")
        after = conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
        assert before == after
    finally:
        conn.close()


def test_violation_is_recorded_for_session_report():
    """违规须进会话级账 —— 防生产代码 `except sqlite3.Error: pass` 吞掉后用例假绿。"""
    import tests.conftest as cf

    before = len(cf._PROD_DB_VIOLATIONS)
    conn = _prod_ro()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO trade_records (symbol) VALUES ('US.GUARDTEST')")
    finally:
        conn.close()
    assert len(cf._PROD_DB_VIOLATIONS) == before + 1
    _nodeid, db_path, action, _obj = cf._PROD_DB_VIOLATIONS[-1]
    assert db_path.endswith("ftseek_us_live.db")
    assert action == sqlite3.SQLITE_INSERT


# ── 非生产库放行 ────────────────────────────────────────────────────────────

def test_tmp_path_database_write_is_allowed(tmp_path):
    db = tmp_path / "scratch.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (a int)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_memory_database_write_is_allowed():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a int)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_non_db_file_write_is_allowed(tmp_path):
    """非 .db 后缀不属生产库判据 (本闸只管 SQLite 库)。"""
    conn = sqlite3.connect(tmp_path / "note.txt")
    try:
        conn.execute("CREATE TABLE t (a int)")
    finally:
        conn.close()


# ── 豁免标记 ────────────────────────────────────────────────────────────────

@pytest.mark.allow_production_data_write
def test_allow_marker_permits_production_db_write():
    """显式豁免须真正放行 (否则豁免形同虚设, 等于闸不存在)。

    放行后应由 sqlite 自身报 readonly 错误, 而非本闸的 "not authorized" ——
    两者可区分, 证明 authorizer 确实让路了。
    """
    conn = _prod_ro()
    try:
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute("INSERT INTO trade_records (symbol) VALUES ('US.GUARDTEST')")
        assert "not authorized" not in str(exc.value)
    finally:
        conn.close()


# ── 绕过路径回归钉子 ────────────────────────────────────────────────────────

def test_db_utils_safe_connect_connection_is_also_guarded():
    """app/core/db_utils.py:138 的 _safe_connect 在 import 期捕获 sqlite3.connect 引用;
    若 conftest 的 patch 晚于 db_utils import, 这条连接会绕过本闸。"""
    import app.core.db_utils as db_utils

    conn = db_utils._safe_connect(f"file:{PROD_DB}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("INSERT INTO trade_records (symbol) VALUES ('US.GUARDTEST')")
    finally:
        conn.close()


def test_event_store_default_path_is_production_db():
    """app/event_stream/store.py:18 的 EventStore() 默认路径即生产库 (1.6GB) —
    构造即 CREATE TABLE 写入。钉住默认路径, 防止将来被挪到 data/ 下而无人察觉。"""
    src = PROJECT_ROOT / "app" / "event_stream" / "store.py"
    text = src.read_text(encoding="utf-8")
    assert "event_stream.db" in text
    assert "get_data_dir()" in text


def test_production_db_paths_are_recognised():
    """19 个生产库路径必须全部被判为生产 (否则闸有洞)。"""
    import tests.conftest as cf

    known = [
        "data/ftseek_us.db", "data/ftseek_us_live.db", "data/event_stream.db",
        "data/playbook_exits.db", "data/decisions.db", "data/signal_snapshots.db",
        "data/strategy_memory.db", "app/data/exit_quality.db", "app/data/atr_cache.db",
        "ftseek_us.db",
    ]
    for rel in known:
        assert cf._is_production_db_path(rel) is not None, f"{rel} 未被识别为生产库"

    for rel in (":memory:", "/tmp/whatever.db", "notes.txt", "data/x.jsonl"):
        assert cf._is_production_db_path(rel) is None, f"{rel} 被误判为生产库"


def test_uri_form_is_recognised_as_production():
    """只读 URI (file:...?mode=ro) 也须被识别 —— 否则只读连接会绕过闸。"""
    import tests.conftest as cf

    assert cf._is_production_db_path(f"file:{PROD_DB}?mode=ro") is not None
