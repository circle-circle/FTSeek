#!/usr/bin/env python3
"""盘后进场仓「不强止损 + 持有到次日开盘」回归测试 (2026-09-17 用户令).

关键陷阱: open_positions.entry_time 是 **CST**。ET 16:30 (盘后) == CST **次日** 04:30 ——
用字符串前 10 位比日期会把"今日盘后仓"判成"次日仓", 必须真转 ET。
"""
import importlib.util
import json
import pathlib
import subprocess as _sp
import sys
import types
from datetime import datetime

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PM = _load("pm_flatten_ah_test", "scripts/premarket_exit_shadow.py")
AH = _load("ah_hold_test", "scripts/afterhours_exit_shadow.py")

TODAY = "2026-09-18"
CST_AH_ENTRY = "2026-09-18 04:30:00"     # = ET 2026-09-17 16:30 (盘后进场)
CST_PRE_ENTRY = "2026-09-18 16:30:00"    # = ET 2026-09-18 04:30 (盘前进场)


def _futu_stub(fields):
    fields = dict(fields)
    reference = fields.get("pre_price") or fields.get("after_price") or fields.get("last_price")
    fields.setdefault("bid_price", reference)
    fields.setdefault("ask_price", float(reference) + 0.01)

    class _Snap:
        def iterrows(self):
            yield 0, dict(fields)

    mod = types.ModuleType("futu")
    mod.RET_OK = 0
    mod.OpenQuoteContext = lambda **kw: types.SimpleNamespace(
        get_market_snapshot=lambda syms: (0, _Snap()), close=lambda: None)
    return mod


def test_entry_time_conversion_is_cst_to_et_across_day():
    et = PM._entry_et_dt(CST_AH_ENTRY)
    assert et.strftime("%Y-%m-%d %H:%M") == "2026-09-17 16:30"     # 跨日
    # 字符串前 10 位会给出 2026-09-18 (错) —— 记录该陷阱
    assert str(CST_AH_ENTRY)[:10] == "2026-09-18" == TODAY


def test_is_afterhours_carry_requires_window_and_previous_day():
    assert PM.is_afterhours_carry(CST_AH_ENTRY, TODAY) is True           # 昨盘后进的仓
    assert PM.is_afterhours_carry(CST_AH_ENTRY, "2026-09-17") is False   # 当日盘后仓归盘后影子
    assert PM.is_afterhours_carry(CST_PRE_ENTRY, TODAY) is False         # 今日盘前进的仓
    assert PM.is_afterhours_carry("2026-09-18 02:30:00", TODAY) is False  # ET 9/17 14:30 = 盘中
    assert PM.is_afterhours_carry(CST_AH_ENTRY, TODAY, enabled=False) is False  # 回滚路径


def test_premarket_shadow_holds_afterhours_carry_before_open(monkeypatch, tmp_path):
    """09:20 ET (开盘前) 对 carry 仓**不得**动作: 既不锁盈也不止损."""
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 9.0, "pre_price": 9.0, "pre_volume": 90000.0,
         "prev_close_price": 10.0, "pre_high_price": 9.2, "update_time": "2026-09-18 09:20:00"}))
    monkeypatch.setattr(PM, "_today_et", lambda: TODAY)
    monkeypatch.setattr(PM, "_now_et_hhmm", lambda: 920)
    monkeypatch.setattr(PM, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, CST_AH_ENTRY, "sig1", '{"stop_loss":9.5}')])
    monkeypatch.setattr(PM, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    PM.collect(dry=False)
    row = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert row["action"] == "hold_to_next_open"
    assert row["is_afterhours_carry"] is True
    assert calls == []                       # 关键: 没有下任何单


def test_premarket_shadow_exits_at_next_open(monkeypatch, tmp_path):
    """09:35 ET → 开盘兑现, 且必须用 --session next_open (RTH 单)."""
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 9.8, "pre_price": 9.8, "pre_volume": 90000.0,
         "prev_close_price": 10.0, "pre_high_price": 9.9, "update_time": "2026-09-18 09:35:00"}))
    monkeypatch.setattr(PM, "_today_et", lambda: TODAY)
    monkeypatch.setattr(PM, "_now_et_hhmm", lambda: 935)
    monkeypatch.setattr(PM, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, CST_AH_ENTRY, "sig1", '{"stop_loss":9.5}')])
    monkeypatch.setattr(PM, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="ok", stderr=""))
    PM.collect(dry=False)
    row = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert row["action"] == "next_open_exit"
    assert len(calls) == 1
    assert "--session" in calls[0] and "next_open" in calls[0]
    assert "next_open_exit" in calls[0]


