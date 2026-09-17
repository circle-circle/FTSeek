# -*- coding: utf-8 -*-
"""切换基建钉子 — scripts/cutover_preflight.py + scripts/cutover_rehearse.py.

钉什么 (2026-09-14, 用户裁「目前只是先将基建做好…sim到实盘以及实盘再切换为sim，无缝实现」):
  1. **只读性**: 两个工具都不得对生产库产生任何写动词 —— 这是它们能被日常巡检/cron
     直接挂载的前提。用 AST/源码级钉 (与仓内既有"新模块无任何 SQL 写动词"同款手法),
     而不是靠"跑一次看指纹" (那只能证当次, 不能证将来)。
  2. **探针灵敏度 + 假阳性**: sl_broker_order_id 污染判定走**跨表铁证**
     (open_positions.sl_broker_order_id == trade_records.futu_order_id), 必须
     "种得中就报、种不中就不报" 双向钉, 防止退化成恒报警或恒静默。
  3. **彩排方向**: `_diff(参照, 目标)` 的语义 —— 本条是**回归钉**, 钉住开发期真实犯过的错
     (把 --to live 的目标库复制成了 sim 库 ⇒ 缺口恒算成 0 ⇒ 工具假绿)。
  4. **良性白名单不得腐化**: BENIGN_* 每一项都必须有源码锚点实证; 锚点消失 (例如
     有人开始引用 capital_state.account_id) 则测试红 —— 白名单最危险的失效方式是静默过期。
  5. **回切缺陷可见性**: capital_state 双臂回切实证必须恢复本臂 peak/lock;
     病灶已在 DEB-20260916 修复，本钉保持绿灯以防止按 id 全局捞行回归。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
import json

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def preflight():
    return _load("cutover_preflight")


@pytest.fixture(scope="module")
def rehearse():
    return _load("cutover_rehearse")


# ─────────────────────────────────────────────────────────────
# 最小库夹具 — 只建被测探针真正读的表/列
# ─────────────────────────────────────────────────────────────
def _mk_db(path: Path, *, sl_id=None, tp_id=None, entry_oid=None,
           trade_mode="simulate", owner="auto", size=10.0, account_type="sim",
           with_sl_col=True) -> Path:
    con = sqlite3.connect(path)
    sl_cols = "sl_broker_order_id TEXT, tp_broker_order_id TEXT," if with_sl_col else ""
    con.execute(f"""CREATE TABLE open_positions (
        symbol TEXT PRIMARY KEY, direction TEXT, size REAL, {sl_cols}
        owner TEXT, account_type TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, futu_order_id TEXT,
        trade_mode TEXT, source_type TEXT, trade_date TEXT)""")
    con.execute("""CREATE TABLE capital_state (
        id INTEGER PRIMARY KEY, initial_capital REAL, current_equity REAL,
        peak_equity REAL, water_level TEXT, is_locked INTEGER,
        lock_reason TEXT, trade_env INTEGER)""")
    if with_sl_col:
        con.execute("INSERT INTO open_positions (symbol, direction, size, sl_broker_order_id,"
                    " tp_broker_order_id, owner, account_type, updated_at)"
                    " VALUES ('US.TEST','LONG',?,?,?,?,?,'2026-09-11')",
                    (size, sl_id, tp_id, owner, account_type))
    else:
        con.execute("INSERT INTO open_positions (symbol, direction, size, owner,"
                    " account_type, updated_at) VALUES ('US.TEST','LONG',?,?,?,'2026-09-11')",
                    (size, owner, account_type))
    if entry_oid is not None:
        con.execute("INSERT INTO trade_records (symbol, futu_order_id, trade_mode,"
                    " source_type, trade_date) VALUES ('US.TEST',?,?, 'PLAYBOOK_SIM','2026-09-11')",
                    (entry_oid, trade_mode))
    con.commit()
    con.close()
    return path


@pytest.fixture
def lanes(tmp_path, monkeypatch, preflight):
    """把探针指向临时双库, 绝不碰生产 data/."""
    sim = _mk_db(tmp_path / "sim.db", sl_id="8170259", tp_id=None,
                 entry_oid="8170259", owner="auto", account_type="sim")
    live = _mk_db(tmp_path / "live.db", sl_id=None, tp_id=None,
                  entry_oid=None, trade_mode="live", owner="manual",
                  account_type="real")
    monkeypatch.setattr(preflight, "SIM_DB", sim)
    monkeypatch.setattr(preflight, "LIVE_DB", live)
    monkeypatch.setattr(preflight, "DATA", tmp_path)
    monkeypatch.setattr(preflight, "OUT_DIR", tmp_path / "cutover")
    return sim, live


# ─────────────────────────────────────────────────────────────
# 1. 只读性 (AST/源码级)
# ─────────────────────────────────────────────────────────────
WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE TABLE",
               "CREATE UNIQUE", "CREATE INDEX", "REPLACE INTO")
EXEC_METHODS = {"execute", "executescript", "executemany"}


def _executed_sql_literals(path: Path) -> list[str]:
    """AST 抽取: 只取真正传给 conn.execute*() 的**字面量 SQL**.

    为什么不用全文 grep: 探针的 why/fix 散文里合法地出现 "ALTER"/"CREATE TABLE"
    等词 (在解释病灶), 全文匹配会把说明文字误判成写操作 ⇒ 钉子恒红、失去判读价值。
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name not in EXEC_METHODS or not node.args:
            continue
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            out.append(a0.value)
        elif isinstance(a0, ast.JoinedStr):  # f-string: 取其字面片段
            out.append("".join(v.value for v in a0.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str)))
    return out


