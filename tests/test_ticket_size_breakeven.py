"""客单价 × 费率盈亏平衡账契约测试 (2026-09-07, DEB-20260907 建议②).

锁的是**算术不变量与归口纪律**, 不是某天的读数:
  1. 渐近地板分解必须与 fee_model SSOT 在大名义额处等价 (证明没有自建费率公式)
  2. fee_bps 对名义额、对股价都单调不增 (二分求解的前提)
  3. 不可达时必须返回 None, 不得返回一个假的最小值 (P=$5 时 10bps 永远达不到)
  4. RFC-071 的算术后果锁死: min_notional 3000→2500 = 费用地板 13.27→15.92bps
  5. 只读: DB 以 mode=ro 打开; 输出落 data/shadow/ (不碰生产判分文件)
  6. 档位反事实归口 admission_replay.py — 本脚本不得自己实现拦单反事实
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts import ticket_size_breakeven as T


# ── 1. 与 fee_model SSOT 等价 (不造平行层) ────────────────────────────────

@pytest.mark.parametrize("price", [5.0, 10.0, 20.0, 50.0, 100.0, 250.0, 600.0])
def test_asymptotic_decomposition_matches_ssot(price):
    """在"佣金地板不绑 + TAF 上限不绑"的名义额上, 分解式必须等于直接调 fee_model。

    取 N = 1000 × price ⇒ qty = 1000 股: 佣金 $10 > $1.99 地板, TAF $0.195 < $9.79 上限。
    (拿 N=1e9 比会因 TAF 触顶而差 ~0.4bps — 见 fee_floor_decomposition 的口径边界注。)
    """
    dec = T.fee_floor_decomposition(price)
    direct = T.rt_fee_bps(1000.0 * price, price)
    assert dec["asymptotic_fee_bps"] == pytest.approx(direct, abs=0.05)
    assert dec["commission_bps"] == pytest.approx(10000.0 * 0.02 / price, abs=1e-6)
    assert dec["sec_bps"] == pytest.approx(0.206, abs=0.01)


def test_uses_fee_model_not_a_local_rate_table():
    src = Path(inspect.getfile(T)).read_text()
    assert "from app.execution.fee_model import estimate_round_trip" in src
    assert "def estimate_round_trip" not in src            # 没有本地重定义
    assert "def estimate_fee" not in src


# ── 2. 单调性 (二分求解前提) ──────────────────────────────────────────────

@pytest.mark.parametrize("price", [5.0, 20.0, 100.0, 600.0])
def test_fee_bps_monotone_decreasing_in_notional(price):
    ns = [1000, 2000, 3000, 5000, 8000, 20000, 100000]
    vals = [T.rt_fee_bps(n, price) for n in ns]
    assert all(a >= b for a, b in zip(vals, vals[1:]))


@pytest.mark.parametrize("notional", [2500, 5000, 20000])
def test_fee_bps_monotone_decreasing_in_price(notional):
    ps = [5, 10, 20, 50, 100, 250, 600, 2000]
    vals = [T.rt_fee_bps(notional, p) for p in ps]
    assert all(a >= b for a, b in zip(vals, vals[1:]))


# ── 3. 双地板: 两条下限互不可替代 ─────────────────────────────────────────

def test_price_floor_cannot_be_beaten_by_bigger_notional():
    """$5 标的的渐近地板 ≈52.6bps — 名义额加到 $1e9 也压不到 10bps。"""
    assert T.rt_fee_bps(5000.0, 5.0) == pytest.approx(52.6, abs=0.05)
    assert T.min_notional_for_fee_bps(10.0, 5.0) is None
    assert T.min_notional_for_fee_bps(60.0, 5.0) is not None


def test_notional_floor_cannot_be_beaten_by_higher_price():
    """$2,500 名义额的佣金地板 ≈15.9bps — 换 $10000 高价股也压不到 14bps。"""
    assert T.rt_fee_bps(2500.0, 10000.0) > 15.0
    assert T.min_price_for_fee_bps(14.0, 2500.0) is None
    assert T.min_price_for_fee_bps(20.0, 2500.0) is not None


def test_min_notional_solver_brackets_the_target():
    """解出的最小名义额: 该点达标, 再小一点就不达标 (二分边界正确)。"""
    n_star = T.min_notional_for_fee_bps(10.0, 600.0)
    assert n_star is not None
    assert T.rt_fee_bps(n_star, 600.0) <= 10.0
    assert T.rt_fee_bps(n_star - 50.0, 600.0) > 10.0


def test_min_price_solver_brackets_the_target():
    p_star = T.min_price_for_fee_bps(10.0, 5900.0)
    assert p_star is not None
    assert T.rt_fee_bps(5900.0, p_star) <= 10.0
    # 佣金地板主导区里 fee_bps 对股价几乎平坦 ⇒ 用腰斩而非 2% 扰动来验边界
    assert T.rt_fee_bps(5900.0, p_star / 2.0) > 10.0


def test_solvers_reject_degenerate_inputs():
    assert T.min_notional_for_fee_bps(0.0, 100.0) is None      # target 非正
    assert T.min_notional_for_fee_bps(10.0, 0.0) is None       # 股价非正
    assert T.min_price_for_fee_bps(0.0, 5000.0) is None
    assert T.min_price_for_fee_bps(10.0, 0.0) is None
    assert T.rt_fee_bps(0.0, 100.0) is None
    assert T.rt_fee_bps(5000.0, 0.0) is None
    assert T.rt_fee_bps(-1.0, 100.0) is None


# ── 4. RFC-071 的算术后果 (锁死, 防止旋钮语义被误读成"风险下限") ──────────

def test_min_notional_knob_is_really_a_fee_floor():
    assert T.notional_only_floor_bps(3000.0) == pytest.approx(13.27, abs=0.01)
    assert T.notional_only_floor_bps(2500.0) == pytest.approx(15.92, abs=0.01)
    # 3000→2500 是把费用地板**抬高** 2.65bps (换吞吐量的代价)
    assert T.notional_only_floor_bps(2500.0) > T.notional_only_floor_bps(3000.0)
    assert T.notional_only_floor_bps(0) is None


def test_equity_conversion():
    assert T.equity_for_notional(6000.0, 0.06) == pytest.approx(100000.0)
    assert T.equity_for_notional(6000.0, 0) is None
    assert T.equity_for_notional(0, 0.06) is None


# ── 5. 统计小工具 ─────────────────────────────────────────────────────────

def test_mean_median_and_excl_extreme():
    xs = [1.0, 2.0, 3.0, 4.0, -100.0]
    assert T.mean(xs) == pytest.approx(-18.0)
    assert T.median(xs) == pytest.approx(2.0)
    # 剔除 |值| 最大的 3 笔后只剩 1,2 ⇒ 极值不再绑架均值
    assert T.excl_extreme_mean(xs, 3) == pytest.approx(1.5)
    assert T.mean([]) is None and T.median([]) is None
    assert T.excl_extreme_mean([1.0, 2.0], 3) is None


def test_bucketize_counts_and_shares():
    rows = [{"notional": 1000, "fee_bps": 40.0}, {"notional": 3000, "fee_bps": 13.0},
            {"notional": 6000, "fee_bps": 7.0}, {"notional": None, "fee_bps": None}]
    out = T.bucketize(rows, "notional", (0, 2500, 5000, 1e9))
    assert [b["n"] for b in out] == [1, 1, 1]
    assert out[0]["band"] == "[0,2500)"
    assert out[0]["share_pct"] == pytest.approx(25.0)
    assert out[0]["mean_fee_bps"] == pytest.approx(40.0)


def test_bucketize_value_key_is_parameterizable():
    rows = [{"price": 10, "notional": 3000}]
    out = T.bucketize(rows, "price", (0, 20, 1e9), value_key="notional")
    assert out[0]["mean_notional"] == pytest.approx(3000.0)


# ── 6. 只读与归口契约 ─────────────────────────────────────────────────────

def test_db_opened_read_only_and_output_under_shadow():
    src = Path(inspect.getfile(T)).read_text()
    assert '?mode=ro' in src
    assert "OUT_LATEST = REPO / \"data\" / \"shadow\"" in src
    # 不得写生产判分文件 (data/ 根下的 *_latest.json 是记分牌落点)
    assert "data/fee_unit_economics_latest.json" not in src


def test_knob_counterfactual_is_delegated_not_reimplemented():
    """档位反事实归口 scripts/admission_replay.py — 本脚本不得自建拦单模拟。"""
    src = Path(inspect.getfile(T)).read_text()
    assert "admission_replay.py" in src
    assert "def blocked(" not in src
    assert "FEE_INEFFICIENT" not in src


def test_price_and_notional_grids_cover_the_live_knobs():
    """网格必须包含现行旋钮值, 否则报告读不到"现在"在哪一格。"""
    cfg = T.load_cfg()
    assert cfg["min_notional_usd"] in T.NOTIONAL_GRID or any(
        abs(n - cfg["min_notional_usd"]) < 1 for n in T.NOTIONAL_GRID)
    assert any(abs(p - cfg["min_entry_price"]) < 0.01 for p in T.PRICE_GRID)
