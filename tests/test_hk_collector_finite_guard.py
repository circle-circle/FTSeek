"""B1 (DEB-20261128-hk-collector-finite-guard): HK 采集器有限值入口守卫契约.

单变量 = 有限有效性 (非有限 → 缺失, 不归零, 不伪装有限值):
  - chg_pct: NaN/±inf 输入 → None (inf prev_close 不得伪算 -100%);
    极端有限输入比值溢出 → None; 有限值行为逐字不变;
  - _num: NaN/±inf → None (旧版只防 NaN 放 inf); bool 拒 / 有限值 / 零 / 负数不变;
  - aggregate_sector: 直接调用者的非有限成员视同缺失
    (不进 eq/median/dispersion/breadth/n_available/turnover);
  - build_tick_row: inf 报价 → last/chg/turnover 全 None, 篮子 n_available 排除,
    json.dumps 无 Infinity/NaN;
  - derive_day_doc: 非有限派生的 None 不进 rollup 序列;
  - 真实数据回归: 2026-11-27 周五 forced-verify 快照 derive_day_doc 幂等重算 == 存储 doc
    (真实数据全有限 → 结果不变; data/ gitignored, 快照缺失自动 skip).

病灶合成证据 (pre-fix, /tmp/rfc218_b1_pre_evidence.log):
  chg_pct(inf,100)=inf / chg_pct(100,inf)=-100.0 伪有限 / chg_pct(nan,100)=nan /
  aggregate(inf 成员) eq=Infinity breadth up=3 n_available=3 / tick json 含 Infinity.
"""
import json
from pathlib import Path

import pytest

import scripts.hk_market_context_collector as mod

INF = float("inf")
NAN = float("nan")
REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SNAPSHOT = REPO_ROOT / "data/shadow/hk_us_linkage/_verify/2026-11-27.json"


def _snap_row(code, last, prev=100.0, **kw):
    row = {"code": code, "last_price": last, "prev_close_price": prev,
           "high_price": kw.get("high", last), "low_price": kw.get("low", prev * 0.99),
           "turnover": kw.get("turnover", 1e6),
           "update_time": kw.get("ut", "2026-11-30 10:07:00"),
           "sec_status": kw.get("status", "NORMAL")}
    return row


class TestChgPctFiniteGuard:
    def test_inf_last_returns_none(self):
        assert mod.chg_pct(INF, 100.0) is None
        assert mod.chg_pct(-INF, 100.0) is None

    def test_inf_prev_close_returns_none_not_pseudo_minus100(self):
        # pre-fix: chg_pct(100, inf) = -100.0 (伪有限, 最危险形态)
        assert mod.chg_pct(100.0, INF) is None
        assert mod.chg_pct(100.0, -INF) is None

    def test_nan_input_returns_none(self):
        assert mod.chg_pct(NAN, 100.0) is None
        assert mod.chg_pct(100.0, NAN) is None

    @pytest.mark.parametrize("s", ["inf", "-inf", "nan"])
    def test_string_nonfinite_returns_none(self, s):
        assert mod.chg_pct(s, 100.0) is None
        assert mod.chg_pct(100.0, s) is None

    def test_finite_inputs_ratio_overflow_returns_none(self):
        # 两输入均有限且为正, 但比值溢出 inf → 结果不得输出 Infinity
        assert mod.chg_pct(1e308, 1e-308) is None

    def test_finite_values_unchanged(self):
        assert mod.chg_pct(110.0, 100.0) == 10.0
        assert mod.chg_pct(97.0, 100.0) == -3.0
        assert mod.chg_pct(103.456789, 100.0) == 3.4568  # round 4 位不变
        assert mod.chg_pct("110", "100") == 10.0

    @pytest.mark.parametrize("last,prev", [
        (None, 100.0), (100.0, None), (100.0, 0.0), (100.0, -1.0),
        (0.0, 100.0), (-5.0, 100.0), (True, 100.0), (100.0, False),
    ])
    def test_legacy_invalid_unchanged(self, last, prev):
        assert mod.chg_pct(last, prev) is None


class TestNumFiniteGuard:
    def test_inf_nan_returns_none(self):
        assert mod._num(INF) is None
        assert mod._num(-INF) is None
        assert mod._num(NAN) is None

    @pytest.mark.parametrize("s", ["inf", "-inf", "nan"])
    def test_string_nonfinite_returns_none(self, s):
        assert mod._num(s) is None

    def test_bool_rejected_unchanged(self):
        assert mod._num(True) is None
        assert mod._num(False) is None

    @pytest.mark.parametrize("x,expect", [
        (3.5, 3.5), ("2.5", 2.5), (0, 0.0), (-4, -4.0), (None, None), ("abc", None),
    ])
    def test_finite_and_legacy_unchanged(self, x, expect):
        assert mod._num(x) == expect


