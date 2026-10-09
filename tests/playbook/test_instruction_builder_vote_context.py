"""阶段 3a 投票输入与构建前置态契约。"""
from enum import Enum
from types import SimpleNamespace

from app.playbook.pipeline_parts.instruction_builder import (
    _apply_buy_std_gate,
    _apply_technical_arbitration,
    _build_vote_market_data,
    _build_instruction_decision,
    _build_instruction,
    _decision_confidence,
    _factor_direction_conflict,
    _load_factor_ic_global,
    _new_filter_audit,
    _normalize_regime_str,
    _record_vote_snapshot,
    _select_horizon,
)


class MarketRegime(Enum):
    RISK_ON = "risk_on"
    CAUTIOUS = "cautious"


class FakeRouter:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def get_full_market_context(self, symbol):
        if self.error:
            raise self.error
        assert symbol == "US.TEST"
        return self.payload


def test_normalize_regime_names_and_fallback():
    assert _normalize_regime_str(SimpleNamespace(regime=MarketRegime.RISK_ON)) == "RISK_ON"
    assert _normalize_regime_str(SimpleNamespace(regime="RISK_OFF")) == "RISK_OFF"
    assert _normalize_regime_str(SimpleNamespace()) == "CAUTIOUS"


def test_build_vote_market_data_from_raw_context():
    regime = SimpleNamespace(regime=MarketRegime.CAUTIOUS, vix=17.5)
    context = _build_vote_market_data(
        "US.TEST", None, regime, FakeRouter({"price": 101.25, "lv3_bid": 101.2})
    )

    assert context.symbol == "US.TEST"
    assert context.price == 101.25
    assert context.bid == 101.2
    assert context.vix == 17.5
    assert context.market_regime == "CAUTIOUS"


def test_build_vote_market_data_unavailable_paths():
    regime = SimpleNamespace(regime=MarketRegime.CAUTIOUS, vix=17.5)
    assert _build_vote_market_data("US.TEST", None, regime, None) is None
    assert _build_vote_market_data(
        "US.TEST", None, regime, FakeRouter(payload={})
    ) is None
    assert _build_vote_market_data(
        "US.TEST", None, regime, FakeRouter(error=RuntimeError("router down"))
    ) is None


def test_factor_ic_loader_uses_explicit_path(tmp_path):
    missing = tmp_path / "missing.json"
    assert _load_factor_ic_global(str(missing)) == {}

    path = tmp_path / "factor_ic_latest.json"
    path.write_text('{"news_positive": {"ic_short": -0.38}}', encoding="utf-8")
    assert _load_factor_ic_global(str(path)) == {
        "news_positive": {"ic_short": -0.38}
    }


def test_filter_audit_initial_state():
    audit = _new_filter_audit(7)

    assert audit["total_input"] == 7
    assert audit["final_instructions"] == 0
    assert all(value == 0 for key, value in audit.items() if key != "total_input")


def test_record_vote_snapshot_uses_authoritative_decision_id(monkeypatch):
    calls = []

    def record_snapshot(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "app.analysis.signal_ic_tracker.record_signal_snapshot", record_snapshot
    )
    vote_result = {
        "decision_id": "DA-1",
        "direction": "long",
        "final_strength": 72,
        "consensus_level": 0.7,
        "strategy_id": "deep_analysis",
    }

    _record_vote_snapshot("US.TEST", vote_result, {"below_ma20": True})

    assert calls == [{
        "decision_id": "DA-1",
        "symbol": "US.TEST",
        "direction": "LONG",
        "strength": 72,
        "confidence": 0.7,
        "consensus_level": 0.7,
        "strategy_id": "deep_analysis",
        "factors": ["below_ma20"],
        "market_context": {"below_ma20": True},
    }]


def test_technical_arbitration_paths():
    no_signal = _apply_technical_arbitration(
        "US.TEST", "LONG", None, {}, 0.7
    )
    assert no_signal == (None, "无技术方向信号", "technical_no_signal")

    strong_conflict = _apply_technical_arbitration(
        "US.TEST", "LONG", "SHORT", {"final_confidence": 0.71}, 0.7
    )
    assert strong_conflict[2] == "direction_conflict_skip"
    assert strong_conflict[1].startswith("方向冲突: LLM=LONG vs technical=SHORT")

    weak_conflict = _apply_technical_arbitration(
        "US.TEST", "LONG", "SHORT", {"final_confidence": 0.69}, 0.7
    )
    assert weak_conflict[:2] == ("SHORT", "")
    assert _apply_technical_arbitration(
        "US.TEST", "LONG", "LONG", {}, 0.7
    )[:2] == ("LONG", "")


