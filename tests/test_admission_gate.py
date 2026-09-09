"""admission gate 单测 (2026-08-06, DEB-20260806-source-control).

覆盖点 (按"最容易出事"排序):
  1. record-only fallback 不改指令 (最重要的安全不变量)
  2. enforce=true 才转 SKIP, 且原方向留审计可追溯
  3. 非 dict 的 raw_instruction 不被吞掉 (数据丢失回归)
  4. SKIP/未知方向直接放行, 不误判
  5. 费用判据的算术正确性 (与 fee_model 同源)
  6. 阈值缺省全为 0 = 不拦 (无证据不设门)
  7. enabled=false 完全短路
  8. 生产 YAML 的 enforce 与费用/价格阈值不漂移
"""
from pathlib import Path

import pytest
import yaml

from app.playbook import admission as A


@pytest.fixture(autouse=True)
def _isolate_config():
    """每个用例独立配置, 避免缓存串味; 结束后复位。"""
    A._cfg_cache = dict(A._DEFAULTS)
    yield
    A.reset_cache()


def _inst(**over):
    base = {
        "date": "2026-08-06", "symbol": "US.NVDA", "direction": "BUY",
        "entry_price": 180.0, "take_profit": 186.0, "stop_loss": 176.0,
        "position_size": 20, "confidence": 35,
    }
    base.update(over)
    return base


# ── 1. 安全不变量: record-only 不改指令 ──
def test_record_only_does_not_mutate_direction():
    A._cfg_cache.update({"enforce": False, "min_notional_usd": 5000})
    out, res = A.apply(_inst())
    assert res["verdict"] == "REJECT"          # 判定为拒
    assert out["direction"] == "BUY"           # 但指令不变
    assert "skip_reason" not in out
    assert out["raw_instruction"]["admission"]["enforced"] is False


# ── 2. enforce 才生效, 且原方向可追溯 ──
def test_enforce_converts_to_skip_and_keeps_original_direction():
    A._cfg_cache.update({"enforce": True, "min_notional_usd": 5000})
    out, res = A.apply(_inst())
    assert res["verdict"] == "REJECT"
    assert out["direction"] == "SKIP"
    assert "admission:" in out["skip_reason"]
    adm = out["raw_instruction"]["admission"]
    assert adm["original_direction"] == "BUY"   # 不丢原方向 → 仍可归因
    assert adm["enforced"] is True


# ── 3. 数据丢失回归: 非 dict raw_instruction 必须保留 ──
def test_non_dict_raw_instruction_is_preserved():
    A._cfg_cache.update({"enforce": False})
    out, _ = A.apply(_inst(raw_instruction="LLM raw text"))
    assert out["raw_instruction"]["_original"] == "LLM raw text"
    assert "admission" in out["raw_instruction"]


def test_dict_raw_instruction_keys_are_kept():
    A._cfg_cache.update({"enforce": False})
    out, _ = A.apply(_inst(raw_instruction={"source": "deep_analysis", "x": 1}))
    assert out["raw_instruction"]["source"] == "deep_analysis"
    assert out["raw_instruction"]["x"] == 1


# ── 8. 止损宽度卫生 (2026-08-07 用户哲学"留空间": 替代静态 R:R 门) ──

def test_sl_too_wide_rejected():
    A._cfg_cache.update({"enforce": True, "max_sl_pct": 6.0})
    _, res = A.apply(_inst(entry_price=100.0, stop_loss=92.0))   # -8% 宽止损
    assert res["verdict"] == "REJECT"
    assert any("SL_STOP_TOO_WIDE" in r for r in res["reasons"])


def test_sl_within_band_passes():
    A._cfg_cache.update({"enforce": True, "max_sl_pct": 6.0})
    _, res = A.apply(_inst(entry_price=100.0, stop_loss=95.5))   # -4.5% OK
    assert all("SL_STOP_TOO_WIDE" not in r for r in res["reasons"])


def test_sl_gate_disabled_when_zero():
    A._cfg_cache.update({"enforce": True})                       # max_sl_pct 缺省 0 = 不拦
    _, res = A.apply(_inst(entry_price=100.0, stop_loss=80.0))   # -20% 也不拦
    assert all("SL_STOP_TOO_WIDE" not in r for r in res["reasons"])


# ── 9. 弱信号审计标记 (2026-08-07 用户哲学"不强不做": 弃权=弱信号, 只标记不拦) ──

def test_weak_signal_marked_not_blocked():
    A._cfg_cache.update({"enforce": True})
    _, res = A.apply(_inst(raw_instruction={"debate_outcome": "➖ 一方弃权(BUY vs SKIP), 保持有观点方向, confidence不罚"}))
    assert res["verdict"] == "PASS"          # 不拦 (sim 采集期供给优先)
    assert res["metrics"].get("weak_signal") is True


