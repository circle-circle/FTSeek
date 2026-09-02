"""[2026-09-01 用户裁"A"·C1 冻结显式豁免] ATR 比例紧 SL 契约测试.

缘起 9/1 V 型日: AMD/LLY/SAIC 三笔 SL_INITIAL 全部 MFE=0 + ATR 3-5% 桶,
固定 1% ≈ 0.25×ATR 低于噪声 → 开盘下探底部被扫后大盘 V 回 (SAIC 场后
+1.96% 误杀 $186)。

锁住的语义:
  A1  tight = clamp(0.5×ATR%, 下限=early_tight_sl_pct, 上限=early_tight_sl_pct_max)
  A2  高 ATR 仓: 下探 2% (旧 1% 必杀) 存活; 跌穿 clamp 上限仍 SL_INITIAL (纪律不放宽)
  A3  meta.atr 缺失/0 → 回退固定 1% (= 8/29 毕业行为, fail-safe)
  A4  atr_enabled=false 单键回滚 → 毕业行为
  A5  毕业契约键 (enabled/mfe_gate) 不被本变更触碰
  A6  yaml SSOT 三新键在位
"""
from datetime import date as _date
from pathlib import Path

import pytest
import yaml

import app.playbook.config as pb_config_mod
from app.playbook.pipeline import _check_playbook_exit_rules

REPO = Path(__file__).resolve().parents[1]


class _CfgOverride:
    """委托真实配置, 仅覆写 early_tight_* 键 (隔离测试与 yaml 漂移)."""

    _OVER = {
        "early_tight_sl_enabled": True,
        "early_tight_sl_minutes": 30,
        "early_tight_sl_pct": 1.0,
        "early_tight_sl_mfe_gate": 0.3,
        "early_tight_sl_atr_enabled": True,
        "early_tight_sl_atr_mult": 0.5,
        "early_tight_sl_pct_max": 2.5,
    }

    def __init__(self, real, **over):
        self._real = real
        self._over = {**self._OVER, **over}

    def get(self, key, default=None):
        if key in self._over:
            return self._over[key]
        return self._real.get(key, default)

    def get_horizon_top(self, *a, **kw):
        return self._real.get_horizon_top(*a, **kw)

    def get_horizon_params(self, *a, **kw):
        return self._real.get_horizon_params(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run(meta, current_price, cfg_over=None):
    """entry=100, 5% 初始SL, 开盘后100min (非冷却), 持仓10min, 峰值=入场 (MFE=0).
    2026-09-02: entry_time 动态当日 (原硬编码 2026-09-01 跨日后被 RFC-073 判隔夜,
    DEAD_SIGNAL 生效口径切 minutes_from_open=100≥60 触发, 与本契约被测面无关)."""
    _orig = pb_config_mod.get_playbook_config
    real = _orig()
    pb_config_mod.get_playbook_config = lambda: _CfgOverride(real, **(cfg_over or {}))
    try:
        return _check_playbook_exit_rules(
            direction="LONG", entry_price=100.0, current_price=current_price,
            initial_sl=95.0, peak_price=100.0, meta=dict(meta),
            held_minutes=10, minutes_to_close=300, minutes_from_open=100,
            entry_time_str=f"{_date.today().isoformat()}T10:00:00",
        )
    finally:
        pb_config_mod.get_playbook_config = _orig


# ── A1/A2: 高 ATR (5%) → tight=clamp(0.5×5, 1, 2.5)=2.5% ───────────────────

def test_high_atr_dip_2pct_survives():
    """9/1 SAIC 场景复刻: ATR 5% 仓下探 -2.0% (旧 1% 必杀) → 存活."""
    reason, *_ = _run({"atr": 5.0, "_symbol": "US.X1"}, current_price=98.0)
    assert reason == "", f"ATR 5% 下探 2% 应存活 (tight=2.5%), 实际退出: {reason}"


def test_high_atr_breaks_clamp_ceiling_still_stops():
    """纪律不放宽: 跌穿 clamp 上限 (2.6% > 2.5%) → SL_INITIAL 照杀."""
    reason, price, *_ = _run({"atr": 5.0, "_symbol": "US.X2"}, current_price=97.4)
    assert reason == "SL_INITIAL"


def test_mid_atr_uses_half_atr():
    """ATR 3.4% → tight=1.7%: -1.5% 存活, -1.8% 触发."""
    reason, *_ = _run({"atr": 3.4, "_symbol": "US.X3"}, current_price=98.5)
    assert reason == ""
    reason, *_ = _run({"atr": 3.4, "_symbol": "US.X3"}, current_price=98.2)
    assert reason == "SL_INITIAL"


# ── A3: ATR 缺失 → 固定 1% (毕业行为) ─────────────────────────────────────

def test_missing_atr_falls_back_to_fixed_1pct():
    reason, *_ = _run({"_symbol": "US.X4"}, current_price=98.5)  # -1.5%
    assert reason == "SL_INITIAL", "无 ATR 应回退固定 1%, -1.5% 应触发"


def test_zero_atr_falls_back_to_fixed_1pct():
    reason, *_ = _run({"atr": 0, "_symbol": "US.X5"}, current_price=98.5)
    assert reason == "SL_INITIAL"


# ── A4: 单键回滚 ───────────────────────────────────────────────────────────

def test_atr_switch_off_restores_graduated_behavior():
    """atr_enabled=false + ATR 5% → 仍 1%: -1.5% 触发 (回滚路径)."""
    reason, *_ = _run({"atr": 5.0, "_symbol": "US.X6"}, current_price=98.5,
                      cfg_over={"early_tight_sl_atr_enabled": False})
    assert reason == "SL_INITIAL"


# ── A6: yaml SSOT ──────────────────────────────────────────────────────────

def test_yaml_atr_keys_present():
    cfg = yaml.safe_load((REPO / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
    ep = cfg.get("playbook", {}).get("execution_parameters") or {}
    assert ep.get("early_tight_sl_atr_enabled") is True
    assert ep.get("early_tight_sl_atr_mult") == 0.5
    assert ep.get("early_tight_sl_pct_max") == 2.5
    # 毕业键不被触碰
    assert ep.get("early_tight_sl_enabled") is True
    assert ep.get("early_tight_sl_mfe_gate") == 0.3
    assert ep.get("early_tight_sl_pct") == 1.0