def test_premarket_shadow_hands_over_after_window(monkeypatch, tmp_path):
    """09:50 ET (错过窗口) → 交盘中引擎, 不补单."""
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 9.8, "pre_price": 9.8, "pre_volume": 90000.0,
         "prev_close_price": 10.0, "pre_high_price": 9.9, "update_time": "2026-09-18 09:50:00"}))
    monkeypatch.setattr(PM, "_today_et", lambda: TODAY)
    monkeypatch.setattr(PM, "_now_et_hhmm", lambda: 950)
    monkeypatch.setattr(PM, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, CST_AH_ENTRY, "sig1", '{"stop_loss":9.5}')])
    monkeypatch.setattr(PM, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    PM.collect(dry=False)
    assert json.loads((tmp_path / "out.jsonl").read_text().strip())["action"] == "hold_intraday_takeover"
    assert calls == []


def test_afterhours_shadow_suppresses_signals_for_same_day_entries(monkeypatch, tmp_path):
    """盘后影子: 当日盘后进的仓 → 停用利润保护信号, 只留 hold_to_next_open."""
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 10.0, "after_price": 12.5, "after_volume": 90000.0,
         "prev_close_price": 10.0, "update_time": "2026-09-17 18:00:00"}))
    monkeypatch.setattr(AH, "_today_et", lambda: "2026-09-17")
    monkeypatch.setattr(AH, "_now_et", lambda: datetime(2026, 9, 17, 18, 0))
    monkeypatch.setattr(AH, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, CST_AH_ENTRY, "sig1", '{"stop_loss":9.5}')])
    monkeypatch.setattr(AH, "OUTPUT", tmp_path / "ah.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    AH.collect(dry=False)
    row = json.loads((tmp_path / "ah.jsonl").read_text().strip())
    assert row["action"] == "hold_to_next_open"
    assert calls == []                                 # 关键: 盘后不再下单


def test_afterhours_shadow_keeps_rules_for_non_afterhours_entries(monkeypatch, tmp_path):
    """盘中进的仓被带到盘后 → 仍走原规则 (cohort 外, 行为不变)."""
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 10.0, "after_price": 12.5, "after_volume": 90000.0,
         "prev_close_price": 10.0, "update_time": "2026-09-17 18:00:00"}))
    monkeypatch.setattr(AH, "_today_et", lambda: "2026-09-17")
    monkeypatch.setattr(AH, "_now_et", lambda: datetime(2026, 9, 17, 18, 0))
    # CST 2026-09-18 02:30 = ET 9/17 14:30 (盘中进场)
    monkeypatch.setattr(AH, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 02:30:00", "sig1",
         '{"stop_loss":9.9,"take_profit":12.4}')])
    monkeypatch.setattr(AH, "OUTPUT", tmp_path / "ah.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="ok", stderr=""))
    AH.collect(dry=False)
    row = json.loads((tmp_path / "ah.jsonl").read_text().strip())
    assert row["action"] == "TAKE_PROFIT"
    assert len(calls) == 1


def test_exit_executor_has_next_open_session():
    src = (ROOT / "scripts" / "extended_hours_exit_executor.py").read_text()
    assert '"next_open": (930, 945)' in src
    assert '"next_open_exit_disabled"' in src
    assert 'session="ETH"' in src
    assert '"premarket", "afterhours", "next_open"' in src

def test_is_afterhours_carry_with_et_aware_timestamp():
    """生产真实格式: EXT/playbook 写的是 **ET-aware** ISO (实测 AEHL 2026-09-17T07:10:25.538879-04:00),
    不是纯 CST. 两种格式都必须判对."""
    assert PM._entry_et_date("2026-09-17T04:15:00-04:00") == "2026-09-17"          # 盘前仓
    assert PM.is_afterhours_carry("2026-09-17T16:30:00-04:00", "2026-09-18") is True
    assert PM.is_afterhours_carry("2026-09-17T04:15:00-04:00", "2026-09-17") is False
    # ET-aware 时间不得被再当成 CST 平移 12h
    assert PM._entry_et_dt("2026-09-17T07:10:25.538879-04:00").strftime("%H:%M") == "07:10"
