#!/usr/bin/env python3
"""延长时段「只做止盈, 不做止损」(2026-09-17 用户令) 回归测试.

2026-09-18 用户勘误: 保留原 SL_BREAKEVEN / SL_TRAIL_LOCK / TAKE_PROFIT,
决策价改为 executable bid/ask + 净费用口径; SL_INITIAL 只能 hold_no_stop.
"""
import importlib.util
import json
import os
import pathlib
import subprocess as _sp
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PM = _load("pm_tponly_test", "scripts/premarket_exit_shadow.py")
AH = _load("ah_tponly_test", "scripts/afterhours_exit_shadow.py")
EE = _load("ee_tponly_test", "scripts/extended_hours_entry_executor.py")

TODAY = "2026-09-18"
# 盘中进场的 carry 仓 (ET 9/17 14:30) —— 不是盘前/盘后 cohort
CST_RTH_ENTRY = "2026-09-18 02:30:00"


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


def _pm_run(monkeypatch, tmp_path, pnl_px, hhmm, entry=CST_RTH_ENTRY,
            trade_metadata='{"stop_loss":9.5}'):
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": pnl_px, "pre_price": pnl_px, "pre_volume": 90000.0,
         "prev_close_price": 10.0, "pre_high_price": pnl_px,
         "update_time": f"2026-09-18 {hhmm // 100:02d}:{hhmm % 100:02d}:00"}))
    monkeypatch.setattr(PM, "_today_et", lambda: TODAY)
    monkeypatch.setattr(PM, "_now_et_hhmm", lambda: hhmm)
    monkeypatch.setattr(PM, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, entry, "sig1", trade_metadata)])
    monkeypatch.setattr(PM, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    PM.collect(dry=False)
    return json.loads((tmp_path / "out.jsonl").read_text().strip()), calls


def test_premarket_no_stop_for_carried_position(monkeypatch, tmp_path):
    """持仓 -3% → 延长时段**不动作** (止损放回盘中)."""
    row, calls = _pm_run(monkeypatch, tmp_path, 9.7, 915)
    assert row["action"] == "hold_no_stop"
    assert row["decision_detail"] == "underwater_no_extended_loss_stop"
    assert calls == []                                 # 关键: 没有下平仓单


def test_premarket_take_profit_still_fires(monkeypatch, tmp_path):
    """原 TAKE_PROFIT 线触达 → 仍真平仓, 且下单价用 LONG bid."""
    row, calls = _pm_run(
        monkeypatch, tmp_path, 10.25, 915,
        trade_metadata='{"stop_loss":9.9,"take_profit":10.2}',
    )
    assert row["action"] == "TAKE_PROFIT"
    assert len(calls) == 1
    assert "10.25" in calls[0]


def test_premarket_breakeven_uses_net_line_as_limit_floor(monkeypatch, tmp_path):
    row, calls = _pm_run(
        monkeypatch, tmp_path, 10.03, 915,
        trade_metadata='{"stop_loss":9.5,"peak_price":10.05}',
    )
    assert row["action"] == "SL_BREAKEVEN"
    assert row["executor_limit_price"] == pytest.approx(10.0462, abs=0.0001)
    assert len(calls) == 1
    assert "10.0462" in calls[0]


def test_afterhours_no_stop_for_carried_position(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 9.7, "after_price": 9.7, "after_volume": 90000.0,
         "prev_close_price": 10.0, "update_time": "2026-09-17 18:00:00"}))
    monkeypatch.setattr(AH, "_today_et", lambda: "2026-09-17")
    monkeypatch.setattr(AH, "_now_et", lambda: __import__("datetime").datetime(2026, 9, 17, 18, 0))
    monkeypatch.setattr(AH, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, CST_RTH_ENTRY, "sig1", '{"stop_loss":9.5}')])
    monkeypatch.setattr(AH, "OUTPUT", tmp_path / "ah.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    AH.collect(dry=False)
    row = json.loads((tmp_path / "ah.jsonl").read_text().strip())
    assert row["action"] == "hold_no_stop"
    assert calls == []


def test_entry_switch_off_blocks_sim_orders(monkeypatch):
    """EXT_ENTRY_ALLOW_SIM=false ⇒ 延长时段不再开新仓 (fail-closed 早退)."""
    monkeypatch.setattr(EE, "ALLOW_SIM_ENTRY", False)
    monkeypatch.setattr(EE, "_is_trading_day", lambda: True)
    monkeypatch.setattr(EE, "_in_session_window", lambda session: True)
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    out = EE.collect("premarket", dry=False)
    assert out and out[0].get("skip") == "simulate_stop_unsupported_fail_closed"
    assert out[0].get("ok") is False


@pytest.mark.parametrize("module", [PM, AH])
def test_parent_timeout_always_covers_exit_limit_timeout(monkeypatch, module):
    """2026-09-18 R2 修正: 父进程上限必须显著大于子进程限价等待, 且旧值 90 是空操作.

    原实现 max(90, t+40) 在 t=20 (默认) 时恒等于 90 = 改动前的硬编码值。
    """
    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "20")
    assert module._executor_timeout() == 180.0          # 不再是 90 (空操作)
    assert module._executor_timeout() > 90.0

    # env 调大子进程限价等待时, 父进程上限必须跟着涨 (旧式 max(90,..) 在 t>50 才动)
    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "120")
    assert module._executor_timeout() == 210.0

    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "400")
    assert module._executor_timeout() == 490.0
    assert module._executor_timeout() > float(os.environ["EXT_EXIT_LIMIT_TIMEOUT_S"])


def test_timeout_chain_is_monotonic(monkeypatch):
    """链条单调性: cron(等待 shadow) > shadow(等待 exit_executor) > 子进程限价等待.

    2026-09-18 R2 实锤的反面教训: 只把 shadow 从 90 抬到 180 而不动 cron 的 120,
    cron 会先杀 shadow ⇒ 孙进程仍在跑 ⇒ 孤儿单 + 零证据, 比不改更糟。
    """
    import inspect
    import scripts.extended_hours_exit_cron as XC

    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "20")
    child_limit = float(os.environ["EXT_EXIT_LIMIT_TIMEOUT_S"])
    shadow = min(PM._executor_timeout(), AH._executor_timeout())
    cron = inspect.signature(XC.run_once).parameters["timeout_s"].default

    assert child_limit < shadow < cron, f"链条断裂: child={child_limit} shadow={shadow} cron={cron}"
