from scripts.replay_ext_entry_sources import (
    _round_trip_fee_pct,
    _simulate,
    _simulate_ah,
)


BARS = [
    {"t": "2026-09-18 04:05:00", "c": 100.0, "h": 100.0, "l": 100.0, "v": 1000.0},
    {"t": "2026-09-18 04:10:00", "c": 100.8, "h": 100.9, "l": 100.7, "v": 1000.0},
    {"t": "2026-09-18 04:15:00", "c": 100.3, "h": 100.4, "l": 100.2, "v": 1000.0},
]
DAY = "2026-09-18"


def test_premarket_replay_uses_shared_profit_exit_rules():
    """2026-09-18 (§18.2c / §23 R9): 净额口径由扁平 8.5bps 改为**逐股真实费用模型**
    (app.execution.fee_model.estimate_round_trip, $100 档往返 ≈0.088%) ⇒ 同一笔
    毛 0.3% 的净额从 0.215% 变 0.212%。本用例钉**关系**而非魔数, 免得下次换费率又变红。"""
    result = _simulate(BARS, prev_close=99.0, entry_bar_i=0)

    assert result["rule_action"] == "SL_TRAIL_LOCK"
    assert result["rule"]["ret_pct"] == 0.3
    fee = _round_trip_fee_pct(100.0, 100.3, DAY)
    assert round(0.3 - fee, 3) == result["rule"]["net_pct"]
    assert result["rule"]["net_pct"] < 0.215  # 不再等于扁平费率的结果


def test_afterhours_replay_uses_shared_profit_exit_rules():
    result = _simulate_ah(BARS, next_day_bars=[], entry_bar_i=0)

    assert result["rule_action"] == "SL_TRAIL_LOCK"
    assert result["rule"]["ret_pct"] == 0.3
    fee = _round_trip_fee_pct(100.0, 100.3, DAY)
    assert round(0.3 - fee, 3) == result["rule"]["net_pct"]
    assert result["rule"]["net_pct"] < 0.215