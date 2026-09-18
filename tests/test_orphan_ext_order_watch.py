#!/usr/bin/env python3
"""孤儿 ETH 单检测器 (DEB-20260918 §23.3 方案 A) 回归测试.

覆盖: 无触发→零券商调用 / 老活单命中 / 去重 / 新鲜单不算孤儿 / 终态单与别人单不算 /
每日告警上限 / 券商失败可见降级 / 必须早于 session 门控 (窗外也要扫)。
"""
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.orphan_ext_order_watch as OW
import scripts.extended_hours_exit_cron as XC

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 17, 9, 49, 30, tzinfo=ET)


class FakeCtx:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """隔离审计文件 + 假券商 + 假告警, 并断言"扫描确实用了券商"。"""
    calls = {"ctx": 0, "rows": 0, "pushes": []}
    monkeypatch.setattr(OW, "AUDIT", tmp_path / "orphan_ext_orders.jsonl")
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")

    def fake_ctx():
        calls["ctx"] += 1
        return FakeCtx()

    def fake_rows(ctx, env, acc_id, ret_ok=0):
        calls["rows"] += 1
        return rows

    rows = []
    monkeypatch.setattr(OW, "_open_trade_context", fake_ctx)
    monkeypatch.setattr(OW, "query_order_rows", fake_rows)
    monkeypatch.setattr(OW, "_push", lambda text: calls["pushes"].append(text) or True)
    monkeypatch.setattr(OW, "_blocked_symbols", lambda now_ts: blocked)
    blocked = {}
    return {"calls": calls, "rows": rows, "blocked": blocked, "tmp": tmp_path}


def _row(order_id="8185508", symbol="US.CRM", remark="ext_exit_tighten",
         status="SUBMITTED", create_time="2026-09-17 05:50:00", **extra):
    row = {"order_id": order_id, "symbol": symbol, "side": "SELL", "status": status,
           "qty": 23.0, "filled_qty": 0.0, "price": 251.27, "remark": remark,
           "create_time": create_time, "update_time": create_time}
    row.update(extra)
    return row


def _blocked(symbol="US.CRM", intent="ea2149f8f34d499686da8f23aa985829"):
    return {symbol: [{"intent_id": intent, "last_error": "exit_unowned_active_order",
                      "updated_at": NOW.timestamp() - 30}]}


def test_no_blocked_intent_means_zero_broker_calls(sandbox):
    out = OW.scan(now=NOW)
    assert out == {"ok": True, "skip": "no_blocked_intent"}
    assert sandbox["calls"]["ctx"] == 0 and sandbox["calls"]["rows"] == 0