class TestAggregateFiniteGuard:
    def test_inf_member_marked_missing(self):
        rows = {"A": {"chg_pct": INF, "turnover": 9e6},
                "B": {"chg_pct": 2.0, "turnover": 1e6},
                "C": {"chg_pct": None, "turnover": 5e6}}
        agg = mod.aggregate_sector(rows)
        assert agg["chg_pct_eq"] == 2.0
        assert agg["n_available"] == 1
        assert agg["n_missing"] == 2
        assert agg["missing"] == ["A", "C"]
        assert agg["breadth"] == {"up": 1, "flat": 0, "down": 0}
        # inf 成员被 continue 跳过 → 其 turnover 亦不计入 (pre-fix 计入 4e6 类污染)
        assert agg["turnover_hkd"] == 1e6

    def test_nan_member_marked_missing(self):
        # pre-fix: nan 进 chgs → eq=nan, 且 nan 两个比较均 False → 误计 flat
        agg = mod.aggregate_sector({"A": {"chg_pct": NAN}, "B": {"chg_pct": 1.0}})
        assert agg["chg_pct_eq"] == 1.0
        assert agg["missing"] == ["A"]
        assert agg["breadth"] == {"up": 1, "flat": 0, "down": 0}

    def test_all_nonfinite_eq_none_not_zero(self):
        agg = mod.aggregate_sector({"A": {"chg_pct": INF, "turnover": 1e6},
                                    "B": {"chg_pct": NAN, "turnover": 1e6}})
        assert agg["chg_pct_eq"] is None
        assert agg["chg_pct_median"] is None
        assert agg["dispersion_pp"] is None
        assert agg["breadth"] == {"up": 0, "flat": 0, "down": 0}
        assert agg["n_available"] == 0
        assert agg["n_missing"] == 2
        assert agg["turnover_hkd"] is None

    def test_finite_regression_unchanged(self):
        agg = mod.aggregate_sector({"A": {"chg_pct": 2.0, "turnover": 1e6},
                                    "B": {"chg_pct": -1.0, "turnover": 2e6},
                                    "C": {"chg_pct": 0.0, "turnover": 3e6}})
        assert agg["chg_pct_eq"] == 0.3333
        assert agg["chg_pct_median"] == 0.0
        assert agg["dispersion_pp"] == 3.0
        assert agg["breadth"] == {"up": 1, "flat": 1, "down": 1}
        assert agg["n_available"] == 3
        assert agg["turnover_hkd"] == 6e6


class TestBuildTickFiniteGuard:
    def test_inf_quote_becomes_missing_everywhere(self):
        rows = {
            "HK.800000": _snap_row("HK.800000", INF, high=INF, turnover=INF),
            "HK.00981": _snap_row("HK.00981", INF),
            "HK.01347": _snap_row("HK.01347", 110.0),
            "HK.00522": _snap_row("HK.00522", 105.0),
        }
        tick = mod.build_tick_row("2026-11-30T10:07:00+08:00", "morning", rows)
        idx = tick["indices"]["HK.800000"]
        assert idx["last"] is None
        assert idx["chg_pct"] is None
        assert idx["high_chg_pct"] is None
        assert idx["low_chg_pct"] == -1.0  # 有限 low=99 (默认 prev*0.99) 仍正常
        assert idx["turnover"] is None
        stock = tick["stocks"]["HK.00981"]
        assert stock["last"] is None and stock["chg_pct"] is None
        semis = tick["sectors"]["semis"]
        assert semis["n_available"] == 2
        assert semis["missing"] == ["HK.00981"]
        assert semis["chg_pct_eq"] == 7.5  # (10+5)/2, inf 成员不参与

    def test_tick_json_no_infinity_nan(self):
        rows = {"HK.800000": _snap_row("HK.800000", INF, high=INF, turnover=INF),
                "HK.00981": _snap_row("HK.00981", NAN)}
        tick = mod.build_tick_row("2026-11-30T10:07:00+08:00", "morning", rows)
        s = json.dumps(tick)
        assert "Infinity" not in s and "NaN" not in s

    def test_finite_tick_unchanged(self):
        rows = {"HK.800000": _snap_row("HK.800000", 110.0, high=112.0, low=99.0),
                "HK.00981": _snap_row("HK.00981", 110.0)}
        tick = mod.build_tick_row("2026-11-30T10:07:00+08:00", "morning", rows)
        assert tick["indices"]["HK.800000"]["chg_pct"] == 10.0
        assert tick["indices"]["HK.800000"]["high_chg_pct"] == 12.0
        assert tick["stocks"]["HK.00981"]["chg_pct"] == 10.0


class TestDayDocFiniteGuard:
    def test_nonfinite_derived_none_excluded_from_rollup(self):
        t1 = mod.build_tick_row("2026-11-30T10:07:00+08:00", "morning",
                                {"HK.800000": _snap_row("HK.800000", 102.0),
                                 "HK.00981": _snap_row("HK.00981", 102.0)})
        t2 = mod.build_tick_row("2026-11-30T10:22:00+08:00", "morning",
                                {"HK.800000": _snap_row("HK.800000", INF),
                                 "HK.00981": _snap_row("HK.00981", INF)})
        doc = mod.derive_day_doc([t1, t2], "2026-11-30")
        idx_roll = doc["indices"]["HK.800000"]
        assert [p["chg_pct"] for p in idx_roll["series"]] == [2.0]
        assert idx_roll["latest_chg_pct"] == 2.0
        semis_roll = doc["sectors"]["semis"]
        assert [p["chg_pct"] for p in semis_roll["series"]] == [2.0]
        assert semis_roll["latest"]["chg_pct_eq"] is None  # 末 tick 全缺失如实呈现
        s = json.dumps(doc)
        assert "Infinity" not in s and "NaN" not in s

    @pytest.mark.skipif(not VERIFY_SNAPSHOT.exists(),
                        reason="真实快照不存在 (data/ gitignored, 新克隆无)")
    def test_friday_real_snapshot_idempotent_regression(self):
        stored = json.loads(VERIFY_SNAPSHOT.read_text(encoding="utf-8"))
        recomputed = mod.derive_day_doc(stored["tick_rows"], stored["date"])
        # 真实数据全有限 → 守卫不改变任何输出, 幂等重算逐字一致
        assert json.loads(json.dumps(recomputed)) == stored
