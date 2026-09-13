"""[P1.1/P1.2 2026-09-12] 统一可交易性初筛 — 纯规则契约 (DEB-20260910 / proud-slope-falcon).

被测对象全部是仓内纯函数/可注入 IO 的薄壳:
  `app/playbook/candidate_eligibility.py` (evaluate_candidate / screen_candidates /
   select_candidates / selection_summary / record_candidate_batch / load_candidate_policy)
  `app/core/atr_provider.py` (compute_screening_atr / screening_asof_date / get_screening_atr)

钉住的不变量 (按计划验证清单 §1 逐条):
  1. 价格地板是**闭区间下界**: price == min_price 放行, 低一分钱拒绝 (边界不误杀);
  2. 低 ATR 门用**比例**不是百分数: 0.03 = 3%; ratio == 阈值放行 (`<` 才拒),
     量纲写错 (3.0 vs 0.03) 会让门恒拒或恒放 — 本文件用双例钉死;
  3. 非有限值 (NaN/Inf/None/bool) 一律进 missing_fields → UNKNOWN, **不是 REJECT**:
     缺数 ≠ 不可交易; 拿 NaN 当"低价"拒绝会静默改变候选集;
  4. 筛选 ATR 只用**严格早于 asof 的已完成日线**: 当日未完成 bar 混进去 = 用半个
     交易日振幅冒充日线 ATR (盘前跑批必错);
  5. 拆股/复权断点不跨算 TR: 相邻收盘比 >1.6x 即截断, 断点后不足 15 根 → UNKNOWN;
  6. 不足 15 根 / 非法 bar / 无数据 → UNKNOWN + reason, 绝不外推、绝不返回 0;
  7. SHORT 借券拒绝**不得误杀同股 LONG 研究** (方向隔离); 借券未知记 missing 不记可借;
  8. select_candidates = "先过滤后预算": 被拒者不占名额, 由剩余合格队列**补位**;
     跨阶段/跨来源重复项不占两个名额; 四态 (selected/rejected_ineligible/
     unknown_deferred/duplicate/budget_cut) 分开记录, 不混成一个 SKIP;
  9. screening_asof_date 拒绝 naive datetime — app.utils.timezone.to_et() 把 naive 当
     北京时间, 静默转换会移半天, 可能把当日未完成 bar 当已完成日线;
 10. 调用方已定出 effective_price 时, 资格判定必须用**同一个价** (筛选价 == 指令锚价);
 11. load_candidate_policy 走 get_playbook_config()/get_admission_config() 单一真源,
     与活 unified_config.yaml 逐项一致 (反分叉钉)。
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.core.atr_provider import (  # noqa: E402
    ATRProvider, SCREENING_MIN_BARS, SCREENING_SPLIT_RATIO, SCREENING_TR_WINDOW,
    compute_screening_atr, screening_asof_date,
)
from app.playbook.candidate_eligibility import (  # noqa: E402
    POLICY_VERSION, canonical_symbol, evaluate_candidate, load_candidate_policy,
    make_batch_id, positive_number, record_candidate_batch, screen_candidates,
    select_candidates, selection_summary,
)

ET = timezone(timedelta(hours=-4))
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=ET)     # 常规时段观测点 (tz-aware)
PRE = NOW.replace(hour=7, minute=0)               # 盘前观测点 (04:00-09:30 ET)
ASOF = "2026-09-11"


def policy(**over):
    """默认策略: 低 ATR 门开 (阈值 0.03=3%), 盘口/借券门关 (DA 侧口径)。"""
    base = {
        "version": POLICY_VERSION,
        "min_price": 5.0,
        "low_atr_enabled": True,
        "min_atr_ratio": 0.03,
        "max_quote_age_sec": 90.0,
        "max_spread_ratio": 0.05,
        "quote_gate_enabled": False,
        "borrow_gate_enabled": False,
    }
    base.update(over)
    return base


def snap(**over):
    """一份默认合格快照 (价 100, ATR 比例 4%, 无盘口/借券门)。"""
    base = {"symbol": "US.TEST", "price": 100.0, "screening_atr_ratio": 0.04,
            "quote_age_sec": None, "bid": None, "ask": None,
            "direction_hint": "LONG"}
    base.update(over)
    return base


def daily_bars(n=20, vol=0.02, base=100.0, last_day=None, jump_at=None):
    """n 根已完成日线 (末根 = last_day, 默认 asof 前一交易日)。

    jump_at=k ⇒ 倒数第 k 根与后一根之间插入 >SCREENING_SPLIT_RATIO 的收盘跳变
    (拆股/复权口径断点)。
    """
    day = last_day or (date.fromisoformat(ASOF) - timedelta(days=1))
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    out, close = [], base
    for idx, d in enumerate(days):
        if jump_at is not None and idx == n - jump_at:
            close *= SCREENING_SPLIT_RATIO * 1.5           # 明确超过断点阈值
        out.append({"time": d.isoformat(), "open": close,
                    "high": close * (1 + vol), "low": close * (1 - vol),
                    "close": close})
        close *= 1.001
    return out


# ══ 1. 价格地板边界 ══════════════════════════════════════════════════════
class TestPriceFloor:
    def test_price_equal_to_floor_passes(self):
        """min_price 是闭区间下界: 恰好等于阈值不算违规 (边界不误杀)。"""
        assert evaluate_candidate(snap(price=5.0), policy())["status"] != "REJECT"

    def test_one_cent_below_floor_rejects(self):
        result = evaluate_candidate(snap(price=4.99), policy())
        assert result["status"] == "REJECT"
        assert "price_floor" in result["reasons"]

    def test_floor_is_not_hardcoded(self):
        """阈值来自 policy (admission.min_entry_price), 不是模块常量。"""
        assert evaluate_candidate(snap(price=3.0), policy(min_price=2.0))["status"] != "REJECT"
        assert "price_floor" in evaluate_candidate(
            snap(price=3.0), policy(min_price=5.0))["reasons"]


# ══ 2. 低 ATR 门 = 比例口径 ══════════════════════════════════════════════
class TestLowAtrRatio:
    def test_ratio_at_threshold_passes(self):
        """`atr < min_atr_ratio` 才拒 ⇒ 恰好 0.03 放行 (8/6 BABA=3.00% 实测边界)。"""
        assert evaluate_candidate(snap(screening_atr_ratio=0.03), policy())["status"] != "REJECT"

    def test_ratio_just_below_threshold_rejects(self):
        result = evaluate_candidate(snap(screening_atr_ratio=0.0299), policy())
        assert "low_atr" in result["reasons"]

    def test_ratio_is_fraction_not_percent(self):
        """量纲钉: 0.03 表示 3%。若有人把阈值写成 3.0, 则 4% (0.04) 会被判低波。"""
        assert evaluate_candidate(snap(screening_atr_ratio=0.04), policy())["status"] != "REJECT"
        # 反向钉: 阈值若被误写成百分数 3.0, 则 0.04 < 3.0 ⇒ 门恒拒 (全部被判低波)
        assert "low_atr" in evaluate_candidate(
            snap(screening_atr_ratio=0.04), policy(min_atr_ratio=3.0))["reasons"]

    def test_gate_disabled_skips_atr_check(self):
        assert evaluate_candidate(
            snap(screening_atr_ratio=0.001), policy(low_atr_enabled=False))["status"] != "REJECT"

    def test_missing_atr_is_unknown_not_reject(self):
        """缺 ATR → UNKNOWN (研究可继续, 执行端原风险门兜底), 不当低波拒绝。"""
        result = evaluate_candidate(snap(screening_atr_ratio=None), policy())
        assert result["status"] == "UNKNOWN"
        assert "screening_atr_ratio" in result["missing_fields"]
        assert result["reasons"] == []


# ══ 3. 非有限值 → missing, 不是 REJECT ═══════════════════════════════════
class TestNonFinite:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                     None, "", "abc", True, False])
    def test_bad_price_is_missing(self, bad):
        result = evaluate_candidate(snap(price=bad), policy())
        assert "price" in result["missing_fields"]
        assert "price_floor" not in result["reasons"]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "x"])
    def test_bad_atr_is_missing(self, bad):
        result = evaluate_candidate(snap(screening_atr_ratio=bad), policy())
        assert "screening_atr_ratio" in result["missing_fields"]
        assert "low_atr" not in result["reasons"]

    def test_positive_number_rejects_bool_and_nonfinite(self):
        assert positive_number(True) is None and positive_number(False) is None
        assert positive_number(float("nan")) is None
        assert positive_number(0) is None and positive_number(-1) is None
        assert positive_number("3.5") == 3.5

    def test_invalid_symbol_is_reject(self):
        """标的格式是确定性可判的 ⇒ REJECT (与"缺数"不同类)。"""
        for bad in ("", "TEST", "US.TOOLONG1", "us.aapl", "US.AAPL1"):
            assert "invalid_symbol" in evaluate_candidate(
                snap(symbol=bad), policy())["reasons"], bad

    def test_warrant_preferred_shape_rejected_with_own_reason(self):
        """权证/优先股形状: 仍拒绝, 但原因码说清"不交易的证券类型"而非"标的写错"。

        实测样本 (2026-09-12, data/shadow/llm_universe_scan*.jsonl 901 次浮现中 4 次):
        US.BBBY-WT / US.PSA-PK / US.CNO-PA / US.TDS-PV。混成 invalid_symbol 会把
        运营引向"哪来的错代码", 真实处置是"新闻点了本策略不交易的证券类型"。
        """
        for shape in ("US.BBBY-WT", "US.PSA-PK", "US.CNO-PA", "US.TDS-PV"):
            result = evaluate_candidate(snap(symbol=shape), policy())
            assert result["status"] == "REJECT", shape
            assert "unsupported_security_shape" in result["reasons"], shape
            assert "invalid_symbol" not in result["reasons"], shape

    def test_share_class_suffix_stays_tradable(self):
        """BRK.B / BRK-B 型分类股是普通股, 不得被上一条误伤 (形状门只咬多字母后缀)。"""
        for good in ("US.BRK.B", "US.BRK-B"):
            assert evaluate_candidate(snap(symbol=good), policy())["reasons"] == [], good

    def test_canonical_symbol_normalizes(self):
        assert canonical_symbol("AAPL") == "US.AAPL"
        assert canonical_symbol("US.AAPL") == "US.AAPL"
        assert canonical_symbol(None) == "US."


# ══ 4/5/6. 筛选 ATR 口径 (纯函数) ════════════════════════════════════════
class TestComputeScreeningAtr:
    def test_matches_tr14_rolling_mean(self):
        """与手算 TR14 rolling-mean 完全一致 (不用 Wilder 平滑, 不缩放)。"""
        bars = daily_bars(n=SCREENING_MIN_BARS + 3, vol=0.02)
        got = compute_screening_atr(bars, ASOF, symbol="US.TEST")
        assert got["status"] == "PASS"
        window = bars[-(SCREENING_MIN_BARS):]
        trs = []
        for idx in range(1, len(window)):
            prev = window[idx - 1]["close"]
            hi, lo = window[idx]["high"], window[idx]["low"]
            trs.append(max(hi - lo, abs(hi - prev), abs(lo - prev)))
        assert len(trs) == SCREENING_TR_WINDOW
        assert got["atr"] == pytest.approx(sum(trs) / len(trs), rel=1e-12)
        assert got["ref_close"] == pytest.approx(window[-1]["close"])

    def test_excludes_incomplete_bar_of_asof_day(self):
        """当日 bar 即使写了 complete=True 也必须排除 (只用已完成交易日)。"""
        bars = daily_bars(n=18, vol=0.02)
        same_day = dict(bars[-1], time=ASOF, high=bars[-1]["high"] * 3)
        with_today = bars + [same_day]
        assert compute_screening_atr(with_today, ASOF)["atr"] == pytest.approx(
            compute_screening_atr(bars, ASOF)["atr"], rel=1e-12)
        assert str(compute_screening_atr(with_today, ASOF)["bars_asof"]) < ASOF

    def test_split_discontinuity_truncates(self):
        """拆股/复权断点之后的段不足 15 根 → UNKNOWN, 不跨断点算 TR。"""
        got = compute_screening_atr(daily_bars(n=20, jump_at=8), ASOF)
        assert got["status"] == "UNKNOWN"
        assert got["reason"] == "split_discontinuity"
        assert got["atr"] is None

    def test_split_far_enough_back_still_passes(self):
        got = compute_screening_atr(daily_bars(n=40, jump_at=30), ASOF)
        assert got["status"] == "PASS"

    @pytest.mark.parametrize("n", [0, 1, 14, SCREENING_MIN_BARS - 1])
    def test_insufficient_bars_is_unknown(self, n):
        got = compute_screening_atr(daily_bars(n=n) if n else [], ASOF)
        assert got["status"] == "UNKNOWN" and got["atr"] is None

    def test_duplicate_same_day_keeps_last(self):
        bars = daily_bars(n=16, vol=0.02)
        dup = bars + [dict(bars[-1], close=bars[-1]["close"] * 1.0,
                           high=bars[-1]["high"], low=bars[-1]["low"])]
        assert compute_screening_atr(dup, ASOF)["atr"] == pytest.approx(
            compute_screening_atr(bars, ASOF)["atr"], rel=1e-12)

    def test_unsorted_input_is_normalized(self):
        bars = daily_bars(n=18)
        shuffled = list(reversed(bars))
        assert compute_screening_atr(shuffled, ASOF)["atr"] == pytest.approx(
            compute_screening_atr(bars, ASOF)["atr"], rel=1e-12)

    @pytest.mark.parametrize("bad", [
        {"high": float("nan")}, {"low": None}, {"close": 0},
        {"high": -1}, {"close": float("inf")},
    ])
    def test_invalid_bar_is_unknown(self, bad):
        bars = daily_bars(n=18)
        bars[-3].update(bad)
        got = compute_screening_atr(bars, ASOF)
        assert got["status"] == "UNKNOWN" and got["atr"] is None

    def test_high_below_low_is_unknown(self):
        bars = daily_bars(n=18)
        bars[-2]["high"], bars[-2]["low"] = bars[-2]["low"], bars[-2]["high"]
        assert compute_screening_atr(bars, ASOF)["status"] == "UNKNOWN"

    def test_no_bars_reports_reason(self):
        got = compute_screening_atr([], ASOF, symbol="US.TEST")
        assert got == {**got, "atr": None, "status": "UNKNOWN",
                       "reason": "no_completed_daily_bars"}
        assert got["symbol"] == "US.TEST"

    def test_declares_basis_source_and_adjustment(self):
        """口径必须自报 (量纲/来源声明军规): basis/source/adjustment 非空。"""
        got = compute_screening_atr(daily_bars(18), ASOF)
        assert got["basis"] == "daily_tr14_rolling_mean"
        assert got["source"] and got["adjustment"]
        assert got["bars_used"] == SCREENING_MIN_BARS


class TestScreeningAsof:
    def test_rejects_naive_datetime(self):
        """to_et(naive) 按北京时间处理会移半天 ⇒ asof 必须 tz-aware。"""
        with pytest.raises(ValueError):
            screening_asof_date(datetime(2026, 9, 11, 8, 0))

    def test_accepts_aware_datetime_date_and_str(self):
        assert screening_asof_date(NOW) == ASOF
        assert screening_asof_date(date(2026, 9, 11)) == ASOF
        assert screening_asof_date("2026-09-11") == ASOF
        assert screening_asof_date("2026-09-11T08:00:00-04:00") == ASOF

    def test_rejects_bad_string_and_type(self):
        with pytest.raises(ValueError):
            screening_asof_date("not-a-date")
        with pytest.raises(TypeError):
            screening_asof_date(12345)

    def test_utc_evening_maps_to_same_et_day(self):
        late = datetime(2026, 9, 11, 21, 0, tzinfo=timezone.utc)   # 17:00 ET
        assert screening_asof_date(late) == "2026-09-11"


class TestProviderScreeningAtr:
    class Router:
        def __init__(self, bars):
            self.bars, self.calls = bars, []

        def get_daily_bars(self, symbol, count=40):
            self.calls.append((symbol, count))
            return self.bars

    def test_uses_router_daily_bars_and_caches(self):
        router = self.Router(daily_bars(20))
        provider = ATRProvider(router)
        first = provider.get_screening_atr("US.TEST", asof=NOW)
        second = provider.get_screening_atr("US.TEST", asof=NOW)
        assert first["status"] == "PASS" and first["cached"] is False
        assert second["cached"] is True and len(router.calls) == 1

    def test_unknown_is_not_cached(self):
        """宁 UNKNOWN 不用旧值冒充 ⇒ 失败结果不进缓存 (下次仍重试)。"""
        router = self.Router([])
        provider = ATRProvider(router)
        assert provider.get_screening_atr("US.TEST", asof=NOW)["status"] == "UNKNOWN"
        assert provider.get_screening_atr("US.TEST", asof=NOW)["status"] == "UNKNOWN"
        assert len(router.calls) == 2

    def test_cache_key_separates_observation_day(self):
        router = self.Router(daily_bars(20))
        provider = ATRProvider(router)
        provider.get_screening_atr("US.TEST", asof=NOW)
        provider.get_screening_atr("US.TEST", asof=NOW + timedelta(days=1))
        assert len(router.calls) == 2

    def test_router_error_is_unknown_not_raise(self):
        class Boom:
            def get_daily_bars(self, symbol, count=40):
                raise RuntimeError("no feed")

        got = ATRProvider(Boom()).get_screening_atr("US.TEST", asof=NOW)
        assert got["status"] == "UNKNOWN" and "daily_bars_error" in got["reason"]

    def test_no_router_is_unknown(self):
        got = ATRProvider(None).get_screening_atr("US.TEST", asof=NOW)
        assert got["status"] == "UNKNOWN" and got["reason"] == "no_router"

    def test_injected_bars_bypass_router(self):
        provider = ATRProvider(None)
        assert provider.get_screening_atr("US.TEST", asof=NOW,
                                          bars=daily_bars(20))["status"] == "PASS"

    def test_screening_path_never_touches_stop_loss_atr(self):
        """口径分离钉: 筛选链不得调用止损口径 get_atr/get_atr_pct (VIX 缩放/三层底限/
        分钟兜底都挂在那条链上)。查字节码引用的名字, 不查源码文本 (docstring 会提名)。"""
        names = set(ATRProvider.get_screening_atr.__code__.co_names)
        for sub in ("get_screening_atr",):
            names |= set(getattr(ATRProvider, sub).__code__.co_names)
        assert not names & {"get_atr", "get_atr_pct", "get_atr_from_cache",
                            "_calc_atr_from_kline", "invalidate"}, sorted(names)
        assert "compute_screening_atr" in names

    def test_rejects_naive_asof(self):
        with pytest.raises(ValueError):
            ATRProvider(None).get_screening_atr("US.TEST", asof=datetime(2026, 9, 11))


# ══ 7. 借券门与方向隔离 ══════════════════════════════════════════════════
class TestBorrowIsolation:
    def test_short_not_shortable_rejects(self):
        result = evaluate_candidate(
            snap(direction_hint="SHORT", shortable=False),
            policy(borrow_gate_enabled=True))
        assert result["status"] == "REJECT" and "not_shortable" in result["reasons"]

    def test_long_on_same_symbol_unaffected(self):
        """SHORT 借券拒绝不得误杀同股 LONG 研究 (方向隔离)。"""
        result = evaluate_candidate(
            snap(direction_hint="LONG", shortable=False),
            policy(borrow_gate_enabled=True))
        assert result["status"] != "REJECT"
        assert "not_shortable" not in result["reasons"]

    def test_shortable_unknown_is_missing_not_borrowable(self):
        """借券未知 ≠ 可借: 记 missing (UNKNOWN), 不静默当可借放行成 PASS。"""
        result = evaluate_candidate(
            snap(direction_hint="SELL_SHORT", shortable=None),
            policy(borrow_gate_enabled=True))
        assert "shortable" in result["missing_fields"]
        assert result["status"] == "UNKNOWN"

    def test_shortable_true_passes(self):
        assert evaluate_candidate(
            snap(direction_hint="SHORT", shortable=True),
            policy(borrow_gate_enabled=True))["status"] != "REJECT"

    def test_gate_disabled_does_not_reject(self):
        assert evaluate_candidate(
            snap(direction_hint="SHORT", shortable=False),
            policy(borrow_gate_enabled=False))["status"] != "REJECT"


# ══ 盘口/报价时效 ════════════════════════════════════════════════════════
class TestQuoteFields:
    def test_missing_quote_age_is_unknown(self):
        result = evaluate_candidate(snap(quote_age_sec=None), policy())
        assert "source_asof" in result["missing_fields"]
        assert result["status"] == "UNKNOWN"

    def test_stale_quote_rejects(self):
        assert "stale_quote" in evaluate_candidate(
            snap(quote_age_sec=600), policy(max_quote_age_sec=90))["reasons"]

    def test_slightly_future_quote_tolerated_then_rejected(self):
        assert "future_quote" not in evaluate_candidate(
            snap(quote_age_sec=-3), policy())["reasons"]
        assert "future_quote" in evaluate_candidate(
            snap(quote_age_sec=-60), policy())["reasons"]

    def test_zero_spread_not_assumed_when_bid_ask_missing(self):
        """缺 bid/ask 不能按零点差计算 ⇒ 记 missing, 不是 PASS。"""
        result = evaluate_candidate(snap(bid=None, ask=None),
                                    policy(quote_gate_enabled=True))
        assert "bid_ask" in result["missing_fields"]
        assert result["status"] == "UNKNOWN"

    def test_crossed_and_wide_spread_reject(self):
        gate = policy(quote_gate_enabled=True, max_spread_ratio=0.05)
        assert "crossed_quote" in evaluate_candidate(
            snap(bid=10.2, ask=10.0), gate)["reasons"]
        assert "wide_spread" in evaluate_candidate(
            snap(bid=10.0, ask=11.0), gate)["reasons"]
        assert evaluate_candidate(snap(bid=10.0, ask=10.05), gate)["status"] != "REJECT"


# ══ 8. select_candidates: 先过滤后预算 + 补位 + 去重 ═════════════════════
def screened(status, symbol, **extra):
    row = {"symbol": symbol}
    row.update(extra)
    row["candidate_screening"] = {"symbol": symbol, "status": status,
                                  "reasons": [], "missing_fields": []}
    return row


class TestSelectCandidates:
    def test_rejected_does_not_consume_budget_and_backfills(self):
        """核心: 淘汰从剩余合格队列补位, 预算被填满 (修前 [:K] 先截断 ⇒ 少分析)。"""
        rows = [screened("REJECT", "US.AAA"), screened("PASS", "US.BBB"),
                screened("REJECT", "US.CCC"), screened("PASS", "US.DDD"),
                screened("PASS", "US.EEE"), screened("PASS", "US.FFF")]
        got = [r["symbol"] for r in select_candidates(rows, 3)]
        assert got == ["US.BBB", "US.DDD", "US.EEE"]

    def test_zero_budget_selects_nothing(self):
        """分析名额不是成交配额 ⇒ 允许零选取, 不硬凑。"""
        rows = [screened("PASS", "US.AAA")]
        assert select_candidates(rows, 0) == []
        assert rows[0]["selection_status"] == "budget_cut"

    def test_negative_and_bad_budget_clamped(self):
        rows = [screened("PASS", "US.AAA")]
        assert select_candidates(rows, -5) == []
        assert select_candidates([screened("PASS", "US.AAA")], "x") == []

    def test_duplicate_does_not_double_charge(self):
        rows = [screened("PASS", "US.AAA"), screened("PASS", "US.AAA"),
                screened("PASS", "US.BBB")]
        got = select_candidates(rows, 3)
        assert [r["symbol"] for r in got] == ["US.AAA", "US.BBB"]
        assert rows[1]["selection_status"] == "duplicate"

    def test_already_selected_excluded_across_stages(self):
        rows = [screened("PASS", "US.AAA"), screened("PASS", "US.BBB")]
        got = select_candidates(rows, 2, already_selected={"AAA"})
        assert [r["symbol"] for r in got] == ["US.BBB"]
        assert rows[0]["selection_status"] == "duplicate"

    def test_unknown_allowed_by_default_but_deferred_when_disabled(self):
        rows = [screened("UNKNOWN", "US.AAA"), screened("PASS", "US.BBB")]
        assert [r["symbol"] for r in select_candidates(rows, 2)] == ["US.AAA", "US.BBB"]
        rows2 = [screened("UNKNOWN", "US.AAA"), screened("PASS", "US.BBB")]
        got = select_candidates(rows2, 2, allow_unknown=False)
        assert [r["symbol"] for r in got] == ["US.BBB"]
        assert rows2[0]["selection_status"] == "unknown_deferred"

    def test_four_states_are_separate_not_one_skip(self):
        rows = [screened("REJECT", "US.AAA"), screened("PASS", "US.BBB"),
                screened("PASS", "US.BBB"), screened("UNKNOWN", "US.CCC"),
                screened("PASS", "US.DDD")]
        select_candidates(rows, 1, allow_unknown=False)
        assert [r["selection_status"] for r in rows] == [
            "rejected_ineligible", "selected", "duplicate",
            "unknown_deferred", "budget_cut"]

    def test_rank_written_on_selected_only(self):
        rows = [screened("REJECT", "US.AAA"), screened("PASS", "US.BBB"),
                screened("PASS", "US.CCC")]
        select_candidates(rows, 5)
        assert rows[1]["selection_rank"] == 1 and rows[2]["selection_rank"] == 2
        assert "selection_rank" not in rows[0]

    def test_stable_tie_break_by_input_order_then_symbol(self):
        rows = [screened("PASS", "US.CCC"), screened("PASS", "US.AAA"),
                screened("PASS", "US.BBB")]
        got = [r["symbol"] for r in select_candidates(rows, 3)]
        assert got == ["US.CCC", "US.AAA", "US.BBB"]            # 保持输入顺序
        ranked = [screened("PASS", s, score=1) for s in ("US.CCC", "US.AAA", "US.BBB")]
        got2 = [r["symbol"] for r in select_candidates(
            ranked, 3, sort_key=lambda r: -r["score"])]
        assert got2 == ["US.CCC", "US.AAA", "US.BBB"]           # 同键 → 原序

    def test_explicit_sort_key_reorders(self):
        """DA 空头方向强度 (10-score) 这类显式排序键必须生效。"""
        rows = [screened("PASS", "US.WEAK", conviction=2.0),
                screened("PASS", "US.STRONG", conviction=9.0)]
        got = select_candidates(rows, 1, sort_key=lambda r: -r["conviction"])
        assert [r["symbol"] for r in got] == ["US.STRONG"]

    def test_input_rows_are_annotated_in_place(self):
        rows = [screened("PASS", "US.AAA")]
        select_candidates(rows, 1)
        assert rows[0]["candidate_screening"]["selection_status"] == "selected"

    # ── defer_reason: 预算层的"本轮不可消费" (与资格层分开记) ──────────────
    def test_deferred_row_does_not_consume_budget_and_backfills(self):
        """无有效价格的候选资格上是 UNKNOWN (缺数≠拒绝), 但**成交预算**不能发给它:
        发出去就是空耗一名, 后面合格候选补不上位 (离线全链 2026-09-12 实测病灶)。"""
        rows = [screened("UNKNOWN", "US.NOPX"), screened("PASS", "US.BBB"),
                screened("PASS", "US.CCC")]
        got = select_candidates(rows, 2,
                                defer_reason=lambda r: "deferred_no_price"
                                if r["symbol"] == "US.NOPX" else None)
        assert [r["symbol"] for r in got] == ["US.BBB", "US.CCC"]
        assert rows[0]["selection_status"] == "deferred_no_price"
        assert rows[0]["candidate_screening"]["status"] == "UNKNOWN"   # 资格层不改判
        assert rows[0]["candidate_screening"]["selection_status"] == "deferred_no_price"

    def test_reject_takes_precedence_over_defer(self):
        """资格拒绝优先记 rejected_ineligible — 一个候选只有一个终态, 不叠标签。"""
        rows = [screened("REJECT", "US.BAD")]
        select_candidates(rows, 3, defer_reason=lambda r: "deferred_no_price")
        assert rows[0]["selection_status"] == "rejected_ineligible"

    def test_defer_predicate_runs_once_per_row(self):
        calls = []

        def spy(row):
            calls.append(row["symbol"])
            return None

        select_candidates([screened("PASS", "US.AAA"), screened("PASS", "US.BBB")], 1,
                          defer_reason=spy)
        assert calls == ["US.AAA", "US.BBB"]

    def test_defer_reason_default_is_noop(self):
        """不传 defer_reason ⇒ 行为逐字不变 (DA 侧既有调用零影响)。"""
        rows = [screened("UNKNOWN", "US.AAA"), screened("PASS", "US.BBB")]
        assert [r["symbol"] for r in select_candidates(rows, 2)] == ["US.AAA", "US.BBB"]

    def test_deferred_counted_separately_in_summary(self):
        rows = [screened("UNKNOWN", "US.NOPX"), screened("PASS", "US.BBB")]
        select_candidates(rows, 2, defer_reason=lambda r: "deferred_no_price"
                          if r["symbol"] == "US.NOPX" else None)
        summary = selection_summary(rows)
        assert summary["deferred_no_price"] == 1
        assert summary["selected"] == 1 and summary["total"] == 2

    def test_summary_counts_and_reason_codes(self):
        rows = [screened("REJECT", "US.AAA"), screened("PASS", "US.BBB")]
        rows[0]["candidate_screening"]["reasons"] = ["low_atr", "price_floor"]
        select_candidates(rows, 1)
        summary = selection_summary(rows)
        assert summary["total"] == 2 and summary["selected"] == 1
        assert summary["rejected_ineligible"] == 1
        assert summary["reason_codes"] == {"low_atr": 1, "price_floor": 1}


# ══ 10. screen_candidates: IO 与规则分离 ═════════════════════════════════
class FakeRouter:
    def __init__(self, quotes=None, bars=None):
        self.quotes, self.bars = quotes or {}, bars or {}
        self.quote_calls, self.bar_calls = [], []

    def get_batch_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {s: self.quotes.get(s, {}) for s in symbols}

    def get_daily_bars(self, symbol, count=40):
        self.bar_calls.append(symbol)
        return self.bars.get(symbol, [])


class TestScreenCandidates:
    QUOTES = {"US.AAA": {"price": 100.0, "source": "futu",
                         "source_asof": NOW.isoformat(),
                         "bid_price": 99.9, "ask_price": 100.1}}

    def test_pass_row_carries_full_audit_fields(self):
        router = FakeRouter(self.QUOTES, {"US.AAA": daily_bars(20)})
        rows = screen_candidates([{"symbol": "AAA"}], router, source="UE",
                                 batch_id="UE:b1", now=NOW, policy=policy())
        sc = rows[0]["candidate_screening"]
        for key in ("symbol", "price", "quote_source", "source_asof", "fetched_at",
                    "screening_atr_ratio", "atr_basis", "bars_asof", "status",
                    "reasons", "missing_fields", "policy_version", "input_hash",
                    "candidate_id", "batch_id", "source", "trade_mode", "observed_at"):
            assert key in sc, key
        assert sc["symbol"] == "US.AAA" and sc["source"] == "UE"
        assert sc["policy_version"] == POLICY_VERSION
        assert sc["candidate_id"] == "UE:b1:US.AAA:UNDECIDED"
        assert sc["status"] == "PASS"

    def test_caller_effective_price_wins(self):
        """资格判定必须用调用方的锚价, 否则"筛过的价"与"写进指令的价"分叉。"""
        router = FakeRouter(self.QUOTES, {"US.AAA": daily_bars(20)})
        rows = screen_candidates([{"symbol": "AAA", "effective_price": 4.0}],
                                 router, source="UE", batch_id="b", now=NOW,
                                 policy=policy())
        sc = rows[0]["candidate_screening"]
        assert sc["price"] == 4.0
        assert "price_floor" in sc["reasons"]
        assert "caller_effective" in str(sc["quote_source"])

    def test_supplied_quotes_skip_router(self):
        router = FakeRouter({}, {"US.AAA": daily_bars(20)})
        rows = screen_candidates([{"symbol": "AAA"}], router, source="UE", batch_id="b",
                                 now=NOW, quotes=self.QUOTES, policy=policy())
        assert router.quote_calls == []
        assert rows[0]["candidate_screening"]["price"] == 100.0

    def test_atr_provider_is_reused_not_rebuilt(self):
        """注入的 provider 必须被用 (与执行门同源同实例), 不自建第二条链。"""
        router = FakeRouter(self.QUOTES, {})
        provider = ATRProvider(FakeRouter({}, {"US.AAA": daily_bars(20)}))
        rows = screen_candidates([{"symbol": "AAA"}], router, source="UE", batch_id="b",
                                 now=NOW, atr_provider=provider, policy=policy())
        assert rows[0]["candidate_screening"]["status"] == "PASS"
        assert router.bar_calls == []

    def test_naive_now_rejected(self):
        with pytest.raises(ValueError):
            screen_candidates([{"symbol": "AAA"}], FakeRouter(), source="UE",
                              batch_id="b", now=datetime(2026, 9, 11, 8, 0))

    def test_router_quote_failure_is_unknown_not_crash(self):
        class Boom:
            def get_batch_quotes(self, symbols):
                raise RuntimeError("feed down")

            def get_daily_bars(self, symbol, count=40):
                return []

        rows = screen_candidates([{"symbol": "AAA"}], Boom(), source="UE",
                                 batch_id="b", now=NOW, policy=policy())
        assert rows[0]["candidate_screening"]["status"] == "UNKNOWN"

    def test_premarket_session_uses_pre_price(self):
        """04:00-09:30 ET 取 pre_price; 缺 pre_price 不回落昨收 (锚价口径不分叉)。"""
        quotes = {"US.AAA": {"price": 100.0, "pre_price": 92.0,
                             "source_asof": PRE.isoformat()}}
        rows = screen_candidates([{"symbol": "AAA"}], FakeRouter(quotes, {}),
                                 source="UE", batch_id="b", now=PRE, policy=policy())
        assert rows[0]["candidate_screening"]["price"] == 92.0
        empty = {"US.AAA": {"price": 100.0, "source_asof": PRE.isoformat()}}
        rows2 = screen_candidates([{"symbol": "AAA"}], FakeRouter(empty, {}),
                                  source="UE", batch_id="b", now=PRE, policy=policy())
        assert rows2[0]["candidate_screening"]["price"] is None

    def test_fetched_at_is_not_passed_off_as_source_asof(self):
        """行情 fetched_at 不能冒充 source_asof: 无源侧时间戳 ⇒ source_asof=None。"""
        quotes = {"US.AAA": {"price": 100.0, "timestamp": 1757587200}}
        sc = screen_candidates([{"symbol": "AAA"}], FakeRouter(quotes, {}),
                               source="UE", batch_id="b", now=NOW,
                               policy=policy())[0]["candidate_screening"]
        assert sc["source_asof"] is None
        assert "source_asof" in sc["missing_fields"]
        assert sc["fetched_at"]

    def test_batch_quote_dedups_symbols(self):
        router = FakeRouter(self.QUOTES, {})
        screen_candidates([{"symbol": "AAA"}, {"symbol": "AAA"}], router,
                          source="UE", batch_id="b", now=NOW, policy=policy())
        assert router.quote_calls == [["US.AAA"]]

    def test_short_direction_recorded_in_candidate_id(self):
        router = FakeRouter(self.QUOTES, {})
        sc = screen_candidates([{"symbol": "AAA", "direction": "SHORT"}], router,
                               source="UE", batch_id="b", now=NOW,
                               policy=policy())[0]["candidate_screening"]
        assert sc["direction_hint"] == "SHORT"
        assert sc["candidate_id"].endswith(":SHORT")

    def test_atr_reason_separates_gate_off_from_bad_shape(self):
        """atr_reason 说清"为什么没有筛选 ATR": 门关着 ≠ 标的无效 ≠ 取数失败。

        此前三种情况统一写 invalid_symbol —— 低ATR门关闭时连合法标的也被标成
        "标的无效", 审计上把配置状态误读成数据质量 (量纲/口径同源纪律)。
        """
        router = FakeRouter(self.QUOTES, {})
        off = screen_candidates([{"symbol": "AAA"}], router, source="UE", batch_id="b",
                                now=NOW, policy=policy(low_atr_enabled=False))[0]
        assert off["candidate_screening"]["atr_reason"] == "low_atr_gate_disabled"
        bad = screen_candidates([{"symbol": "AAA-WT"}], router, source="UE", batch_id="b",
                                now=NOW, policy=policy())[0]
        assert bad["candidate_screening"]["atr_reason"] == "symbol_shape_unsupported"


# ══ 11. 配置单一真源 + 漏斗载体 ══════════════════════════════════════════
class TestConfigAndFunnel:
    def test_policy_matches_live_yaml(self):
        """反分叉钉: 阈值必须与活 unified_config.yaml 同一取值, 不是本地常量。"""
        import yaml
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        with open(os.path.join(root, "config", "unified_config.yaml"),
                  encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        pb = (cfg.get("playbook") or {})
        exec_params = pb.get("execution_parameters") or {}
        admission = pb.get("admission") or {}
        got = load_candidate_policy()
        assert got["min_atr_ratio"] == pytest.approx(
            float(exec_params["low_atr_gate_min_atr_pct"]))
        assert got["low_atr_enabled"] is bool(exec_params["low_atr_gate_enabled"])
        assert got["max_scan_age_hours"] == pytest.approx(
            float(exec_params["candidate_scan_max_age_hours"]))
        assert got["min_price"] == pytest.approx(float(admission["min_entry_price"]))
        assert got["borrow_gate_enabled"] is bool(
            exec_params["borrow_pool_gate_enabled"])
        assert got["version"] == POLICY_VERSION

    def test_min_atr_ratio_is_fraction(self):
        """量纲声明: 比例 (0.03), 不是百分数 (3.0)。"""
        assert 0 < load_candidate_policy()["min_atr_ratio"] <= 1.0

    def test_batch_id_is_deterministic_and_source_scoped(self):
        a = make_batch_id("UE", NOW)
        assert a == make_batch_id("UE", NOW) and a != make_batch_id("DA", NOW)
        assert a.startswith("UE:")

    def test_record_candidate_batch_writes_jsonl(self, tmp_path):
        rows = [screened("PASS", "US.AAA"), screened("REJECT", "US.BBB")]
        rows[1]["candidate_screening"]["reasons"] = ["low_atr"]
        select_candidates(rows, 1)
        target = tmp_path / "shadow" / "candidate_funnel.jsonl"
        record_candidate_batch(rows, batch_id="UE:b1", source="UE",
                               phase="inject", path=target)
        lines = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        payload = lines[0]
        assert payload["type"] == "candidate_batch"
        assert payload["batch_id"] == "UE:b1" and payload["source"] == "UE"
        assert payload["phase"] == "inject"
        assert payload["policy_version"] == POLICY_VERSION
        assert [c["rank"] for c in payload["candidates"]] == [1, 2]
        assert [c["selection_status"] for c in payload["candidates"]] == [
            "selected", "rejected_ineligible"]

    def test_rows_without_screening_are_skipped(self, tmp_path):
        target = tmp_path / "funnel.jsonl"
        record_candidate_batch([{"symbol": "US.AAA"}], batch_id="b", source="UE",
                               phase="inject", path=target)
        assert json.loads(target.read_text(encoding="utf-8"))["candidates"] == []

    def test_funnel_path_is_redirected_to_sandbox(self):
        """conftest 缝: 单测期间 FUNNEL_PATH 必须指向沙箱, 不得是生产 data/shadow。"""
        from app.playbook import candidate_eligibility as ce
        assert "production_data_sandbox" in str(ce.FUNNEL_PATH), str(ce.FUNNEL_PATH)
        assert not str(ce.FUNNEL_PATH).endswith("data/shadow/candidate_funnel.jsonl")