def test_preflight_executes_no_write_sql():
    """preflight 是纯只读工具: 任何传给 execute*() 的 SQL 都不得含写动词."""
    sqls = _executed_sql_literals(SCRIPTS / "cutover_preflight.py")
    assert sqls, "抽不到任何 SQL — AST 钉子失效 (探针改名/换写法了?)"
    bad = [(v, s.strip()[:90]) for s in sqls for v in WRITE_VERBS if v in s.upper()]
    assert not bad, f"preflight 对生产库执行写操作: {bad}"


def test_preflight_never_opens_writable_connection():
    """preflight 里每个 sqlite3.connect 都必须是 mode=ro URI (双保险: 动词钉之外的连接钉)."""
    src = (SCRIPTS / "cutover_preflight.py").read_text(encoding="utf-8")
    assert "sqlite3.connect(" in src
    for line in src.splitlines():
        if "sqlite3.connect(" in line:
            assert "mode=ro" in line or "_ro(" in line, f"出现可写连接: {line.strip()}"


def test_rehearse_touches_production_only_by_copy():
    """彩排脚本确实含 CREATE/DELETE/ALTER —— 但只许作用于沙箱副本.

    钉法: 唯一接触生产路径的 `_copy_db` 只能 copy2 + 对**副本**checkpoint,
    且全模块的写动词 SQL 只允许出现在明确以沙箱变量为目标的函数里。
    """
    src = (SCRIPTS / "cutover_rehearse.py").read_text(encoding="utf-8")
    body = src.split("def _copy_db(", 1)[1].split("\ndef ", 1)[0]
    assert "shutil.copy2(src, dst)" in body, "_copy_db 必须用 copy2 复制, 不得就地改源库"
    for v in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert v not in body.upper(), f"_copy_db 内出现写动词 {v}"
    # 生产库路径常量只允许出现在 _copy_db 的实参位与 main 的副本构造处
    assert src.count("SIM_DB") <= 4 and src.count("LIVE_DB") <= 4, (
        "生产库常量引用点增多 — 每新增一处都要复核它是否只读")


def test_ro_helper_uses_readonly_uri(preflight):
    import inspect
    src = inspect.getsource(preflight._ro)
    assert "mode=ro" in src, "_ro 必须以只读 URI 连接"
    assert "FileNotFoundError" in src, "库缺失必须抛错, 不得静默建空库 (建空库=把'没数据'伪装成'数据干净')"


