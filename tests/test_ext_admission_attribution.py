#!/usr/bin/env python3
"""RFC-126 契约测试: EXT 旁路归因收口 (did 写入 + admission verdict 留痕).

钉住三件事:
  1. `_write_open_position` 把 decision_id 写入 trade_records.decision_id,
     admission verdict/reasons/fee_bps 写入 metadata_json (open_positions 不受影响);
  2. did 幂等: 同 order_id 重放不产生第二行 (既有幂等语义 + did 保持);
  3. `_execute` 源码契约: did 格式 EXT-<date>-<symbol>-<session>, admission
     evaluate 失败绝不阻成交 (record-only), initial_sl 先于 admission 计算;
  4. observe_nightly_supply 段4: EXT- 前缀 did = 归因成功 (不 P1),
     verdict=REJECT → P2 人工复核, 仍空 did → P1 不变。
"""
import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_entry_executor as executor  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


observer = _load("observe_nightly_supply_t", "scripts/observe_nightly_supply.py")


_OP_COLS = ("position_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, size REAL, "
            "avg_entry_price REAL, entry_time TEXT, strategy_id TEXT, source TEXT, "
            "owner_strategy TEXT, owner TEXT, mode TEXT, account_type TEXT, "
            "trader_type TEXT, is_filled INTEGER, version INTEGER, updated_at TEXT, "
            "metadata_json TEXT, sync_status TEXT, initial_sl REAL, current_sl REAL, "
            "stop_loss_price REAL")
_TR_COLS = ("id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT, "
            "entry_time TEXT, entry_price REAL, quantity REAL, source_type TEXT, "
            "trade_mode TEXT, mode TEXT, owner_strategy TEXT, strategy_id TEXT, "
            "futu_order_id TEXT, metadata_json TEXT, created_at TEXT, exit_price REAL, "
            "exit_time TEXT, config_version TEXT, entry_session TEXT, decision_id TEXT, "
            "realized_pnl REAL")


@pytest.fixture()
def ext_db(tmp_path, monkeypatch):
    path = tmp_path / "ext.db"
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE open_positions ({_OP_COLS})")
    conn.execute(f"CREATE TABLE trade_records ({_TR_COLS})")
    conn.commit()
    conn.close()
    monkeypatch.setattr(executor, "DB", str(path))
    return path


def _write(ext_db, symbol="US.ACN", side="SHORT", order_id="8219132",
           decision_id="EXT-2026-10-01-US.ACN-premarket", admission_meta=None):
    return executor._write_open_position(
        symbol, 23, 212.53, 212.0, "premarket",
        {"surprise_pct": 2.36, "period": "2026-09-30"}, 15.9, 900000,
        buy_order_id=order_id, gate_mode="strict", side=side,
        decision_id=decision_id, admission_meta=admission_meta,
    )


def _trade_row(ext_db):
    conn = sqlite3.connect(f"file:{ext_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM trade_records").fetchone())
    finally:
        conn.close()


# ── 1. did + admission 留痕落库 ──────────────────────────────────

def test_write_position_persists_decision_id(ext_db):
    did = "EXT-2026-10-01-US.ACN-premarket"
    _write(ext_db, decision_id=did)
    row = _trade_row(ext_db)
    assert row["decision_id"] == did


def test_write_position_persists_admission_meta(ext_db):
    meta = {"admission_verdict": "PASS", "admission_reasons": [],
            "admission_fee_bps": 8.7}
    _write(ext_db, admission_meta=meta)
    row = _trade_row(ext_db)
    md = json.loads(row["metadata_json"])
    assert md["admission_verdict"] == "PASS"
    assert md["admission_fee_bps"] == 8.7
    assert md["decision_id"] == "EXT-2026-10-01-US.ACN-premarket"
    # 既有键不受影响 (盘中退出监控依赖 stop_loss)
    assert md["stop_loss"] and md["stop_source"] == "rule_engine_initial_sl"


def test_write_position_persists_research_source(ext_db):
    """研究臂来源必须落库, 否则 20 日观察无法按 source 复算 (10-19 实跑 4 笔全是 unknown)。"""
    executor._write_open_position(
        "US.RSCH", 10, 100.0, 100.0, "afterhours",
        {"gate_mode": "research_momentum", "research_source": "research_watchlist",
         "late_premarket": True,
         "surprise_pct": None, "period": None, "period_age_days": None,
         "report_date": None, "report_phase": None},
        1.0, 1000, buy_order_id="rsch-1", gate_mode="research_momentum",
        decision_id="EXT-2026-10-19-US.RSCH-afterhours",
    )
    md = json.loads(_trade_row(ext_db)["metadata_json"])
    assert md["gate_mode"] == "research_momentum"
    assert md["research_source"] == "research_watchlist"
    assert md["late_premarket"] is True


