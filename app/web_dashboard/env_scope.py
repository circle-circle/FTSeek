r"""看板(实盘手动操作台) 与 SIM 数据收集仓的硬隔离谓词.

立案 (2026-09-05, DEB-20260905 §13.15 · 用户裁「webdashboard 是我用来手动操作实盘,
自管理的, 和 SIM 模拟账号数据不要混乱了」):

看板进程的**券商侧恒为 REAL** (`ManualOrderService._is_live()` 硬编码 `return True`,
2026-06-24 三态共存 Phase 0: "手动交易永远是实盘, TRADE_ENV 现仅控制 DB 路由"),
而**DB 侧由 TRADE_ENV 路由** — 现值 `TRADE_ENV=1` ⇒ `get_main_db()` = **sim 库**
`data/ftseek_us.db`, 其 `open_positions` 现役 3 行全是 `account_type='simulate'`
的 sim 数据收集仓。两侧 env 不一致, 于是看板的每一次读写都可能落到 sim 数据上。

**为什么不能靠 account_type 分栏了事**: `open_positions` 的 **PRIMARY KEY 是 `symbol`**
(sim/live 两库实测一致) ⇒ 同一标的的实盘手动仓与 sim 收集仓**在同一张表里物理上
无法共存**。真实的隔离必须落在**库文件级**; 本模块是库级分离落地前的**收敛层**。

已被本模块挡住的破坏 (修复前均可静默发生):
  · `order/audit.mark_position_manual` 把 sim 行翻成 `owner='manual'` ⇒ 主线
    Reconciler 判 `LEGACY/MANUAL → PROTECTED` ⇒ **sim 仓静默失去退出监控**
    (SL/trailing 不再管它), 且 `account_type` 从 'simulate' 变 'real' = 数据集被改写;
  · `routes/dashboard.py` 的止损价/触发标记写操作改写 sim 行的 SL;
  · `order/executor.py` 的 SL/TP/broker_order_id 写操作改写 sim 行;
  · `order/oco_handler.py` / `order/thesis.py` 同类;
  · `routes/battle_plan.py` 用 **sim 行**的 `is_anomalous` 去过滤**实盘**标的 (读侧串味)。

**列容错是必需的**: `data/ftseek_us_live.db` 的 `open_positions` 曾**没有 `account_type`
列** (实测 40 列 vs sim 库 41 列, 差集恰为 `account_type`)。若把谓词硬拼进 SQL,
将来 DB 路由切到 live 库时**每一条查询都会抛 `OperationalError`** ⇒ 手动实盘单
落库全断。故谓词按"库里有这列才过滤, 没有就恒真"生成 (缺列 = 单 env 库, 无需过滤)。

2026-09-06 更新 (DEB-20260905 §13.17): 该缺口已按 `scripts/env_separation_shadow.py`
的双侧 EXPLAIN 影子核对补齐 —— live 库 `open_positions` 加 `account_type TEXT
DEFAULT 'real'`、`trade_records` 加 `pnl_net REAL`, 两库列数对齐 41/41, 阻塞缺口归零
(判定 READY_TO_SWITCH)。**容错分支不删**: 恢复旧备份或另建实盘库时仍可能缺列, 而其
失败模式是整条查询抛错 ⇒ 属便宜保险。库级路由切换本身仍挂在用户验收上, 未动。

回滚: 删本文件 + `git revert` 各接入点 ⇒ 回到"看板可静默改写 sim 数据收集仓"。
"""
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("ftseek_us.env_scope")

# 隔离谓词: 排除 SIM 数据收集仓 (与 data_service._fetch_positions_from_db 既有口径一致)
SIM_EXCLUDING_CLAUSE = "LOWER(COALESCE(account_type,'')) != 'simulate'"
SIM_MATCHING_CLAUSE = "LOWER(COALESCE(account_type,'')) = 'simulate'"

_COLUMN_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

# `... WHERE symbol=?` 结尾 (允许任意空白), scoped_update 据此拼接隔离谓词
_PAT_WHERE_SYMBOL = re.compile(r"WHERE\s+symbol\s*=\s*\?\s*$", re.IGNORECASE)


def reset_cache() -> None:
    """清空列探测缓存 (测试用: 同一进程内会切换多个临时库)."""
    with _CACHE_LOCK:
        _COLUMN_CACHE.clear()


def _db_file_of(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2]) if row and len(row) > 2 else ""
    except Exception:
        return ""


