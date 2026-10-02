"""INCIDENT-002 契约: 释放卡死 exit_intent 必须真正空出 close_key.

病灶 (2026-10-02 US.ACN 全链路实证, DB 副本四项检验):
  旧版 release_stuck_exit_intent.py 把卡死意图改成 BOOKED, docstring 声称这是
  「唯一能解除 busy + 交回 SL 兜底的态」— 后半句被证伪:
    - exit_intents 有 UNIQUE(account_id, target_env, close_key) **无 state 谓词**
      ⇒ BOOKED 行永久占用 close_key;
    - ETH 链 request() 命中旧 BOOKED 行原样返回 ⇒ reserve_leg 对 BOOKED/FILLED
      直接 None ⇒ 盘前锁盈每分钟「决策正确、一单发不出」(误导推送「待确认」);
    - RTH 链 close_position 默认 session='RTH' ≠ 旧行 'ETH' ⇒
      exit_session_conflict BLOCKED ⇒ SL 兜底同样封死 = 静默零保护。
  正确释放 = 归档 (证据保全) + 删除 intent/legs。10/2 09:21 ET 生产实证:
  删除后下一轮 cron 重建 intent 并 FILLED_ALL。

本测试钉住脚本存在的唯一理由:
  释放后, 同 close_key + session='RTH' (SL 兜底路径) 与 session='ETH' (盘前
  重放路径) 的 request() 都必须能重建意图, 且 reserve_leg 能拿到腿。
外加: 旧 BOOKED 方案的死锁形态复现 (防「顺手改回 BOOKED」的回退),
  归档文件内容完整性, --restore 回插, 非终态腿拒删, FILLED_UNBOOKED 拒删。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.execution.exit_lifecycle import ExitLedger

ACC = "19930823"
ENV = "sim"


# ---------------------------------------------------------------- helpers

def _ledger(tmp_path) -> ExitLedger:
    return ExitLedger(tmp_path / "playbook_exits.db")


@pytest.fixture
def ledger(tmp_path):
    return _ledger(tmp_path)


def _owner(tmp_path, *, entry_id="8219132", trade_id=1598):
    db_path = str(Path(tmp_path / "ftseek_us.db").resolve())
    owner = {
        "kind": "trade", "symbol": "US.ACN", "direction": "SHORT",
        "quantity": 23.0, "db_path": db_path, "trade_id": trade_id,
        "entry_order_id": entry_id,
        "entry_time": "2026-10-01T08:10:43.201009-04:00",
        "entry_price": 212.529, "source_type": "extended_hours_entry",
    }
    owner["close_key"] = f"entry:{entry_id}"
    return owner


_DECISION = {"reason": "SL_TRAIL_LOCK",
             "metadata": {"session": "premarket", "stop_loss": 223.16}}


def _request(ledger, owner, *, session, limit_price=212.35):
    return ledger.request(
        account_id=ACC, target_env=ENV, symbol="US.ACN", direction="SHORT",
        quantity=23.0, owner=owner, decision=_DECISION,
        limit_price=limit_price, limit_timeout_s=60, now=time.time(),
        session=session)


def _seed_stuck_eth_intent(ledger, tmp_path):
    """复现 10/1 ACN 卡死形态: ETH 限价腿 TERMINAL/CANCELLED_ALL/dealt=0。"""
    intent, err = _request(ledger, _owner(tmp_path), session="ETH",
                           limit_price=215.62)
    assert intent and intent["intent_id"], err
    leg = ledger.reserve_leg(intent, "LIMIT", 23.0, 215.62,
                             time.time() + 60, time.time())
    assert leg
    ledger.attach_order(leg, "8219159", time.time())
    ledger.observe(leg, {"order_id": "8219159", "order_status": "CANCELLED_ALL",
                         "dealt_qty": 0, "dealt_avg_price": 0}, time.time())
    # 10/1 人工处置把它改成 BOOKED (旧脚本语义) — 这正是死锁的来源
    with ledger.transaction() as db:
        db.execute("UPDATE exit_intents SET state='BOOKED' WHERE intent_id=?",
                   (intent["intent_id"],))
    return intent["intent_id"]


def _run_script(tmp_path, monkeypatch, db_path, argv):
    """在隔离的 db 下跑脚本 main(), 返回 (exit_code, stdout)。

    归档目录不是脚本常量 — 它由 ledger 从 DB 路径派生 (DB 同级
    `exit_intents_archive/`), 与 RFC-128 自动释放共用同一路径口径。
    """
    import scripts.release_stuck_exit_intent as rel
    monkeypatch.setattr(rel, "EXITS_DB", Path(db_path))
    # 券商查询不进测试 (只读地面真值, 非被测对象; 且 conftest 封网络出口)
    monkeypatch.setattr(rel, "_broker_position",
                        lambda *a, **k: [{"_note": "test-stub"}])
    monkeypatch.setattr("sys.argv", ["release_stuck_exit_intent.py"] + argv)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rel.main()
    return rc, buf.getvalue()


def _archive_dir(tmp_path):
    """ledger 派生的归档目录 (与 ExitLedger.archive_dir 同口径)。"""
    return tmp_path / "exit_intents_archive"


# ---------------------------------------------------------------- tests

class TestBookedIsDeadlockNotRelease:
    """旧 BOOKED 方案的死锁形态复现 — 防回退到已被证伪的语义。"""

    def test_booked_row_still_occupies_close_key(self, ledger, tmp_path):
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        # ETH 重放: request 命中旧 BOOKED 行原样返回 ⇒ reserve_leg=None
        intent, err = _request(ledger, _owner(tmp_path), session="ETH")
        assert err is None and intent["intent_id"] == iid
        assert intent["state"] == "BOOKED"
        assert ledger.reserve_leg(intent, "LIMIT", 23.0, 212.35,
                                  time.time() + 60, time.time()) is None
        # RTH SL 兜底: session 不同 ⇒ exit_session_conflict BLOCKED
        blocked, held_err = _request(ledger, _owner(tmp_path), session="RTH")
        assert blocked is None
        assert held_err.get("error") == "exit_session_conflict"

    def test_session_rewrite_alone_insufficient(self, ledger, tmp_path):
        """检验 C: 把旧 BOOKED 行 session 改成 RTH 仍拿不到腿。"""
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        with ledger.transaction() as db:
            db.execute("UPDATE exit_intents SET session='RTH' WHERE intent_id=?",
                       (iid,))
        intent, err = _request(ledger, _owner(tmp_path), session="RTH")
        assert err is None and intent["intent_id"] == iid
        assert ledger.reserve_leg(intent, "LIMIT", 23.0, 212.35,
                                  time.time() + 60, time.time()) is None


class TestArchiveAndDeleteReleases:
    """检验 D (脚本新语义): 归档+删除后 ETH/RTH 两链都能重建意图并拿腿。"""

    @pytest.mark.parametrize("session", ["ETH", "RTH"])
    def test_release_then_request_and_reserve_ok(self, session, tmp_path,
                                                 monkeypatch):
        ledger = _ledger(tmp_path)
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        rc, out = _run_script(tmp_path, monkeypatch,
                              str(tmp_path / "playbook_exits.db"),
                              ["--intent-id", iid, "--apply",
                               "--reason", "test-release"])
        assert rc == 0, out
        assert "close_key空出=✅" in out

        # 释放后: 两链 request 都重建新意图, reserve_leg 拿到腿
        intent, err = _request(ledger, _owner(tmp_path), session=session)
        assert err is None, f"{session} 链释放后仍被拒: {err}"
        assert intent and intent["intent_id"] != iid
        assert intent["state"] == "PENDING"
        leg = ledger.reserve_leg(intent, "LIMIT", 23.0, 212.35,
                                 time.time() + 60, time.time())
        assert leg is not None, f"{session} 链释放后 reserve_leg 仍为 None"

    def test_archive_file_preserves_evidence(self, tmp_path, monkeypatch):
        ledger = _ledger(tmp_path)
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        rc, out = _run_script(tmp_path, monkeypatch,
                              str(tmp_path / "playbook_exits.db"),
                              ["--intent-id", iid, "--apply",
                               "--reason", "evidence-check"])
        assert rc == 0, out
        archives = list(_archive_dir(tmp_path).glob(f"{iid}_*.json"))
        assert len(archives) == 1
        payload = json.loads(archives[0].read_text())
        assert payload["reason"] == "evidence-check"
        assert payload["kind"] == "manual_release"
        assert payload["intent"]["intent_id"] == iid
        assert payload["intent"]["close_key"] == "entry:8219132"
        assert len(payload["legs"]) == 1
        assert payload["legs"][0]["order_id"] == "8219159"
        assert payload["legs"][0]["broker_status"] == "CANCELLED_ALL"
        # 券商地面真值随档保存 (操作者当时的判断依据)
        assert payload["broker_qty_evidence"]["broker_position_snapshot"] == [
            {"_note": "test-stub"}]
        # 生产库中该行已删
        conn = sqlite3.connect(tmp_path / "playbook_exits.db")
        assert conn.execute("SELECT 1 FROM exit_intents WHERE intent_id=?",
                            (iid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM exit_legs WHERE intent_id=?",
                            (iid,)).fetchone() is None
        conn.close()

    def test_same_second_release_restore_release_keeps_both_archives(self, tmp_path):
        """同一秒重复归档不得覆盖旧证据 (并发/回滚路径的文件名唯一性)。"""
        ledger = _ledger(tmp_path)
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        now = 1790947259.5

        first, refuse = ledger.archive_and_delete(
            iid, reason="first", now=now, kind="manual_release")
        assert first and refuse is None
        restored, restore_refuse = ledger.restore_archived(iid)
        assert restored and restore_refuse is None
        second, second_refuse = ledger.archive_and_delete(
            iid, reason="second", now=now, kind="manual_release")

        assert second and second_refuse is None
        assert second != first
        assert len(list(_archive_dir(tmp_path).glob(f"{iid}_*.json"))) == 2

    def test_restore_reinserts_from_archive(self, tmp_path, monkeypatch):
        ledger = _ledger(tmp_path)
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        db = str(tmp_path / "playbook_exits.db")
        rc, _ = _run_script(tmp_path, monkeypatch, db,
                            ["--intent-id", iid, "--apply"])
        assert rc == 0
        rc2, out2 = _run_script(tmp_path, monkeypatch, db,
                                ["--intent-id", iid, "--restore"])
        assert rc2 == 0, out2
        restored = ledger.get(iid)
        assert restored and restored["state"] == "BOOKED"
        assert len(ledger.legs(iid)) == 1
        # 幂等: 再释放一次已删的行 → 走「已释放」出口
        rc3, out3 = _run_script(tmp_path, monkeypatch, db,
                                ["--intent-id", iid, "--apply"])
        # (restore 后行又存在, 所以这次是真删; 删完再跑才是幂等出口)
        assert rc3 == 0, out3
        rc4, out4 = _run_script(tmp_path, monkeypatch, db,
                                ["--intent-id", iid, "--apply"])
        assert rc4 == 0 and "幂等跳过" in out4

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        ledger = _ledger(tmp_path)
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        rc, out = _run_script(tmp_path, monkeypatch,
                              str(tmp_path / "playbook_exits.db"),
                              ["--intent-id", iid])
        assert rc == 0 and "dry-run" in out
        assert ledger.get(iid) is not None
        arch = _archive_dir(tmp_path)
        assert not arch.exists() or not list(arch.glob("*.json")), \
            "dry-run 绝不落档 (不落档 ⇒ 不会留下'已释放'的假证据)"


class TestSafetyGuards:
    def test_unsafe_leg_refuses_release(self, tmp_path, monkeypatch):
        """活跃腿 (state=ACTIVE) ⇒ 拒删, 防孤儿单。"""
        ledger = _ledger(tmp_path)
        intent, err = _request(ledger, _owner(tmp_path), session="ETH")
        assert intent, err
        leg = ledger.reserve_leg(intent, "LIMIT", 23.0, 215.62,
                                 time.time() + 60, time.time())
        ledger.attach_order(leg, "9999999", time.time())  # ACTIVE
        rc, out = _run_script(tmp_path, monkeypatch,
                              str(tmp_path / "playbook_exits.db"),
                              ["--intent-id", intent["intent_id"], "--apply"])
        assert rc == 2 and "非终态" in out
        assert ledger.get(intent["intent_id"]) is not None

    def test_filled_unbooked_refuses_release(self, tmp_path, monkeypatch):
        """成交已发生簿记未完成 ⇒ 拒删 (删行会掐断 acknowledge_close)。"""
        ledger = _ledger(tmp_path)
        iid = _seed_stuck_eth_intent(ledger, tmp_path)
        with ledger.transaction() as db:
            db.execute("UPDATE exit_intents SET state='FILLED_UNBOOKED' "
                       "WHERE intent_id=?", (iid,))
        rc, out = _run_script(tmp_path, monkeypatch,
                              str(tmp_path / "playbook_exits.db"),
                              ["--intent-id", iid, "--apply"])
        assert rc == 2 and "FILLED_UNBOOKED" in out
        assert ledger.get(iid) is not None

    def test_missing_intent_without_archive_errors(self, tmp_path, monkeypatch):
        ledger = _ledger(tmp_path)  # 建表
        rc, out = _run_script(tmp_path, monkeypatch,
                              str(tmp_path / "playbook_exits.db"),
                              ["--intent-id", "deadbeef" * 4, "--apply"])
        assert rc == 2 and "不存在" in out


class TestDocstringHonesty:
    """源码契约: docstring 不得再含已被证伪的「改成 BOOKED 是唯一」论断。"""

    def test_no_falsified_booked_claim(self):
        src = (Path(__file__).resolve().parents[1] / "scripts" /
               "release_stuck_exit_intent.py").read_text(encoding="utf-8")
        assert "改成 BOOKED 是唯一" not in src
        assert "归档并删除" in src or "归档" in src
        assert "exit_session_conflict" in src  # 死锁机理必须写清
        assert "INCIDENT-002" in src
