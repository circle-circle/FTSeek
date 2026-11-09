"""VIX 水平/变化分离的契约 (RFC-190 第六轮, 2026-11-09)。

守四件:
  1. 估计 = T-1 真值 × (1 + beta×VIXY 当日收益), beta 标定 2.0; 极端收益截断;
  2. 输入不全 ⇒ None (不猜), 绝不用 0/20 兜底;
  3. 跳变只报上跳, 且同日只报一次; 锚点过期 (>5 天) 不报;
  4. 级别判定 (level/message) 与估计解耦 —— 加估计不改变既有告警语义。
"""
from datetime import datetime

import pytest

from app.data import vix_estimator as ve
from app.risk import vix_monitor as vm


def test_estimate_math_matches_calibration():
    # 11-05 样式: 真值 16.61, VIXY 16.00 → 20.80 (+30%) ⇒ VIX ≈ 16.61×(1+2×0.30)
    assert ve.estimate_level(16.61, 16.0, 20.8) == pytest.approx(16.61 * 1.6, abs=0.01)


def test_estimate_clips_extreme_proxy_returns():
    assert ve.estimate_level(20.0, 10.0, 100.0) == pytest.approx(20.0 * 4.0, abs=0.01)   # +900% → cap +150%
    assert ve.estimate_level(20.0, 10.0, 1.0) == pytest.approx(1.0, abs=0.01)  # -90% → floor -60% ⇒ 触底 1.0


@pytest.mark.parametrize("args", [
    (None, 16.0, 20.0), (16.6, None, 20.0), (16.6, 16.0, None),
    (0, 16.0, 20.0), (16.6, 0, 20.0), (16.6, 16.0, 0),
])
def test_estimate_refuses_incomplete_inputs(args):
    assert ve.estimate_level(*args) is None


def test_jump_is_upside_only_and_thresholded():
    assert ve.is_jump(20.0, 25.0) is True        # +25% 边界
    assert ve.is_jump(20.0, 24.9) is False
    assert ve.is_jump(20.0, 14.0) is False       # 下跌不告警
    assert ve.is_jump(None, 30.0) is False


def _monitor(monkeypatch, tmp_path, *, prev_truth, prev_close, live, as_of=None):
    m = vm.VIXMonitor()
    monkeypatch.setattr(m, "get_vix", lambda: 28.5)          # 级别用真值 28.5 (HIGH)
    monkeypatch.setattr(m, "_fetch_vix_fred", lambda: prev_truth)
    m._last_as_of = as_of
    monkeypatch.setattr("app.data.vix_source.fetch_vixy_quote_via_opend",
                        lambda *a, **k: {"last": live, "pre_price": None, "prev_close": prev_close})
    monkeypatch.setattr(vm, "VIX_JUMP_STATE_PATH", tmp_path / "jump_state.json")
    return m


def test_level_semantics_unchanged_and_estimate_attached(monkeypatch, tmp_path):
    m = _monitor(monkeypatch, tmp_path, prev_truth=21.0, prev_close=21.0, live=27.0,
                 as_of=datetime.now().date().isoformat())
    base = m._check_vix_level_base()          # 同一 vix 的原始判定
    r = m.check_vix_level()
    assert (r["level"], r["message"]) == (base["level"], base["message"])   # 级别语义不变
    assert r["vix"] == 28.5
    assert r["estimate"]["est"] == pytest.approx(21.0 * (1 + 2 * (27 / 21 - 1)), abs=0.05)
    assert r["estimate"]["change_pct"] > 25
    assert "jump" in r and "跳变" in r["jump"]["message"]


def test_jump_alerts_only_once_per_day(monkeypatch, tmp_path):
    m = _monitor(monkeypatch, tmp_path, prev_truth=21.0, prev_close=21.0, live=27.0,
                 as_of=datetime.now().date().isoformat())
    assert "jump" in m.check_vix_level()
    assert "jump" not in m.check_vix_level()      # 同日去重 (落盘)


def test_stale_anchor_suppresses_jump(monkeypatch, tmp_path):
    m = _monitor(monkeypatch, tmp_path, prev_truth=21.0, prev_close=21.0, live=27.0,
                 as_of="2026-01-02")
    r = m.check_vix_level()
    assert "jump" not in r
    assert r["estimate"]["est"] is not None       # 估计仍展示, 只是不告警


def test_no_quote_means_no_estimate_and_no_jump(monkeypatch, tmp_path):
    m = vm.VIXMonitor()
    monkeypatch.setattr(m, "get_vix", lambda: 22.0)
    monkeypatch.setattr(m, "_fetch_vix_fred", lambda: 21.0)
    m._last_as_of = datetime.now().date().isoformat()
    monkeypatch.setattr("app.data.vix_source.fetch_vixy_quote_via_opend", lambda *a, **k: None)
    monkeypatch.setattr(vm, "VIX_JUMP_STATE_PATH", tmp_path / "s.json")
    r = m.check_vix_level()
    assert r["estimate"]["est"] is None
    assert "jump" not in r
    assert (r["level"], r["vix"]) == (m._check_vix_level_base()["level"], 22.0)