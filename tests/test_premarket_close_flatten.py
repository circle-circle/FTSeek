#!/usr/bin/env python3
"""盘前收口强平 (2026-09-17 回放裁决) 回归测试.

覆盖: ① 三个必要条件 (开关/窗内且≥09:20/今日盘前开仓) 缺一不可
      ② `_entry_et_date` 的 CST→ET 真转换 (跨日必须靠转换, 不能取字符串前10位)
"""
import importlib.util
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    p = ROOT / "scripts" / "premarket_exit_shadow.py"
    spec = importlib.util.spec_from_file_location("pm_shadow_flatten_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()
TODAY = "2026-09-18"


def test_entry_et_date_converts_cst_to_et_across_day():
    # CST 9/18 16:30 → ET 9/18 04:30 (盘前开仓) → 同一日
    assert M._entry_et_date("2026-09-18 16:30:00") == TODAY
    # CST 9/18 21:25 → ET 9/18 09:25 (盘前尾) → 同一日
    assert M._entry_et_date("2026-09-18 21:25:00") == TODAY
    # CST 9/18 00:30 → ET 9/17 12:30 (**跨日**) → 取字符串前 10 位会算错成 9/18
    assert M._entry_et_date("2026-09-18 00:30:00") == "2026-09-17"
    # datetime 对象同样支持
    assert M._entry_et_date(datetime(2026, 9, 18, 16, 30, 0)) == TODAY


def test_flatten_requires_all_three_conditions():
    today_entry = "2026-09-18 16:30:00"
    # ① 时间未到 09:20
    assert M.flatten_now(919, today_entry, TODAY, True) is False
    # ② 到点 + 窗内 + 今日仓 → 强平
    assert M.flatten_now(920, today_entry, TODAY, True) is True
    assert M.flatten_now(925, today_entry, TODAY, True) is True
    # ③ 窗外 (cron 明明不在盘前窗)
    assert M.flatten_now(920, today_entry, TODAY, False) is False
    # ④ 开关关 (回滚路径)
    assert M.flatten_now(920, today_entry, TODAY, True, enabled=False) is False


def test_flatten_skips_overnight_and_legacy_positions():
    """隔夜/legacy 仓**不在**回放覆盖的 cohort 内 → 不得被强平."""
    # 昨夜 CST 22:00 建的仓 = ET 9/17 10:00 → ET 日期 9/17 ≠ 今日 9/18
    assert M.flatten_now(920, "2026-09-17 22:00:00", TODAY, True) is False
    # 更早的 legacy 仓
    assert M.flatten_now(920, "2026-09-10 16:05:00", TODAY, True) is False


def test_flatten_action_overrides_rule_signals():
    """强平优先级高于 lock_in/breakeven_lock/tighten (回放: 强平 >> 现役规则 >> 止损)."""
    src = (ROOT / "scripts" / "premarket_exit_shadow.py").read_text()
    assert 'action = "pre_close_flatten"' in src
    assert '"lock_in", "breakeven_lock", "tighten", "pre_close_flatten"' in src
    assert 'EXT_PREMARKET_FLATTEN' in src

def test_collect_wires_flatten_to_exit_executor(monkeypatch, tmp_path):
    """接线级验证 (防"纯函数绿但没接上"): 09:25 时今日盘前仓 → 子进程必须收到
    --reason pre_close_flatten, 且**覆盖** decide() 的 tighten."""
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import types

    calls = {}

    class _FakeRow(dict):
        pass

    class _FakeSnap:
        def iterrows(self):
            yield 0, _FakeRow({"code": "US.FAKE", "last_price": 9.7, "pre_price": 9.7,
                               "pre_volume": 90000.0, "prev_close_price": 10.0,
                               "pre_high_price": 9.9, "update_time": "2026-09-18 09:25:00"})

    fake_futu = types.ModuleType("futu")
    fake_futu.RET_OK = 0
    fake_futu.OpenQuoteContext = lambda **kw: types.SimpleNamespace(
        get_market_snapshot=lambda syms: (0, _FakeSnap()), close=lambda: None)
    monkeypatch.setitem(_sys.modules, "futu", fake_futu)

    monkeypatch.setattr(M, "_today_et", lambda: TODAY)
    monkeypatch.setattr(M, "_now_et_hhmm", lambda: 925)
    # 今日盘前开仓 (CST 16:30 = ET 04:30), 成本 10.0 → 现价 9.7 = -3% ⇒ decide() 会出 tighten
    monkeypatch.setattr(M, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 16:30:00", "sig1")])
    monkeypatch.setattr(M, "OUTPUT", tmp_path / "out.jsonl")

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(_sp, "run", _fake_run)
    assert M.decide(-3.0, 90000.0) == "tighten"          # 前提: 原规则会给 tighten
    n = M.collect(dry=False)
    assert n == 1
    assert "--reason" in calls["cmd"] and "pre_close_flatten" in calls["cmd"]
    row = _json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert row["action"] == "pre_close_flatten"
    assert row["is_today_entry"] is True


def test_collect_does_not_flatten_before_0920(monkeypatch, tmp_path):
    """同一持仓在 09:19 不得被强平 (仍走原规则 tighten → 保持原行为)."""
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import types

    calls = {}

    class _FakeSnap:
        def iterrows(self):
            yield 0, dict({"code": "US.FAKE", "last_price": 9.7, "pre_price": 9.7,
                           "pre_volume": 90000.0, "prev_close_price": 10.0,
                           "pre_high_price": 9.9, "update_time": "2026-09-18 09:19:00"})

    fake_futu = types.ModuleType("futu")
    fake_futu.RET_OK = 0
    fake_futu.OpenQuoteContext = lambda **kw: types.SimpleNamespace(
        get_market_snapshot=lambda syms: (0, _FakeSnap()), close=lambda: None)
    monkeypatch.setitem(_sys.modules, "futu", fake_futu)
    monkeypatch.setattr(M, "_today_et", lambda: TODAY)
    monkeypatch.setattr(M, "_now_et_hhmm", lambda: 919)
    monkeypatch.setattr(M, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 16:30:00", "sig1")])
    monkeypatch.setattr(M, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.setdefault("cmd", cmd)
                        or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    M.collect(dry=False)
    row = _json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert row["action"] == "tighten"                    # 未被强平覆盖
