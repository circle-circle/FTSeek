"""DEB-20260816-fullchain 批次D 契约测试: 配置宣称与实际不符家族 #16/#18/#19/#21.

红线①: 不改任何生效阈值 — 本批只做"回退值与 SSOT 同步 + 死键告警 + 日志纠偏"。
"""
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── #16: registry 缺省不再遮蔽 yaml supply_conf_gate_short ──────────────

def test_16_registry_defaults_no_short_conf_shadow():
    from app.core import strategy_registry
    strategy_registry.reload()
    df = strategy_registry.get_strategy_config("__unregistered_for_test__")["direction_filter"]
    assert "SHORT_min_confidence" not in df
    # 消费方 .get 语义: 现回落到 yaml supply_conf_gate_short (55)
    assert df.get("SHORT_min_confidence", 55) == 55


def test_16_buy_and_short_fallback_symmetric():
    # pipeline.py 消费语义: 两方向同回落 yaml 键 (多空对称)
    from app.core import strategy_registry
    df = strategy_registry.get_strategy_config("__unregistered_for_test__")["direction_filter"]
    assert df.get("BUY_min_confidence", 55) == 55
    assert df.get("SHORT_min_confidence", 55) == 55


# ── #18: 死键分歧启动告警 (不改生效值) ──────────────────────────────────

class _FakeCfg:
    def __init__(self, data):
        self._d = data

    def get_dotted(self, key, default=None):
        return self._d.get(key, default)


def test_18_divergent_dead_key_warns(caplog):
    from app.core.scheduler_core import _warn_strength_key_divergence
    cfg = _FakeCfg({
        "risk.pre_trade.min_effective_strength_floor": 50,
        "strategies.defaults.min_strength": 60,
    })
    with caplog.at_level(logging.WARNING):
        _warn_strength_key_divergence(
            cfg, "risk.pre_trade.min_effective_strength_floor",
            ("strategies.defaults.min_strength", "scheduler.min_signal_strength"))
    assert any("strategies.defaults.min_strength=60" in r.message for r in caplog.records)


def test_18_consistent_or_absent_keys_silent(caplog):
    from app.core.scheduler_core import _warn_strength_key_divergence
    cfg = _FakeCfg({
        "risk.pre_trade.min_effective_strength_floor": 50,
        "strategies.defaults.min_strength": 50,  # 一致 → 不告警
        "scheduler.min_signal_strength": 60,     # 会告警 (分歧)
    })
    with caplog.at_level(logging.WARNING):
        _warn_strength_key_divergence(
            cfg, "risk.pre_trade.min_effective_strength_floor",
            ("strategies.defaults.min_strength", "scheduler.min_signal_strength"))
    msgs = [r.message for r in caplog.records]
    assert not any("strategies.defaults" in m for m in msgs)
    assert any("scheduler.min_signal_strength" in m for m in msgs)


def test_18_missing_effective_key_silent(caplog):
    from app.core.scheduler_core import _warn_strength_key_divergence
    cfg = _FakeCfg({"strategies.defaults.min_strength": 60})
    with caplog.at_level(logging.WARNING):
        _warn_strength_key_divergence(
            cfg, "risk.pre_trade.min_effective_strength_floor",
            ("strategies.defaults.min_strength",))
    assert not caplog.records


# ── #19: max_per_symbol 回退值与 yaml SSOT (0.08) 同步 ──────────────────

def test_19_pre_trade_gate_default_synced():
    from app.risk.pre_trade_gate import _DEFAULTS
    assert _DEFAULTS["max_per_symbol"] == 0.08


def test_19_config_defaults_synced():
    from app.core.config_defaults import ConfigDefaults
    assert ConfigDefaults.MAX_PER_SYMBOL == 0.08


def test_19_no_stale_025_fallbacks_remain():
    # 源码契约: 消费点回退值全部同步 (防回归漂移); 只看代码部分, 注释里的历史值不算
    stale = []
    for rel in ("app/risk/pre_trade_gate.py", "app/core/position_sync.py",
                "app/core/scheduler_triggers.py", "app/pipeline/llm_pipeline.py",
                "app/core/config_defaults.py"):
        for i, line in enumerate((PROJECT_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "max_per_symbol" in code and "0.25" in code:
                stale.append(f"{rel}:{i}")
    assert stale == []


# ── #21: 方向翻转日志不再宣称未实现的 "+15" 门槛 ────────────────────────

def test_21_flip_log_matches_implementation():
    src = (PROJECT_ROOT / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    assert "confidence门槛+15" not in src, "日志宣称 +15 门槛但实现是降仓50% (#21 回归)"
    assert "降仓50%" in src
