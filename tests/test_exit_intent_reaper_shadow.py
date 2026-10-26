"""出场意图 reaper 影子契约 (RFC-168 方案 E, 2026-10-26)

钉住三件事:

  1. **检出条件**: exit_intents.state='FILLED_UNBOOKED' 且成交台账已落账
     (owner_json.db_path + trade_id 交叉核对) 且已超观察窗。
  2. **只检测不改状态**: 影子期不得调用 ack_booked / 不得改 exit_intents ——
     自动补 ack 会改写出场状态机, 须另立提案。
  3. **无料不推**: 无检出时不落影子文件、不推告警。
"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

import scripts.exit_intent_reaper as reaper


def _make_ledger(tmp_path: Path, intents) -> Path:
    """构造一个 exit_intents 账本。"""
    db = tmp_path / "playbook_exits.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE exit_intents (intent_id TEXT PRIMARY KEY, symbol TEXT, "
        "direction TEXT, quantity REAL, owner_json TEXT, state TEXT, "
        "created_at REAL, updated_at REAL)"
    )
    for row in intents:
        conn.execute(
            "INSERT INTO exit_intents VALUES (?,?,?,?,?,?,?,?)", row
        )
    conn.commit()
    conn.close()
    return db


def _make_trade_db(tmp_path: Path, trade_id, exit_time) -> Path:
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, "
        "exit_time TEXT, exit_price REAL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?)",
        (trade_id, "US.TEST", exit_time, 100.0),
    )
    conn.commit()
    conn.close()
    return db


def _owner(db_path: Path, trade_id) -> str:
    return json.dumps({
        "db_path": str(db_path), "trade_id": trade_id,
        "symbol": "US.TEST", "direction": "LONG", "quantity": 10.0,
    })


def _intent(intent_id, owner_json, state="FILLED_UNBOOKED", age_s=3600):
    now = time.time()
    return (intent_id, "US.TEST", "LONG", 10.0, owner_json, state,
            now - age_s, now - age_s)


# ── 检出 ────────────────────────────────────────────────────────────────────

def test_detects_booked_fill_without_ack(tmp_path):
    """成交已落账 + intent 仍 FILLED_UNBOOKED + 超龄 ⇒ 检出。"""
    trade_db = _make_trade_db(tmp_path, 1489, "2026-10-26T09:00:00")
    ledger = _make_ledger(tmp_path, [_intent("I-1", _owner(trade_db, 1489))])

    findings = reaper.detect(ledger, time.time())
    assert len(findings) == 1
    assert findings[0]["intent_id"] == "I-1"
    assert findings[0]["action"] == "detected_only"


def test_does_not_detect_when_trade_not_closed(tmp_path):
    """台账未落账 ⇒ 在途, 不是账实不符。"""
    trade_db = _make_trade_db(tmp_path, 1489, None)
    ledger = _make_ledger(tmp_path, [_intent("I-1", _owner(trade_db, 1489))])

    assert reaper.detect(ledger, time.time()) == []


def test_does_not_detect_within_stale_window(tmp_path):
    """刚落账可能正在 ack ⇒ 观察窗内不检出。"""
    trade_db = _make_trade_db(tmp_path, 1489, "2026-10-26T09:00:00")
    ledger = _make_ledger(tmp_path, [_intent("I-1", _owner(trade_db, 1489), age_s=10)])

    assert reaper.detect(ledger, time.time()) == []


def test_does_not_detect_booked_intents(tmp_path):
    """BOOKED 是正常终态, 不得检出。"""
    trade_db = _make_trade_db(tmp_path, 1489, "2026-10-26T09:00:00")
    ledger = _make_ledger(
        tmp_path, [_intent("I-1", _owner(trade_db, 1489), state="BOOKED")]
    )

    assert reaper.detect(ledger, time.time()) == []


# ── 影子边界: 不改状态 ─────────────────────────────────────────────────────

def test_shadow_mode_does_not_modify_intent_state(tmp_path):
    """影子期**绝不**改 exit_intents —— 自动补 ack 会改写出场状态机, 须另立提案。"""
    trade_db = _make_trade_db(tmp_path, 1489, "2026-10-26T09:00:00")
    ledger = _make_ledger(tmp_path, [_intent("I-1", _owner(trade_db, 1489))])

    reaper.detect(ledger, time.time())

    conn = sqlite3.connect(ledger)
    try:
        state = conn.execute(
            "SELECT state FROM exit_intents WHERE intent_id='I-1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert state == "FILLED_UNBOOKED", "影子模式不得改写 intent 状态"


def test_shadow_mode_does_not_call_ack_booked():
    """硬钉: 脚本**代码**里不得出现 ack_booked 调用。

    用 AST 而非字符串匹配 —— 文档字符串/注释里说明"不调 ack_booked"是正常的,
    字符串断言会把说明文字本身误判为违规。
    """
    import ast

    tree = ast.parse(Path(reaper.__file__).read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert "ack_booked" not in called, \
        "reaper 不得调用 ack_booked (影子期只检测; 自动补 ack 须另立提案)"


# ── 无料不推 ────────────────────────────────────────────────────────────────

def test_no_findings_writes_no_shadow_file(tmp_path, monkeypatch):
    """无检出 ⇒ 不落影子文件 (无料不落)。"""
    shadow = tmp_path / "reaper.jsonl"
    monkeypatch.setattr(reaper, "SHADOW_PATH", shadow)
    monkeypatch.setattr(reaper, "_notify", lambda f: None)

    ledger = _make_ledger(tmp_path, [])
    findings = reaper.detect(ledger, time.time())
    reaper._append_shadow(findings)

    assert not shadow.exists()


def test_no_findings_sends_no_alert(tmp_path, monkeypatch):
    """无检出 ⇒ 不推告警 (无料不推)。"""
    pushed = []
    monkeypatch.setattr(reaper, "SHADOW_PATH", tmp_path / "r.jsonl")
    import app.push.hub as hub_mod
    monkeypatch.setattr(
        hub_mod.PushHub, "push",
        lambda self, msg: pushed.append(msg) or True, raising=False,
    )

    ledger = _make_ledger(tmp_path, [])
    reaper._notify(reaper.detect(ledger, time.time()))

    assert pushed == []


def test_findings_are_written_to_shadow_file(tmp_path, monkeypatch):
    """有检出 ⇒ 落影子 JSONL (含 intent_id / trade_id / age)。"""
    shadow = tmp_path / "reaper.jsonl"
    monkeypatch.setattr(reaper, "SHADOW_PATH", shadow)
    monkeypatch.setattr(reaper, "_notify", lambda f: None)

    trade_db = _make_trade_db(tmp_path, 1489, "2026-10-26T09:00:00")
    ledger = _make_ledger(tmp_path, [_intent("I-1", _owner(trade_db, 1489))])

    reaper._append_shadow(reaper.detect(ledger, time.time()))

    lines = shadow.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    item = json.loads(lines[0])
    assert item["intent_id"] == "I-1"
    assert item["trade_id"] == 1489
    assert item["action"] == "detected_only"
    assert item["age_seconds"] > 300
