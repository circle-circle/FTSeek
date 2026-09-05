r"""看板(实盘手动台) 与 SIM 数据收集仓的硬隔离契约 (2026-09-05, DEB-20260905 §13.15).

用户裁: 「webdashboard 是我用来手动操作实盘, 自管理的, 和 SIM 模拟账号数据不要混乱了」。

病灶: 看板进程**券商侧恒 REAL** (`ManualOrderService._is_live()` 硬编码 `return True`),
**DB 侧却由 TRADE_ENV 路由** (=1 ⇒ sim 库 `data/ftseek_us.db`), 且 `open_positions` 的
**PRIMARY KEY 是 `symbol`** (sim/live 两库实测一致) ⇒ 同一标的的实盘手动仓与 sim 收集仓
**物理上无法共存于一张表**。于是看板每一次以 symbol 为键的读写都可能落到 sim 数据上。

最严重的一条 (本批拦下): `order/audit.mark_position_manual` 会把 sim 行翻成
`owner='manual'` ⇒ 主线 Reconciler 判 `LEGACY/MANUAL → PROTECTED` ⇒ **sim 仓静默失去
退出监控**, 且 `account_type` 'simulate'→'real' = 正在收集的数据集被改写。

被测: `app/web_dashboard/env_scope.py` 及其 6 个接入文件
(audit / executor / oco_handler / thesis / routes.dashboard / routes.battle_plan)。

刻意不在本批范围: **库级分离** (看板 DB 路由切到 `ftseek_us_live.db`) —— 需要 live 库
schema/表集补齐 (实测 live 44 表 vs sim 44 表但**集合不同**; `open_positions` 缺
`account_type` 列) 且会把看板分析口径从 795 笔 sim 成交换成 17 笔实盘成交 = 产品语义
变更, 须用户裁。本批只做**收敛层**: 保证在分离落地前, 看板不可能静默改写 sim 数据。
"""
import logging
import re
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.web_dashboard import env_scope  # noqa: E402
from app.web_dashboard.env_scope import (  # noqa: E402
    is_simulate_row,
    real_only,
    scoped_update,
    warn_sim_collision,
)

SCHEMA_SIM = """
CREATE TABLE open_positions (
    symbol TEXT PRIMARY KEY, direction TEXT, size REAL, avg_entry_price REAL,
    current_sl REAL, initial_tp REAL, stop_loss_price REAL,
    stop_loss_triggered INTEGER DEFAULT 0, trailing_stop_triggered INTEGER DEFAULT 0,
    sl_broker_order_id TEXT, tp_broker_order_id TEXT,
    thesis TEXT, entry_context_json TEXT, expected_hold_days INTEGER,
    invalidation_price REAL, entry_time TEXT, updated_at TEXT,
    sync_status TEXT, is_anomalous INTEGER DEFAULT 0,
    account_type TEXT, owner TEXT
)
"""
# ftseek_us_live.db 的实际形态: 无 account_type 列 (实测 40 列 vs sim 41 列)
SCHEMA_LIVE = SCHEMA_SIM.replace(",\n    account_type TEXT", "")


@pytest.fixture(autouse=True)
def _reset_column_cache():
    env_scope.reset_cache()
    yield
    env_scope.reset_cache()


def _mkdb(path: Path, with_account_type: bool = True, rows=()) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SIM if with_account_type else SCHEMA_LIVE)
    for row in rows:
        # rows 允许 (symbol, account_type, owner) 或 (symbol, owner) — 后者用于无
        # account_type 列的 live 形态库
        sym, acct, owner = row if len(row) == 3 else (row[0], None, row[1])
        conn.execute(
            "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, "
            "owner, sync_status, account_type) VALUES (?, 'LONG', 10, 100.0, ?, 'synced', ?)"
            if with_account_type else
            "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, "
            "owner, sync_status) VALUES (?, 'LONG', 10, 100.0, ?, 'synced')",
            (sym, owner, acct) if with_account_type else (sym, owner),
        )
    conn.commit()
    conn.close()
    return path


def _row(path: Path, symbol: str) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM open_positions WHERE symbol=?", (symbol,)).fetchone()
    conn.close()
    return dict(r) if r else {}


class _FakeService:
    """只提供 mark_position_manual 用到的 `_db()`"""

    def __init__(self, path: Path):
        self._path = str(path)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


