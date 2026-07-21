"""轨道A 阶段0 单测: entry_reference → entry_price 解析。

覆盖枚举映射、方向差异 (LONG/SHORT)、引擎位注入、缺失安全回退 market、无价兜底。
并验证 market 语义与 generate 路径 (BUY=ask / SHORT=bid / 回退 price) 一致。
"""
from types import SimpleNamespace

from app.playbook.entry_reference import resolve_entry_reference as R, ALLOWED_ENTRY_REFERENCES


def _tech(ask=0.0, bid=0.0, price=0.0, ma20=0.0, ma50=0.0):
    return SimpleNamespace(ask=ask, bid=bid, price=price, ma20=ma20, ma50=ma50)


def test_market_long_uses_ask():
    p, src = R("BUY", "market", _tech(ask=101.0, bid=100.0, price=100.5))
    assert p == 101.0 and src == "market"


def test_market_short_uses_bid():
    p, src = R("SELL_SHORT", "market", _tech(ask=101.0, bid=100.0, price=100.5))
    assert p == 100.0 and src == "market"


def test_market_falls_back_to_price_when_no_quote():
    p, src = R("BUY", "market", _tech(price=100.5))
    assert p == 100.5 and src == "market"


def test_no_price_returns_zero():
    p, src = R("BUY", "market", _tech())
    assert p == 0.0 and src == "no_price"


def test_pullback_long_uses_support():
    p, src = R("LONG", "pullback_to_support", _tech(ask=101, ma20=98), levels={"support": 97.0})
    assert p == 97.0 and "support" in src


def test_pullback_long_falls_back_to_ma20():
    p, src = R("LONG", "pullback_to_support", _tech(ask=101, ma20=98))
    assert p == 98.0 and "ma20" in src


def test_pullback_short_uses_resistance():
    p, src = R("SHORT", "pullback_to_support", _tech(bid=100, ma20=102), levels={"resistance": 103.0})
    assert p == 103.0 and "resistance" in src


def test_pullback_no_level_falls_back_to_market():
    # 无 support 且无 ma20 → 回退 market(ask)
    p, src = R("BUY", "pullback_to_support", _tech(ask=101, price=100))
    assert p == 101.0 and "market" in src


def test_breakout_long_uses_resistance():
    p, src = R("LONG", "breakout_above_resistance", _tech(ask=101), levels={"resistance": 105.0})
    assert p == 105.0 and "resistance" in src


def test_breakout_short_uses_support():
    p, src = R("SHORT", "breakout_above_resistance", _tech(bid=100), levels={"support": 95.0})
    assert p == 95.0 and "support" in src


def test_vwap_and_fallback():
    p, _ = R("BUY", "vwap", _tech(ask=101), levels={"vwap": 99.5})
    assert p == 99.5
    p2, src2 = R("BUY", "vwap", _tech(ask=101))
    assert p2 == 101.0 and "market" in src2


def test_ma20_ma50():
    assert R("BUY", "ma20", _tech(ask=101, ma20=98))[0] == 98.0
    assert R("BUY", "ma50", _tech(ask=101, ma50=95))[0] == 95.0


def test_orb_direction():
    assert R("LONG", "orb", _tech(ask=101), levels={"orb_high": 102, "orb_low": 99})[0] == 102.0
    assert R("SHORT", "orb", _tech(bid=100), levels={"orb_high": 102, "orb_low": 99})[0] == 99.0


def test_unknown_ref_falls_back_to_market():
    p, src = R("BUY", "some_garbage", _tech(ask=101))
    assert p == 101.0 and "market" in src


def test_all_enums_never_crash_and_bounded():
    # 遍历所有枚举: 有价时必返回正价, 且不抛异常
    for ref in ALLOWED_ENTRY_REFERENCES + ["", "weird"]:
        for direction in ("BUY", "SELL_SHORT"):
            p, src = R(direction, ref, _tech(ask=101, bid=100, price=100.5, ma20=98, ma50=95),
                       levels={"support": 97, "resistance": 105, "vwap": 99.5, "orb_high": 102, "orb_low": 99})
            assert p > 0 and isinstance(src, str)
