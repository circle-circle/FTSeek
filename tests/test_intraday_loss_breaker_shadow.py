#!/usr/bin/env python3
"""GLM A' 熔断影子 (intraday_loss_breaker_shadow) 契约测试 (2026-09-05, #15/#17).

钉住的不变量:
  1. 预注册冻结常量: THRESHOLD=-400 / 判据期望≤-10 / n≥30 (DEB-20260904 §11.5)
  2. exit 事件流语义: 入场在亏损实现之前 → 不可被挡 (簇崩塌日形态: 同分钟簇入场挡 0 笔)
  3. tie 规则: 同刻 exit 先于 entry 结算 (exit 把累计砸穿 -400, 同刻 entry 必被挡)
  4. ET 桶位 (回归钉): 盘后带偏移戳 (20:15-04:00) 归同一 ET 日, 不错桶次日
     (2026-09-05 自审查修复: 初版取 UTC 墙钟日)
  5. 隔夜仓: 出场计入出场日的当日已实现累计
  6. 未平仓被挡笔 settled=0, 不进期望分母
  7. 幂等重建: persist 跑两遍行数不变 (PK 不炸)
  8. PYTEST_CURRENT_TEST 守卫: 测试下 main() 无 --db 时绝不写生产库/生产 JSON
  9. 毕业判据逻辑: 期望与 n 双门
"""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m = _load_script("intraday_loss_breaker_shadow")