def test_write_position_without_did_keeps_legacy_shape(ext_db):
    """向后兼容: 不传 did/admission 时行为与旧版一致 (NULL, 无新键崩溃)。"""
    _write(ext_db, decision_id="", admission_meta=None)
    row = _trade_row(ext_db)
    assert row["decision_id"] is None
    md = json.loads(row["metadata_json"])
    assert md["decision_id"] is None
    assert "admission_verdict" not in md


# ── 2. 幂等: 同 order_id 重放不产生第二行 ────────────────────────

def test_replay_same_order_id_is_idempotent_and_keeps_did(ext_db):
    did = "EXT-2026-10-01-US.ACN-premarket"
    _write(ext_db, decision_id=did)
    _write(ext_db, decision_id=did)  # 重放
    conn = sqlite3.connect(f"file:{ext_db}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
        d = conn.execute("SELECT decision_id FROM trade_records").fetchone()[0]
    finally:
        conn.close()
    assert n == 1 and d == did


# ── 3. _execute 源码契约 (record-only 语义钉住) ──────────────────

def test_execute_did_format_and_record_only_contract():
    import inspect
    src = inspect.getsource(executor._execute)
    # did 格式钉住 (体检脚本按 EXT- 前缀识别归因)
    assert 'ext_did = f"EXT-{_today_et()}-{symbol}-{session}"' in src
    # record-only: admission 结果只进 rec/metadata, 绝不改 direction/skip
    assert 'rec["admission"] = {"verdict": _adm["verdict"]' in src
    assert "admission_meta=admission_meta" in src
    # evaluate 失败不阻成交 (宁缺归因不误交易)
    assert 'rec["admission_error"] = str(_adm_exc)[:120]' in src
    # initial_sl 必须先于 admission 计算 (max_sl_pct 判据拿真值)
    assert src.index('rec["initial_sl"]') < src.index("_adm_evaluate")


def test_admission_verdict_is_same_source_as_main_chain():
    """EXT 留痕必须用主链同一 evaluate + 同一生产配置 (不许私配阈值)。"""
    from app.playbook.admission import evaluate, get_admission_config
    cfg = get_admission_config(refresh=True)
    assert cfg["min_notional_usd"] == 2500 and cfg["max_fee_bps"] == 11.54 \
        and cfg["min_entry_price"] == 5.0, \
        "生产配置漂移: EXT 留痕口径将失去与主链可比性"
    # ACN 10/1 实单重放 (23 股 × $212.53 = $4,888): 恢复后名义额与真实费率均过门。
    r = evaluate({"date": "2026-10-01", "symbol": "US.ACN", "direction": "SELL_SHORT",
                  "entry_price": 212.53, "position_size": 23, "stop_loss": 223.16})
    assert r["verdict"] == "PASS"
    # 更大合规规模同样 PASS。
    r_ok = evaluate({"date": "2026-10-01", "symbol": "US.ACN", "direction": "SELL_SHORT",
                     "entry_price": 212.53, "position_size": 26, "stop_loss": 223.16})
    assert r_ok["verdict"] == "PASS"
    # CTNT 9/17 灾难单重放: 统一口径必须 REJECT ($5 地板同源判据)
    r2 = evaluate({"date": "2026-09-17", "symbol": "US.CTNT", "direction": "BUY",
                   "entry_price": 0.072, "position_size": 4931, "stop_loss": 0})
    assert r2["verdict"] == "REJECT" and any("PRICE_TOO_LOW" in x for x in r2["reasons"])


# ── 4. 体检脚本段4: EXT 归因识别 ─────────────────────────────────

@pytest.fixture()
def obs_db(tmp_path):
    path = tmp_path / "obs.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row  # observer._rows 用 dict(r), 依赖 Row 工厂
    conn.execute("CREATE TABLE playbook_instructions (id INTEGER PRIMARY KEY, "
                 "date TEXT, created_at TEXT, symbol TEXT, direction TEXT, "
                 "confidence INTEGER, entry_price REAL, decision_id TEXT, "
                 "executed INTEGER, skip_reason TEXT, raw_instruction_json TEXT)")
    conn.execute(f"CREATE TABLE trade_records ({_TR_COLS})")
    conn.commit()
    return conn


def _insert_trade(conn, symbol, did, verdict=None):
    md = json.dumps({"admission_verdict": verdict}) if verdict else "{}"
    conn.execute(
        "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, "
        " entry_price, quantity, source_type, strategy_id, metadata_json, decision_id) "
        "VALUES ('2026-10-02','{}','SHORT','2026-10-02T08:10:00',212.53,23,"
        " 'PLAYBOOK_SIM','extended_hours_entry',?,?)".format(symbol), (md, did))


def test_observer_ext_did_not_p1_and_reject_is_p2(obs_db):
    _insert_trade(obs_db, "US.ACN", "EXT-2026-10-02-US.ACN-premarket", "PASS")
    _insert_trade(obs_db, "US.XYZ", "EXT-2026-10-02-US.XYZ-premarket", "REJECT")
    _insert_trade(obs_db, "US.GHOST", None)  # 仍空 did → P1 必须保留
    obs_db.commit()
    findings = []
    observer.section_single_entry(obs_db, "2026-10-02", findings)
    levels = [(lv, msg) for lv, msg in findings]
    assert not any(lv == "P1" and "US.ACN" in m for lv, m in levels), "已归因 EXT 不得 P1"
    assert any(lv == "P2" and "US.XYZ" in m for lv, m in levels), "REJECT 必须 P2"
    assert any(lv == "P1" and "US.GHOST" in m for lv, m in levels), "空 did 仍须 P1"
    obs_db.close()


def test_observer_da_did_without_instruction_row_is_p1(obs_db):
    """非 EXT did 必须能在 playbook_instructions 找到同 did 指令行 —
    空 did 之外的第二种幽灵成交形态 (dangling did) 不得静默放行。"""
    _insert_trade(obs_db, "US.DANGLING", "DA-20261002-US.DANGLING-x1")
    obs_db.execute(
        "INSERT INTO playbook_instructions (date, symbol, direction, decision_id) "
        "VALUES ('2026-10-02', 'US.OTHER', 'BUY', 'DA-20261002-US.OTHER-x2')")
    obs_db.commit()
    findings = []
    observer.section_single_entry(obs_db, "2026-10-02", findings)
    assert any(
        lv == "P1" and "幽灵归因" in msg and "US.DANGLING" in msg
        for lv, msg in findings
    ), "非 EXT did 无指令行 = 归因链断, 必须 P1"
    obs_db.close()


def test_observer_jsonl_bad_line_warns_and_skips(tmp_path, capsys):
    """坏 JSON 行不得与"无事件"同形 (诊断器静默死亡守卫)。"""
    path = tmp_path / "bad.jsonl"
    path.write_text('{"date": "2026-10-02"}\nnot-json\n', encoding="utf-8")
    rows = observer._jsonl(path)
    assert len(rows) == 1 and rows[0]["date"] == "2026-10-02"
    err = capsys.readouterr().err
    assert "1 行坏 JSON" in err


def test_observer_chase_cap_malformed_rows_are_counted(obs_db, capsys):
    """cap/entry 畸形行留痕计数, 不与合规零违例混淆。"""
    obs_db.execute(
        "INSERT INTO playbook_instructions (date, symbol, direction, entry_price, "
        "raw_instruction_json) VALUES ('2026-10-02', 'US.BAD', 'SELL_SHORT', "
        "100.0, '{\"chase_cap_price\": \"abc\"}')")
    obs_db.commit()
    findings = []
    observer.section_chase_cap(obs_db, "2026-10-02", findings)
    out = capsys.readouterr().out
    assert "cap/entry 畸形无法判定 1 条" in out
    assert not findings
    obs_db.close()


def test_observer_skew_shadow_prints_pct_not_none(tmp_path, monkeypatch, capsys):
    """影子记录键名是 pct (非 majority_pct) — 体检打印 None = 口径键错配。"""
    monkeypatch.setattr(observer, "SHADOW", tmp_path)
    (tmp_path / "skew_gate_vision.jsonl").write_text(
        json.dumps({
            "ts": "2026-10-02T13:05:41", "date": "2026-10-02",
            "batch_vision": {"buy": 2, "short": 0, "pct": 1.0,
                             "violates": True},
            "portfolio_vision": {"buy": 6, "short": 2, "pct": 0.75,
                                 "violates": False},
            "divergent": True,
        }) + "\n", encoding="utf-8")
    observer.section_shadows("2026-10-02", [])
    out = capsys.readouterr().out
    assert "batch=2B/0S=1.0" in out
    assert "batch=2B/0S=None" not in out
