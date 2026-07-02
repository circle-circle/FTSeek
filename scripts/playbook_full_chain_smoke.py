#!/usr/bin/env python3
"""Playbook 全链路冒烟测试

覆盖: 信号生成 → 因子退出规则 → 市场状态叠加 → 重新定价 → 退出判断
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.playbook.config import get_playbook_config
from app.playbook.instruction_generator import compute_instruction_values
from app.playbook.data_collector import PlaybookData, TechnicalData, SentimentData
from app.playbook.market_regime import MarketRegime, RegimeContext
from app.playbook.pipeline import (
    _get_factor_exit_config,
    _check_playbook_exit_rules,
    _compute_volatility_based_sl_tp,
)


def _data(price: float, atr_14: float, vix: float) -> PlaybookData:
    return PlaybookData(
        symbol="US.AAPL",
        technical=TechnicalData(price=price, bid=price - 0.01, ask=price + 0.01, atr_14=atr_14, quality="ok"),
        sentiment=SentimentData(vix=vix, quality="ok"),
    )


def _regime(regime: MarketRegime, vix: float) -> RegimeContext:
    return RegimeContext(
        regime=regime,
        vix=vix,
        spy_price=500.0,
        spy_ma20=490.0,
        spy_ma50=480.0,
        spy_ma200=450.0,
        spy_change_pct=0.5,
        score=6 if regime == MarketRegime.RISK_ON else 0,
        top_n=10,
        position_multiplier=1.0,
        min_strength=60,
    )


def test_market_state_overlay():
    print("--- 市场状态叠加 ---")
    dec = {"symbol": "US.AAPL", "direction": "BUY"}
    base = compute_instruction_values(
        dec, _data(150, 3.0, 18), total_equity=82000, risk_pct=1.0,
        horizon="short", regime_context=_regime(MarketRegime.RISK_ON, 18)
    )
    print(f"RISK_ON VIX=18: entry={base['entry_price']} SL={base['stop_loss']} TP={base['take_profit']} size={base['position_size']} | {base['atr_source']}")

    high = compute_instruction_values(
        dec, _data(150, 3.0, 27), total_equity=82000, risk_pct=1.0,
        horizon="short", regime_context=_regime(MarketRegime.CAUTIOUS, 27)
    )
    print(f"CAUTIOUS VIX=27: entry={high['entry_price']} SL={high['stop_loss']} TP={high['take_profit']} size={high['position_size']} | {high['atr_source']}")

    no_trade = compute_instruction_values(
        dec, _data(150, 3.0, 32), total_equity=82000, risk_pct=1.0,
        horizon="short", regime_context=_regime(MarketRegime.NO_TRADE, 32)
    )
    assert no_trade == {}, f"NO_TRADE 应该返回空指令, 实际={no_trade}"
    print("NO_TRADE 返回空指令 ✔")


def test_reprice_logic():
    print("\n--- re-price 波动率分桶 ---")
    pb = get_playbook_config()
    hp = pb.get_horizon_params("short")
    for atr_pct in [0.01, 0.02, 0.05]:
        info = _compute_volatility_based_sl_tp(atr_pct, "BUY", hp, pb)
        entry = 150.0
        if info:
            sl = round(entry * (1 - info["sl_pct"] / 100), 2)
            tp = round(entry * (1 + info["sl_pct"] * info["tp_multiplier"] / 100), 2) if info["tp_multiplier"] < 100 else 0.0
            print(f"atr_pct={atr_pct:.3f} bucket={info['bucket']} SL={sl} TP={tp}")
    # rr_ratio 999 哨兵处理
    assert _compute_volatility_based_sl_tp(0.02, "BUY", hp, pb)["tp_multiplier"] < 100
    print("rr_ratio 哨兵未导致异常 TP ✔")


def test_factor_exit_rules():
    print("\n--- 因子退出规则 ---")
    pb = get_playbook_config()
    rules = pb.get_horizon_top("factor_exit_rules", {})
    assert "insider_cluster_buy" in rules, "缺少 insider_cluster_buy 退出规则"
    insider = _get_factor_exit_config(["insider_cluster_buy", "above_ma20"], rules)
    print(f"insider_cluster_buy: trailing={insider['trailing_enabled']}, time_exit_hours={insider['time_exit_hours']}, max_hold_days={insider['max_hold_days']}")

    vol = _get_factor_exit_config(["volume_expansion"], rules)
    print(f"volume_expansion: trailing={vol['trailing_enabled']}, time_exit_hours={vol['time_exit_hours']}, max_hold_days={vol['max_hold_days']}")

    meta = {"conditions": ["insider_cluster_buy"], "horizon": "short", "_symbol": "US.AAPL", "_log_tag": "test", "take_profit": 200}
    reason, _, _, _ = _check_playbook_exit_rules(
        "LONG", 150, 149, 140, peak_price=0, meta=meta,
        held_minutes=400, minutes_to_close=120, minutes_from_open=60, entry_time_str="2026-06-30T09:35:00"
    )
    assert reason == "", "趋势因子持仓 400 分钟且亏损不应触发 time_exit"
    print("趋势因子 400min 亏损未触发 time_exit ✔")

    meta2 = {"conditions": ["volume_expansion"], "horizon": "short", "_symbol": "US.TSLA", "_log_tag": "test", "take_profit": 200}
    reason, _, _, _ = _check_playbook_exit_rules(
        "LONG", 150, 149, 140, peak_price=0, meta=meta2,
        held_minutes=40, minutes_to_close=120, minutes_from_open=60, entry_time_str="2026-06-30T09:35:00"
    )
    assert reason == "TIME_EXIT", "volume_expansion 应 30 分钟内不盈利即退出"
    print("volume_expansion 40min 亏损触发 TIME_EXIT ✔")


def main():
    test_market_state_overlay()
    test_reprice_logic()
    test_factor_exit_rules()
    print("\n全链路冒烟测试通过 ✔")


if __name__ == "__main__":
    main()