# ─────────────────────────────────────────────────────────────────────────
# A. 谓词的列容错 (缺列 ⇒ 恒真, 否则 live 库上每条查询都会抛错)
# ─────────────────────────────────────────────────────────────────────────

def test_real_only_is_column_tolerant(tmp_path):
    sim = _mkdb(tmp_path / "sim.db")
    live = _mkdb(tmp_path / "live.db", with_account_type=False)

    assert real_only(sim) == "LOWER(COALESCE(account_type,'')) != 'simulate'"
    assert real_only(live) == "1=1", \
        "live 库无 account_type 列时必须退化成恒真 — 否则整条 SQL 抛 OperationalError, 手动实盘单落库全断"
    assert real_only(str(tmp_path / "不存在.db")) == "1=1", "探测失败必须落在安全侧 (不过滤)"


def test_is_simulate_row_truth_table(tmp_path):
    db = _mkdb(tmp_path / "sim.db", rows=[
        ("US.UBER", "simulate", "auto"),
        ("US.MU", "real", "manual"),
        ("US.NVDA", None, "auto"),
    ])
    live = _mkdb(tmp_path / "live.db", with_account_type=False)

    assert is_simulate_row(db, "US.UBER") is True
    assert is_simulate_row(db, "US.MU") is False
    assert is_simulate_row(db, "US.NVDA") is False, "account_type 为 NULL 不算 sim (与 COALESCE 口径一致)"
    assert is_simulate_row(db, "US.不存在") is False
    assert is_simulate_row(live, "US.UBER") is False, "缺列库无法判定 ⇒ False (不误伤实盘侧写入)"
    # 连接与路径两种入参都要能用 (路由层手上只有路径)
    conn = sqlite3.connect(str(db))
    assert is_simulate_row(conn, "US.UBER") is True
    conn.close()


# ─────────────────────────────────────────────────────────────────────────
# B. scoped_update: 撞 sim 仓 ⇒ 0 行 + CRITICAL 出声; 实盘行照写
# ─────────────────────────────────────────────────────────────────────────

def test_scoped_update_refuses_sim_row_and_speaks_up(tmp_path, caplog):
    db = _mkdb(tmp_path / "sim.db", rows=[("US.UBER", "simulate", "auto")])
    before = _row(db, "US.UBER")
    conn = sqlite3.connect(str(db))

    with caplog.at_level(logging.CRITICAL, logger="ftseek_us.env_scope"):
        rows = scoped_update(
            conn,
            "UPDATE open_positions SET stop_loss_price = ?, stop_loss_triggered = 0 WHERE symbol = ?",
            (123.45, "US.UBER"), "US.UBER", "设置止损",
        )
    conn.commit()
    conn.close()

    assert rows == 0
    assert _row(db, "US.UBER") == before, "sim 数据收集仓被看板改写了"
    assert any("[EnvIsolation]" in r.message and r.levelno >= logging.CRITICAL
               for r in caplog.records), \
        "静默 no-op 是最坏结局: 券商侧单子已出、DB 侧没记下、用户看不到任何提示"


def test_scoped_update_still_writes_real_row(tmp_path, caplog):
    """正向对照: 隔离不得把实盘侧正常写入也挡掉 (否则等于修死了功能)"""
    db = _mkdb(tmp_path / "sim.db", rows=[
        ("US.MU", "real", "manual"),
        ("US.UBER", "simulate", "auto"),
    ])
    conn = sqlite3.connect(str(db))
    with caplog.at_level(logging.CRITICAL, logger="ftseek_us.env_scope"):
        rows = scoped_update(
            conn,
            "UPDATE open_positions SET stop_loss_price = ?, stop_loss_triggered = 0 WHERE symbol = ?",
            (99.0, "US.MU"), "US.MU", "设置止损",
        )
    conn.commit()
    conn.close()

    assert rows == 1
    assert _row(db, "US.MU")["stop_loss_price"] == 99.0
    assert _row(db, "US.UBER")["stop_loss_price"] is None
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]


def test_scoped_update_works_on_live_shaped_db(tmp_path):
    db = _mkdb(tmp_path / "live.db", with_account_type=False, rows=[("US.MU", "manual")])
    conn = sqlite3.connect(str(db))
    rows = scoped_update(
        conn, "UPDATE open_positions SET current_sl=? WHERE symbol=?",
        (88.0, "US.MU"), "US.MU", "改止损",
    )
    conn.commit()
    conn.close()
    assert rows == 1 and _row(db, "US.MU")["current_sl"] == 88.0


