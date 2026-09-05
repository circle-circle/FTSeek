"""playbook 日亏熔断的滞回带 (2026-09-05, 用户裁"按照建议执行").

被测: app/playbook/pipeline.py `_check_daily_loss_circuit` — playbook 唯一一条
**看账面浮亏**的账户级闸 (富途实时净值 vs 当日首评基线), 也是唯一一条原本就带
"自恢复"语义的闸 (docstring 原话: 净值回升过阈即放行, 无 pause 状态机)。

立起的失效模式: 放行线 == 触发线 ⇒ 净值在 -3% 附近来回时, 每轮 auto_execute 独立重算
→ 闸门逐轮 on/off 抖动。后果不是"多拦一笔"而是**同一交易日内入场口径不稳定**:
采到的样本分不清"被熔断拦下"还是"恰好那一轮放行", 直接污染数据采集模式的对照性。

现语义: ≤-3% 触发并锁存 → 回升到 ≥-2% 才解除 → -3%~-2% 之间维持触发态。
latch 与基线同文件持久化 (data/daily_loss_baseline.json), 跨重启不丢, 新交易日首评清零。
健全性守卫: 放行线若被配得比触发线更负 (永不解除方向), 代码退回无滞回 — 宁可抖动
也不把闸门焊死 (焊死=当日剩余时段全停新开, 与数据采集目标冲突)。

回滚: 删本文件 + `daily_loss_release_threshold` 改成与 `daily_loss_threshold` 同值。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import app.playbook.pipeline as pipe


class _FakeState:
    def __init__(self, equity):
        self.current_equity = equity


class _FakeCapitalSafety:
    def __init__(self, equity):
        self._state = _FakeState(equity)


class _FakeCfg:
    def __init__(self, thr=-0.03, rel=-0.02):
        self._d = {
            "risk.circuit_breaker.strategy.daily_loss_threshold": thr,
            "risk.circuit_breaker.strategy.daily_loss_release_threshold": rel,
        }

    def get_dotted(self, key, default=None):
        return self._d.get(key, default)


BASE = 100_000.0
TODAY = "2026-09-08"


@pytest.fixture
def circuit(tmp_path, monkeypatch):
    """返回 call(equity, cfg) → 结果 dict; 基线文件隔离到 tmp_path."""
    baseline = tmp_path / "daily_loss_baseline.json"
    monkeypatch.setattr(pipe, "_DAILY_LOSS_BASELINE", baseline)

    import app.risk.capital_safety as cs_mod
    import app.core.config_container as cc_mod
    box = {"equity": BASE, "cfg": _FakeCfg()}
    monkeypatch.setattr(cs_mod, "get_capital_safety_system",
                        lambda: _FakeCapitalSafety(box["equity"]))
    monkeypatch.setattr(cc_mod, "get_config", lambda: box["cfg"])

    def call(equity, cfg=None, today=TODAY):
        box["equity"] = equity
        if cfg is not None:
            box["cfg"] = cfg
        return pipe._check_daily_loss_circuit(today)

    return call, baseline


def _read(baseline):
    return json.loads(baseline.read_text()) if baseline.exists() else {}


def test_first_evaluation_seeds_baseline_and_returns_none(circuit):
    call, baseline = circuit
    assert call(BASE) is None, "新交易日首评只落基线, 不判定 (下一轮起可比)"
    assert _read(baseline)["date"] == TODAY
    assert _read(baseline)["latched"] is False, "新交易日 latch 必须清零"


def test_breach_triggers_and_persists_latch(circuit):
    call, baseline = circuit
    call(BASE)                                   # 首评落基线
    r = call(BASE * 0.965)                       # -3.5% ≤ -3%
    assert r["triggered"] is True and r["raw_breach"] is True
    assert _read(baseline)["latched"] is True, "latch 必须落盘 (跨重启不丢触发态)"


def test_inside_band_stays_triggered(circuit):
    """核心断言: -3%~-2% 的滞回带内维持触发态, 不逐轮抖动."""
    call, baseline = circuit
    call(BASE)
    assert call(BASE * 0.965)["triggered"] is True       # -3.5% 触发
    r = call(BASE * 0.975)                               # -2.5% 回升进带内
    assert r["triggered"] is True, "带内必须维持触发 (否则放行线==触发线, 逐轮抖动)"
    assert r["raw_breach"] is False, "raw_breach 记原始越线与否 — 与 latch 态可区分"
    assert r["release_threshold_pct"] == pytest.approx(-2.0)


def test_recovery_past_release_line_unlatches(circuit):
    call, baseline = circuit
    call(BASE)
    call(BASE * 0.965)                                   # 触发
    r = call(BASE * 0.985)                               # -1.5% ≥ -2% 放行线
    assert r["triggered"] is False
    assert _read(baseline)["latched"] is False


def test_band_does_not_block_when_never_triggered(circuit):
    """滞回只作用于**已触发**态: 从未触发时 -2.5% 不该被拦 (否则等于收紧阈值)."""
    call, _ = circuit
    call(BASE)
    r = call(BASE * 0.975)                               # 直接 -2.5%, 未曾触发
    assert r["triggered"] is False
    assert r["raw_breach"] is False


def test_misconfigured_release_line_falls_back_to_no_hysteresis(circuit):
    """放行线比触发线更负 = 触发后永不解除 (焊死方向) → 自动退回无滞回."""
    call, _ = circuit
    call(BASE)
    bad = _FakeCfg(thr=-0.03, rel=-0.05)
    assert call(BASE * 0.965, cfg=bad)["triggered"] is True
    r = call(BASE * 0.975, cfg=bad)                      # -2.5%: 无滞回 ⇒ 立即放行
    assert r["triggered"] is False, "健全性守卫: 不得把闸门焊死"
    assert r["release_threshold_pct"] == pytest.approx(-3.0), "退回时放行线==触发线"


def test_latch_survives_restart(circuit):
    """latch 在基线文件里 → 进程重启 (重新 import) 后仍保持触发态."""
    call, baseline = circuit
    call(BASE)
    call(BASE * 0.965)
    assert _read(baseline)["latched"] is True
    # 模拟重启: 文件仍在, 新一轮调用直接读到 latch (带内不放行)
    r = call(BASE * 0.975)
    assert r["triggered"] is True


def test_equity_unavailable_fail_open(circuit):
    call, _ = circuit
    call(BASE)
    assert call(0) is None, "净值不可用 → 本轮跳过 (原有 fail-open 语义不变)"


def test_flip_written_once_per_state_change(circuit):
    """状态翻转才写盘: 带内反复调用不应把 flip_at 刷成新时刻 (20s/5min 级调用不每轮写盘)."""
    call, baseline = circuit
    call(BASE)
    call(BASE * 0.965)
    first_flip = _read(baseline)["flip_at"]
    call(BASE * 0.975)
    call(BASE * 0.974)
    assert _read(baseline)["flip_at"] == first_flip, "带内无翻转 → 不应重写落盘态"
    assert _read(baseline)["latched"] is True


def test_exact_release_line_unlatches_despite_float_noise(circuit):
    """恰卡放行线 (-2.00%) 必须解除 — 锁浮点容差.

    98000/100000-1 在 IEEE754 下 = -0.020000000000000018, 严格小于 -0.02 →
    无容差时"回升到放行线"被判成仍在带内, 解除**永久失效** (旧测
    tests/test_daily_loss_circuit.py::test_recovers_when_equity_rebounds 即此撞红)。
    """
    call, _ = circuit
    call(BASE)
    call(BASE * 0.965)                                   # 触发
    r = call(BASE * 0.98)                                # 恰 -2.00% = 放行线
    assert r["triggered"] is False, "恰卡放行线应解除 (容差方向: 卡线即放行)"
    assert r["loss_pct"] == pytest.approx(-2.0)


def test_exact_trigger_line_triggers(circuit):
    """恰卡触发线 (-3.00%) 必须触发 — 容差不得把触发方向也放松."""
    call, _ = circuit
    call(BASE)
    r = call(BASE * 0.97)                                # 恰 -3.00%
    assert r["triggered"] is True and r["raw_breach"] is True
