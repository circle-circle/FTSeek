"""日亏熔断单测 (2026-08-27, 组合风控P1 死键接线).

覆盖点 (按"最容易出事"排序):
  1. 触发判据算术: 跌幅 ≤ 阈值 → triggered (≤ 非 <, 恰卡阈值也触发)
  2. 新交易日首评落基线不触发 (基线写盘 + 返回 None)
  3. 净值回升自恢复 (同日两轮: 触发后回升 → 放行)
  4. fail-open: 净值源异常 → None 不误拦 (与 R1b 预算门 fail-close 对比:
     日亏门数据不可用 ≠ 预算未知, 混尺子误触发比漏一轮更危险)
  5. 同尺保护: 净值 0/缺失 → None (不比较)
  6. 阈值读 yaml SSOT (risk.circuit_breaker.strategy.daily_loss_threshold)
  7. 基线文件损坏 → 当作新日重落, 不炸
"""
import json

import pytest

from app.playbook import pipeline as P


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例独立基线文件 + 独立净值源, 不碰生产 data/ 与 capital_safety 单例."""
    monkeypatch.setattr(P, "_DAILY_LOSS_BASELINE", tmp_path / "baseline.json")
    yield


def _set_equity(monkeypatch, eq):
    class _St:
        current_equity = eq

    class _CS:
        _state = _St() if eq is not None else None

    import app.risk.capital_safety as cs_mod
    monkeypatch.setattr(cs_mod, "get_capital_safety_system", lambda: _CS())


def _write_base(tmp_path, today, eq):
    P._DAILY_LOSS_BASELINE.write_text(json.dumps({"date": today, "equity": eq}))


def _set_threshold(monkeypatch, thr):
    class _Cfg:
        def get_dotted(self, key, default=None):
            if key == "risk.circuit_breaker.strategy.daily_loss_threshold":
                return thr
            return default

    import app.core.config_container as cc
    monkeypatch.setattr(cc, "get_config", lambda: _Cfg())


# ── 1. 触发判据 ──
def test_triggers_at_threshold_and_beyond(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-27", 100000)
    _set_threshold(monkeypatch, -0.03)
    _set_equity(monkeypatch, 97000)          # 恰 -3.0% → ≤ 触发
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r and r["triggered"] is True
    assert r["loss_pct"] == -3.0

    _set_equity(monkeypatch, 95000)          # -5% → 触发
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r["triggered"] is True


def test_no_trigger_within_threshold(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-27", 100000)
    _set_threshold(monkeypatch, -0.03)
    _set_equity(monkeypatch, 98000)          # -2% → 放行
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r["triggered"] is False


# ── 2. 新交易日首评: 落基线不触发 ──
def test_first_eval_of_day_writes_baseline(tmp_path, monkeypatch):
    _set_equity(monkeypatch, 97000)
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r is None                          # 首评无比较
    base = json.loads(P._DAILY_LOSS_BASELINE.read_text())
    assert base["date"] == "2026-08-27" and base["equity"] == 97000

    _set_equity(monkeypatch, 90000)           # 下一轮 -7.2% → 触发
    assert P._check_daily_loss_circuit("2026-08-27")["triggered"] is True


def test_stale_baseline_from_prior_day_rewritten(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-26", 100000)
    _set_equity(monkeypatch, 99000)
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r is None                          # 日期不符 → 重落基线
    base = json.loads(P._DAILY_LOSS_BASELINE.read_text())
    assert base["date"] == "2026-08-27" and base["equity"] == 99000


# ── 3. 自恢复 ──
def test_recovers_when_equity_rebounds(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-27", 100000)
    _set_threshold(monkeypatch, -0.03)
    _set_equity(monkeypatch, 96000)           # -4% 触发
    assert P._check_daily_loss_circuit("2026-08-27")["triggered"] is True
    _set_equity(monkeypatch, 98000)           # 回升 -2% → 放行, 无需人工复位
    assert P._check_daily_loss_circuit("2026-08-27")["triggered"] is False


# ── 4. fail-open ──
def test_equity_source_exception_fails_open(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-27", 100000)
    import app.risk.capital_safety as cs_mod

    def _boom():
        raise RuntimeError("accinfo down")

    monkeypatch.setattr(cs_mod, "get_capital_safety_system", _boom)
    assert P._check_daily_loss_circuit("2026-08-27") is None


# ── 5. 同尺保护 ──
def test_zero_or_missing_equity_skips(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-27", 100000)
    _set_equity(monkeypatch, 0)               # 净值缺失 → 不比较 (防混尺假触发)
    assert P._check_daily_loss_circuit("2026-08-27") is None
    _set_equity(monkeypatch, None)            # _state None → 同上
    assert P._check_daily_loss_circuit("2026-08-27") is None


# ── 6. 阈值 SSOT ──
def test_threshold_read_from_config(tmp_path, monkeypatch):
    _write_base(tmp_path, "2026-08-27", 100000)
    _set_threshold(monkeypatch, -0.01)        # 收紧到 -1%
    _set_equity(monkeypatch, 98500)           # -1.5% → 按配置阈值触发
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r["triggered"] is True and r["threshold_pct"] == -1.0


# ── 7. 基线文件损坏 ──
def test_corrupt_baseline_treated_as_new_day(tmp_path, monkeypatch):
    P._DAILY_LOSS_BASELINE.write_text("not json{{{")
    _set_equity(monkeypatch, 99000)
    r = P._check_daily_loss_circuit("2026-08-27")
    assert r is None                          # 当作新日重落, 不炸
    base = json.loads(P._DAILY_LOSS_BASELINE.read_text())
    assert base["equity"] == 99000