def _mk_db(path, trades):
    """trades: (id, symbol, direction, entry_time, exit_time, pnl_net, trade_mode)"""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
        entry_time TEXT, exit_time TEXT, pnl_net REAL,
        trade_mode TEXT DEFAULT 'simulate')""")
    conn.executemany(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?)", trades)
    conn.commit()
    conn.close()
    return path


def _run(db_path):
    conn = sqlite3.connect(db_path)
    trades, skipped = m.load_trades(conn)
    daily, blocked = m.replay(trades)
    m.persist(conn, daily, blocked)
    conn.close()
    return trades, skipped, daily, blocked


ET = "-04:00"  # 9 月 EDT


def T(day, hm):
    return f"2026-09-{day}T{hm}:00{ET}"


# ── 1. 预注册冻结常量 ─────────────────────────────────────
def test_frozen_constants():
    assert m.THRESHOLD == -400.0
    assert m.GRAD_MAX_EXPECT == -10.0
    assert m.GRAD_MIN_N == 30


# ── 2. 簇崩塌日形态: 同分钟簇入场, 亏损实现恒晚 → 挡 0 笔 ──
def test_cluster_day_blocks_nothing(tmp_path):
    # 8/14 形态复刻: 9 笔 10:16 同分钟入场, 出场 10:23 起陆续亏
    trades = []
    for i in range(9):
        trades.append((i + 1, f"S{i}", "LONG", T("02", "10:16"),
                       T("02", f"1{min(i, 5)}:23"), -60.0, "simulate"))
    db = _mk_db(tmp_path / "t.db", trades)
    _, _, daily, blocked = _run(db)
    assert blocked == []                       # 可实现口径下一笔都挡不住
    assert len(daily) == 1 and daily[0]["n_blocked"] == 0
    assert daily[0]["cum_at_trigger"] <= -400  # 9×-60=-540 仍触发记录


# ── 3. 触发后入场才被挡 + 假想轨迹=真实结局 ────────────────
def test_entry_after_trigger_blocked(tmp_path):
    trades = [
        (1, "A", "LONG", T("02", "09:45"), T("02", "10:00"), -300.0, "simulate"),
        (2, "B", "LONG", T("02", "10:05"), T("02", "10:30"), -200.0, "simulate"),
        # 10:30 累计 -500 触发; 之后两笔入场被挡
        (3, "C", "LONG", T("02", "10:31"), T("02", "11:00"), -50.0, "simulate"),
        (4, "D", "LONG", T("02", "11:05"), T("02", "12:00"), +80.0, "simulate"),
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    _, _, daily, blocked = _run(db)
    assert [b["trade_record_id"] for b in blocked] == [3, 4]
    assert all(b["settled"] == 1 for b in blocked)
    assert daily[0]["n_blocked"] == 2
    assert daily[0]["blocked_pnl_net"] == 30.0   # -50+80, 假想轨迹=真实结局
    s = m.summarize(daily, blocked)
    assert s["n"] == 2 and s["blocked_expectancy"] == 15.0
    assert s["graduation_ready"] is False        # 期望 +15 不过 ≤-10


# ── 4. tie 规则: 同刻 exit 先于 entry ─────────────────────
def test_tie_exit_settles_before_entry(tmp_path):
    trades = [
        (1, "A", "LONG", T("02", "09:45"), T("02", "10:00"), -450.0, "simulate"),
        (2, "B", "LONG", T("02", "10:00"), T("02", "11:00"), -10.0, "simulate"),
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    _, _, _, blocked = _run(db)
    assert [b["trade_record_id"] for b in blocked] == [2]  # 同刻 entry 必被挡


# ── 5. ET 桶位回归钉: 盘后带偏移戳不错桶次日 ──────────────
def test_afterhours_exit_stays_same_et_day(tmp_path):
    trades = [
        # 盘后 20:15 ET 出场: 必须计入 09-02, 不得滑到 09-03
        (1, "A", "LONG", T("02", "19:50"), T("02", "20:15"), -450.0, "simulate"),
        (2, "B", "LONG", T("03", "09:45"), T("03", "10:00"), +5.0, "simulate"),
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    _, _, daily, blocked = _run(db)
    assert len(daily) == 1 and daily[0]["trade_date"] == "2026-09-02"
    assert blocked == []  # 次日首笔在当日累计触发前入场 (新日累计清零)


# ── 6. 隔夜仓出场计入出场日 ────────────────────────────────
def test_overnight_exit_counts_on_exit_day(tmp_path):
    trades = [
        (1, "A", "LONG", T("02", "15:30"), T("03", "09:45"), -450.0, "simulate"),
        (2, "B", "LONG", T("03", "10:00"), T("03", "11:00"), -20.0, "simulate"),
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    _, _, daily, blocked = _run(db)
    assert len(daily) == 1 and daily[0]["trade_date"] == "2026-09-03"
    assert [b["trade_record_id"] for b in blocked] == [2]


# ── 7. 未平仓被挡笔 settled=0 不进期望 ─────────────────────
def test_open_trade_blocked_unsettled(tmp_path):
    trades = [
        (1, "A", "LONG", T("02", "09:45"), T("02", "10:00"), -450.0, "simulate"),
        (2, "B", "LONG", T("02", "10:05"), T("02", "11:00"), -30.0, "simulate"),
        (3, "C", "LONG", T("02", "10:10"), None, None, "simulate"),  # 未平仓
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    _, _, daily, blocked = _run(db)
    assert len(blocked) == 2
    assert {b["trade_record_id"]: b["settled"] for b in blocked} == {2: 1, 3: 0}
    s = m.summarize(daily, blocked)
    assert s["n"] == 1                          # 未平仓不进分母
    assert s["blocked_expectancy"] == -30.0


# ── 8. 幂等重建 ───────────────────────────────────────────
def test_idempotent_rebuild(tmp_path):
    trades = [
        (1, "A", "LONG", T("02", "09:45"), T("02", "10:00"), -450.0, "simulate"),
        (2, "B", "LONG", T("02", "10:05"), T("02", "11:00"), -30.0, "simulate"),
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    _run(db)
    _run(db)  # 第二遍
    conn = sqlite3.connect(db)
    d = conn.execute("SELECT COUNT(*) FROM cb_shadow_daily").fetchone()[0]
    b = conn.execute("SELECT COUNT(*) FROM cb_shadow_blocked").fetchone()[0]
    conn.close()
    assert (d, b) == (1, 1)


# ── 9. PYTEST_CURRENT_TEST 守卫: 不写生产库/生产 JSON ─────
def test_pytest_guard_redirects_production(tmp_path, monkeypatch):
    fake_prod = _mk_db(tmp_path / "prod.db", [
        (1, "A", "LONG", T("02", "09:45"), T("02", "10:00"), -450.0, "simulate"),
        (2, "B", "LONG", T("02", "10:05"), T("02", "11:00"), -30.0, "simulate"),
    ])
    # 预埋哨兵行: 若守卫失效, 全量重建会删掉它
    conn = sqlite3.connect(fake_prod)
    m.persist(conn, [], [])  # 建表
    conn.execute("INSERT INTO cb_shadow_daily VALUES ('2099-01-01','SENTINEL',0,0,0,0,0,0,'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(m, "PROD_DB", fake_prod)
    monkeypatch.setattr(m, "OUT_JSON", tmp_path / "prod.json")
    assert os.environ.get("PYTEST_CURRENT_TEST")  # pytest 进程内恒真

    rc = m.main([])  # 无 --db → 守卫必须重定向
    assert rc == 0
    conn = sqlite3.connect(fake_prod)
    sentinel = conn.execute(
        "SELECT COUNT(*) FROM cb_shadow_daily WHERE trade_date='2099-01-01'").fetchone()[0]
    conn.close()
    assert sentinel == 1                        # 生产库未被重建
    assert not (tmp_path / "prod.json").exists()  # 生产 JSON 未被写


# ── 10. 显式 --db 直通 (报告段 cron 用法) ──────────────────
def test_explicit_db_writes_target(tmp_path):
    db = _mk_db(tmp_path / "t.db", [
        (1, "A", "LONG", T("02", "09:45"), T("02", "10:00"), -450.0, "simulate"),
        (2, "B", "LONG", T("02", "10:05"), T("02", "11:00"), -30.0, "simulate"),
    ])
    out = tmp_path / "o.json"
    rc = m.main(["--db", str(db), "--out", str(out)])
    assert rc == 0
    import json
    s = json.loads(out.read_text())
    assert s["n"] == 1 and s["threshold"] == -400.0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM cb_shadow_blocked").fetchone()[0] == 1
    conn.close()


# ── 11. 毕业判据双门 ──────────────────────────────────────
def test_graduation_double_gate():
    mk = lambda i, pnl: dict(trade_date="2026-09-02", trade_record_id=i,
                             settled=1, pnl_net=pnl)
    # 期望过线但 n 不足
    blocked = [mk(i, -50.0) for i in range(29)]
    assert m.summarize([], blocked)["graduation_ready"] is False
    # n 足 + 期望过线 → ready
    blocked = [mk(i, -50.0) for i in range(30)]
    assert m.summarize([], blocked)["graduation_ready"] is True
    # n 足但期望不过线 → not ready
    blocked = [mk(i, +5.0) for i in range(30)]
    assert m.summarize([], blocked)["graduation_ready"] is False


# ── 12. 坏时间戳跳过 + 非 simulate 过滤 ────────────────────
def test_garbage_ts_skipped_and_mode_filtered(tmp_path):
    trades = [
        (1, "A", "LONG", "garbage", T("02", "10:00"), -500.0, "simulate"),
        (2, "B", "LONG", T("02", "09:45"), T("02", "10:00"), -450.0, "MANUAL"),
    ]
    db = _mk_db(tmp_path / "t.db", trades)
    loaded, skipped, daily, blocked = _run(db)
    assert skipped == 1 and len(loaded) == 0   # MANUAL 被 SQL 过滤, garbage 被跳过
    assert daily == [] and blocked == []
