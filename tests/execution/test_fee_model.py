"""fee_model 单测 (DEB-20260730-fee-net-pnl G1): 覆盖每条费率规则边界。

判据 (预注册): min $1.99 触发/未触发、max 0.3%、SEC 分段日与 min、TAF min/max、
卖侧-only、SHORT 方向对称 (入场腿=SELL 照收 SEC/TAF)、OTC CAT、借券费日内=0。
全部手算期望值, 不引用模型自身输出。
"""
import pytest

from app.execution.fee_model import (
    accrue_borrow_fee,
    estimate_fee,
    estimate_round_trip,
    get_schedule,
)


@pytest.fixture(autouse=True)
def _fresh_schedule():
    """强制内置缺省费率表 (隔离 config 环境差异)。"""
    import app.execution.fee_model as fm
    fm._schedule_cache = dict(fm._DEFAULT_SCHEDULE)
    yield
    fm._schedule_cache = None


# ── 佣金三段: floor $1.99 / per-share / 0.3% cap ──

def test_commission_min_floor_triggered():
    # 100 股: per_share=1.00 < 1.99 → floor 托底
    f = estimate_fee(100, 50.0, "BUY", "2026-07-29")
    assert f["commission"] == 1.99


def test_commission_per_share_zone():
    # 300 股 × $50: per_share=3.00, cap=0.003×15000=45 → 取 3.00
    f = estimate_fee(300, 50.0, "BUY", "2026-07-29")
    assert f["commission"] == 3.00


def test_commission_max_cap_triggered():
    # 1000 股 × $2 (低价股): per_share=10, cap=0.003×2000=6.00 → cap 压制
    f = estimate_fee(1000, 2.0, "BUY", "2026-07-29")
    assert f["commission"] == 6.00


def test_commission_cap_below_floor_takes_floor():
    # 100 股 × $1: per_share=1.00, cap=0.30 → min=0.30 再被 floor 1.99 托底 (先 cap 后 floor)
    f = estimate_fee(100, 1.0, "BUY", "2026-07-29")
    assert f["commission"] == 1.99


# ── 卖侧-only 费项 (SEC/TAF) 与买侧对照 ──

def test_buy_side_no_sec_taf():
    f = estimate_fee(500, 40.0, "BUY", "2026-07-29")
    assert f["sec"] == 0.0 and f["taf"] == 0.0


def test_sell_side_sec_and_taf():
    # 500 股 × $40 = $20000: SEC=0.0000206×20000=0.412; TAF=0.000195×500=0.0975
    f = estimate_fee(500, 40.0, "SELL", "2026-07-29")
    assert f["sec"] == pytest.approx(0.412, abs=1e-4)
    assert f["taf"] == pytest.approx(0.0975, abs=1e-4)


def test_sec_min_001():
    # 10 股 × $10 = $100: SEC=0.00206 < 0.01 → min 托底
    f = estimate_fee(10, 10.0, "SELL", "2026-07-29")
    assert f["sec"] == 0.01


def test_taf_min_and_max():
    # min: 10 股 → 0.00195 < 0.01 → 0.01
    assert estimate_fee(10, 10.0, "SELL", "2026-07-29")["taf"] == 0.01
    # max: 60000 股 → 11.7 > 9.79 → cap
    assert estimate_fee(60000, 5.0, "SELL", "2026-07-29")["taf"] == 9.79


# ── SEC 时间序列分段 (2026-04-04 生效) ──

def test_sec_zero_before_effective_date():
    f = estimate_fee(500, 40.0, "SELL", "2026-04-03")
    assert f["sec"] == 0.0  # 生效日前费率 0, 且 min 不托底 (rate=0 整项免收)


def test_sec_effective_from_date():
    f = estimate_fee(500, 40.0, "SELL", "2026-04-04")
    assert f["sec"] == pytest.approx(0.412, abs=1e-4)


# ── 双边费项: 交收 + CAT (NMS/OTC) ──

def test_settlement_and_cat_both_sides():
    for side in ("BUY", "SELL"):
        f = estimate_fee(1000, 20.0, side, "2026-07-29")
        assert f["settlement"] == 3.00  # 0.003×1000
        assert f["cat"] == pytest.approx(0.003, abs=1e-6)  # 0.000003×1000


def test_cat_otc_rate():
    f = estimate_fee(1000, 20.0, "BUY", "2026-07-29", mkt="OTC")
    assert f["cat"] == pytest.approx(0.00003, abs=1e-8)


# ── 往返与 SHORT 对称 ──

def test_round_trip_long_vs_short_symmetry():
    # LONG: 入场 BUY (无 SEC/TAF) + 出场 SELL (有); SHORT 恰好镜像 — 同价往返总费用相等
    lng = estimate_round_trip(300, 50.0, 50.0, "LONG", "2026-07-29")
    sht = estimate_round_trip(300, 50.0, 50.0, "SHORT", "2026-07-29")
    assert lng["total"] == pytest.approx(sht["total"], abs=1e-6)
    # SHORT 入场腿是 SELL → SEC/TAF 在入场侧
    assert sht["entry"]["sec"] > 0 and sht["exit"]["sec"] == 0.0
    assert lng["entry"]["sec"] == 0.0 and lng["exit"]["sec"] > 0


def test_round_trip_fixed_cost_structure():
    # DEB 结构性发现复算: 小单往返固定成本 ≈$4-5 (双边 min 佣金主导)
    rt = estimate_round_trip(30, 80.0, 81.0, "LONG", "2026-07-29")
    assert 4.0 < rt["total"] < 5.0


# ── 借券费 ──

def test_borrow_fee_intraday_zero():
    assert accrue_borrow_fee(100, 50.0, 3.0, hold_days=0.4) == 0.0


def test_borrow_fee_overnight():
    # 100×$50×3%×1/365 = 0.411
    assert accrue_borrow_fee(100, 50.0, 3.0, hold_days=1.0) == pytest.approx(0.411, abs=0.001)


# ── 退化输入 ──

def test_zero_qty_or_price_returns_zero():
    assert estimate_fee(0, 50.0, "SELL")["total"] == 0.0
    assert estimate_fee(100, 0, "SELL")["total"] == 0.0


def test_schedule_has_all_keys():
    s = get_schedule()
    for k in ("commission_per_share", "sec_fee_schedule", "taf_max", "borrow_day_basis"):
        assert k in s
