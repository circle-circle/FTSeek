r"""幽灵仓对账的 env 作用域与权威闩契约 (2026-09-05, DEB-20260905 §13.14).

被测: `app/web_dashboard/data_service.py`
  ① `_env_account_scope` / `_load_position_meta(account_scope=...)` — env 作用域裁剪
  ② `_reconcile_ghosts` 的权威闩闸门 (券商权威应答才允许写 DB)
  ③ `_fetch_positions` / `_fetch_positions_from_db` 对闩的置位与清零
  ④ `_build_enriched_positions` 端到端: 一次 dashboard 请求不得改动 sim 持仓

事故现场 (实测, 非推理): 2026-09-05 22:24:35 一次 `GET /api/decision-hub` 让
`logs/dashboard.log` 打出 `[GhostReconcile] 标记幽灵仓 is_anomalous=1 (3):
['US.UBER', 'US.RIVN', 'US.HOOD']` —— 三个**真实在仓**的 sim 数据收集仓被标异常,
按既有逻辑 24h 后会被 `DELETE FROM open_positions`。已按 04:09 备份原值逐行回滚
(回滚后全表 md5 与污染前一致)。

两条独立成因 (缺一不会打穿):
  A. **跨 env 差集**: `DataService._get_env_ctx` 复用 `ManualOrderService._is_live()`,
     而该方法**硬编码 return True** ("手动交易固定走实盘", 2026-06-24 三态共存 Phase 0)
     ⇒ dashboard 恒查 REAL 账户; 但 `.env.us` 的 TRADE_ENV=1 ⇒ `get_main_db()` 是 sim 库
     `ftseek_us.db`, 现役 3 行全是 `account_type='simulate'`。REAL 应答 {MU, TSLA}
     与 sim 库 {UBER, RIVN, HOOD} 做差集 ⇒ 全判幽灵。同文件的
     `_fetch_positions_from_db` 早就按 `account_type != 'simulate'` 过滤, 证明
     `open_positions` 是**混 env 表**且 REAL 视图本就该忽略 sim 行 —
     `_load_position_meta` 漏了这层过滤。
  B. **空结果被当成零持仓**: `_fetch_positions` 失败/超时 → DB fallback → fallback
     又恒为空 (sim 行被它自己排除) → `if not r: return None` → `get_positions_raw`
     交出 `[]` → `futu_symbols=set()` → **所有** meta 行判幽灵。DEB-20260904 #S11
     只在 `_fetch_positions` 内部区分了 "RET_OK+空表" 与 "查询失败", 但这个区分
     **没有传给对账层**。

刻意不在本批范围 (须用户裁决, 勿在此扩权):
  - dashboard 究竟该看 REAL 还是 SIMULATE (`_is_live()` 硬编码 True 与 TRADE_ENV=1
    的 DB 路由互相矛盾) — 改它会变更看板显示的账户, 属产品语义变更;
  - `_load_position_meta` / `_reconcile_ghosts` 是否该在 REAL env 下改读
    `ftseek_us_live.db` (该库 open_positions **无 account_type 列**, 实测), 同上;
  - `trade_store:1111` 的 `updated_at` 写方 = B1 第五阶, 须单独 DEB。

回滚: `git revert <本批提交>` + 删本文件 ⇒ 回到"每次看板请求都可能标异常/删仓"。
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.web_dashboard.data_service import DataService  # noqa: E402
# 真实常量: futu.RET_OK 是 **0** (int)。用字符串 "RET_OK" 冒充会让 `ret == RET_OK`
# 恒 False ⇒ 静默走 DB fallback, 三个用例全部测到错的分支 (首跑即踩)。
from futu import RET_OK  # noqa: E402

# 事故现场的三行 sim 持仓 (symbol, size, entry_time/updated_at, current_sl)
SIM_ROWS = [
    ("US.UBER", 70.0, "2026-09-03T09:39:51.298036-04:00", 74.57),
    ("US.RIVN", 312.0, "2026-09-03T09:50:51.519368-04:00", 15.12),
    ("US.HOOD", 23.0, "2026-09-04T13:30:12.864694-04:00", 119.13),
]
# 事故当时 REAL 账户的应答 (dashboard.log 未记内容, 由 /api/decision-hub 输出反推)
REAL_ROWS = [
    {"code": "US.MU", "qty": -100, "average_cost": 114.0, "nominal_price": 112.0, "pl_val": 200.0},
    {"code": "US.TSLA", "qty": 10, "average_cost": 250.0, "nominal_price": 246.0, "pl_val": -40.0},
]

_SCHEMA_WITH_ACCOUNT_TYPE = """
CREATE TABLE open_positions (
    symbol TEXT PRIMARY KEY,
    direction TEXT, size REAL, avg_entry_price REAL,
    current_sl REAL, initial_tp REAL,
    entry_time TEXT, updated_at TEXT,
    sync_status TEXT, is_anomalous INTEGER DEFAULT 0,
    account_type TEXT, owner TEXT
)
"""
# ftseek_us_live.db 的实际形态: 无 account_type 列 (实测 PRAGMA)
_SCHEMA_NO_ACCOUNT_TYPE = _SCHEMA_WITH_ACCOUNT_TYPE.replace(",\n    account_type TEXT", "")


def _make_db(path: Path, rows=SIM_ROWS, with_account_type: bool = True,
             account_type: str = "simulate") -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA_WITH_ACCOUNT_TYPE if with_account_type else _SCHEMA_NO_ACCOUNT_TYPE)
    tail_col = ", account_type" if with_account_type else ""
    tail_bind = ", ?" if with_account_type else ""
    for sym, size, ts, sl in rows:
        params = [sym, size, sl, sl, ts, ts] + ([account_type] if with_account_type else [])
        conn.execute(
            "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, current_sl, "
            "entry_time, updated_at, sync_status, is_anomalous, owner" + tail_col + ") "
            "VALUES (?, 'LONG', ?, ?, ?, ?, ?, 'synced', 0, 'auto'" + tail_bind + ")",
            params,
        )
    conn.commit()
    conn.close()
    return path


def _read_rows(path: Path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM open_positions ORDER BY symbol")]
    conn.close()
    return rows


class _FakeFrame:
    """最小 pandas.DataFrame 替身: 只提供 _fetch_positions 用到的 .empty / .iterrows()"""

    def __init__(self, rows):
        self._rows = list(rows)

    @property
    def empty(self) -> bool:
        return not self._rows

    def iterrows(self):
        return iter(list(enumerate(self._rows)))


class _FakeBrokerCtx:
    def __init__(self, ret, frame=None, exc=None):
        self._ret, self._frame, self._exc = ret, frame, exc
        self.calls = []

    def position_list_query(self, trd_env=None, acc_id=None):
        self.calls.append((trd_env, acc_id))
        if self._exc:
            raise self._exc
        return self._ret, self._frame


class _FakeSvc:
    def __init__(self, ctx):
        self._broker_ctx = ctx

    def connect(self) -> bool:
        return True

    def _is_live(self) -> bool:
        return True

    def _read_env(self, key, default=""):
        return default


@pytest.fixture
def svc_factory():
    """构造 DataService + 假券商, 并保证线程池回收 (勿泄漏 8 个 worker/例)"""
    made = []

    def _build(ret=RET_OK, frame=None, exc=None, env="REAL"):
        ds = DataService()
        made.append(ds)
        ctx = _FakeBrokerCtx(ret, frame, exc)
        fake = _FakeSvc(ctx)
        ds._get_env_ctx = lambda: (fake, env, 12345)
        return ds, ctx

    yield _build
    for ds in made:
        ds._pool.shutdown(wait=False)


# ─────────────────────────────────────────────────────────────────────────
# A. 事故回归: REAL env 的一次看板请求不得改动 sim 持仓
# ─────────────────────────────────────────────────────────────────────────

def test_incident_regression_real_env_request_leaves_sim_rows_untouched(tmp_path, svc_factory, monkeypatch):
    """复现 2026-09-05 22:24:35: REAL 账户回 {MU,TSLA}, sim 库 3 行必须原文不动。

    修前: 3 行被写 is_anomalous=1 + updated_at=当时 (24h 后 DELETE)。
    弱测试陷阱: 只断言"is_anomalous 仍为 0"不够 —— updated_at 被改写同样是污染
    (它是 24h 删除倒计时的起点), 故逐列比对整行。
    """
    db = _make_db(tmp_path / "ftseek_us.db")
    before = _read_rows(db)
    ds, _ctx = svc_factory(frame=_FakeFrame(REAL_ROWS), env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))

    enriched = asyncio.run(ds._build_enriched_positions())

    assert [p["symbol"] for p in enriched] == ["US.MU", "US.TSLA"], \
        "REAL env 的看板视图应只渲染实盘仓 (与修前一致, 本批不改看板显示口径)"
    assert _read_rows(db) == before, \
        "sim 持仓被看板请求改写了 —— 跨 env 差集把真实在仓判成幽灵仓"


def test_real_env_meta_scope_excludes_simulate_rows(tmp_path, svc_factory, monkeypatch):
    """成因 A 的直接钉桩: scope='real' 时 meta_map 里不得出现 simulate 行"""
    db = _make_db(tmp_path / "ftseek_us.db")
    ds, _ctx = svc_factory(frame=_FakeFrame(REAL_ROWS), env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))

    ds._last_positions_env = "REAL"
    assert ds._env_account_scope() == "real"
    assert ds._load_position_meta(False, ds._env_account_scope()) == {}

    ds._last_positions_env = "SIMULATE"
    assert ds._env_account_scope() == "simulate"
    assert set(ds._load_position_meta(False, ds._env_account_scope())) == \
        {"US.UBER", "US.RIVN", "US.HOOD"}

    ds._last_positions_env = None
    assert ds._env_account_scope() is None
    assert len(ds._load_position_meta(False, ds._env_account_scope())) == 3, \
        "env 未知时保持旧口径 (不过滤), 由权威闩负责挡写入"


# ─────────────────────────────────────────────────────────────────────────
# B. 权威闩: 降级/失败/超时的空结果不可解释为"券商确认零持仓"
# ─────────────────────────────────────────────────────────────────────────

def test_db_fallback_clears_authoritative_latch(tmp_path, svc_factory, monkeypatch):
    """成因 B: 券商抛异常 → DB fallback → 闩必须为 False, 对账不得写 DB"""
    db = _make_db(tmp_path / "ftseek_us.db")
    before = _read_rows(db)
    ds, _ctx = svc_factory(exc=RuntimeError("OpenD 断连"), env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))

    asyncio.run(ds.get_positions_raw())

    assert ds._last_positions_authoritative is False
    assert _read_rows(db) == before, "降级轮次仍然写了 DB"


def test_reconcile_skipped_when_not_authoritative(tmp_path, svc_factory, monkeypatch):
    """直接打对账层: 闩为 False 时, 即便 meta_map 全不匹配也不得标记"""
    db = _make_db(tmp_path / "ftseek_us.db")
    before = _read_rows(db)
    ds, _ctx = svc_factory(env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))

    meta = {r["symbol"]: {"is_anomalous": 0, "updated_at": r["updated_at"]} for r in before}
    ds._last_positions_authoritative = False
    ds._last_positions_env = "REAL"
    ds._reconcile_ghosts({"US.MU", "US.TSLA"}, meta)

    assert _read_rows(db) == before


def test_gate_runs_before_throttle_window(tmp_path, svc_factory, monkeypatch):
    """闸门必须在限流之前: 非权威轮次不得消耗 5min 窗口

    否则一次券商抖动会把紧随其后的**权威**轮次挤掉 (限流 return), 幽灵仓照旧留存。
    """
    db = _make_db(tmp_path / "ftseek_us.db")
    ds, _ctx = svc_factory(env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))
    ds._last_positions_authoritative = False
    ds._last_positions_env = "REAL"

    ds._reconcile_ghosts(set(), {"US.UBER": {"is_anomalous": 0, "updated_at": ""}})
    ds._reconcile_ghosts(set(), {"US.UBER": {"is_anomalous": 0, "updated_at": ""}})

    assert ds._last_reconcile_time == 0.0, "非权威轮次消耗了限流窗口"


def test_unknown_env_blocks_reconcile_even_if_authoritative(tmp_path, svc_factory, monkeypatch):
    """env 未知 (svc.connect 失败) 时 meta_map 未裁剪 ⇒ 即使闩为 True 也不得写"""
    db = _make_db(tmp_path / "ftseek_us.db")
    before = _read_rows(db)
    ds, _ctx = svc_factory(env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))
    ds._last_positions_authoritative = True
    ds._last_positions_env = None

    ds._reconcile_ghosts(set(), {r["symbol"]: {"is_anomalous": 0, "updated_at": ""} for r in before})

    assert _read_rows(db) == before


# ─────────────────────────────────────────────────────────────────────────
# C. 没把功能修死: 同 env 的真幽灵仍要被标记, 24h 后仍要被删除
# ─────────────────────────────────────────────────────────────────────────

def test_legit_ghost_in_same_env_is_still_marked(tmp_path, svc_factory, monkeypatch):
    """SIMULATE env + 券商权威空应答 ⇒ sim 行是真幽灵, 必须照旧标记 (DEB-20260904 #S11)"""
    db = _make_db(tmp_path / "ftseek_us.db")
    ds, ctx = svc_factory(frame=_FakeFrame([]), env="SIMULATE")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))

    enriched = asyncio.run(ds._build_enriched_positions())

    assert ds._last_positions_authoritative is True, "RET_OK+空表就是权威应答, 不得降级"
    assert ds._last_positions_env == "SIMULATE"
    assert enriched == []
    after = {r["symbol"]: r for r in _read_rows(db)}
    assert all(after[s]["is_anomalous"] == 1 for s, *_ in SIM_ROWS), \
        "同 env 的真幽灵没被标记 ⇒ 本批把功能修死了"
    assert all(after[s]["updated_at"] != after[s]["entry_time"] for s, *_ in SIM_ROWS), \
        "标记时必须刷新 updated_at (它是 24h 删除倒计时的起点)"