def test_old_own_active_order_is_recorded_and_alerted(sandbox):
    sandbox["blocked"].update(_blocked())
    sandbox["rows"].append(_row())
    out = OW.scan(now=NOW)

    assert out["ok"] is True and len(out["orphans"]) == 1 and out["alerted"] == 1
    rec = out["orphans"][0]
    assert rec["symbol"] == "US.CRM" and rec["order_id"] == "8185508"
    assert rec["age_min"] == 239.5 and rec["action"] == "alert_only"
    assert rec["alerted"] is True and rec["intent_ids"] == ["ea2149f8f34d499686da8f23aa985829"]
    assert "仅告警, 未撤单" in sandbox["calls"]["pushes"][0]

    lines = [json.loads(line) for line in OW.AUDIT.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["alerted"] is True


def test_same_order_is_never_alerted_twice(sandbox):
    sandbox["blocked"].update(_blocked())
    sandbox["rows"].append(_row())
    OW.scan(now=NOW)
    out = OW.scan(now=NOW)

    assert out["orphans"] == [] and out["alerted"] == 0
    assert out["skipped"] == ["US.CRM:already_recorded"]
    assert len(OW.AUDIT.read_text().splitlines()) == 1
    assert len(sandbox["calls"]["pushes"]) == 1


def test_fresh_order_is_not_an_orphan(sandbox):
    sandbox["blocked"].update(_blocked())
    sandbox["rows"].append(_row(create_time="2026-09-17 09:45:00"))   # 4.5 分钟
    out = OW.scan(now=NOW)
    assert out["orphans"] == [] and out["alerted"] == 0
    assert out["skipped"] == ["US.CRM:fresh"]
    assert not OW.AUDIT.exists()


@pytest.mark.parametrize("bad", [
    _row(status="CANCELLED_ALL"),        # 已终态
    _row(status="FILLED_ALL"),
    _row(remark="pb_entry"),             # 不是延长时段退出单 (别人的/入场单)
    _row(remark=""),
    _row(symbol="US.TSLA"),              # 别的标的 (不在被拦名单里)
])
def test_non_orphans_are_ignored(sandbox, bad):
    sandbox["blocked"].update(_blocked())
    sandbox["rows"].append(bad)
    out = OW.scan(now=NOW)
    assert out["orphans"] == [] and out["alerted"] == 0
    assert not OW.AUDIT.exists()


def test_daily_alert_cap_records_but_stops_pushing(sandbox, monkeypatch):
    monkeypatch.setenv("EXT_ORPHAN_ALERT_MAX_PER_DAY", "1")
    # 模块级常量在 import 时已固化 ⇒ 直接改属性 (与 env 覆盖等价)
    monkeypatch.setattr(OW, "ALERT_MAX_PER_DAY", 1)
    sandbox["blocked"].update(_blocked())
    sandbox["rows"].extend([
        _row(order_id="8185508"),
        _row(order_id="8185509", create_time="2026-09-17 06:10:00"),
    ])
    out = OW.scan(now=NOW)

    assert len(out["orphans"]) == 2 and out["alerted"] == 1
    assert out["orphans"][1]["alert_skipped"] == "daily_cap"
    assert len(sandbox["calls"]["pushes"]) == 1
    assert len(OW.AUDIT.read_text().splitlines()) == 2   # 事件仍留痕


def test_broker_failure_degrades_visibly(sandbox, monkeypatch):
    sandbox["blocked"].update(_blocked())

    def boom(*args, **kwargs):
        raise RuntimeError("OpenD 挂了")

    monkeypatch.setattr(OW, "query_order_rows", boom)
    out = OW.scan(now=NOW)
    assert out["ok"] is False and "OpenD 挂了" in out["error"]
    assert not OW.AUDIT.exists()


def test_dry_run_writes_nothing_and_pushes_nothing(sandbox):
    sandbox["blocked"].update(_blocked())
    sandbox["rows"].append(_row())
    out = OW.scan(now=NOW, dry=True)
    assert out["dry"] is True and len(out["orphans"]) == 1
    assert sandbox["calls"]["pushes"] == [] and not OW.AUDIT.exists()


def test_watch_switch_off_skips(sandbox, monkeypatch):
    monkeypatch.setattr(OW, "ENABLED", False)
    sandbox["blocked"].update(_blocked())
    assert OW.scan(now=NOW) == {"ok": True, "skip": "watch_disabled"}
    assert sandbox["calls"]["ctx"] == 0


def test_exit_cron_scans_orphans_before_session_gate(monkeypatch):
    """窗外 (无 session) 也必须扫 —— 阻塞事件在 RTH, 孤儿单在 ETH, 都落在本 cron 窗外。"""
    seen = {}

    def fake_scan(dry=False):
        seen["dry"] = dry
        return {"ok": True, "skip": "no_blocked_intent"}

    monkeypatch.setattr(XC, "scan_orphan_orders", fake_scan)
    monkeypatch.setattr(XC, "get_exit_cron_session", lambda now=None: None)

    result = XC.run_once(now=None)
    assert result["skip"] == "outside_extended_hours"
    assert result["orphan"] == {"ok": True, "skip": "no_blocked_intent"}
    assert "dry" in seen


def test_exit_cron_survives_orphan_scan_crash(monkeypatch):
    """检测器炸了不能让退出链炸 (但错误必须出现在结果里, 不静默)。"""
    def boom(dry=False):
        raise RuntimeError("ledger 锁超时")

    monkeypatch.setattr(XC, "scan_orphan_orders", boom)
    monkeypatch.setattr(XC, "get_exit_cron_session", lambda now=None: None)
    result = XC.run_once(now=None)
    assert result["skip"] == "outside_extended_hours"
    assert result["orphan"]["ok"] is False and "ledger 锁超时" in result["orphan"]["error"]