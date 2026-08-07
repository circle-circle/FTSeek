"""VWAP 门 A/B 分臂纯函数单测 (2026-08-07).

2026-08-07 复核补: 集成路径测试 — 原 4 例仅覆盖 _vwap_ab_arm 纯函数,
未能揪出入库时的 3 缺陷 (开关层级错配 / 台账 NameError / B9 符号量纲),
下方补齐开关语义、首次放行、台账唯一、sim 硬守卫与 B9 公式用例.
"""
import json
from pathlib import Path

import pytest

from app.playbook.pipeline import _max_drawdown_from_mfe_mae, _vwap_ab_arm


def test_arm_deterministic_same_input():
    a1 = _vwap_ab_arm("AVGO", "2026-08-07", "BUY")
    a2 = _vwap_ab_arm("AVGO", "2026-08-07", "BUY")
    assert a1 == a2 and a1 in ("bypass", "gated")


def test_arm_varies_across_dimensions():
    # 不同日期/方向应可能换臂 (不强制, 但全同则哈希有问题)
    arms = {_vwap_ab_arm("AVGO", f"2026-08-{d:02d}", "BUY") for d in range(1, 20)}
    assert arms == {"bypass", "gated"}


def test_arm_roughly_balanced():
    # 大样本近似 50/50 (宽松区间 35%-65%)
    n = 400
    bypass = sum(
        1 for i in range(n)
        if _vwap_ab_arm(f"SYM{i}", "2026-08-07", "BUY" if i % 2 else "SELL_SHORT") == "bypass"
    )
    ratio = bypass / n
    assert 0.35 < ratio < 0.65, f"分臂失衡: {ratio:.2%}"


def test_arm_symbol_day_stable_across_calls():
    # 日内稳定性: 同 (symbol,date,direction) 多次调用恒定 (不随轮询漂移)
    for sym in ("GS", "CMG", "BABA"):
        arms = {_vwap_ab_arm(sym, "2026-08-07", "BUY") for _ in range(10)}
        assert len(arms) == 1


# ── 2026-08-07 复核补: 集成路径 (开关语义 / 台账 / 首次放行 / 硬守卫) ──

_LEDGER = Path(__file__).resolve().parents[1] / "data" / "vwap_gate_ab.jsonl"
_TEST_SYM = "ZZTEST_VWAPAB"


@pytest.fixture
def _clean_ab_state():
    """测试后清理进程 set 与台账测试行, 防污染真实实验台账."""
    yield
    import app.playbook.pipeline as _p
    for _lk in list(_p._VWAP_AB_LOGGED):
        if _lk[0] == _TEST_SYM:
            _p._VWAP_AB_LOGGED.discard(_lk)
    if _LEDGER.exists():
        kept = []
        with open(_LEDGER) as _f:
            for _line in _f:
                try:
                    if json.loads(_line).get("symbol") == _TEST_SYM:
                        continue
                except Exception:
                    pass
                kept.append(_line)
        if kept:
            _LEDGER.write_text("".join(kept))
        else:
            _LEDGER.unlink()


class _FakePB:
    """PlaybookConfig 替身: 仅接管 vwap_gate_ab_enabled."""

    def __init__(self, enabled):
        self._enabled = enabled

    def get(self, key, default=None):
        if key == "vwap_gate_ab_enabled":
            return self._enabled
        return default


class _FakeCC:
    """ConfigContainer 替身: 仅接管 playbook.execute_mode."""

    def __init__(self, mode="futu_simulate"):
        self._mode = mode

    def get_dotted(self, path, default=None):
        if path == "playbook.execute_mode":
            return self._mode
        return default


def _ledger_rows():
    if not _LEDGER.exists():
        return []
    rows = []
    with open(_LEDGER) as _f:
        for _line in _f:
            try:
                _j = json.loads(_line)
            except Exception:
                continue
            if _j.get("symbol") == _TEST_SYM:
                rows.append(_j)
    return rows


def test_config_switch_consistent_with_yaml():
    """缺陷A 回归防护: PlaybookConfig 扁平合并读值必须与
    yaml playbook.execution_parameters.vwap_gate_ab_enabled 原始值一致
    (get_dotted 原始嵌套路径读不到该键, 曾致实验从未生效)."""
    import yaml as _y
    from app.playbook.config import get_playbook_config
    _raw = _y.safe_load(open(Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml"))
    expect = bool(_raw["playbook"]["execution_parameters"]["vwap_gate_ab_enabled"])
    assert bool(get_playbook_config().get("vwap_gate_ab_enabled", False)) == expect


def test_first_poll_releases_and_logs_once(monkeypatch, _clean_ab_state):
    """缺陷B 回归: 首次判定即与臂一致 (bypass 不延迟一轮), 台账恰好 1 行且字段完整."""
    import app.playbook.pipeline as _p
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _FakePB(True))
    monkeypatch.setattr("app.core.config_container.get_config", lambda: _FakeCC())
    _d = _p._today_et_str()
    arm = _vwap_ab_arm(_TEST_SYM, _d, "SHORT")
    r1 = _p._vwap_gate_ab_bypass(_TEST_SYM, True, 0.01, 150.0, 151.0, "inst-t")
    r2 = _p._vwap_gate_ab_bypass(_TEST_SYM, True, 0.01, 150.0, 151.0, "inst-t")
    assert r1 == (arm == "bypass") and r2 == r1
    rows = _ledger_rows()
    assert len(rows) == 1
    assert rows[0]["arm"] == arm and rows[0]["vwap"] == 150.0
    assert rows[0]["direction"] == "SHORT" and rows[0]["rule"] == "vwap_gate_ab_50_50"


def test_switch_off_disables(monkeypatch, _clean_ab_state):
    import app.playbook.pipeline as _p
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _FakePB(False))
    monkeypatch.setattr("app.core.config_container.get_config", lambda: _FakeCC())
    assert _p._vwap_gate_ab_bypass(_TEST_SYM, True, 0.01, 150.0, 151.0, "inst-t") is False
    assert _ledger_rows() == []


def test_non_sim_hard_guard(monkeypatch, _clean_ab_state):
    """sim-only 硬守卫: 开关开但 execute_mode 非 futu_simulate → 维持拦截且不落账."""
    import app.playbook.pipeline as _p
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _FakePB(True))
    monkeypatch.setattr("app.core.config_container.get_config", lambda: _FakeCC("futu_live"))
    assert _p._vwap_gate_ab_bypass(_TEST_SYM, True, 0.01, 150.0, 151.0, "inst-t") is False
    assert _ledger_rows() == []


# ── 2026-08-07 复核补: B9 max_drawdown 公式 (正数口径, 百分比量纲) ──


def test_max_drawdown_formula_matches_peak_trough():
    # entry=100, peak=102.99 (mfe=2.99), trough=99.03 (mae=0.97) → (102.99-99.03)/102.99*100
    assert _max_drawdown_from_mfe_mae(2.99, 0.97) == round((102.99 - 99.03) / 102.99 * 100, 4)
    # mfe=5, mae=2 → peak=105, trough=98 → 7/105*100
    assert _max_drawdown_from_mfe_mae(5.0, 2.0) == round(7 / 105 * 100, 4)


def test_max_drawdown_boundaries():
    # 从未水下 (mae=0) / 数据缺失 → 0, 与历史口径一致
    assert _max_drawdown_from_mfe_mae(3.0, 0.0) == 0.0
    assert _max_drawdown_from_mfe_mae(0.0, 1.5) == 0.0
    assert _max_drawdown_from_mfe_mae(0.0, 0.0) == 0.0