def test_buy_std_gate_uses_strategy_registry(monkeypatch):
    import app.core.strategy_registry as registry

    monkeypatch.setattr(
        registry,
        "get_strategy_config",
        lambda strategy_id: {"direction_filter": {"BUY_max_std": 12}},
    )
    assert _apply_buy_std_gate(
        "US.TEST", {"strategy_id": "test", "std_dev": 12.1}, "BUY"
    )
    assert not _apply_buy_std_gate(
        "US.TEST", {"strategy_id": "test", "std_dev": 12.0}, "BUY"
    )
    assert not _apply_buy_std_gate("US.TEST", {"std_dev": 99.0}, "SELL_SHORT")


def test_factor_direction_conflict_uses_direction_side():
    factor_ic = {"below_ma20": {"ic_long": -0.20, "n_long": 100}}

    hit, side, net_score, scored = _factor_direction_conflict(
        "BUY", {"below_ma20": True}, factor_ic
    )
    assert (hit, side, scored) == (True, "long", ["below_ma20"])
    assert net_score < -0.05

    assert not _factor_direction_conflict(
        "SELL_SHORT", {"below_ma20": True}, factor_ic
    )[0]


def test_select_horizon_success_and_error_fallback():
    decision = SimpleNamespace(
        horizon="long", confidence=0.8, reasoning={"source": "test"}
    )
    selector = SimpleNamespace(select=lambda sr, regime: decision)
    assert _select_horizon(
        "US.TEST", "RISK_ON", selector_provider=lambda: selector
    ) == ("long", 0.8, {"source": "test"})

    horizon, confidence, reasoning = _select_horizon(
        "US.TEST", "RISK_ON", selector_provider=lambda: 1 / 0
    )
    assert horizon == "short"
    assert confidence == 0.0
    assert reasoning["source"] == "error_fallback"


def test_decision_confidence_prefers_bridge_authority():
    bridge = _decision_confidence(
        {"strategy_id": "deep_analysis", "final_confidence": 0.73},
        None,
        {"selected_conditions": []},
        {},
    )
    assert bridge == 73


def test_build_instruction_decision_uses_archetype():
    decision = _build_instruction_decision(
        "US.TEST",
        "BUY",
        {"macd_golden_cross": True},
        {"entry_archetype": "PULLBACK_LIMIT"},
    )
    assert decision == {
        "symbol": "US.TEST",
        "direction": "BUY",
        "selected_conditions": ["macd_golden_cross"],
        "thesis_template": "trend_pullback",
    }


def test_build_instruction_preserves_da_fields_and_risks():
    data = SimpleNamespace(
        fundamental=SimpleNamespace(quality="ok", industry="Semiconductors"),
        technical=SimpleNamespace(quality="ok"),
        insider=SimpleNamespace(quality="ok"),
        news=SimpleNamespace(quality="ok"),
        earnings=SimpleNamespace(quality="ok", days_until=7),
        sentiment=SimpleNamespace(quality="ok"),
    )
    decision = {
        "selected_conditions": ["below_ma20"],
        "thesis_template": "momentum_continuation",
    }
    vote_result = {
        "conditions": ["pe_above_industry"],
        "invalidation": "break $50",
        "thesis": "DA thesis",
        "anchor_dev_pct": 1.2,
        "entry_archetype": "BREAKOUT",
        "confirmation": {"side": "above"},
        "chase_cap_price": 101.0,
    }
    values = {
        "entry_price": 100.0,
        "stop_loss": 96.0,
        "take_profit": 108.0,
        "position_size": 40,
        "risk_amount": 160.0,
        "risk_reward": 2.0,
        "entry_price_source": "bid_ask",
        "atr_source": "FALLBACK_TEST",
    }

    inst = _build_instruction(
        symbol="US.TEST",
        direction="BUY",
        data=data,
        factors={"below_ma20": True},
        vote_result=vote_result,
        decision=decision,
        values=values,
        decision_id="DA-1",
        confidence=72,
        horizon="short",
        horizon_confidence=0.6,
        horizon_reasoning={"source": "test"},
    )

    assert inst.conditions == ["pe_above_industry"]
    assert inst.invalidation == "break $50"
    assert inst.da_thesis == "DA thesis"
    assert inst.anchor_dev_pct == 1.2
    assert inst.entry_archetype == "BREAKOUT"
    assert inst.confirmation == {"side": "above"}
    assert inst.chase_cap_price == 101.0
    assert inst.risks == ["财报 7 天后", "ATR 缺失, 使用 fallback 止损"]