def has_account_type(target: Union[sqlite3.Connection, str, Path]) -> bool:
    """目标库的 open_positions 是否有 account_type 列 (按库文件路径缓存).

    接受**连接**或**路径**: 有连接就用连接探 (顺带拿到库文件做缓存键), 只有路径时
    开一个只读连接探。探测失败一律按 False 处理 ⇒ 谓词退化成恒真 ⇒ **宁可不过滤,
    也不要让实盘侧写操作整条抛错** (方向与"删除不可逆"一致: 失败要落在安全侧)。
    """
    conn: Optional[sqlite3.Connection] = None
    opened = False
    try:
        if isinstance(target, sqlite3.Connection):
            conn = target
            key = _db_file_of(conn)
        else:
            key = str(target)
            conn = sqlite3.connect(f"file:{key}?mode=ro", uri=True)
            opened = True
        with _CACHE_LOCK:
            if key in _COLUMN_CACHE:
                return _COLUMN_CACHE[key]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(open_positions)")}
        found = "account_type" in cols
    except Exception as e:
        logger.debug(f"[EnvIsolation] account_type 列探测失败 ({target}): {e}")
        found = False
    finally:
        if opened and conn is not None:
            try:
                conn.close()
            except Exception as e:  # R5 留痕: 只读探测连接的 close 失败不影响判定结果
                logger.debug(f"[EnvIsolation] 探测连接 close 失败 ({key}): {e}")
    with _CACHE_LOCK:
        _COLUMN_CACHE[key] = found
    return found


def real_only(target: Union[sqlite3.Connection, str, Path]) -> str:
    """可直接拼进 WHERE 的**实盘侧**隔离谓词 (缺列 ⇒ '1=1')."""
    return SIM_EXCLUDING_CLAUSE if has_account_type(target) else "1=1"


def is_simulate_row(target: Union[sqlite3.Connection, str, Path], symbol: str) -> bool:
    """该 symbol 在目标库里是否是 SIM 数据收集仓 (缺列 ⇒ False).

    接受**连接**或**路径**: 路由层 (`routes/dashboard.py`) 走 `DataService.db_execute`,
    手上没有连接, 只有 `ds._get_db()` 的路径。
    """
    if not has_account_type(target):
        return False
    opened = not isinstance(target, sqlite3.Connection)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = (sqlite3.connect(f"file:{target}?mode=ro", uri=True)
                if opened else target)
        row = conn.execute(
            f"SELECT 1 FROM open_positions WHERE symbol=? AND {SIM_MATCHING_CLAUSE}",
            (symbol,),
        ).fetchone()
        return bool(row)
    except Exception as e:
        logger.debug(f"[EnvIsolation] is_simulate_row({symbol}) 探测失败: {e}")
        return False
    finally:
        if opened and conn is not None:
            try:
                conn.close()
            except Exception as e:  # R5 留痕: 同上
                logger.debug(f"[EnvIsolation] 探测连接 close 失败 ({symbol}): {e}")


def warn_sim_collision(target: Union[sqlite3.Connection, str, Path], symbol: str,
                       action: str) -> bool:
    """实盘侧写操作 0 行生效且原因是撞上 SIM 仓时**出声**, 返回是否确为撞仓.

    静默 no-op 是最坏结局: 券商侧单子已经出去了, DB 侧却没记下, 而用户看不到任何
    提示 ⇒ 实盘持仓变成"账外仓"。故这里一律 CRITICAL, 把冲突摊到日志上。
    """
    if not is_simulate_row(target, symbol):
        return False
    logger.critical(
        f"[EnvIsolation] {action} 未落库: {symbol} 当前是 SIM 数据收集仓 "
        f"(open_positions.account_type='simulate'; PK=symbol ⇒ 实盘仓无法与其共存)。"
        f"看板是实盘手动操作台, **不改写 SIM 行**; 该实盘动作需人工对账 "
        f"(库级分离方案见 DEB-20260905 §13.15)。"
    )
    return True


def scoped_update(conn: sqlite3.Connection, sql: str, params: tuple,
                  symbol: str, action: str) -> int:
    """给 `... WHERE symbol=?` 形式的实盘侧写操作自动加隔离谓词, 返回 rowcount.

    约定 (强制校验): `sql` 必须以 `WHERE symbol=?` 结尾, `params` 最后一个元素是
    symbol。不满足直接 ValueError —— 宁可炸在开发期, 也不要拼出一条谓词位置错误的
    SQL (那种错误会让隔离静默失效)。
    """
    if not _PAT_WHERE_SYMBOL.search(sql.rstrip()):
        raise ValueError(
            f"scoped_update 只接受以 'WHERE symbol=?' 结尾的 SQL, 实得: …{sql[-60:]!r}"
        )
    cursor = conn.execute(f"{sql.rstrip()} AND {real_only(conn)}", params)
    if cursor.rowcount == 0:
        warn_sim_collision(conn, symbol, action)
    return cursor.rowcount