def test_legit_ghost_deleted_after_24h(tmp_path, svc_factory, monkeypatch):
    """已标记 + updated_at 超 24h ⇒ DELETE 仍要生效 (账龄比较走 parse_ledger_ts 同尺)"""
    from app.utils.timezone import now_et

    db = _make_db(tmp_path / "ftseek_us.db")
    ds, ctx = svc_factory(frame=_FakeFrame([]), env="SIMULATE")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))
    stale = (now_et().replace(microsecond=0) - __import__("datetime").timedelta(hours=25)).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE open_positions SET is_anomalous = 1, updated_at = ?", (stale,))
    conn.commit()
    conn.close()

    asyncio.run(ds._build_enriched_positions())

    assert _read_rows(db) == [], "超 24h 的已标记幽灵仓没被删除"
    assert ctx.calls, "券商查询压根没发生 (闩不可能为 True)"


# ─────────────────────────────────────────────────────────────────────────
# D. 缺列容错: live 库 open_positions 无 account_type 列 (实测)
# ─────────────────────────────────────────────────────────────────────────

def test_missing_account_type_column_does_not_lose_meta(tmp_path, svc_factory, monkeypatch):
    """get_main_db 在 live 路由下指向 ftseek_us_live.db, 该库无 account_type 列。

    若直接拼过滤条件, 整条 SELECT 抛 OperationalError ⇒ except 吞掉 ⇒ meta_map 为空
    ⇒ 实盘仓的 SL/TP 全部消失 (比不修更糟)。故必须先 PRAGMA 探列。
    """
    db = _make_db(tmp_path / "live.db", with_account_type=False)
    ds, _ctx = svc_factory(env="REAL")
    monkeypatch.setattr(ds, "_get_db", lambda: str(db))

    meta = ds._load_position_meta(False, "real")

    assert set(meta) == {"US.UBER", "US.RIVN", "US.HOOD"}, "缺列时退化成元数据全丢"
    assert meta["US.UBER"]["stop_loss_price"] == 74.57


def test_env_label_normalizes_futu_trdenv_and_plain_strings(svc_factory):
    """_trd_env_label 要同时吃下 futu 的 TrdEnv 常量、其 .value 与未知值"""
    ds, _ctx = svc_factory(env="REAL")
    from futu import TrdEnv

    assert ds._trd_env_label(TrdEnv.REAL) == "REAL"
    assert ds._trd_env_label(TrdEnv.SIMULATE) == "SIMULATE"
    assert ds._trd_env_label("REAL") == "REAL"
    assert ds._trd_env_label("SIMULATE") == "SIMULATE"
    assert ds._trd_env_label(None) is None
    assert ds._trd_env_label(object()) is None
