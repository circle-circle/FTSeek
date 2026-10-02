"""INCIDENT-002 P2-5 契约: close_key 死锁体检探针 (scripts/exit_closekey_health.py).

病灶形态 (10/2 US.ACN 生产实证): 活仓的 close_key 被 BOOKED intent 永久占用
(UNIQUE(account_id,target_env,close_key) 无 state 谓词) ⇒ ETH 重放与 RTH SL
兜底双双发不出单, 且 list_unsettled()=0 / blocked_exits 空 / orphan watch 无
触发源 ⇒ **静默期唯一哨兵就是本探针**。

钉住:
  ① 阳性: 活仓 + BOOKED 占 close_key + 年龄过窗 ⇒ 命中且告警文案带释放命令;
  ② 阴性(放行安全): 正常已平仓意图 / 年龄未过窗 / 无活仓 ⇒ 零命中零推送;
  ③ 去重: 同 intent 第二轮不再推 (审计 jsonl 跨进程去重);
  ④ dry: 不写审计不推送;
  ⑤ 载体接线: extended_hours_exit_cron.run_once 在会话门控**之前**调用 (源级钉),
     探针异常不阻塞退出链;
  ⑥ 告警走 PushHub CRITICAL/RISK_ALERT, 文案含 intent_id + 释放命令 + 「零保护」后果。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.exit_closekey_health as health


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的仓位库 + 账本库 + 审计文件 + 推送捕获。"""
    pos_db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(pos_db)
    conn.executescript("""
        CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL);
        CREATE TABLE trade_records (symbol TEXT, futu_order_id TEXT,
            exit_time TEXT, exit_price REAL);
    """)
    conn.commit()
    conn.close()

    ledger_db = tmp_path / "playbook_exits.db"
    lconn = sqlite3.connect(ledger_db)
    lconn.execute("""CREATE TABLE exit_intents (intent_id TEXT PRIMARY KEY,
        symbol TEXT, close_key TEXT, session TEXT, state TEXT, updated_at REAL)""")
    lconn.commit()
    lconn.close()

    monkeypatch.setattr(health, "_POSITION_DBS", ((pos_db, "sim"),))
    monkeypatch.setattr(health, "_ledger_path", lambda: ledger_db)
    monkeypatch.setattr(health, "AUDIT", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(health, "BOOKED_MIN_AGE_S", 180.0)
    sent = []
    monkeypatch.setattr(health, "_push", lambda text: sent.append(text) or True)
    return {"pos": pos_db, "ledger": ledger_db, "tmp": tmp_path, "sent": sent}


def _add_position(env, symbol="US.ACN", direction="SHORT", size=23.0,
                  futu_order_id="8219132", exit_time="", exit_price=0):
    conn = sqlite3.connect(env["pos"])
    conn.execute("INSERT INTO open_positions VALUES (?,?,?)",
                 (symbol, direction, size))
    conn.execute("INSERT INTO trade_records (symbol, futu_order_id, exit_time, exit_price) "
                 "VALUES (?,?,?,?)",
                 (symbol, futu_order_id, exit_time, exit_price))
    conn.commit()
    conn.close()


def _add_intent(env, intent_id="i1", symbol="US.ACN", close_key="entry:8219132",
                session="ETH", state="BOOKED", age_s=600):
    conn = sqlite3.connect(env["ledger"])
    conn.execute("INSERT INTO exit_intents VALUES (?,?,?,?,?,?)",
                 (intent_id, symbol, close_key, session, state,
                  time.time() - age_s))
    conn.commit()
    conn.close()


class TestPositiveDetection:
    def test_deadlock_detected_and_alerted(self, env):
        _add_position(env)
        _add_intent(env)
        out = health.scan()
        assert out["ok"] and len(out["deadlocks"]) == 1
        row = out["deadlocks"][0]
        assert row["symbol"] == "US.ACN" and row["close_key"] == "entry:8219132"
        assert row["intent_id"] == "i1" and row["alerted"] is True
        assert len(env["sent"]) == 1
        text = env["sent"][0]
        # 告警必须给值班人: 哪个意图、什么后果、可粘贴的释放命令
        assert "i1" in text and "US.ACN" in text
        assert "release_stuck_exit_intent.py" in text
        assert "发不出单" in text or "零保护" in text
        # 审计落盘 (跨进程去重依据)
        audit = [json.loads(l) for l in health.AUDIT.read_text().splitlines()]
        assert len(audit) == 1 and audit[0]["intent_id"] == "i1"

    def test_second_scan_dedupes_by_intent(self, env):
        _add_position(env)
        _add_intent(env)
        assert len(health.scan()["deadlocks"]) == 1
        out2 = health.scan()
        assert len(out2["deadlocks"]) == 1  # 仍报告 (机读面不失明)
        assert out2["alerted"] == 0         # 但不再推送
        assert "US.ACN:already_alerted" in out2["skipped"]
        assert len(env["sent"]) == 1

    def test_dry_run_no_write_no_push(self, env):
        _add_position(env)
        _add_intent(env)
        out = health.scan(dry=True)
        assert len(out["deadlocks"]) == 1
        assert out["dry"] is True and out["alerted"] == 0
        assert env["sent"] == []
        assert not health.AUDIT.exists()


class TestNegativeNoFalseAlarm:
    """放行安全 (Gate 4 双向审计): 正常形态必须零命中零推送。"""

    def test_no_open_position_no_hit(self, env):
        _add_intent(env)  # BOOKED 意图存在但仓已平 (正常终态)
        out = health.scan()
        assert out["deadlocks"] == [] and env["sent"] == []

    def test_closed_trade_row_excluded(self, env):
        _add_position(env, exit_time="2026-10-02T09:21:53-04:00", exit_price=211.5)
        _add_intent(env)
        assert health.scan()["deadlocks"] == []

    def test_non_booked_intent_not_a_deadlock(self, env):
        """PENDING/HELD 意图走 blocked_exits/fast loop 既有告警面, 本探针不重复报。"""
        _add_position(env)
        _add_intent(env, state="HELD")
        assert health.scan()["deadlocks"] == []

    def test_fresh_booked_within_grace_not_hit(self, env):
        """年龄窗过滤: 成交刚簿记、open_positions 行还没被 position_sync 删掉的瞬态。"""
        _add_position(env)
        _add_intent(env, age_s=30)
        assert health.scan()["deadlocks"] == []

    def test_close_key_mismatch_not_hit(self, env):
        _add_position(env)
        _add_intent(env, close_key="entry:9999999")  # 别的仓的意图
        assert health.scan()["deadlocks"] == []

    def test_disabled_switch_skips(self, env, monkeypatch):
        monkeypatch.setattr(health, "ENABLED", False)
        _add_position(env)
        _add_intent(env)
        assert health.scan() == {"ok": True, "skip": "health_disabled"}

    def test_missing_ledger_db_no_crash(self, env, monkeypatch):
        monkeypatch.setattr(health, "_ledger_path",
                            lambda: env["tmp"] / "nonexistent.db")
        _add_position(env)
        assert health.scan()["deadlocks"] == []


class TestCarrierWiring:
    """源级钉: 探针必须接在分钟级载体上、且在会话门控之前 (窗外也要扫)。"""

    def test_cron_calls_health_before_session_gate(self):
        src = (ROOT / "scripts" / "extended_hours_exit_cron.py").read_text()
        assert "scan_closekey_health" in src
        call_pos = src.index("closekey_health = scan_closekey_health")
        gate_pos = src.index("session = get_exit_cron_session(now)")
        assert call_pos < gate_pos, "死锁不分时段 — 必须在会话门控之前扫"
        # 探针异常不阻塞退出链 (fail-open 到结果字段, 不抛出)
        ctx = src[src.index("# close_key 死锁体检"):call_pos + 400]
        assert "except Exception" in ctx
        # 结果必须回填 (可外部对账, 不静默)
        assert '"closekey_health": closekey_health' in src

    def test_ledger_row_mentions_probe(self):
        ledger = (ROOT / "docs" / "CONTROL-LOOP-LEDGER.md").read_text()
        assert "exit_closekey_health.py" in ledger, "台账行必须点名载体脚本 (守卫按字面量匹配)"