def test_scoped_update_rejects_sql_it_cannot_scope(tmp_path):
    """谓词是**拼在末尾**的 ⇒ SQL 不以 `WHERE symbol=?` 结尾就必须炸在开发期.

    否则拼出一条谓词位置错误的 SQL, 隔离会**静默失效** (比不修更危险)。
    """
    db = _mkdb(tmp_path / "sim.db", rows=[("US.UBER", "simulate", "auto")])
    conn = sqlite3.connect(str(db))
    for bad in (
        "UPDATE open_positions SET current_sl=?",                      # 没有 WHERE
        "UPDATE open_positions SET current_sl=? WHERE symbol=? AND size>0",  # 尾部还有条件
        "UPDATE open_positions SET a=1 WHERE symbol = 'US.UBER'",       # 不是占位符
    ):
        with pytest.raises(ValueError):
            scoped_update(conn, bad, ("US.UBER",), "US.UBER", "x")
    conn.close()


def test_warn_sim_collision_is_false_for_plain_missing_row(tmp_path):
    db = _mkdb(tmp_path / "sim.db", rows=[("US.UBER", "simulate", "auto")])
    conn = sqlite3.connect(str(db))
    assert warn_sim_collision(conn, "US.不存在", "x") is False, \
        "行压根不存在不是撞仓, 不该报 CRITICAL (否则告警会被噪音磨钝)"
    assert warn_sim_collision(conn, "US.UBER", "x") is True
    conn.close()


# ─────────────────────────────────────────────────────────────────────────
# C. 最严重那条: mark_position_manual 不得把 sim 仓翻成 manual
# ─────────────────────────────────────────────────────────────────────────

def test_mark_position_manual_does_not_hijack_sim_row(tmp_path, caplog):
    from app.web_dashboard.order.audit import mark_position_manual

    db = _mkdb(tmp_path / "sim.db", rows=[("US.UBER", "simulate", "auto")])
    before = _row(db, "US.UBER")

    with caplog.at_level(logging.CRITICAL):
        mark_position_manual(_FakeService(db), "US.UBER", "LONG")

    after = _row(db, "US.UBER")
    assert after == before, (
        "sim 数据收集仓被翻成手动实盘仓了 — 后果是主线 Reconciler 判 "
        "LEGACY/MANUAL → PROTECTED ⇒ sim 仓静默失去退出监控, 且 account_type 被改写"
    )
    assert after["owner"] == "auto" and after["account_type"] == "simulate"
    assert any("[EnvIsolation]" in r.message and r.levelno >= logging.CRITICAL
               for r in caplog.records)


def test_mark_position_manual_still_marks_real_row(tmp_path):
    """正向对照: 实盘行 (account_type='real') 仍须照旧标成 owner='manual'"""
    from app.web_dashboard.order.audit import mark_position_manual

    db = _mkdb(tmp_path / "sim.db", rows=[("US.MU", "real", "auto")])
    mark_position_manual(_FakeService(db), "US.MU", "LONG")

    after = _row(db, "US.MU")
    assert after["owner"] == "manual" and after["account_type"] == "real"


def test_mark_position_manual_stub_insert_still_works(tmp_path):
    """行不存在时仍须建 stub (2026-06-11 的 race fix 不能被本批弄丢)"""
    from app.web_dashboard.order.audit import mark_position_manual

    db = _mkdb(tmp_path / "sim.db", rows=[])
    mark_position_manual(_FakeService(db), "US.TSLA", "LONG")

    after = _row(db, "US.TSLA")
    assert after and after["owner"] == "manual" and after["sync_status"] == "pending_sync"


# ─────────────────────────────────────────────────────────────────────────
# D. 源码契约 + 豁免台账: 防止后来的人把守卫删掉
# ─────────────────────────────────────────────────────────────────────────

