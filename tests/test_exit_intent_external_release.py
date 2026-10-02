"""RFC-128 契约: external_close / 真幽灵仓对账时同步释放未决 exit_intent.

病灶 (2026-10-01 US.ACN 首例, 三方实证): external_close 对账只写 trade_records
+ DELETE open_positions, 完全不碰 exit_intents ⇒ 意图永久卡 PENDING/HELD:
  ① 该 symbol 进 blocked_exits ⇒ SL 评估被整体跳过 (当日 SL 越线 2.0pp 未平);
  ② fast loop 每 ~10s 告警 (当日 2029 条 ERROR);
  ③ RFC-127 影子落幽灵样本 ⇒ 阶段1 判据2 被顶成假 PASS。

释放语义: state → 'BOOKED' (与 ack_booked 同一终态, 不新造状态值), 同时退出
list_unsettled() 枚举 + 解除 partial unique index 的 symbol busy。
**纯账目, 不会自动平仓。**

测试取数纪律: `release_unsettled` 只消费 exit_intents 的
(account_id, target_env, symbol, direction, state) 五列 ⇒ 大多数用例直接 INSERT
构造行 (不耦合 `request()` 的身份校验规则, 那不是被测对象); 另有一条用真实
`request()` 验证「释放前 symbol busy 拒建 / 释放后可再建」的端到端语义。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest

from app.execution.exit_lifecycle import ExitLedger

ROOT = Path(__file__).resolve().parents[1]
_SYNC_SRC = (ROOT / "app" / "core" / "position_sync.py").read_text(encoding="utf-8")

ACC = "19930823"
ENV = "sim"


@pytest.fixture
def ledger(tmp_path):
    return ExitLedger(tmp_path / "playbook_exits.db")


def _insert(ledger, *, intent_id=None, symbol="US.ACN", direction="SHORT",
            quantity=23.0, state="PENDING", session="ETH", account_id=ACC,
            target_env=ENV, close_key=None, last_error="exit_eth_partial_fill_held",
            limit_price=215.62):
    """直接落一行意图 (绕过 request 的身份校验 — 那不是本 RFC 的被测对象)。"""
    iid = intent_id or uuid.uuid4().hex
    now = time.time()
    with ledger.transaction() as db:
        db.execute(
            "INSERT INTO exit_intents (intent_id,account_id,target_env,symbol,close_key,"
            "direction,quantity,owner_json,decision_json,state,last_error,session,"
            "limit_price,limit_timeout_s,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, str(account_id), target_env, symbol,
             close_key or f"entry:test-{iid[:8]}", direction, quantity,
             json.dumps({"kind": "trade", "symbol": symbol}),
             json.dumps({"reason": "test", "metadata": {}}),
             state, last_error, session, limit_price, 60, now, now))
    return iid


def _row(ledger, intent_id):
    with ledger.transaction() as db:
        row = db.execute("SELECT * FROM exit_intents WHERE intent_id=?",
                         (intent_id,)).fetchone()
    return dict(row) if row else None


def _unsettled(ledger):
    return [i["intent_id"] for i in ledger.list_unsettled()]


def _leg(ledger, intent_id, *, sequence=1, state="ACTIVE", dealt_qty=None,
          order_id="900001", broker_status="SUBMITTED"):
    now = time.time()
    with ledger.transaction() as db:
        db.execute(
            "INSERT INTO exit_legs (intent_id,sequence,kind,quantity,price,remark,"
            "order_id,state,broker_status,dealt_qty,deadline,created_at,updated_at) "
            "VALUES (?,?,'LIMIT',23.0,215.62,?,?,?,?,?,?,?,?)",
            (intent_id, sequence, f"pbe_{uuid.uuid4().hex[:24]}", order_id,
             state, broker_status, dealt_qty, now, now, now))


def _release(ledger, **overrides):
    kwargs = dict(account_id=ACC, target_env=ENV, symbol="US.ACN",
                  direction="SHORT", reason="external_close", now=time.time())
    kwargs.update(overrides)
    return ledger.release_unsettled(**kwargs)


class TestReleaseUnsettled:
    def test_releases_stuck_intent_and_marks_booked(self, ledger):
        """卡死的 HELD 意图被释放 ⇒ BOOKED + 退出未决枚举 (SL 兜底重新接管)。"""
        iid = _insert(ledger, state="PENDING")
        assert iid in _unsettled(ledger)

        released = _release(ledger, evidence={"exit_price": 209.0,
                                              "close_order_id": "8219620"})

        assert released == [iid]
        row = _row(ledger, iid)
        assert row["state"] == "BOOKED"
        assert row["last_error"] == ""
        assert iid not in _unsettled(ledger)
        receipt = json.loads(row["receipt_json"])
        assert receipt["external_close_release"] is True
        assert receipt["kind"] == "external_close_release"
        assert receipt["reason"] == "external_close"
        assert receipt["broker_qty_evidence"]["close_order_id"] == "8219620"
        assert receipt["released_at"]

    def test_held_state_also_released(self, ledger):
        """10/1 实况形态: state=PENDING 而 last_error=exit_eth_partial_fill_held。"""
        iid = _insert(ledger, state="PENDING",
                      last_error="exit_eth_partial_fill_held")
        assert _release(ledger) == [iid]
        assert _row(ledger, iid)["state"] == "BOOKED"

    def test_released_at_derives_from_now_param(self, ledger):
        """receipt released_at 与 DB updated_at 必须同源 (同一 now 瞬间, 审计可对齐)。

        旧实现 receipt 用 `datetime.now(utc)` 独立取时, 与 SQL `updated_at=?` 之间
        隔着 leg 检查循环, 两时间戳可发散 (2026-10-02 复查) ⇒ 改为从 now 派生。
        """
        from datetime import datetime, timezone
        iid = _insert(ledger, state="PENDING")
        fixed_now = 1780000000.0
        assert _release(ledger, now=fixed_now) == [iid]
        row = _row(ledger, iid)
        receipt = json.loads(row["receipt_json"])
        assert receipt["released_at"] == (
            datetime.fromtimestamp(fixed_now, timezone.utc)
            .isoformat(timespec="seconds"))
        assert row["updated_at"] == fixed_now

    def test_direction_mismatch_does_not_release(self, ledger):
        """方向不符 ⇒ 零释放 (同 symbol 双向意图互不误伤)。"""
        iid = _insert(ledger, direction="SHORT", state="PENDING")
        assert _release(ledger, direction="LONG") == []
        assert _row(ledger, iid)["state"] == "PENDING"
        assert iid in _unsettled(ledger)

    def test_direction_value_domain_is_closed(self, ledger):
        """值域闭集守卫: 两侧 direction 都只能是 LONG/SHORT ⇒ 无需别名归一。

        钉住两个事实 (任一变化必红, 提醒复查释放匹配是否静默失效):
          ① `Direction` 枚举成员 = {LONG, SHORT} (position_sync 侧传
             `existing_pos.direction.name`);
          ② `ExitLedger.request()` 强制 direction ∈ {LONG, SHORT} (ledger 侧)。
        若未来枚举扩出 SELL_SHORT 之类, 本测试红 ⇒ 那时再决定是否加归一
        (现在加 = 投机防御, 违反极简优先)。
        """
        from app.core.models import Direction
        assert sorted(d.name for d in Direction) == ["LONG", "SHORT"]
        assert sorted(d.value for d in Direction) == ["LONG", "SHORT"]

        # ledger 侧同样闭集: 非 LONG/SHORT 无法入库 (request 校验)
        import inspect

        from app.execution import exit_lifecycle as el
        code = inspect.getsource(el.ExitLedger.request)
        assert "direction not in ('LONG', 'SHORT')" in code

        # 实测生产库值域 (已落地数据不得混入别名)
        import sqlite3
        db = sqlite3.connect(str(ROOT / "data" / "playbook_exits.db"))
        got = {r[0] for r in db.execute(
            "SELECT DISTINCT direction FROM exit_intents")}
        db.close()
        assert got <= {"LONG", "SHORT"}, f"ledger direction 值域漂移: {got}"

    def test_direction_mismatch_logs_warning(self, ledger, caplog):
        """方向不符要留痕 (fail-safe 不静默): 万一值域漂移, 日志能看见。"""
        import logging
        _insert(ledger, direction="SHORT", state="PENDING")
        with caplog.at_level(logging.WARNING, logger="app.execution.exit_lifecycle"):
            assert _release(ledger, direction="LONG") == []
        assert "exit_intent_release_direction_mismatch" in caplog.text

    def test_other_account_does_not_release(self, ledger):
        """按账户隔离: 他账户意图不释放 (券商'无持仓'是按账户查的)。"""
        iid = _insert(ledger, account_id="99999999", state="PENDING")
        assert _release(ledger) == []
        assert _row(ledger, iid)["state"] == "PENDING"

    def test_other_env_does_not_release(self, ledger):
        iid = _insert(ledger, target_env="real", state="PENDING")
        assert _release(ledger) == []
        assert _row(ledger, iid)["state"] == "PENDING"

    def test_other_symbol_does_not_release(self, ledger):
        iid = _insert(ledger, symbol="US.MU", state="PENDING")
        assert _release(ledger) == []
        assert _row(ledger, iid)["state"] == "PENDING"

    def test_idempotent_second_call_returns_empty(self, ledger):
        iid = _insert(ledger, state="PENDING")
        assert _release(ledger) == [iid]
        assert _release(ledger) == []
        assert _row(ledger, iid)["state"] == "BOOKED"

    def test_already_booked_untouched(self, ledger):
        """已 BOOKED 的意图 (正常平仓收尾) 不被改写 — 幂等且不覆盖既有 receipt。"""
        iid = _insert(ledger, state="BOOKED", last_error="")
        with ledger.transaction() as db:
            db.execute("UPDATE exit_intents SET receipt_json=? WHERE intent_id=?",
                       ('{"kind":"trade","trade_id":1489}', iid))
        before = _row(ledger, iid)["receipt_json"]
        assert _release(ledger) == []
        assert _row(ledger, iid)["receipt_json"] == before

    @pytest.mark.parametrize("overrides", [
        {"account_id": ""}, {"account_id": None}, {"target_env": ""},
        {"symbol": ""}, {"direction": ""}, {"direction": None},
    ])
    def test_incomplete_keys_return_empty(self, ledger, overrides):
        """缺任一匹配键 ⇒ 返回空, 绝不做宽匹配 (fail-safe, 不误释放)。"""
        iid = _insert(ledger, state="PENDING")
        assert _release(ledger, **overrides) == []
        assert _row(ledger, iid)["state"] == "PENDING"

    def test_release_does_not_place_orders_or_add_legs(self, ledger):
        """释放是纯账目: 不新增腿、不下单 (不会自动平仓)。"""
        iid = _insert(ledger, state="PENDING")
        _release(ledger)
        with ledger.transaction() as db:
            legs = db.execute("SELECT COUNT(*) FROM exit_legs WHERE intent_id=?",
                              (iid,)).fetchone()[0]
        assert legs == 0

    def test_evidence_non_dict_is_ignored(self, ledger):
        """evidence 非 dict ⇒ 落空字典, 不炸 (对账侧传值形态可能演化)。"""
        iid = _insert(ledger, state="PENDING")
        assert _release(ledger, evidence="not-a-dict") == [iid]
        receipt = json.loads(_row(ledger, iid)["receipt_json"])
        assert receipt["broker_qty_evidence"] == {}

    @pytest.mark.parametrize("leg_state,dealt_qty,broker_status", [
        ("ACTIVE", None, "SUBMITTED"),
        ("PREPARED", None, ""),
        ("CANCEL_REQUESTED", None, "CANCELLING_ALL"),
        ("TERMINAL", None, "CANCELLED_ALL"),
    ])
    def test_unsettled_leg_blocks_release(self, ledger, caplog, leg_state,
                                          dealt_qty, broker_status):
        import logging
        iid = _insert(ledger, state="PENDING")
        _leg(ledger, iid, state=leg_state, dealt_qty=dealt_qty,
             broker_status=broker_status)
        with caplog.at_level(logging.WARNING, logger="app.execution.exit_lifecycle"):
            assert _release(ledger) == []
        assert "exit_intent_release_blocked_by_unsettled_leg" in caplog.text
        row = _row(ledger, iid)
        assert row["state"] == "PENDING"
        assert row["receipt_json"] is None
        assert iid in _unsettled(ledger)


class TestSymbolBusyEndToEnd:
    """用真实 request() 验证 busy 语义: 释放前拒建 / 释放后可再建。"""

    @staticmethod
    def _owner(tmp_path, symbol, direction, quantity, trade_id, entry_time):
        db_path = str(Path(tmp_path / "ftseek_us.db").resolve())
        owner = {
            "kind": "trade", "symbol": symbol, "direction": direction,
            "quantity": quantity, "db_path": db_path, "trade_id": trade_id,
            "entry_time": entry_time, "entry_price": 212.53,
            "source_type": "PLAYBOOK_SIM",
        }
        owner["close_key"] = f"trade:{db_path}:{trade_id}:{entry_time}"
        return owner

    def test_busy_before_release_then_ok_after(self, ledger, tmp_path):
        entry_time = "2026-10-01T08:10:43-04:00"
        owner = self._owner(tmp_path, "US.ACN", "SHORT", 23.0, 1489, entry_time)
        decision = {"reason": "pre_close_flatten", "metadata": {"session": "premarket"}}

        first, err = ledger.request(
            account_id=ACC, target_env=ENV, symbol="US.ACN", direction="SHORT",
            quantity=23.0, owner=owner, decision=decision, limit_price=215.62,
            limit_timeout_s=60, now=time.time(), session="ETH")
        assert first and first["intent_id"], f"首次 request 应成功: {err}"
        iid = first["intent_id"]

        # 模拟卡死 (ETH 腿撤销后 held)
        with ledger.transaction() as db:
            db.execute("UPDATE exit_intents SET state='PENDING', last_error=? "
                       "WHERE intent_id=?", ("exit_eth_partial_fill_held", iid))
        assert iid in _unsettled(ledger)

        # 释放前: 同 symbol 新意图被 busy 闸拒
        owner2 = self._owner(tmp_path, "US.ACN", "SHORT", 23.0, 1500, entry_time)
        blocked, res = ledger.request(
            account_id=ACC, target_env=ENV, symbol="US.ACN", direction="SHORT",
            quantity=23.0, owner=owner2, decision=decision, limit_price=215.0,
            limit_timeout_s=60, now=time.time(), session="ETH")
        assert blocked is None
        assert res.get("error") == "exit_symbol_busy"

        # 释放 ⇒ busy 解除
        assert _release(ledger) == [iid]
        retry, err2 = ledger.request(
            account_id=ACC, target_env=ENV, symbol="US.ACN", direction="SHORT",
            quantity=23.0, owner=owner2, decision=decision, limit_price=215.0,
            limit_timeout_s=60, now=time.time(), session="ETH")
        assert retry and retry["intent_id"], f"释放后应可再建: {err2}"
        assert retry["intent_id"] != iid


class TestSourceContract:
    """源码契约: 两处删仓分支都必须接上释放 (漏一处 = 半边修, 必红)。"""

    def test_helper_defined_once_and_called_twice(self):
        sites = [m.start() for m in
                 re.finditer(r"release_exit_intents_on_external_close\(", _SYNC_SRC)]
        assert len(sites) == 3, f"期望 1 处 def + 2 处调用, 实际 {len(sites)}"

    def test_external_close_branch_wires_release(self):
        # 锚点用调用行 (唯一定位): 函数签名里也有 "external_close" 字样
        pos = _SYNC_SRC.index('symbol, _dir_name, "external_close",')
        ctx = _SYNC_SRC[pos - 1800:pos + 800]
        assert "release_exit_intents_on_external_close" in ctx
        assert "DELETE FROM open_positions" in ctx
        assert "RFC-128" in ctx
        # 释放必须发生在删仓之后 (仓不在了才谈得上释放), 且证据带上对账字段
        assert ctx.index("DELETE FROM open_positions") < ctx.index(
            'symbol, _dir_name, "external_close",')
        assert '"close_order_id"' in ctx

    def test_ghost_branch_wires_release(self):
        pos = _SYNC_SRC.index('"ghost_position_close"')
        ctx = _SYNC_SRC[pos - 2000:pos + 400]
        assert "真幽灵仓" in ctx
        assert "release_exit_intents_on_external_close" in ctx
        assert "DELETE FROM open_positions" in ctx

    def test_ghost_branch_direction_is_fail_safe(self):
        """方向不可判时传空 ⇒ release_unsettled 返回 [] (不误释放) + 留痕。"""
        pos = _SYNC_SRC.index('"ghost_position_close"')
        ctx = _SYNC_SRC[pos - 1200:pos + 400]
        assert "_ghost_dir" in ctx
        assert "if _ghost_dir:" in ctx
        assert "无法判定方向" in ctx

    def test_release_helper_is_fail_open(self):
        """释放失败不得阻断对账主流程 (trade_records/通知更重要)。"""
        pos = _SYNC_SRC.index("def release_exit_intents_on_external_close")
        body = _SYNC_SRC[pos:pos + 2800]
        assert "except Exception" in body
        assert "return []" in body
        assert "对账继续" in body

    def test_helper_uses_trading_mode_ssot(self):
        """匹配键走 TradingMode SSOT, 不硬编码账户/env (切盘时不会静默失配)。"""
        pos = _SYNC_SRC.index("def release_exit_intents_on_external_close")
        body = _SYNC_SRC[pos:pos + 2800]
        assert "TradingMode()" in body
        assert "mode.account_id()" in body and "mode.target_env()" in body
        assert not re.search(r'account_id\s*=\s*"1993', body), "禁止硬编码账户号"

    def test_no_silent_except_pass_in_new_code(self):
        """新增代码不得 except:pass (埋点禁静默死亡 — 本仓既有教训)。"""
        pos = _SYNC_SRC.index("def release_exit_intents_on_external_close")
        body = _SYNC_SRC[pos:pos + 2800]
        assert not re.search(r"except[^\n]*:\s*\n\s*pass", body)
        assert "logger.warning" in body

    def test_trade_id_initialized_before_try(self):
        """[2026-10-02 复查] `_trade_id` 必须在 trade_record try 块前初始化。

        病灶: `_dup_skip` 有 try 前初始化 (防 NameError) 而 `_trade_id` 没有 ⇒
        写入失败走 except 后, release evidence 仍引用 `_trade_id` ⇒ NameError
        炸穿 release 调用 (fail-open 变 fail-closed) 并跳过平仓通知。"""
        pos = _SYNC_SRC.index("_dup_skip = False  # 查重守卫命中标记")
        ctx = _SYNC_SRC[pos:pos + 400]
        assert "_trade_id = None" in ctx, (
            "_trade_id 须与 _dup_skip 同款 try 前初始化 (防 NameError 炸穿释放)")

    def test_ledger_method_lives_with_ack_booked(self):
        """释放方法与 ack_booked 同属 ExitLedger (数据所有者持有写操作)。"""
        from app.execution import exit_lifecycle as el
        assert hasattr(el.ExitLedger, "release_unsettled")
        assert hasattr(el.ExitLedger, "ack_booked")


class TestAcnScenarioReplay:
    """真库形态复现 10/1 ACN 事件 (ETH HELD + 已撤腿) 并验证释放链路。"""

    def test_acn_scenario_end_to_end(self, ledger):
        iid = _insert(ledger, symbol="US.ACN", direction="SHORT", quantity=23.0,
                      session="ETH", state="PENDING", limit_price=215.62)
        # 模拟已撤腿 (盘前收口限价腿 60s 零成交被撤)
        with ledger.transaction() as db:
            db.execute(
                "INSERT INTO exit_legs (intent_id,sequence,kind,quantity,price,remark,"
                "order_id,state,broker_status,dealt_qty,deadline,created_at,updated_at) "
                "VALUES (?,1,'LIMIT',23.0,215.62,?,'8219159','TERMINAL','CANCELLED_ALL',"
                "0.0,?,?,?)",
                (iid, f"ext_exit_pre_close_flatten{uuid.uuid4().hex[:20]}",
                 time.time(), time.time(), time.time()))

        # 释放前: 意图未决 ⇒ 会进 blocked_exits ⇒ SL 评估被跳过
        assert iid in _unsettled(ledger)

        released = _release(ledger, evidence={"exit_price": 209.0, "exit_qty": 23.0,
                                              "close_order_id": "8219620",
                                              "trade_id": 9999})
        assert released == [iid]

        # 释放后: 退出未决枚举 ⇒ 不再 blocked ⇒ SL 兜底重新接管
        assert _unsettled(ledger) == []
        row = _row(ledger, iid)
        assert row["state"] == "BOOKED" and row["last_error"] == ""
        assert json.loads(row["receipt_json"])["broker_qty_evidence"]["trade_id"] == 9999

        # 腿记录保留 (证据保全, 不因释放而删)
        with ledger.transaction() as db:
            n = db.execute("SELECT COUNT(*) FROM exit_legs WHERE intent_id=?",
                           (iid,)).fetchone()[0]
        assert n == 1
