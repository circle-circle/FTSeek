"""RFC-186b: 费用可行下限 sizing + 价格地板拒因。

背景 (2026-11-06 实锤): 费用地板 $1.99/腿是固定成本 ⇒ 名义额越小, 往返费率越高。
`RISK_OFF ×0.3` 下 risk 预算 $148.69, 4-6% 止损 ⇒ 名义 $2.6–3.3k ⇒ 费率 12.8–31.2bps,
全部越过 `max_fee_bps=11.54` ⇒ 当日 20 条指令 18 条被 admission 拒, 只剩 2 条且未算尺寸。

本文件钉住三条不变量:
 ① fee_floor_sizing=True 时, 产出股数的费用率必须 ≤ max_fee_bps (且在 6% 仓位帽内);
 ② fee_floor_sizing=False (旧行为) 时**不**满足费用线 —— 保证本修复不是空操作;
 ③ 低价股 (price < 200/max_fee_bps) 得到 PRICE_BELOW_FEE_FLOOR 拒因 (费用物理不可行),
    与"这单太小"分开。
"""
from __future__ import annotations

import pytest

import app.playbook.admission as _adm
from app.playbook.admission import evaluate, fee_bps_at, price_floor_usd
from app.playbook.instruction_generator import compute_position_size

EQ = 99128.0
RISK_PCT = 0.5
MAX_POS_PCT = 0.06
MAX_FEE_BPS = 11.54
MIN_NOTIONAL = 2500.0
REGIME_MULT = 0.3          # RISK_OFF
DATE = "2026-11-06"


def _size(fee_floor: bool, entry: float, sl: float, direction: str = "BUY") -> int:
    return compute_position_size(
        EQ, RISK_PCT, entry, sl, MAX_POS_PCT,
        regime_mult=REGIME_MULT, water_mult=1.0, conf_factor=1.0,
        fee_floor=fee_floor, max_fee_bps=MAX_FEE_BPS,
        min_notional_usd=MIN_NOTIONAL, direction=direction, trade_date=DATE,
    )


def test_fee_floor_meets_fee_line_within_cap():
    """开启后: 费用率 ≤ 11.54bps 且不越 6% 仓位帽 (AMZN 11-06 实参: 255/248)。"""
    entry, sl = 255.0, 241.5   # AMZN 11-06 实参形态: 5.3% 止损 ⇒ 费用地板绑定
    q = _size(True, entry, sl)
    assert q > 0
    bps = fee_bps_at(q, entry, "BUY", DATE)
    assert bps is not None and bps <= MAX_FEE_BPS, (q, bps)
    assert q * entry <= EQ * MAX_POS_PCT + 1e-6, (q, q * entry)
    assert q * entry >= MIN_NOTIONAL


def test_floor_off_does_not_meet_fee_line():
    """关闭 (旧行为): 同样参数费用率越线 —— 证明这行修复不是空操作 (变异等价)。"""
    entry, sl = 255.0, 241.5
    q_old = _size(False, entry, sl)
    bps_old = fee_bps_at(q_old, entry, "BUY", DATE)
    assert bps_old is not None and bps_old > MAX_FEE_BPS, (q_old, bps_old)
    assert _size(True, entry, sl) > q_old


def test_cap_binds_before_fee_line_is_left_alone():
    """高价股 + 6% 帽: 抬到帽上限仍不满足费用线时不得越帽 (宁可不成交也不放大仓位)。"""
    entry, sl = 2611.0 * 2, 2611.0 * 2 * 0.97   # $5,222: 6%帽 ≈ 1 股
    q = _size(True, entry, sl)
    assert q * entry <= EQ * MAX_POS_PCT + 1e-6


@pytest.fixture
def fee_gate_on():
    """测试必须钉住费用门的开关状态, 不得依赖线上 YAML。

    2026-11-06 用户裁「关闭费用门拦截」后, 线上 `fee_gate_enforce: false` ⇒ 若测试直接读
    真配置, "应当被拒"的断言会随线上裁量而红/绿翻转 (测试变成环境的函数)。
    这里把缓存配置临时改回 true, 用例结束还原。
    """
    cfg = _adm.get_admission_config(refresh=True)
    old = cfg.get("fee_gate_enforce")
    cfg["fee_gate_enforce"] = True
    try:
        yield
    finally:
        cfg["fee_gate_enforce"] = old


def test_price_below_floor_reason_distinct_from_size(fee_gate_on):
    """低价股 (INTT 11-06 实参 $12.30): 任何规模都过不了 → 独立拒因, 与 NOTIONAL 类分开。"""
    floor = price_floor_usd(MAX_FEE_BPS)
    assert abs(floor - 200.0 / MAX_FEE_BPS) <= 0.01  # 展示值保留 2 位小数
    res = evaluate({
        "symbol": "US.INTT", "direction": "BUY", "entry_price": 12.30,
        "stop_loss": 11.68, "take_profit": 13.85, "position_size": 400,  # 名义 $4,920 已过 2500 门
        "confidence": 50, "date": DATE,
    })
    assert res["verdict"] == "REJECT"
    assert any(r.startswith("PRICE_BELOW_FEE_FLOOR") for r in res["reasons"]), res["reasons"]
    assert not any(r.startswith("NOTIONAL_TOO_SMALL") for r in res["reasons"])