def test_ro_raises_and_creates_nothing_on_missing_db(preflight, tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        preflight._ro(missing)
    assert not missing.exists(), "只读连接不得创建库文件"


# ─────────────────────────────────────────────────────────────
# 2. sl_broker_order_id 探针: 跨表铁证, 双向钉
# ─────────────────────────────────────────────────────────────
def test_detects_entry_id_pollution_by_cross_table_proof(preflight, lanes):
    sim, live = lanes
    rep = preflight.Report()
    preflight.p5_sl_semantics(rep)
    item = next(i for i in rep.items if i["id"] == "P5")
    assert item["status"] == "warn", f"应报污染, 实得 {item['status']}: {item['reading']}"
    pol = item["evidence"]["sim"]["polluted"]
    assert [p["symbol"] for p in pol] == ["US.TEST"]
    assert pol[0]["value"] == "8170259"


def test_no_false_positive_when_sl_id_differs(preflight, tmp_path, monkeypatch):
    """sl 列装的是**真 SL 单号** (≠ 入场单号) 时必须判 ok —— 否则探针恒红失去意义."""
    sim2 = _mk_db(tmp_path / "sim2.db", sl_id="FH1CB1B96E52BF2000", tp_id="FH1TP0001",
                  entry_oid="8170259", owner="auto")
    monkeypatch.setattr(preflight, "LIVE_DB", tmp_path / "absent.db")
    monkeypatch.setattr(preflight, "SIM_DB", sim2)
    rep = preflight.Report()
    preflight.p5_sl_semantics(rep)
    item = next(i for i in rep.items if i["id"] == "P5")
    assert item["status"] == "ok", f"真 SL 单号被误判为污染: {item['reading']}"
    assert item["evidence"]["sim"]["genuine"][0]["value"] == "FH1CB1B96E52BF2000"


def test_sl_probe_tolerates_missing_column(preflight, tmp_path, monkeypatch):
    """无 sl_broker_order_id 列的旧库不得让探针崩 (切换预检必须能在异形库上跑完)."""
    sim = _mk_db(tmp_path / "s.db", with_sl_col=False)
    live = _mk_db(tmp_path / "l.db", with_sl_col=False)
    monkeypatch.setattr(preflight, "SIM_DB", sim)
    monkeypatch.setattr(preflight, "LIVE_DB", live)
    rep = preflight.Report()
    preflight.p5_sl_semantics(rep)
    assert not rep.items or rep.items[0]["status"] in ("ok", "na")


# ─────────────────────────────────────────────────────────────
# 3. 半翻态 / 混同 / 僵尸行探针
# ─────────────────────────────────────────────────────────────
def _write_env(tmp_path, monkeypatch, preflight, **kv):
    p = tmp_path / ".env.us"
    p.write_text("".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    return p


def test_half_flip_is_block(tmp_path, monkeypatch, preflight):
    """broker 真钱 + 账本仍进 sim 库 = 半翻态, 必须 block (RFC-080 S2' 拦的就是这个)."""
    _write_env(tmp_path, monkeypatch, preflight, TRADE_ENV="1",
               AUTO_TRADE_ACCOUNT="real", ALLOW_REAL_TRADING="false")
    rep = preflight.Report()
    preflight.p0_env_triple(rep)
    item = next(i for i in rep.items if i["id"] == "P0")
    assert item["status"] == "block"
    assert item["evidence"]["half_flip"] is True


def test_reverse_half_flip_is_block(tmp_path, monkeypatch, preflight):
    """账本进 live 库但 broker 仍 sim = 反向半翻 (记录身份与成交身份分裂), 同样 block."""
    _write_env(tmp_path, monkeypatch, preflight, TRADE_ENV="0",
               AUTO_TRADE_ACCOUNT="sim", ALLOW_REAL_TRADING="true")
    rep = preflight.Report()
    preflight.p0_env_triple(rep)
    item = next(i for i in rep.items if i["id"] == "P0")
    assert item["status"] == "block"
    assert item["evidence"]["half_flip_rev"] is True


def test_consistent_all_sim_is_ok(tmp_path, monkeypatch, preflight):
    _write_env(tmp_path, monkeypatch, preflight, TRADE_ENV="1",
               AUTO_TRADE_ACCOUNT="sim", ALLOW_REAL_TRADING="false",
               FTSEEK_DASHBOARD_DB_ISOLATION="1")
    rep = preflight.Report()
    preflight.p0_env_triple(rep)
    item = next(i for i in rep.items if i["id"] == "P0")
    assert item["status"] == "ok", item["reading"]


def test_contamination_block_and_clean_ok(preflight, tmp_path, monkeypatch):
    # 夹具前提: 必须传 entry_oid 才会插 trade_records 行 (否则空表 ⇒ 探针"0 行"是真空而非漏报)
    sim = _mk_db(tmp_path / "s.db", trade_mode="simulate", entry_oid="S1")
    live = _mk_db(tmp_path / "l.db", trade_mode="simulate", entry_oid="L1")  # live 库现 simulate 行 = 混同
    monkeypatch.setattr(preflight, "SIM_DB", sim)
    monkeypatch.setattr(preflight, "LIVE_DB", live)
    rep = preflight.Report()
    preflight.p7_contamination(rep)
    assert rep.items[0]["status"] == "block", rep.items[0]["reading"]
    assert rep.items[0]["evidence"]["live"][0]["symbol"] == "US.TEST"

    live2 = _mk_db(tmp_path / "l2.db", trade_mode="live", entry_oid="L2")
    monkeypatch.setattr(preflight, "LIVE_DB", live2)
    rep2 = preflight.Report()
    preflight.p7_contamination(rep2)
    assert rep2.items[0]["status"] == "ok"

    # sim 库出现 trade_mode='live' 行 (反向混同) 同样必须 block
    sim2 = _mk_db(tmp_path / "s2.db", trade_mode="live", entry_oid="S3")
    monkeypatch.setattr(preflight, "SIM_DB", sim2)
    rep3 = preflight.Report()
    preflight.p7_contamination(rep3)
    assert rep3.items[0]["status"] == "block"
    assert rep3.items[0]["evidence"]["sim"][0]["trade_mode"] == "live"


def test_zombie_rows_flagged_and_overlap_blocks(preflight, tmp_path, monkeypatch):
    """size=0 僵尸行 → warn; 两库同标的重叠 → block (单库翻转后立刻撞 PK=symbol)."""
    sim = _mk_db(tmp_path / "s.db", size=10.0, owner="auto")
    live = _mk_db(tmp_path / "l.db", size=0.0, owner="manual", account_type="real")
    monkeypatch.setattr(preflight, "SIM_DB", sim)
    monkeypatch.setattr(preflight, "LIVE_DB", live)
    rep = preflight.Report()
    preflight.p4_open_positions(rep)
    item = rep.items[0]
    assert item["status"] == "block", f"同标的 US.TEST 重叠应 block: {item['reading']}"
    assert item["evidence"]["overlap"] == ["US.TEST"]
    assert item["evidence"]["live"]["zombie"] == ["US.TEST"]


# ─────────────────────────────────────────────────────────────
# 4. 彩排: 方向回归钉 + 沙箱护栏 + 白名单防腐
# ─────────────────────────────────────────────────────────────
def test_diff_direction_reports_target_gaps(rehearse, tmp_path):
    """回归钉: `_diff(参照, 目标)` 必须报"目标缺什么".

    开发期真实犯过的错 —— 把 --to live 的目标库复制成 sim 库, 于是 diff 恒 0 缺口、
    工具假绿。这里用两列差异明确的方向性夹具把语义钉死。
    """
    ref = tmp_path / "ref.db"     # 参照 (schema 完整侧)
    tgt = tmp_path / "tgt.db"     # 目标 (缺列侧)
    con = sqlite3.connect(ref)
    con.execute("CREATE TABLE t (a TEXT, b TEXT, c TEXT)")
    con.commit(); con.close()
    con = sqlite3.connect(tgt)
    con.execute("CREATE TABLE t (a TEXT)")
    con.commit(); con.close()
    d = rehearse._diff(ref, tgt)
    assert d["missing_columns"] == {"t": ["b", "c"]}, d
    assert d["n_missing_columns"] == 2
    # 反向必须报 0 (tgt 没有 ref 缺的东西) —— 钉住方向不可混用
    assert rehearse._diff(tgt, ref)["n_missing_columns"] == 0


def test_rehearse_direction_wiring_anchor(rehearse):
    """接线锚点钉: `--to live` 的目标库必须是 **live**, 缺口必须是 `_diff(参照=sim, 目标)`.

    上一条钉的是 `_diff` 的**语义**, 钉不到 main() 里的**接线** —— 而开发期真实犯的错
    恰恰是接线错 (目标库复制成了 sim ⇒ 缺口恒 0 ⇒ 工具假绿)。故再钉源码锚点。
    """
    import inspect
    src = inspect.getsource(rehearse.main)
    assert "src, other = LIVE_DB, SIM_DB" in src, (
        "--to live 的目标库不再是 LIVE_DB — 方向接线被改, 工具会假绿")
    # 必须带赋值前缀: main() 里 before/after 各有一次 _diff, 只查裸子串的话
    # 反转其中一处仍会被另一处"顶包"命中 ⇒ 变异测试实测存活过 (刀1), 故钉到唯一串。
    for stmt in ("before = _diff(other, tgt)", "after = _diff(other, tgt)"):
        assert stmt in src, (
            f"缺口计算必须用 (参照, 目标) 顺序, 缺锚点 `{stmt}`; "
            "反了会报'目标多出的列'而非'目标缺的列' ⇒ 工具假绿")


def test_rehearse_refuses_sandbox_under_data_dir(rehearse, monkeypatch, tmp_path):
    """沙箱落在 data/ 下 = 可能把副本当生产库写 ⇒ 必须硬拒 (退出码 2)."""
    monkeypatch.setattr(rehearse, "SIM_DB", tmp_path / "s.db")
    monkeypatch.setattr(rehearse, "LIVE_DB", tmp_path / "l.db")
    (tmp_path / "s.db").write_bytes(b"")
    monkeypatch.setattr(rehearse, "DATA", tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(sys, "argv", ["cutover_rehearse.py", "--to", "live",
                                      "--sandbox", str(tmp_path / "data" / "sbx")])
    assert rehearse.main() == 2


def test_copy_db_carries_wal(rehearse, tmp_path):
    """只拷主库会丢 WAL 里未 checkpoint 的行 ⇒ 副本必须与源同内容."""
    src = tmp_path / "src.db"
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (a TEXT)")
    con.execute("INSERT INTO t VALUES ('in-wal')")
    con.commit()
    assert (tmp_path / "src.db-wal").exists(), "夹具前提: WAL 里确有未 checkpoint 数据"
    con.close()
    dst = tmp_path / "dst.db"
    rehearse._copy_db(src, dst)
    got = sqlite3.connect(f"file:{dst}?mode=ro", uri=True).execute("SELECT a FROM t").fetchall()
    assert got == [("in-wal",)], f"副本丢行: {got}"


def test_benign_allowlist_has_live_source_anchors(rehearse):
    """白名单防腐: 每一项都必须有**当下仍成立**的源码锚点.

    白名单最危险的失效方式是静默过期 (有人开始引用 account_id / 给 close_retry_state
    建了表), 那时"良性"就不再良性, 而工具会继续放过它。
    """
    cs_src = (ROOT / "app" / "risk" / "capital_safety.py").read_text(encoding="utf-8")
    assert ("capital_state", "account_id") in rehearse.BENIGN_MISSING_COLUMNS
    assert "account_id" not in cs_src, (
        "capital_safety.py 开始引用 account_id ⇒ 该列不再是死列, "
        "必须从 BENIGN_MISSING_COLUMNS 移除并重新评估")

    for t in ("cb_shadow_blocked", "cb_shadow_daily"):
        assert t in rehearse.BENIGN_MISSING_TABLES
        shadow = (ROOT / "scripts" / "intraday_loss_breaker_shadow.py").read_text(encoding="utf-8")
        assert f"CREATE TABLE IF NOT EXISTS {t}" in shadow, f"{t} 的自建锚点消失"

    assert "close_retry_state" in rehearse.BENIGN_MISSING_TABLES
    live_proc = (ROOT / "app" / "pipeline" / "order_executor.py").read_text(encoding="utf-8")
    assert "_close_retry_state" in live_proc, "进程内重试态锚点消失"


def test_benign_tables_retired_anchor(rehearse):
    """3 张"退役死表"必须仍有 trade_store 的主动 DROP 锚点.

    ⚠️ 反向含义 (彩排实证): --to live 时 TradeStore._init_db 会在**真钱库副本**上
    真的执行这 2 条 DROP (daemon_heartbeat / entry_context_snapshot 实测被删)。
    若哪天 DROP 被撤掉, "良性"的前提就变了 ⇒ 必须让本钉红。
    """
    ts = (ROOT / "app" / "storage" / "trade_store.py").read_text(encoding="utf-8")
    for t in ("daemon_heartbeat", "entry_context_snapshot"):
        assert t in rehearse.BENIGN_MISSING_TABLES
        assert f"DROP TABLE IF EXISTS {t}" in ts, f"trade_store 不再 DROP {t} — 白名单前提失效"
    assert "option_positions" in rehearse.BENIGN_MISSING_TABLES
    assert "option_positions 表已删除" in ts, "option_positions 退役说明锚点消失"


def test_benign_tables_selfcreate_and_orphan_anchors(rehearse):
    """另 8 张: 4 张必须有 CREATE IF NOT EXISTS 自建锚点, 4 张必须全仓零引用 (孤儿)."""
    selfcreate = {"live_deal_records": "app/web_dashboard/live_ledger.py",
                  "live_position_snapshots": "app/web_dashboard/live_ledger.py",
                  "live_round_trips": "app/web_dashboard/live_ledger.py",
                  "thesis_challenges": "app/llm/thesis_challenge.py"}
    for t, rel in selfcreate.items():
        assert t in rehearse.BENIGN_MISSING_TABLES
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert f"CREATE TABLE IF NOT EXISTS {t}" in src, f"{t} 的自建锚点在 {rel} 消失"

    orphans = ("option_peak_profits", "performance_stats",
               "shadow_daily_stats", "shadow_schema_version")
    corpus = []
    for base in ("app", "scripts"):
        corpus += [p for p in (ROOT / base).rglob("*.py")
                   if p.name != "cutover_rehearse.py" and "archive" not in p.parts]
    assert corpus, "语料为空 — 扫描路径错了, 本钉会假绿"
    for t in orphans:
        assert t in rehearse.BENIGN_MISSING_TABLES
        readers = [str(p.relative_to(ROOT)) for p in corpus
                   if t in p.read_text(encoding="utf-8", errors="replace")]
        assert not readers, f"{t} 出现引用方 {readers} ⇒ 不再是孤儿表, 须移出白名单重评"


def test_capital_roundtrip_probe_flags_lost_baseline(rehearse, tmp_path):
    """回切必须恢复本臂基线；禁止退回“全局最新 id”捞行."""
    db = tmp_path / "cap.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE capital_state (id INTEGER PRIMARY KEY, initial_capital REAL,"
                " current_equity REAL, peak_equity REAL, cash REAL, market_value REAL,"
                " buying_power REAL, drawdown_pct REAL, max_drawdown_pct REAL,"
                " water_level TEXT, is_locked INTEGER, lock_reason TEXT,"
                " last_update TEXT, trade_env INTEGER DEFAULT 1)")
    con.commit(); con.close()
    r = rehearse.probe_capital_roundtrip(db)
    assert r["seamless_roundtrip"] is True
    assert r["load_state_result"]["peak_equity"] == 99127.14
    assert r["load_state_result"]["is_locked"] is True


def test_report_worst_severity_and_exit_contract(preflight):
    rep = preflight.Report()
    rep.add("X", "n", "ok", "r")
    assert rep.worst == "ok" and rep.blocked is False
    rep.add("Y", "n", "warn", "r")
    assert rep.worst == "warn" and rep.blocked is False
    rep.add("Z", "n", "block", "r")
    assert rep.worst == "block" and rep.blocked is True
    with pytest.raises(ValueError):
        rep.add("W", "n", "bogus", "r")


# ─────────────────────────────────────────────────────────────
# 5. 棘轮哨兵 (--watch): 只对状态跃迁告警, 常驻项静默
# ─────────────────────────────────────────────────────────────
def _rep(pairs):
    """构造只含指定 (id, status) 的 Report, 用于棘轮比对."""
    r = preflight_mod().Report()
    for pid, st in pairs:
        r.add(pid, f"name-{pid}", st, f"reading-{pid}")
    return r


def preflight_mod():
    return _load("cutover_preflight")


def test_statuses_compares_status_not_reading():
    """棘轮只比 status, 不比 reading —— P1 的 ic_t3 / P8 的行数每天都漂, 比原文会天天告警."""
    pf = preflight_mod()
    r = pf.Report()
    r.add("P1", "毕业门禁", "block", "ic_t3=-0.0155")
    r.add("P8", "研究臂", "warn", "2151 行")
    assert r.statuses() == {"P1": "block", "P8": "warn"}
    r2 = pf.Report()
    r2.add("P1", "毕业门禁", "block", "ic_t3=-0.0999")   # 读数大变
    r2.add("P8", "研究臂", "warn", "2600 行")
    assert r2.statuses() == r.statuses(), "status 未变 ⇒ 棘轮必须判为无跃迁"


def test_diff_classifies_four_transition_kinds():
    pf = preflight_mod()
    base = {"P0": "ok", "P1": "block", "P2": "warn", "P9": "warn"}
    cur = _rep([("P0", "block"),    # 升级 ok→block
                ("P1", "ok"),       # 改善 block→ok
                ("P2", "warn"),     # 不变
                ("P7", "ok")])      # 新探针 (P9 消失)
    d = pf.diff_against_baseline(cur, base)
    assert [x["id"] for x in d["escalated"]] == ["P0"]
    assert [x["id"] for x in d["improved"]] == ["P1"]
    assert [x["id"] for x in d["new"]] == ["P7"]
    assert [x["id"] for x in d["gone"]] == ["P9"]
    assert d["escalated"][0]["from"] == "ok" and d["escalated"][0]["to"] == "block"


def test_diff_severity_ordering_is_not_alphabetical():
    """钉住 _SEV 的**严重度序**: block > warn > na > ok.

    `na` 刻意排在 `ok` **之上** (不是笔误): na = "没测/跳过" (如 --skip-gate)。
    若 na < ok, 那么给 cron 悄悄加上 --skip-gate 就会让探针从 ok 变 na 而**被判为改善**
    ⇒ 告警被无声关掉。排成 na > ok 后, "测了通过"→"没测" 是**升级**, 会告警。
    """
    pf = preflight_mod()
    assert pf._SEV["block"] > pf._SEV["warn"] > pf._SEV["na"] > pf._SEV["ok"]
    # ok→na (开始跳过检查) 必须报**升级**, 不能报改善
    d = pf.diff_against_baseline(_rep([("P1", "na")]), {"P1": "ok"})
    assert [x["id"] for x in d["escalated"]] == ["P1"], "跳过检查被当成改善 = 告警可被无声关掉"
    assert not d["improved"]
    # na→ok (恢复实算) 才是改善
    d2 = pf.diff_against_baseline(_rep([("P1", "ok")]), {"P1": "na"})
    assert [x["id"] for x in d2["improved"]] == ["P1"]
    # warn→block 升级 / block→warn 改善
    assert pf.diff_against_baseline(_rep([("P4", "block")]), {"P4": "warn"})["escalated"]
    assert pf.diff_against_baseline(_rep([("P4", "warn")]), {"P4": "block"})["improved"]


def test_accept_baseline_roundtrip_and_watch_never_autowrites(tmp_path, monkeypatch):
    """基线冻结只能是人工动作: --watch 绝不自动写基线 (否则棘轮会自我合法化)."""
    pf = preflight_mod()
    bl = tmp_path / "preflight_baseline.json"
    monkeypatch.setattr(pf, "BASELINE", bl)
    monkeypatch.setattr(pf, "OUT_DIR", tmp_path)
    assert pf.load_baseline() == {}                     # 无基线 → 空
    keys = pf.accept_baseline(_rep([("P0", "ok"), ("P1", "block")]))
    assert keys == {"P0": "ok", "P1": "block"}
    assert bl.exists()
    loaded = pf.load_baseline()
    assert loaded == {"P0": "ok", "P1": "block"}        # 回读一致
    doc = json.loads(bl.read_text(encoding="utf-8"))
    assert "人工动作" in doc["rule"] and "永不自动写基线" in doc["rule"]


def test_watch_fails_loud_without_baseline(tmp_path, monkeypatch):
    """无基线时 --watch 必须 exit=2 + 告警, 不得静默 (静默=把"没在哨"伪装成"哨了没事")."""
    pf = preflight_mod()
    monkeypatch.setattr(pf, "BASELINE", tmp_path / "absent.json")
    monkeypatch.setattr(pf, "OUT_DIR", tmp_path)
    monkeypatch.setattr(pf, "DATA", tmp_path)
    for fn in ("p0_env_triple", "p1_graduation_gate", "p2_schema_drift", "p3_capital_state",
               "p4_open_positions", "p5_sl_semantics", "p6_stop_mileage", "p7_contamination",
               "p8_research_arm", "p9_ledger_continuity"):
        monkeypatch.setattr(pf, fn, lambda *a, **k: None)
    monkeypatch.setattr(pf.Report, "add",
                        lambda self, pid, name, status, reading, **k: self.items.append(
                            {"id": pid, "name": name, "status": status, "reading": reading,
                             "why": "", "fix": "", "evidence": {}}))
    pushed = []
    monkeypatch.setattr(pf, "push_alert", lambda msg, push=False: pushed.append(msg) or "dry")
    monkeypatch.setattr(sys, "argv", ["cutover_preflight.py", "--watch", "--no-write"])
    # 探针被 stub 成 no-op ⇒ Report 为空; 但"无基线"这条必须先于比对触发
    assert pf.main() == 2
    assert pushed and "基线未冻结" in pushed[0]


def test_watch_silent_when_unchanged(tmp_path, monkeypatch):
    pf = preflight_mod()
    monkeypatch.setattr(pf, "BASELINE", tmp_path / "b.json")
    monkeypatch.setattr(pf, "OUT_DIR", tmp_path)
    monkeypatch.setattr(pf, "DATA", tmp_path)
    pf.accept_baseline(_rep([("P0", "ok"), ("P1", "block")]))
    monkeypatch.setattr(pf, "load_baseline", lambda: {"P0": "ok", "P1": "block"})
    stub = _rep([("P0", "ok"), ("P1", "block")])
    monkeypatch.setattr(pf.Report, "statuses", lambda self: stub.statuses())
    pushed = []
    monkeypatch.setattr(pf, "push_alert", lambda msg, push=False: pushed.append(msg) or "dry")
    for fn in ("p0_env_triple", "p1_graduation_gate", "p2_schema_drift", "p3_capital_state",
               "p4_open_positions", "p5_sl_semantics", "p6_stop_mileage", "p7_contamination",
               "p8_research_arm", "p9_ledger_continuity"):
        monkeypatch.setattr(pf, fn, lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["cutover_preflight.py", "--watch", "--no-write"])
    assert pf.main() == 0, "与基线一致必须静默 exit=0 (否则天天红 = 告警疲劳 = 真回退没人看)"
    assert not pushed, "无跃迁不得推送"


def test_missing_lane_is_recorded_not_silently_skipped(preflight, monkeypatch, tmp_path):
    """库文件缺失时必须**留痕**, 不得静默跳臂 (2026-09-15, R5 同族病灶).

    钉什么: 诊断器最坏的失效形态是「数据源打不开」与「查了没查出东西」**输出同形** ——
    库被改名/删除时 P3-P9 会集体报"干净" = 假绿。`_open_lane()` 把跳过写进
    `SKIPPED_SOURCES` 并由 main() 汇总显式打印, 本钉双向钉住:
      阳性: 指向不存在的库 ⇒ 必须留痕 (缺库不报 = 钉子红);
      阴性: 库在位 ⇒ 不得误留痕 (恒留痕 = 噪声, 同样红)。
    只断言"不崩"的钉子会被"永远 continue"这种最蠢的变异白拿。
    """
    probes = ["p3_capital_state", "p4_open_positions", "p5_sl_semantics",
              "p6_stop_mileage", "p7_contamination", "p9_ledger_continuity"]

    monkeypatch.setattr(preflight, "LIVE_DB", tmp_path / "definitely_absent.db")
    for name in probes:
        preflight.SKIPPED_SOURCES.clear()
        getattr(preflight, name)(preflight.Report())
        assert any(s.startswith("live:") for s in preflight.SKIPPED_SOURCES), (
            f"{name}: live 库缺失却未留痕 ⇒ 库被改名后该探针会报'干净'=假绿")

    # 阴性对照的替身库要带齐 6 个探针真正 SELECT 的表/列 (空表即可, 本钉只验"不误留痕")。
    # 注意 P9 **刻意不做** trade_records 表存在性守卫 (与 P7 不同) —— live 库少了
    # trade_records 是真事故, 应当冒到 main() 的 `except → return 2` 走钉钉兜底,
    # 而不是被容忍成"分臂 {}"这种看着合理的错读数 (见 test_probe_crash_returns_two…)。
    present = tmp_path / "present_live.db"
    con = sqlite3.connect(present)
    con.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT,"
                " trade_mode TEXT, source_type TEXT, futu_order_id TEXT)")
    con.execute("CREATE TABLE open_positions (symbol TEXT PRIMARY KEY, direction TEXT,"
                " size REAL, sl_broker_order_id TEXT, tp_broker_order_id TEXT,"
                " owner TEXT, account_type TEXT, remark TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE capital_state (id INTEGER PRIMARY KEY, trade_env INTEGER,"
                " initial_capital REAL, current_equity REAL, peak_equity REAL,"
                " is_locked INTEGER, lock_reason TEXT)")
    con.commit(); con.close()
    monkeypatch.setattr(preflight, "LIVE_DB", present)
    for name in probes:
        preflight.SKIPPED_SOURCES.clear()
        getattr(preflight, name)(preflight.Report())
        assert not any(s.startswith("live:") for s in preflight.SKIPPED_SOURCES), (
            f"{name}: 库在位却误留痕 {preflight.SKIPPED_SOURCES} ⇒ 留痕退化成恒报警噪声")


def test_probe_crash_returns_two_so_dingtalk_fallback_fires(preflight, monkeypatch):
    """探针抛异常 ⇒ `main()` 必须 `return 2` —— 这是整条告警链的命门.

    退出码契约: rc=0 静默 / rc=1 有跃迁且**已推飞书** / **rc≥2 = 哨兵自己坏了**,
    cron 第 276 行只在 `rc>=2` 时走**钉钉**兜底。若把这里改成 return 1 或就地吞掉
    异常, 哨兵崩溃就**双通道全静默**(飞书没推、钉钉不触发), 而"守卫自己崩了"与
    "零违反"输出不可区分 —— 正是 DEB-20260831 R5/E13 族的失效形态。
    """
    def boom(rep):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(preflight, "p0_env_triple", lambda rep: {})
    for fn in ("p1_graduation_gate", "p2_schema_drift", "p4_open_positions",
               "p5_sl_semantics", "p6_stop_mileage", "p7_contamination",
               "p8_research_arm", "p9_ledger_continuity"):
        monkeypatch.setattr(preflight, fn, lambda *a, **k: None)
    monkeypatch.setattr(preflight, "p3_capital_state", boom)
    monkeypatch.setattr(sys, "argv", ["cutover_preflight.py", "--no-write"])
    assert preflight.main() == 2, (
        "探针崩溃未返回 2 ⇒ cron 的钉钉兜底不会触发, 哨兵坏了却无人知晓")
