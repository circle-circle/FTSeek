# -*- coding: utf-8 -*-
"""BE 保本垫几何契约测试 (DEB-20260909-exit-params 行动项C, 2026-09-09 用户裁"同意").

钉住的潜伏缺陷: fee-aware (breakeven_fee_aware_enabled) 静默失败时
(exit_engine.py `except Exception: pass`), 回退垫 =
breakeven_fee_cushion_pct (yaml 旧值 0.4) + breakeven_slip_cushion_pct (0.2) = 0.6
> breakeven_at_pct (0.4) → 武装瞬间 SL > 现价 = 变相硬 TP (武装即触发),
把 BE 引擎变成截断引擎。修复 = yaml 0.4→0.15 + 五处代码兜底收敛共享常量
BE_FALLBACK_FEE_CUSHION_PCT (app/playbook/config.py)。

契约:
  A. 配置几何: 真 yaml 回退垫 (fee+slip) < 全部 factor 规则的 breakeven_at_pct
  B. 常量几何: 共享常量 + slip < 最小 be 触发线, 且量级 ≈ fee-aware 实弹均值 0.147%
  C. 功能双臂: fee-aware 故障 + 浮盈刚到武装线 → 不得瞬间触发 SL_BREVEVEN (修复后);
     同场景注入旧值 0.4 → 必触发 (缺陷复现, 证明测试敏感); 真跌破回退垫线 → 触发
  D. 源收敛: 五处消费方引用共享常量防再漂移; pipeline fill 腿保留豁免标记
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.config import BE_FALLBACK_FEE_CUSHION_PCT  # noqa: E402
from app.playbook.pipeline import _check_playbook_exit_rules  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402

_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def _run(current_price, peak_price, qty=10, entry_price=100.0, initial_sl=95.0):
    """最小场景: 当日 LONG, 持仓30min, 隔离 DEAD_SIGNAL/TIME_EXIT/PRE_CLOSE/MAX_HOLD."""
    meta = {
        "_log_tag": "test_be_pad_geom", "_symbol": "TEST", "cohort": "A",
        "horizon": "short", "_source_type": "PLAYBOOK_SIM", "_skip_allowed": False,
    }
    if qty > 0:
        meta["_qty"] = qty  # 激活 fee-aware 尝试路径 (再由 monkeypatch 注故障)
    result = _check_playbook_exit_rules(
        direction="LONG", entry_price=entry_price, current_price=current_price,
        initial_sl=initial_sl, peak_price=peak_price, meta=meta,
        held_minutes=30, minutes_to_close=30, minutes_from_open=20,
        entry_time_str=now_et().isoformat(),
    )
    return (*result, meta)  # 引擎返回4元组; meta 原地变异, 附带给断言侧检查标记


@pytest.fixture()
def fee_aware_broken(monkeypatch):
    """注入 fee-aware 静默故障 (estimate_round_trip 抛错 → except: pass → 回退垫)."""
    def _boom(*a, **k):
        raise RuntimeError("simulated fee model failure")
    monkeypatch.setattr("app.execution.fee_model.estimate_round_trip", _boom)


class _FakeCfgOldFallback:
    """旧缺陷配置: breakeven_fee_cushion_pct=0.4 (其余键透传真 config)."""

    def __init__(self):
        from app.playbook.config import get_playbook_config as _real_get
        self._real = _real_get()

    def get(self, key, default=None):
        if key == "breakeven_fee_cushion_pct":
            return 0.4
        return self._real.get(key, default)

    def get_horizon_top(self, key, default=None):
        return self._real.get_horizon_top(key, default)


# ─────────────────────────────────────────────────────────────────────────────
# A. 配置几何: 真 yaml 回退垫 < 全部 factor 规则的 be 触发线
# ─────────────────────────────────────────────────────────────────────────────

class TestYamlPadGeometry:

    def test_fallback_pad_below_all_factor_be_triggers(self):
        """fee_cushion + slip_cushion < min(breakeven_at_pct) across factor rules."""
        from app.playbook.config import get_playbook_config
        cfg = get_playbook_config()
        fee = float(cfg.get("breakeven_fee_cushion_pct", 0.0))
        slip = float(cfg.get("breakeven_slip_cushion_pct", 0.0))
        rules = cfg.get_horizon_top("factor_exit_rules", {}) or {}
        be_values = [float(r["breakeven_at_pct"]) for r in rules.values()
                     if isinstance(r, dict) and "breakeven_at_pct" in r]
        assert be_values, "factor_exit_rules 里应至少有一条含 breakeven_at_pct 的规则"
        min_be = min(be_values)
        assert fee + slip < min_be, \
            f"回退垫 {fee}+{slip}={fee + slip} 破几何: ≥ 最小 be 触发线 {min_be} " \
            f"(武装瞬间 SL>现价 = 变相硬 TP)"


# ─────────────────────────────────────────────────────────────────────────────
# B. 常量几何
# ─────────────────────────────────────────────────────────────────────────────

class TestFallbackConstantGeometry:

    def test_constant_plus_slip_below_be(self):
        slip = 0.2  # yaml 现值 (本契约 A 臂已钉 yaml 真值)
        assert BE_FALLBACK_FEE_CUSHION_PCT + slip < 0.4, \
            f"共享常量破几何: {BE_FALLBACK_FEE_CUSHION_PCT}+{slip} ≥ 0.4"

    def test_constant_magnitude_matches_fee_aware_reality(self):
        """0.15 ≈ fee-aware 实弹均值 0.147% (过小=假保本净亏, 过大=锁利线虚高)."""
        assert 0.05 < BE_FALLBACK_FEE_CUSHION_PCT < 0.25


# ─────────────────────────────────────────────────────────────────────────────
# C. 功能双臂: fee-aware 故障场景的几何行为
# ─────────────────────────────────────────────────────────────────────────────

class TestFeeAwareFailureGeometry:
    """entry=100, 武装线 be=0.4%. 修复后回退垫 0.15+0.2=0.35 → SL线=100.35;
    旧值 0.4+0.2=0.6 → SL线=100.60. peak=100.41 (0.41%≥0.4 武装)."""

    def test_armed_at_trigger_line_no_instant_fire(self, fee_aware_broken):
        """修复后: 现价 100.40 (刚武装) > 回退垫线 100.35 → 不得触发."""
        reason, _, _, be_trig, meta = _run(current_price=100.40, peak_price=100.41)
        assert be_trig, "peak 0.41% ≥ 0.4% 应已武装"
        assert "_be_cushion_fee_aware" not in meta, "fee-aware 故障时不应写标记"
        assert reason == "", \
            f"武装瞬间现价在回退垫线上方, 不应触发 SL_BREAKEVEN, got {reason}"

    def test_price_below_fallback_line_fires(self, fee_aware_broken):
        """修复后: 现价 100.34 < 回退垫线 100.35 → 必须触发 (回退垫仍是真止损)."""
        reason, _, _, be_trig, _ = _run(current_price=100.34, peak_price=100.41)
        assert be_trig
        assert reason == "SL_BREAKEVEN", f"跌破回退垫线应触发, got {reason}"

    def test_defect_reproduction_old_value_instant_fire(self, fee_aware_broken, monkeypatch):
        """缺陷复现 (敏感臂): 注入旧值 0.4 → 垫线 100.60 > 现价 100.40 → 武装即触发.
        本臂证明前两臂不是假绿 — 旧几何下同场景必爆."""
        fake = _FakeCfgOldFallback()
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        reason, _, _, be_trig, _ = _run(current_price=100.40, peak_price=100.41)
        assert be_trig
        assert reason == "SL_BREAKEVEN", \
            f"旧值 0.4 应复现武装即触发缺陷, got {reason!r} (若空=测试场景未命中缺陷路径)"


# ─────────────────────────────────────────────────────────────────────────────
# D. 源收敛: 防再漂移
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceConvergence:

    @pytest.mark.parametrize("rel", [
        "app/playbook/exit_engine.py",
        "app/playbook/ic_tracker.py",
        "app/playbook/feishu_push.py",
        "app/playbook/exit_shadow.py",
        "scripts/exit_path_replay.py",
    ])
    def test_consumers_reference_shared_constant(self, rel):
        path = os.path.join(_REPO, rel)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "BE_FALLBACK_FEE_CUSHION_PCT" in src, \
            f"{rel} 未引用共享常量 BE_FALLBACK_FEE_CUSHION_PCT (兜底口径再漂移风险)"

    def test_pipeline_fill_leg_keeps_exempt_marker(self):
        """pipeline PATIENT_EXIT_FILL 腿有意保持 0 回退 (在仓旧仓无标记),
        豁免标记必须存在以防后人误收敛."""
        path = os.path.join(_REPO, "app/playbook/pipeline.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "BE_FALLBACK_EXEMPT" in src, \
            "pipeline.py fill 腿的 BE_FALLBACK_EXEMPT 豁免标记丢失"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