def test_gate_off_records_but_never_rejects():
    """2026-11-06 用户裁: 关闸后判据仍算 (would_reject 留痕), 但不得把指令打成 REJECT。"""
    cfg = _adm.get_admission_config(refresh=True)
    old = cfg.get("fee_gate_enforce")
    cfg["fee_gate_enforce"] = False
    try:
        res = evaluate({
            "symbol": "US.AMZN", "direction": "BUY", "entry_price": 261.06,
            "stop_loss": 248.01, "take_profit": 280.64, "position_size": 11,
            "confidence": 50, "date": DATE,
        })
    finally:
        cfg["fee_gate_enforce"] = old
    assert res["verdict"] == "PASS", res["reasons"]
    fg = res["metrics"].get("fee_gate") or {}
    assert fg.get("enforced") is False
    assert any(r.startswith("FEE_BPS_TOO_HIGH") for r in fg.get("would_reject") or []), fg


def test_price_above_floor_no_price_reason(fee_gate_on):
    """价格高于地板时不得误报价格地板 (避免把正常单打成结构性不可行)。"""
    res = evaluate({
        "symbol": "US.AMZN", "direction": "BUY", "entry_price": 255.0,
        "stop_loss": 241.5, "take_profit": 285.0, "position_size": 15,  # 3825$ ⇒ 10.9bps
        "confidence": 50, "date": DATE,
    })
    assert not any(r.startswith("PRICE_BELOW_FEE_FLOOR") for r in res["reasons"]), res["reasons"]
    assert res["verdict"] == "PASS", res["reasons"]

def test_gate_off_still_blocks_low_price_floor():
    """2026-11-06 用户裁·二次: 费用门拦截关闭后, 低价股地板必须继续硬拦。

    机制不同不能用同一开关: INTT $12.30 < 200/11.54=$17.33 ⇒ $0.01/股佣金使费率与股数无关,
    任何规模都过不了线 (实测 <$10 档 13 笔 毛 +0.77% / 费 1.21% ⇒ 恒亏)。
    """
    cfg = _adm.get_admission_config(refresh=True)
    old_fee, old_pf = cfg.get("fee_gate_enforce"), cfg.get("price_floor_enforce")
    cfg["fee_gate_enforce"], cfg["price_floor_enforce"] = False, True
    try:
        res = evaluate({
            "symbol": "US.INTT", "direction": "BUY", "entry_price": 12.30,
            "stop_loss": 11.70, "take_profit": 13.80, "position_size": 300,
            "confidence": 50, "date": DATE,
        })
    finally:
        cfg["fee_gate_enforce"], cfg["price_floor_enforce"] = old_fee, old_pf
    assert res["verdict"] == "REJECT", res
    assert any(r.startswith("PRICE_BELOW_FEE_FLOOR") for r in res["reasons"]), res["reasons"]
    # 尺寸类仍只留痕
    assert not any(r.startswith(("FEE_BPS_TOO_HIGH", "NOTIONAL_TOO_SMALL")) for r in res["reasons"]), res["reasons"]


def test_gate_off_mid_price_still_passes():
    """回归: 关闸后正常价位的单不得被任何费用判据翻 SKIP。"""
    cfg = _adm.get_admission_config(refresh=True)
    old_fee, old_pf = cfg.get("fee_gate_enforce"), cfg.get("price_floor_enforce")
    cfg["fee_gate_enforce"], cfg["price_floor_enforce"] = False, True
    try:
        res = evaluate({
            "symbol": "US.AMZN", "direction": "BUY", "entry_price": 261.06,
            "stop_loss": 248.01, "take_profit": 280.64, "position_size": 11,
            "confidence": 50, "date": DATE,
        })
    finally:
        cfg["fee_gate_enforce"], cfg["price_floor_enforce"] = old_fee, old_pf
    assert res["verdict"] == "PASS", res["reasons"]
    assert not any(r.startswith(("PRICE_BELOW_FEE_FLOOR", "FEE_BPS_TOO_HIGH")) for r in res["reasons"])


def test_price_floor_off_records_only():
    """两键全关 ⇒ 只留痕, 不拦 (可回滚态)。"""
    cfg = _adm.get_admission_config(refresh=True)
    old_pf = cfg.get("price_floor_enforce")
    cfg["fee_gate_enforce"], cfg["price_floor_enforce"] = False, False
    try:
        res = evaluate({
            "symbol": "US.INTT", "direction": "BUY", "entry_price": 12.30,
            "stop_loss": 11.70, "take_profit": 13.80, "position_size": 300,
            "confidence": 50, "date": DATE,
        })
    finally:
        cfg["price_floor_enforce"] = old_pf
    fg = res["metrics"].get("fee_gate") or {}
    assert any(r.startswith("PRICE_BELOW_FEE_FLOOR") for r in fg.get("would_reject") or []), fg
    assert res["verdict"] == "PASS", res["reasons"]