# 每条 = (相对路径, SQL 锚点子串, 豁免理由)。台账必须**穷尽**: 测试断言
# "实测未加守卫的数量 == 台账条数", 且逐条能对上 ⇒ 新增未守卫的写点会直接报红。
EXEMPTIONS = [
    (
        "app/web_dashboard/order/executor.py",
        "SET current_sl=?, initial_tp=? WHERE symbol=?",
        "该分支由 `trd_env == TrdEnv.SIMULATE` 进入 (模拟盘不下 STOP 单、仅记录到 DB), "
        "写的**本就是 sim 行**, 加实盘隔离谓词反而错。今天它是死代码 "
        "(`_is_live()` 恒 True ⇒ `_trd_env()` 恒 REAL), 但语义上属 sim 侧, 故豁免。",
    ),
    (
        "app/web_dashboard/data_service.py",
        "SET is_anomalous = 1, updated_at = ?",
        "幽灵对账写方, 隔离不靠 SQL 谓词而靠**上游两道闸**: meta_map 已按 "
        "`_env_account_scope()` 裁剪 + `_reconcile_ghosts` 的权威闩 (§13.14)。"
        "此处再加谓词是重复防御, 且它没有 symbol 之外的键可用。",
    ),
]

_PAT_UPDATE = re.compile(r"UPDATE\s+open_positions")


def _dashboard_py_files():
    return sorted((PROJECT_ROOT / "app" / "web_dashboard").rglob("*.py"))


def _unguarded_update_sites():
    """扫出 app/web_dashboard 下所有**未加隔离守卫**的 UPDATE open_positions 写点"""
    out = []
    for f in _dashboard_py_files():
        src = f.read_text(encoding="utf-8")
        for m in _PAT_UPDATE.finditer(src):
            window_before = src[max(0, m.start() - 200): m.start()]
            window_after = src[m.end(): m.end() + 300]
            guarded = ("scoped_update" in window_before) or ("real_only" in window_after)
            if not guarded:
                line = src[: m.start()].count("\n") + 1
                anchor = src[m.start(): m.start() + 90].replace("\n", " ")
                out.append((str(f.relative_to(PROJECT_ROOT)), line, anchor))
    return out


def test_every_dashboard_position_write_is_env_guarded():
    unguarded = _unguarded_update_sites()
    unexplained = [
        (path, line, anchor) for path, line, anchor in unguarded
        if not any(path == ex_path and ex_anchor in anchor
                   for ex_path, ex_anchor, _reason in EXEMPTIONS)
    ]
    assert not unexplained, (
        "发现**未加 SIM 隔离守卫**的看板写点 (会把实盘侧操作写到 sim 数据收集仓上):\n"
        + "\n".join(f"  {p}:{ln}  {a}" for p, ln, a in unexplained)
        + "\n修法: 用 env_scope.scoped_update() 或在 WHERE 里拼 real_only(); "
          "若确属 sim 侧/另有闸门, 请把它连同理由加进本文件的 EXEMPTIONS 台账。"
    )


def test_exemption_ledger_is_exact():
    """台账必须穷尽且**不冗余** — 冗余条目会掩盖"守卫被人删掉"的事实"""
    unguarded = _unguarded_update_sites()
    assert len(unguarded) == len(EXEMPTIONS), (
        f"实测未守卫写点 {len(unguarded)} 处, 台账 {len(EXEMPTIONS)} 条 ⇒ 台账已失真。\n"
        f"实测: {[(p, ln) for p, ln, _ in unguarded]}"
    )
    for ex_path, ex_anchor, reason in EXEMPTIONS:
        assert reason.strip(), f"{ex_path} 的豁免理由为空"
        assert any(p == ex_path and ex_anchor in a for p, _ln, a in unguarded), \
            f"台账条目已失效 (代码里找不到): {ex_path} / {ex_anchor!r}"


def test_guarded_sites_cover_the_six_wired_files():
    """正向钉桩: 本批接入的 6 个文件必须都真的引用了 env_scope (防止 import 被"清理"掉)"""
    expected = {
        "app/web_dashboard/order/audit.py",
        "app/web_dashboard/order/executor.py",
        "app/web_dashboard/order/oco_handler.py",
        "app/web_dashboard/order/thesis.py",
        "app/web_dashboard/routes/dashboard.py",
        "app/web_dashboard/routes/battle_plan.py",
    }
    actual = {
        str(f.relative_to(PROJECT_ROOT)) for f in _dashboard_py_files()
        if "env_scope" in f.read_text(encoding="utf-8")
    }
    assert expected <= actual, f"以下文件已不再引用 env_scope: {sorted(expected - actual)}"
