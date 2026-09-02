"""[DEB-20260831 §8.7 · 2026-09-02 实施] atr_basis 打标契约测试.

锁住的语义:
  B1  basis 三分: real (atr_source 解析, 新旧格式均计) / proxy (SL 距离反算) / missing (无从反算)
  B2  打标不改值 — 与 test_atr_source_unit_extraction.py 的数值契约逐案一致
  B3  规则0.5 消费端: meta 带/不带 atr_basis 均不崩 (历史仓无此键 → 日志 '?')
"""
import json

from app.playbook.playbook_service import _extract_atr_with_basis, _extract_atr_from_instruction


def test_basis_new_percent_format_real():
    raw = json.dumps({"entry_price": 1179.99,
                      "atr_source": "VOL_medium: atr_pct=3.13% sl=4.50% rr=1.80"})
    val, basis = _extract_atr_with_basis({"entry_price": 1179.99, "raw_instruction_json": raw})
    assert (val, basis) == (36.93, "real")


def test_basis_old_format_real():
    raw = json.dumps({"atr_source": "technical.atr_14=8.35 (quality=good)"})
    val, basis = _extract_atr_with_basis({"raw_instruction_json": raw})
    assert (val, basis) == (8.35, "real")


def test_basis_proxy_path():
    val, basis = _extract_atr_with_basis({"entry_price": 100.0, "stop_loss": 95.0})
    assert (val, basis) == (5.0, "proxy")


def test_basis_missing_path():
    val, basis = _extract_atr_with_basis({})
    assert (val, basis) == (0.0, "missing")


def test_float_contract_unchanged():
    """兼容包装仍返回纯 float (旧契约, 防回归)."""
    raw = json.dumps({"entry_price": 113.28,
                      "atr_source": "VOL_high: atr_pct=3.88% sl=5.00% rr=1.80 vix_widen=1.5%"})
    assert _extract_atr_from_instruction({"entry_price": 113.28, "raw_instruction_json": raw}) == 4.4


def test_rule05_consumes_meta_with_and_without_basis():
    """历史仓无 atr_basis 键 → '?' 兜底, 规则0.5 不崩 (端到端走一遍规则)."""
    import app.playbook.config as pb_config_mod
    from app.playbook.pipeline import _check_playbook_exit_rules
    _orig = pb_config_mod.get_playbook_config
    real = _orig()

    class _Over:
        _O = {"early_tight_sl_enabled": True, "early_tight_sl_atr_enabled": True}
        def get(self, k, d=None):
            return self._O.get(k, real.get(k, d))
        def get_horizon_top(self, *a, **kw):
            return real.get_horizon_top(*a, **kw)
        def get_horizon_params(self, *a, **kw):
            return real.get_horizon_params(*a, **kw)
        def __getattr__(self, n):
            return getattr(real, n)

    pb_config_mod.get_playbook_config = lambda: _Over()
    try:
        for meta in ({"atr": 5.0, "_symbol": "US.T1"},
                     {"atr": 5.0, "_symbol": "US.T2", "atr_basis": "real"}):
            reason, *_ = _check_playbook_exit_rules(
                direction="LONG", entry_price=100.0, current_price=98.0,
                initial_sl=95.0, peak_price=100.0, meta=dict(meta),
                held_minutes=10, minutes_to_close=300, minutes_from_open=100,
                entry_time_str="2026-09-02T10:00:00")
            assert reason == ""  # tight=2.5% 上限, -2% 存活 (无论有无 basis 键)
    finally:
        pb_config_mod.get_playbook_config = _orig