def test_consensus_not_marked_weak():
    A._cfg_cache.update({"enforce": True})
    _, res = A.apply(_inst(raw_instruction={"debate_outcome": "✅ 2模型一致(BUY)"}))
    assert res["metrics"].get("weak_signal") is None


# ── 4. 非可执行方向直接放行 ──
@pytest.mark.parametrize("direction", ["SKIP", "", "UNKNOWN"])
def test_non_actionable_direction_passes(direction):
    A._cfg_cache.update({"enforce": True, "min_notional_usd": 10 ** 9})
    out, res = A.apply(_inst(direction=direction))
    assert res["verdict"] == "PASS"
    assert out["direction"] == direction


# ── 5. 费用判据算术 (与 fee_model 同源) ──
def test_fee_metrics_match_fee_model():
    from app.execution.fee_model import estimate_round_trip
    A._cfg_cache.update({"enforce": False})
    inst = _inst(entry_price=100.0, position_size=15)
    _, res = A.apply(inst)
    expect = estimate_round_trip(15, 100.0, 100.0, "BUY", "2026-08-06")["total"]
    assert res["metrics"]["rt_fee"] == pytest.approx(expect, abs=0.01)
    assert res["metrics"]["notional"] == pytest.approx(1500.0)
    # fee_bps = fee/notional*10000
    assert res["metrics"]["fee_bps"] == pytest.approx(10000 * expect / 1500.0, abs=0.2)


def test_profit_fee_ratio_gate():
    A._cfg_cache.update({"enforce": True, "min_profit_fee_ratio": 100.0})
    out, res = A.apply(_inst())
    assert any(r.startswith("FEE_INEFFICIENT") for r in res["reasons"])
    assert out["direction"] == "SKIP"


def test_low_price_and_share_knee_gates():
    A._cfg_cache.update({"enforce": False, "min_entry_price": 30.0})
    _, res = A.apply(_inst(entry_price=8.5, position_size=400))
    assert any(r.startswith("PRICE_TOO_LOW") for r in res["reasons"])

    A._cfg_cache = dict(A._DEFAULTS)
    A._cfg_cache.update({"enforce": False, "max_shares_per_order": 199})
    _, res2 = A.apply(_inst(position_size=400))
    assert any(r.startswith("SHARES_ABOVE_FEE_KNEE") for r in res2["reasons"])


# ── 6. 缺省阈值全 0 = 不拦 (无 IC 证据不设门) ──
def test_defaults_block_nothing():
    """confidence=35 远低于历史 70 门槛, 但缺省配置下不应被拦 —
    因为 60 日实测 confidence IC=-0.021 (t=-0.33), 无证据支持该门。"""
    assert A._DEFAULTS["min_confidence_buy"] == 0
    assert A._DEFAULTS["min_confidence_short"] == 0
    assert A._DEFAULTS["enforce"] is False
    out, res = A.apply(_inst(confidence=1))
    assert res["verdict"] == "PASS"
    assert res["reasons"] == []
    assert out["direction"] == "BUY"


def test_confidence_gate_only_when_explicitly_set():
    A._cfg_cache.update({"enforce": False, "min_confidence_buy": 70})
    _, res = A.apply(_inst(confidence=35))
    assert any(r.startswith("LOW_CONFIDENCE") for r in res["reasons"])


# ── 7. 总开关 ──
def test_disabled_short_circuits():
    A._cfg_cache.update({"enabled": False, "enforce": True, "min_notional_usd": 10 ** 9})
    out, res = A.apply(_inst())
    assert res["verdict"] == "PASS"
    assert out["direction"] == "BUY"
    assert "raw_instruction" not in out or "admission" not in out.get("raw_instruction", {})


# ── 边界: 缺价/缺量不应抛异常, 也不应误拦 ──
@pytest.mark.parametrize("over", [
    {"entry_price": 0}, {"position_size": 0}, {"take_profit": None},
    {"entry_price": None, "position_size": None},
])
def test_missing_fields_do_not_raise(over):
    A._cfg_cache.update({"enforce": True, "min_profit_fee_ratio": 6})
    out, res = A.apply(_inst(**over))
    assert res["verdict"] in ("PASS", "REJECT")
    assert isinstance(res["metrics"], dict)


def test_production_yaml_admission_contract():
    path = Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    admission = config["playbook"]["admission"]

    assert admission["enabled"] is True
    assert admission["enforce"] is True
    assert admission["min_notional_usd"] == 2500
    assert admission["min_entry_price"] == 5.0
    assert admission["min_confidence_buy"] == 0
    assert admission["min_confidence_short"] == 0
