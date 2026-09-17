#!/usr/bin/env python3
"""延长时段「只做止盈, 不做止损」(2026-09-17 用户令) 回归测试.

依据 (自建盘前影子台账, 20 个真实盘中持仓):
  现役组合(回本锁+0.3%/止盈+2% + 盘前止损-1.5%) -0.11%; 去掉盘前止损 -0.07%;
  只止盈 +1.5%/+2%/+3% = +0.42%/+0.39%/+0.38%; 什么都不做 +0.21% ⇒ 盘前止损负贡献。
"""
import importlib.util
import json
import pathlib
import subprocess as _sp
import sys
import types

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
    class _Snap:
        def iterrows(self):
            yield 0, dict(fields)

    mod = types.ModuleType("futu")
    mod.RET_OK = 0
    mod.OpenQuoteContext = lambda **kw: types.SimpleNamespace(
        get_market_snapshot=lambda syms: (0, _Snap()), close=lambda: None)
    return mod


def _pm_run(monkeypatch, tmp_path, pnl_px, hhmm, entry=CST_RTH_ENTRY, tp_only=True):
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": pnl_px, "pre_price": pnl_px, "pre_volume": 90000.0,
         "prev_close_price": 10.0, "pre_high_price": pnl_px,
         "update_time": f"2026-09-18 {hhmm // 100:02d}:{hhmm % 100:02d}:00"}))
    monkeypatch.setattr(PM, "_today_et", lambda: TODAY)
    monkeypatch.setattr(PM, "_now_et_hhmm", lambda: hhmm)
    monkeypatch.setattr(PM, "TAKE_PROFIT_ONLY", tp_only)
    monkeypatch.setattr(PM, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, entry, "sig1")])
    monkeypatch.setattr(PM, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    PM.collect(dry=False)
    return json.loads((tmp_path / "out.jsonl").read_text().strip()), calls


def test_premarket_no_stop_for_carried_position(monkeypatch, tmp_path):
    """持仓 -3% → 延长时段**不动作** (止损放回盘中)."""
    assert PM.decide(-3.0, 90000.0) == "tighten"      # 前提: 原规则会止损
    row, calls = _pm_run(monkeypatch, tmp_path, 9.7, 915)
    assert row["action"] == "hold_no_stop"
    assert calls == []                                 # 关键: 没有下平仓单


def test_premarket_stop_returns_when_tp_only_off(monkeypatch, tmp_path):
    """回滚路径: EXT_EXIT_TAKE_PROFIT_ONLY=false → 恢复盘前止损."""
    row, calls = _pm_run(monkeypatch, tmp_path, 9.7, 915, tp_only=False)
    assert row["action"] == "tighten"
    assert len(calls) == 1


def test_premarket_take_profit_still_fires(monkeypatch, tmp_path):
    """止盈不受影响: +2.5% 有量 → lock_in 仍真平仓."""
    row, calls = _pm_run(monkeypatch, tmp_path, 10.25, 915)
    assert row["action"] == "lock_in"
    assert len(calls) == 1


def test_afterhours_no_stop_for_carried_position(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(sys.modules, "futu", _futu_stub(
        {"code": "US.FAKE", "last_price": 9.7, "after_price": 9.7, "after_volume": 90000.0,
         "prev_close_price": 10.0, "update_time": "2026-09-17 18:00:00"}))
    monkeypatch.setattr(AH, "_today_et", lambda: "2026-09-17")
    monkeypatch.setattr(AH, "_now_et", lambda: __import__("datetime").datetime(2026, 9, 17, 18, 0))
    monkeypatch.setattr(AH, "TAKE_PROFIT_ONLY", True)
    monkeypatch.setattr(AH, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, CST_RTH_ENTRY, "sig1")])
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
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    out = EE.collect("premarket", dry=False)
    assert out and out[0].get("skip") == "simulate_stop_unsupported_fail_closed"
    assert out[0].get("ok") is False