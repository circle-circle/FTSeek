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


def test_flatten_is_fallback_to_profit_protection():
    """有效止盈优先；无有效止盈时，盘前收口仅作为兜底."""
    from scripts.extended_hours_profit_exit_rules import prioritize_profit_exit

    for action in ("TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK"):
        assert prioritize_profit_exit(action, "pre_close_flatten") == action
    assert prioritize_profit_exit("hold_no_stop", "pre_close_flatten") == "pre_close_flatten"

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
                               "bid_price": 9.7, "ask_price": 9.71,
                               "pre_volume": 90000.0, "prev_close_price": 10.0,
                               "pre_high_price": 9.9, "update_time": "2026-09-18 09:25:00"})

    fake_futu = types.ModuleType("futu")
    fake_futu.RET_OK = 0
    fake_futu.OpenQuoteContext = lambda **kw: types.SimpleNamespace(
        get_market_snapshot=lambda syms: (0, _FakeSnap()), close=lambda: None)
    monkeypatch.setitem(_sys.modules, "futu", fake_futu)

    monkeypatch.setattr(M, "_today_et", lambda: TODAY)
    monkeypatch.setattr(M, "_now_et_hhmm", lambda: 925)
    # 今日盘前开仓 (CST 16:30 = ET 04:30), 成本 10.0 → bid 9.7 = -3%.
    monkeypatch.setattr(M, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 16:30:00", "sig1", None)])
    monkeypatch.setattr(M, "OUTPUT", tmp_path / "out.jsonl")

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(_sp, "run", _fake_run)
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
                           "bid_price": 9.7, "ask_price": 9.71,
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
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 16:30:00", "sig1", None)])
    monkeypatch.setattr(M, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: calls.setdefault("cmd", cmd)
                        or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    M.collect(dry=False)
    row = _json.loads((tmp_path / "out.jsonl").read_text().strip())
    # 09:19 未被强平覆盖; 延长时段亏损侧只能 hold_no_stop.
    assert row["action"] == "hold_no_stop"


# ── RFC-087 (2026-09-19) 收口豁免预注册: 条件收口判据 + 影子字段 ──────────────

def test_conditional_flatten_verdict_boundaries():
    """纯函数边界表: unconditional 恒收口; conditional/band 的四个分支; 拼错模式 fail-safe."""
    # unconditional (现役默认) ⇒ 恒收口, 与浮盈/武装无关
    for pnl in (5.0, 0.0, -0.1, -2.9, -3.0, -5.9, -6.0, -20.0):
        assert M.conditional_flatten_verdict(pnl, False, mode="unconditional") == (True, "unconditional")
    # 未识别模式 ⇒ 按 unconditional (拼错开关名不得意外放宽风控)
    assert M.conditional_flatten_verdict(-4.0, False, mode="conditonal")[0] is True
    # conditional: 盈利 / 已武装 ⇒ 收口
    assert M.conditional_flatten_verdict(0.01, False, mode="conditional") == (True, "profit")
    assert M.conditional_flatten_verdict(-4.0, True, mode="conditional") == (True, "armed")
    # conditional: 浅亏 (-3~0) / 中亏 (-6~-3) ⇒ 豁免 (交 RTH)
    assert M.conditional_flatten_verdict(-0.5, False, mode="conditional") == (False, "shallow_loss")
    assert M.conditional_flatten_verdict(-3.0, False, mode="conditional") == (False, "shallow_loss")
    assert M.conditional_flatten_verdict(-3.01, False, mode="conditional") == (False, "mid_loss")
    assert M.conditional_flatten_verdict(-5.99, False, mode="conditional") == (False, "mid_loss")
    # conditional: 深亏也豁免 (无条件豁免的极端版本 —— 这正是"不该无条件豁免"的那档)
    assert M.conditional_flatten_verdict(-6.5, False, mode="conditional")[0] is False
    # band: 深亏 (≤ -6) 仍收口; -6.0 含等号
    assert M.conditional_flatten_verdict(-6.0, False, mode="band") == (True, "deep_loss")
    assert M.conditional_flatten_verdict(-6.5, False, mode="band") == (True, "deep_loss")
    assert M.conditional_flatten_verdict(-5.9, False, mode="band") == (False, "mid_loss")
    # 缺报价 ⇒ 保守收口 (不因缺数据放宽)
    assert M.conditional_flatten_verdict(None, False, mode="conditional") == (True, "no_quote")
    # 深亏界可覆盖
    assert M.conditional_flatten_verdict(-4.0, False, mode="band", deep_loss_pct=-4.0) == (True, "deep_loss")


def _snap_row(bid):
    class _FakeSnap:
        def iterrows(self):
            yield 0, dict({"code": "US.FAKE", "last_price": bid, "pre_price": bid,
                           "bid_price": bid, "ask_price": round(bid * 1.001, 4),
                           "pre_volume": 90000.0, "prev_close_price": 10.0,
                           "pre_high_price": 9.9, "update_time": "2026-09-18 09:25:00"})
    return _FakeSnap()


def _run_collect(monkeypatch, tmp_path, bid, mode):
    """跑一轮 collect, 返回 (调用记录, 影子行)."""
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import types

    calls = {}
    fake_futu = types.ModuleType("futu")
    fake_futu.RET_OK = 0
    fake_futu.OpenQuoteContext = lambda **kw: types.SimpleNamespace(
        get_market_snapshot=lambda syms: (0, _snap_row(bid)), close=lambda: None)
    monkeypatch.setitem(_sys.modules, "futu", fake_futu)
    monkeypatch.setattr(M, "FLATTEN_MODE", mode)
    monkeypatch.setattr(M, "_today_et", lambda: TODAY)
    monkeypatch.setattr(M, "_now_et_hhmm", lambda: 925)
    monkeypatch.setattr(M, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 16:30:00", "sig1", None)])
    monkeypatch.setattr(M, "OUTPUT", tmp_path / "out.jsonl")

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(_sp, "run", _fake_run)
    M.collect(dry=False)
    row = _json.loads((tmp_path / "out.jsonl").read_text().strip())
    return calls, row


def test_collect_conditional_mode_hands_off_shallow_loss(monkeypatch, tmp_path):
    """A 条件收口: 浅亏 (-3%) 未武装 ⇒ 不收口 (action 落回亏损侧 hold_no_stop), 且
    影子行必须写明 verdict=False / reason=shallow_loss — 这是后续 join 的唯一凭据。"""
    calls, row = _run_collect(monkeypatch, tmp_path, 9.7, "conditional")
    assert "cmd" not in calls, "conditional 模式下浅亏仓不得调 exit executor"
    assert row["action"] == "hold_no_stop"
    assert row["cond_flatten_verdict"] is False
    assert row["cond_flatten_reason"] == "shallow_loss"
    assert row["flatten_mode"] == "conditional"
    assert row["limit_only"] is True


def test_collect_band_mode_still_flattens_deep_loss(monkeypatch, tmp_path):
    """B 分档豁免: 深亏 (-6.5%, 破位档) **仍收口** — 免得把最大单笔损失类别放进来。"""
    calls, row = _run_collect(monkeypatch, tmp_path, 9.35, "band")
    assert "pre_close_flatten" in calls.get("cmd", [])
    assert row["action"] == "pre_close_flatten"
    assert row["cond_flatten_reason"] == "deep_loss"


def test_collect_conditional_mode_keeps_profit_flatten(monkeypatch, tmp_path):
    """A 条件收口对**盈利仓**零变化: 仍收口 (锁盈语义不变)。"""
    calls, row = _run_collect(monkeypatch, tmp_path, 10.3, "conditional")
    assert "pre_close_flatten" in calls.get("cmd", [])
    assert row["action"] == "pre_close_flatten"
    assert row["cond_flatten_reason"] == "profit"
