"""RFC-157 契约: RTH 零成交卡死意图的自动释放 (INCIDENT-003 缺口).

钉住两件事:
  ① 判据真值表 — 只有「全部腿 TERMINAL + dealt=0 + 仓位仍在 + 够老 + 未超当日上限」
     才释放; 任何一条不满足都必须 skip (部分成交/非终态腿/ETH/仓位不明一律不碰)。
  ② 端到端 — 在真 ExitLedger 上造一条 TSLA 形态意图 (MARKET 腿 CANCELLED_ALL
     dealt=0), scan() 必须归档+删除, close_key 空出 (同 key 能重建意图),
     且重跑幂等。

为什么写操作必须 fail-closed (与影子打点 fail-open 相反): 释放 = 删账本行。
误删活跃腿会让在途单变孤儿; 误删部分成交会让已成交数量丢账。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import scripts.rth_zero_fill_autorelease as rz


T0 = 1_791_000_000.0   # 固定基准时间戳 (2026-10-xx), 只用相对差


def _intent(**over):
    row = {"intent_id": "i" * 32, "account_id": "19930823", "target_env": "sim",
           "symbol": "US.TSLA", "direction": "SHORT", "quantity": 12.0,
           "session": "RTH", "state": "PENDING",
           "created_at": T0, "updated_at": T0 + 30}
    row.update(over)
    return row


def _leg(**over):
    row = {"intent_id": "i" * 32, "sequence": 1, "kind": "MARKET", "state": "TERMINAL",
           "broker_status": "CANCELLED_ALL", "order_id": "8276449", "dealt_qty": 0.0}
    row.update(over)
    return row


# ── ① 判据真值表 ────────────────────────────────────────────────────
def test_releases_terminal_zero_fill_with_position_open():
    action, reason, ev = rz.verdict(
        _intent(), [_leg()], now=T0 + 3600, position_open=True, min_age_s=1800)
    assert (action, reason) == ("release", "all_legs_terminal_zero_fill")
    assert ev["total_dealt"] == 0.0 and ev["position_open"] is True
    assert ev["legs"][0]["broker_status"] == "CANCELLED_ALL"


@pytest.mark.parametrize("state", ["BOOKED", "FILLED_UNBOOKED"])
def test_never_touches_booking_owned_states(state):
    action, reason, _ = rz.verdict(
        _intent(state=state), [_leg()], now=T0 + 3600, position_open=True)
    assert action == "skip" and "owned_by_booking" in reason


def test_never_touches_eth_intents():
    # ETH 有自身 escalation (_release_eth_held_exit_for_rth_takeover) — 本模块不碰
    action, reason, _ = rz.verdict(
        _intent(session="ETH"), [_leg()], now=T0 + 3600, position_open=True)
    assert (action, reason) == ("skip", "session_not_rth")


def test_no_legs_is_not_releasable():
    action, reason, _ = rz.verdict(_intent(), [], now=T0 + 3600, position_open=True)
    assert (action, reason) == ("skip", "no_leg")


def test_active_leg_blocks_release():
    action, reason, ev = rz.verdict(
        _intent(), [_leg(state="ACTIVE", broker_status="SUBMITTED")],
        now=T0 + 3600, position_open=True)
    assert (action, reason) == ("skip", "leg_not_terminal")
    assert ev["leg_state"] == "ACTIVE"


def test_unknown_dealt_blocks_release():
    action, reason, _ = rz.verdict(
        _intent(), [_leg(dealt_qty=None, broker_status="")], now=T0 + 3600,
        position_open=True)
    assert (action, reason) == ("skip", "dealt_qty_unknown")


def test_partial_fill_never_released():
    # 12 股里成交 2 股: 必须由簿记收口, 释放会让这 2 股丢账
    action, reason, ev = rz.verdict(
        _intent(), [_leg(dealt_qty=2.0, broker_status="CANCELLED_PART")],
        now=T0 + 3600, position_open=True)
    assert (action, reason) == ("skip", "partial_fill_booked_elsewhere")
    assert ev["total_dealt"] == 2.0


def test_young_intent_is_left_alone():
    action, reason, ev = rz.verdict(
        _intent(), [_leg()], now=T0 + 600, position_open=True, min_age_s=1800)
    assert (action, reason) == ("skip", "too_young")
    assert ev["age_s"] == pytest.approx(600.0)   # created_at 锚, 不是 updated_at(+30)


def test_age_anchor_is_created_at_because_updated_at_is_bumped_by_resume():
    # updated_at 每轮 resume 都被 result()/observe() 刷成"刚刚" ⇒ 锚它 = 恒 too_young
    # (死门)。锚 created_at: updated_at 再新也不影响判定。
    action, reason, _ = rz.verdict(
        _intent(created_at=T0, updated_at=T0 + 5000), [_leg()],
        now=T0 + 3600, position_open=True, min_age_s=1800)
    assert (action, reason) == ("release", "all_legs_terminal_zero_fill")


@pytest.mark.parametrize("pos,reason", [(False, "position_open_absent"),
                                        (None, "position_open_unknown")])
def test_position_guard_is_fail_closed(pos, reason):
    action, got, _ = rz.verdict(_intent(), [_leg()], now=T0 + 3600, position_open=pos)
    assert (action, got) == ("skip", reason)


# ── ② 端到端: 真 ledger + 临时仓位库/审计台账 ────────────────────────
def _make_intent(ledger, *, session="RTH", now=T0, limit_price=0):
    owner = {
        "kind": "trade", "db_path": "/tmp/none_ftseek.db",
        "entry_time": "2026-10-21T16:45:23-04:00", "entry_price": 390.05,
        "source_type": "PLAYBOOK_SIM", "symbol": "US.TSLA", "direction": "SHORT",
        "quantity": 12, "close_key": "entry:8276448", "entry_order_id": "8276448",
    }
    decision = {"reason": "EOD_FORCE_CLOSE", "metadata": {}}
    intent, blocked = ledger.request(
        account_id="19930823", target_env="sim", symbol="US.TSLA", direction="SHORT",
        quantity=12, owner=owner, decision=decision, limit_price=limit_price,
        limit_timeout_s=30, now=now, session=session)
    assert blocked is None and intent is not None
    return intent


def _kill_leg(ledger, intent, *, status="CANCELLED_ALL", dealt=0.0, at=T0 + 5):
    leg = ledger.reserve_leg(intent, "MARKET", 12, 0, at + 30, at)
    ledger.attach_order(leg, "8276449", at)
    fresh = [x for x in ledger.legs(intent["intent_id"]) if x["sequence"] == 1][0]
    ledger.observe(fresh, {"order_id": "8276449", "order_status": status,
                           "dealt_qty": dealt}, at)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.execution.exit_lifecycle import ExitLedger

    ledger_path = tmp_path / "playbook_exits.db"
    pos_db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(pos_db))
    conn.execute("CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL)")
    conn.execute("INSERT INTO open_positions VALUES ('US.TSLA','SHORT',12)")
    conn.commit()
    conn.close()

    audit = tmp_path / "rth_zero_fill_autorelease.jsonl"
    monkeypatch.setattr(rz, "AUDIT", audit)
    monkeypatch.setattr(rz, "_POSITION_DBS", ((pos_db, "sim"),))
    monkeypatch.setattr(rz, "_ledger_path", lambda: ledger_path)
    monkeypatch.setattr(rz, "ENABLED", True)
    return ExitLedger(str(ledger_path))


def test_scan_releases_and_frees_close_key(env, monkeypatch):
    ledger = env
    pushes = []
    monkeypatch.setattr(rz, "_push", lambda text: pushes.append(text) or True)
    intent = _make_intent(ledger)
    _kill_leg(ledger, intent)
    iid = intent["intent_id"]

    out = rz.scan(dry=False, now=T0 + 3600)
    assert out["released"] and out["released"][0]["status"] == "released"
    assert out["released"][0]["reason"] == "all_legs_terminal_zero_fill"
    # 归档留证 (含腿证据) + 行已删除
    archive = out["released"][0]["archive"]
    payload = json.loads(open(archive).read())
    assert payload["kind"] == rz.KIND
    assert payload["intent"]["intent_id"] == iid
    assert payload["legs"][0]["dealt_qty"] == 0
    assert ledger.get(iid) is None
    # close_key 空出 ⇒ 同 key 能重建意图 (释放的唯一理由)
    again = _make_intent(ledger, now=T0 + 3700)
    assert again["intent_id"] != iid
    # 幂等: 无未决意图时再扫零动作
    assert rz.scan(dry=False, now=T0 + 3800)["released"] == []
    # 审计台账留痕 (跨进程当日计数依赖它) + 释放必推一条 (不静默自愈)
    assert json.loads(rz.AUDIT.read_text().splitlines()[-1])["status"] == "released"
    assert len(pushes) == 1 and "退出链自愈" in pushes[0]


def test_scan_dry_run_writes_nothing(env):
    ledger = env
    intent = _make_intent(ledger)
    _kill_leg(ledger, intent)
    out = rz.scan(dry=True, now=T0 + 3600)
    assert out["released"][0]["status"] == "would_release"
    assert ledger.get(intent["intent_id"]) is not None
    assert not rz.AUDIT.exists()


def test_scan_skips_when_position_gone(env):
    ledger = env
    pos_db = str(rz._POSITION_DBS[0][0])
    conn = sqlite3.connect(pos_db)
    conn.execute("DELETE FROM open_positions")
    conn.commit()
    conn.close()
    intent = _make_intent(ledger)
    _kill_leg(ledger, intent)
    out = rz.scan(dry=True, now=T0 + 3600)
    assert out["released"] == []
    assert out["skipped"][0]["reason"] == "position_open_absent"
    assert ledger.get(intent["intent_id"]) is not None


def test_daily_cap_stops_runaway_retries(env, monkeypatch):
    ledger = env
    monkeypatch.setattr(rz, "DAILY_CAP", 3)
    rz.AUDIT.write_text("\n".join(json.dumps({
        "symbol": "US.TSLA", "et_date": rz._et_today(), "status": "released",
        "intent_id": "old%d" % i}) for i in range(3)) + "\n")
    intent = _make_intent(ledger)
    _kill_leg(ledger, intent)
    out = rz.scan(dry=False, now=T0 + 3600)
    assert out["released"] == []
    assert out["skipped"][0]["reason"] == "daily_cap_reached"
    assert ledger.get(intent["intent_id"]) is not None


def test_scan_skips_eth_intent(env):
    ledger = env
    intent = _make_intent(ledger, session="ETH", limit_price=390.0)   # ETH 需限价
    _kill_leg(ledger, intent, status="CANCELLED_ALL")
    out = rz.scan(dry=True, now=T0 + 3600)
    assert out["released"] == []
    assert out["skipped"][0]["reason"] == "session_not_rth"


def test_disabled_kill_switch_is_total(env, monkeypatch):
    ledger = env
    monkeypatch.setattr(rz, "ENABLED", False)
    intent = _make_intent(ledger)
    _kill_leg(ledger, intent)
    out = rz.scan(dry=False, now=T0 + 3600)
    assert out.get("skipped_disabled") is True and out["released"] == []
    assert ledger.get(intent["intent_id"]) is not None


def test_cron_carrier_wires_the_scan():
    # 载体契约: extended_hours_exit_cron.run_once 必须调用本 scan (单点接线, 无新环路)
    from pathlib import Path
    src = Path(rz.__file__).resolve().parents[1] / "scripts" / "extended_hours_exit_cron.py"
    text = src.read_text(encoding="utf-8")
    assert "from scripts.rth_zero_fill_autorelease import scan as scan_rth_zero_fill" in text
    assert text.index("scan_rth_zero_fill(dry=dry)") < text.index("result = {\"ok\": True, \"orphan\"")