"""[OKR-2026-11 §12] KR1/KR2 判分器契约测试。

锁死四类东西:
  1) **判据常量与 OKR 原文同源** — 改常量 = 改判据 = 须先改 OKR + 用户裁, 故用源码字面量钉子;
  2) **fail-close** — 空分母 / 缺列 / 日数<2 一律 None, 绝不退化成 0 (0 与"没测到"是两个结论);
  3) **量纲** — bps=金额/名义额×1e4; `mfe_pct` 是百分数 (0.29 死 / 0.31 活, 不是分数);
     `realized_pnl`=毛 / `fees_est`=费 / `pnl_net`=净;
  4) **日历与 §12 裁决** — 感恩节 11-26 休市、11-27 半日市计入、**11-28 是周六**;
     §12.4 方案 B 只顺延 §6, **§5 (11-30) 不得被类推顺延** (§12.6 边界)。
"""
import json
from datetime import date
from pathlib import Path

import pytest

from scripts import okr_2026_11_scoring as scoring

REPO = Path(__file__).resolve().parents[1]
OKR_TEXT = (REPO / "docs" / "OKR-2026-11.md").read_text(encoding="utf-8")


def _row(symbol="US.TEST", trade_date="2026-11-03", decision_id="DA-20261103-x",
         entry=100.0, qty=10, gross=10.0, fees=4.0, net=None, mfe=1.0,
         exit_reason="SL_TRAIL_LOCK"):
    return {"symbol": symbol, "trade_date": trade_date, "decision_id": decision_id,
            "entry_price": entry, "quantity": qty, "realized_pnl": gross,
            "fees_est": fees, "pnl_net": (gross - fees) if net is None else net,
            "mfe_pct": mfe, "exit_reason": exit_reason, "exit_price": entry}


# --- 1) 判据常量与 OKR 原文同源 ---------------------------------------------


def test_criteria_constants_are_pinned_to_okr_text():
    assert scoring.COHORT_START == "2026-11-01"
    assert scoring.KR1_GATE_N == 120 and "n≥120" in OKR_TEXT
    assert scoring.KR1_NO_REGRESS_BPS == -2.25 and "-2.25bps" in OKR_TEXT
    assert scoring.KR2_GATE_DEAD_PCT == 23.0 and "≤23.0%" in OKR_TEXT
    assert scoring.A11_DEAD_MFE_PCT == 0.3
    assert scoring.SETTLEMENT_DATE == "2026-11-30"
    assert scoring.POST_WINDOW_START == "2026-11-17"


def test_pivot_ruling_records_option_b_and_no_threshold_change():
    """§12.4 用户裁决 = 方案 B; 顺延的只是判日与人群, 阈值必须标 False。"""
    assert scoring.PIVOT_RULE["ruled"] == "B"
    assert scoring.PIVOT_RULE["pivot_population"] == "post"
    assert scoring.PIVOT_RULE["threshold_changed"] is False
    assert scoring.PIVOT_RULE["settlement_date_deferred"] is False
    assert "方案 B" in OKR_TEXT and "✅ 方案 B（已裁）" in OKR_TEXT


def test_settlement_date_is_not_deferred_by_the_pivot_ruling():
    """§12.6 边界钉子: agent 不得把 §6 的顺延类推到 §5。"""
    block = scoring.collect_and_score([_row()], as_of="2026-11-17", era_where="AND 1=1")
    settlement = block["section12"]["settlement"]
    assert settlement["date"] == "2026-11-30"
    assert settlement["deferred"] is False
    assert "不得类推" in settlement["note"]


# --- 2) fail-close ----------------------------------------------------------


def test_empty_window_is_none_not_zero():
    stats = scoring.window_stats([], "空窗")
    assert stats["n"] == 0
    for key in ("notional", "gross", "fees", "net", "gross_bps", "fee_bps", "net_bps",
                "day_clustered_t", "mde80_bps", "ci80_lower_bps", "ci80_upper_bps"):
        assert stats[key] is None, f"{key} 空分母必须是 None, 不得写 0"


def test_single_day_window_has_no_t_or_mde():
    """日数 <2 ⇒ sd 不可算 ⇒ t/MDE/CI 全 None (不得用 0 或 inf 冒充)。"""
    rows = [_row(trade_date="2026-11-03"), _row(symbol="US.B", trade_date="2026-11-03")]
    stats = scoring.window_stats(rows, "单日")
    assert stats["n"] == 2 and stats["n_days"] == 1
    assert stats["net_bps"] is not None, "点估计仍可算 (不依赖日聚类)"
    assert stats["day_clustered_t"] is None
    assert stats["mde80_bps"] is None
    assert stats["ci80_contains_zero"] is None


def test_kr1_empty_is_not_measurable():
    verdict = scoring.kr1_verdict(scoring.window_stats([], "空"))
    assert verdict["verdict"] == "NOT_MEASURABLE"
    assert verdict["gate_met"] is None
    assert "不判 0 也不判达标" in verdict["note"]


def test_kr2_missing_mfe_is_excluded_from_denominator_not_counted_as_zero():
    """缺 mfe_pct 的行不得当 0 (0 < 0.3 会被误判成死单)。"""
    rows = [_row(mfe=0.1), _row(symbol="US.B", mfe=None), _row(symbol="US.C", mfe=2.0)]
    out = scoring.kr2_dead_rate(rows)
    assert out["n"] == 3 and out["n_with_mfe"] == 2 and out["mfe_missing"] == 1
    assert out["dead_n"] == 1
    assert out["dead_pct"] == pytest.approx(50.0), "分母=有 mfe 的 2 行, 不是 3 行"


def test_kr2_all_missing_is_not_measurable():
    out = scoring.kr2_dead_rate([_row(mfe=None)])
    assert out["verdict"] == "NOT_MEASURABLE"
    assert out["dead_pct"] is None


def test_kr2_net_per_trade_fail_closes_on_unknown_net():
    rows = [_row(mfe=0.1, gross=10.0, fees=4.0),
            _row(symbol="US.B", mfe=2.0, gross=10.0, fees=4.0)]
    rows[1]["fees_est"] = None
    rows[1]["pnl_net"] = None
    out = scoring.kr2_dead_rate(rows)
    assert out["dead_net_per_trade"] == pytest.approx(6.0)
    assert out["alive_net_per_trade"] is None
    assert out["alive_net_bps"] is None


def test_sum_net_and_exit_breakdown_fail_close_on_unknown_net():
    rows = [_row(exit_reason="KNOWN", gross=10.0, fees=4.0),
            _row(symbol="US.B", exit_reason="UNKNOWN", gross=10.0, fees=4.0)]
    rows[1]["fees_est"] = None
    rows[1]["pnl_net"] = None
    assert scoring._sum_net(rows) is None
    breakdown = scoring.exit_reason_breakdown(rows)
    assert [item["exit_reason"] for item in breakdown] == ["KNOWN", "UNKNOWN"]
    assert breakdown[0]["net"] == pytest.approx(6.0)
    assert breakdown[1]["net"] is None
    assert breakdown[1]["net_per_trade"] is None
    assert breakdown[1]["net_bps"] is None


# --- 3) 量纲与算术 ----------------------------------------------------------


def test_bps_is_amount_over_notional_times_1e4():
    rows = [_row(entry=100.0, qty=10, gross=10.0, fees=4.0)]   # 名义 1000, 毛 10, 费 4, 净 6
    stats = scoring.window_stats(rows, "单行")
    assert stats["notional"] == pytest.approx(1000.0)
    assert stats["gross_bps"] == pytest.approx(100.0)     # 10/1000*1e4
    assert stats["fee_bps"] == pytest.approx(40.0)
    assert stats["net_bps"] == pytest.approx(60.0)
    assert stats["net_per_trade"] == pytest.approx(6.0)


def test_pnl_net_falls_back_to_gross_minus_fees():
    row = _row(gross=10.0, fees=4.0)
    row["pnl_net"] = None
    stats = scoring.window_stats([row], "回落")
    assert stats["net"] == pytest.approx(6.0)
    assert stats["net_bps"] == pytest.approx(60.0)


def test_missing_fee_and_net_are_not_measurable_not_zero():
    row = _row(gross=10.0, fees=4.0)
    row["fees_est"] = None
    row["pnl_net"] = None
    stats = scoring.window_stats([row], "缺费缺净")
    assert stats["gross"] == pytest.approx(10.0)
    assert stats["gross_bps"] == pytest.approx(100.0)
    assert stats["fees"] is None and stats["fee_bps"] is None
    assert stats["net"] is None and stats["net_bps"] is None
    assert stats["net_per_trade"] is None
    assert stats["gross_missing_n"] == 0
    assert stats["fee_missing_n"] == 1 and stats["net_missing_n"] == 1
    assert stats["ci80_lower_bps"] is None and stats["ci80_upper_bps"] is None
    verdict = scoring.kr1_verdict(stats)
    assert verdict["verdict"] == "NOT_MEASURABLE"
    block = scoring.collect_and_score([row], [row], [row], as_of="2026-11-17",
                                      era_where="AND 1=1")
    assert any("fees_est 缺失" in warning for warning in block["warnings"])
    assert any("净额缺失" in warning for warning in block["warnings"])


def test_missing_fee_with_known_net_keeps_net_measurable():
    row = _row(gross=10.0, fees=4.0, net=6.0)
    row["fees_est"] = None
    stats = scoring.window_stats([row], "缺费有净")
    assert stats["fees"] is None and stats["fee_bps"] is None
    assert stats["net"] == pytest.approx(6.0)
    assert stats["net_bps"] == pytest.approx(60.0)
    assert stats["fee_missing_n"] == 1 and stats["net_missing_n"] == 0


def test_missing_gross_with_known_net_keeps_net_measurable():
    row = _row(gross=10.0, fees=4.0, net=6.0)
    row["realized_pnl"] = None
    stats = scoring.window_stats([row], "缺毛有净")
    assert stats["gross"] is None and stats["gross_bps"] is None
    assert stats["net"] == pytest.approx(6.0)
    assert stats["net_bps"] == pytest.approx(60.0)
    assert stats["gross_missing_n"] == 1 and stats["net_missing_n"] == 0


def test_mfe_threshold_is_percentage_not_fraction():
    """A11: 0.29 = 0.29% ⇒ 死单; 0.31 ⇒ 活单。若误当分数口径, 两者都会判死。"""
    out = scoring.kr2_dead_rate([_row(mfe=0.29), _row(symbol="US.B", mfe=0.31)])
    assert out["dead_n"] == 1 and out["dead_pct"] == pytest.approx(50.0)
    assert "百分数" in out["caliber"]


def test_kr2_does_not_read_alternative_mfe_calibers():
    """A11: tick/闸门/决策时刻 peak 三口径仅并列披露、不参与判定 ⇒ 判分器连读都不读。"""
    row = _row(mfe=5.0)
    row["mfe_tick_pct"] = 0.1        # 若被误读会变成死单
    row["mfe_gate_pct"] = 0.1
    out = scoring.kr2_dead_rate([row])
    assert out["dead_n"] == 0


def test_day_clustered_t_and_mde_on_known_fixture():
    """两日净额 +60/+120 (名义各 1000) ⇒ 日 bps 序列 [60,120]: mean 90, sd 42.43。"""
    rows = [_row(trade_date="2026-11-03", gross=10.0, fees=4.0),
            _row(symbol="US.B", trade_date="2026-11-04", gross=16.0, fees=4.0)]
    stats = scoring.window_stats(rows, "两日")
    assert stats["n_days"] == 2
    assert stats["day_clustered_mean_bps"] == pytest.approx(90.0)
    assert stats["day_clustered_sd_bps"] == pytest.approx(42.4264, abs=1e-3)
    se = 42.4264 / 2 ** 0.5
    assert stats["day_clustered_t"] == pytest.approx(90.0 / se, abs=1e-3)
    assert stats["mde80_bps"] == pytest.approx(2.8016 * se, abs=1e-2)
    assert stats["ci80_lower_bps"] == pytest.approx(90.0 - 1.2816 * se, abs=1e-2)
    assert stats["ci80_upper_bps"] == pytest.approx(90.0 + 1.2816 * se, abs=1e-2)
    assert stats["ci80_contains_zero"] is False


def test_ci_contains_zero_needs_both_bounds():
    """只用下界会把"点估计为负且上界>0"误判成不含 0 — 必须两端都算。"""
    rows = [_row(trade_date="2026-11-03", gross=-2.0, fees=4.0),
            _row(symbol="US.B", trade_date="2026-11-04", gross=8.0, fees=4.0)]
    stats = scoring.window_stats(rows, "跨零")
    assert stats["ci80_lower_bps"] < 0 < stats["ci80_upper_bps"]
    assert stats["ci80_contains_zero"] is True


# --- 4) KR1 判定分支 --------------------------------------------------------


def _stats(n, net_bps, mde=None, ci_lower=None, ci_upper=None, days=5):
    return {"n": n, "net_bps": net_bps, "net": None, "notional": 1.0,
            "mde80_bps": mde, "ci80_lower_bps": ci_lower, "ci80_upper_bps": ci_upper,
            # 与 window_stats 同语义: 两端任一不可算 ⇒ None (不是 False)
            "ci80_contains_zero": (None if (ci_lower is None or ci_upper is None)
                                   else bool(ci_lower <= 0 <= ci_upper)),
            "day_clustered_t": None, "n_days": days}


def test_kr1_under_120_is_underpowered_regardless_of_sign():
    v = scoring.kr1_verdict(_stats(109, -18.52, mde=31.15))
    assert v["verdict"] == "UNDERPOWERED" and v["gate_met"] is False
    assert "功效不足" in v["note"] and "不作达标/证伪用" in v["note"]
    v2 = scoring.kr1_verdict(_stats(119, +50.0, mde=31.0))
    assert v2["verdict"] == "UNDERPOWERED", "正数也不得在 n<120 时判达标"


def test_kr1_met_point_estimate_with_ci_containing_zero():
    v = scoring.kr1_verdict(_stats(130, 5.0, mde=20.0, ci_lower=-8.0, ci_upper=18.0))
    assert v["verdict"] == "MET_POINT_ESTIMATE"
    assert "证据不足" in v["note"] and "不得宣称稳定 edge" in v["note"]


def test_kr1_met_with_ci_lower_above_zero():
    v = scoring.kr1_verdict(_stats(130, 12.0, mde=8.0, ci_lower=3.0, ci_upper=21.0))
    assert v["verdict"] == "MET_POINT_ESTIMATE" and "CI 下界 >0" in v["note"]
    assert "CI=[3.0, 21.0]" in v["note"] and "日等权" in v["note"]


def test_kr1_negative_day_ci_does_not_claim_lower_above_zero():
    v = scoring.kr1_verdict(_stats(130, 5.0, mde=20.0, ci_lower=-10.0, ci_upper=-1.0))
    assert v["verdict"] == "MET_POINT_ESTIMATE"
    assert "CI 下界 >0" not in v["note"]
    assert "整体 <0" in v["note"] and "日等权" in v["note"]
    assert "不得宣称稳定 edge" in v["note"]


def test_kr1_ci_unknown_is_not_reported_as_lower_above_zero():
    v = scoring.kr1_verdict(_stats(130, 12.0, mde=None, ci_lower=None, ci_upper=None, days=1))
    assert "不可算" in v["note"] and "CI 下界 >0" not in v["note"]


def test_kr1_not_met_when_gate_reached_and_nonpositive():
    v = scoring.kr1_verdict(_stats(140, -3.0, mde=10.0, ci_lower=-9.0, ci_upper=3.0))
    assert v["verdict"] == "NOT_MET" and v["point_estimate_positive"] is False


def test_kr1_below_no_regress_line_but_within_mde_is_indistinguishable():
    v = scoring.kr1_verdict(_stats(109, -18.52, mde=31.15))
    assert v["below_no_regress_line"] is True
    assert "低于不回归线" in v["note"] and "不可分辨" in v["note"]


def test_kr1_below_no_regress_line_beyond_mde_is_flagged_without_excuse():
    v = scoring.kr1_verdict(_stats(200, -40.0, mde=10.0))
    assert "低于不回归线" in v["note"] and "不可分辨" not in v["note"]


# --- 5) 日历 (曾按工作日粗估算错过) -----------------------------------------


def test_thanksgiving_and_weekend_excluded_half_day_included():
    days = scoring.trading_days("2026-11-18", "2026-11-28")
    assert [d.isoformat() for d in days] == [
        "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23",
        "2026-11-24", "2026-11-25", "2026-11-27"]
    assert len(days) == 7, "11-26 感恩节休市、11-28 周六 ⇒ 只有 7 个交易日 (粗估会得 9)"
    assert date(2026, 11, 27) in days, "半日市仍有成交, 必须计入"
    assert date(2026, 11, 26) not in days
    assert date(2026, 11, 28).weekday() == 5


def test_pivot_original_date_is_not_a_trading_day():
    """§12.2 实证: §6 原判日 11-28 本身是周六。"""
    assert not scoring.is_trading_day(date(2026, 11, 28))


def test_project_gate_date_returns_none_without_rate():
    assert scoring.project_gate_date(None, 115, "2026-11-18") is None
    assert scoring.project_gate_date(0, 115, "2026-11-18") is None


def test_projection_uses_both_rates_and_plans_on_the_later_end():
    """post 速率在切换初期分母仅 1~2 天 ⇒ 偏低; 必须与整窗速率并列并报区间。

    复刻 11-17 实盘形态: pre 窗密集 (100 笔/11 个交易日) + post 窗只有 1 天 5 笔
    ⇒ 整窗速率 8.75 > post 速率 5.0 (实测生产值 9.08 vs 5.0)。
    """
    pre_dates = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
                 "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
                 "2026-11-16"]
    rows = [_row(trade_date="2026-11-17", symbol=f"US.P{i}", decision_id=f"DA-p{i}")
            for i in range(5)]
    rows += [_row(trade_date=pre_dates[i % len(pre_dates)], symbol=f"US.W{i}",
                  decision_id=f"DA-w{i}") for i in range(100)]
    out = scoring.three_window_disclosure(rows, as_of="2026-11-17")
    projection = out["post_projection"]
    assert projection["post_trading_days_elapsed"] == 1
    assert projection["whole_trading_days_elapsed"] == 12
    assert projection["post_daily_rate"] == pytest.approx(5.0)
    assert projection["whole_daily_rate"] == pytest.approx(105 / 12)
    assert projection["whole_daily_rate"] > projection["post_daily_rate"]
    assert projection["post_n_needed_for_gate"] == 115
    lo, hi = projection["gate_date_range"]
    assert lo == projection["gate_date_by_whole_rate"]
    assert hi == projection["gate_date_by_post_rate"]
    assert lo < hi
    assert projection["gate_date_planning"] == hi, "规划取较晚端 (宁晚判不早判)"


# --- 6) 分源与汇总 ----------------------------------------------------------


@pytest.mark.parametrize("decision_id,expected", [
    ("DA-20261117213252-US.BWXT-c280", "DA"),
    ("EXT-20261117-US.OXY", "EXT"),
    ("MOM-20261117", "MOM"),
    ("universe_expansion_20261005", "UE"),
    ("", "OTHER"), (None, "OTHER"), ("something_else", "OTHER"),
])
def test_classify_source(decision_id, expected):
    assert scoring.classify_source(decision_id) == expected


def test_kr1_only_scores_da_and_reports_other_sources_side_by_side():
    rows = ([_row(decision_id=f"DA-x{i}", trade_date=f"2026-11-{d:02d}")
             for i, d in enumerate(range(2, 12))]
            + [_row(decision_id="EXT-y", symbol="US.EXT", trade_date="2026-11-03",
                    gross=-50.0, fees=4.0)])
    block = scoring.collect_and_score(rows, rows, rows, as_of="2026-11-17",
                                      era_where="AND trade_date >= '2026-07-01'")
    assert block["kr1"]["primary"]["n"] == 10, "KR1 主口径只算 DA"
    assert "EXT" in block["kr1"]["by_source"]
    assert block["warnings"] == [], "A9/A10 双口径都给了 ⇒ 不应有 KR5 WARNING"


def test_missing_dual_caliber_raises_kr5_warning():
    block = scoring.collect_and_score([_row()], None, None, as_of="2026-11-17",
                                      era_where="")
    assert any("A9/A10 双口径未并列" in w for w in block["warnings"])
    assert any("era_clean_where" in w for w in block["warnings"])


def test_disclosure_separates_estimators_as_of_and_fee_coverage():
    rows = [_row(trade_date="2026-11-03", qty=100, gross=13.0, fees=4.0),
            _row(symbol="US.B", trade_date="2026-11-04", entry=10.0, qty=10,
                 gross=3.0, fees=4.0)]
    block = scoring.collect_and_score(rows, rows, rows, as_of="2026-11-20",
                                      era_where="AND 1=1")
    caliber = block["caliber"]
    assert "不设 trade_date 上界" in caliber["sample_scope"]
    assert "不是历史快照" in caliber["sample_scope"]
    assert caliber["net_bps_estimator"] == "10000 × Σ净额 / Σ入场名义额 (名义加权)"
    assert "日等权" in caliber["day_cluster_estimator"]
    assert "不是名义加权净 bps 的严格区间" in caliber["day_cluster_estimator"]
    assert "估算" in caliber["fee_coverage"]
    assert "不得重复扣" in caliber["fee_coverage"]
    primary = block["kr1"]["primary"]
    assert primary["net_bps"] == pytest.approx(7.9208)
    assert primary["day_clustered_mean_bps"] == pytest.approx(-45.5)
    assert primary["ci80_lower_bps"] == pytest.approx(-115.3472)
    assert primary["ci80_upper_bps"] == pytest.approx(24.3472)
    assert primary["ci80_contains_zero"] is True


def test_a9_a10_exclusion_counts_are_reported():
    rows = [_row(), _row(symbol="US.B")]
    block = scoring.collect_and_score(rows, rows + [_row(symbol="US.C")],
                                      rows + [_row(symbol="US.D"), _row(symbol="US.E")],
                                      as_of="2026-11-17", era_where="AND 1=1")
    assert block["caliber"]["a9_excluded_n"] == 1
    assert block["caliber"]["a10_excluded_n"] == 2


def test_post_window_underpowered_blocks_pivot_trigger():
    """§12.5: post n<120 ⇒ 即使点估计为负也**不触发** Kill/Pivot, 只判功效不足 + 顺延。"""
    rows = [_row(trade_date="2026-11-17", gross=-30.0, fees=4.0,
                 decision_id=f"DA-z{i}") for i in range(5)]
    block = scoring.collect_and_score(rows, rows, rows, as_of="2026-11-17",
                                      era_where="AND 1=1")
    pivot = block["section12"]["pivot_evaluation"]
    assert pivot["population"] == "post"
    assert pivot["post_n"] == 5 and pivot["post_gate_met"] is False
    assert pivot["triggered"] is False, "n<120 不得触发 Kill/Pivot"
    assert pivot["deferred_to"] is not None
    assert "功效不足" in pivot["note"] and "一字未动" in pivot["note"]


@pytest.mark.parametrize("daily_nets,expected_net,expected_ci,expected_trigger", [
    ([6, -16], -50.0, True, True),
    ([-10, -20], -150.0, False, False),
    ([-10], -100.0, None, False),
    ([6, 16], 110.0, False, False),
])
def test_post_pivot_requires_nonpositive_net_and_ci_containing_zero(
        daily_nets, expected_net, expected_ci, expected_trigger):
    rows = []
    for day_index, net in enumerate(daily_nets):
        for row_index in range(120 // len(daily_nets)):
            rows.append(_row(symbol=f"US.P{day_index}.{row_index}",
                             decision_id=f"DA-pivot-{day_index}-{row_index}",
                             trade_date=f"2026-11-{17 + day_index:02d}",
                             gross=net + 4.0, fees=4.0))
    block = scoring.collect_and_score(rows, rows, rows, as_of="2026-11-20",
                                      era_where="AND 1=1")
    pivot = block["section12"]["pivot_evaluation"]
    assert pivot["post_n"] == 120 and pivot["post_gate_met"] is True
    assert pivot["net_bps"] == expected_net
    assert pivot["ci_contains_zero"] is expected_ci
    assert pivot["triggered"] is expected_trigger
    assert "不自动停单" in pivot["note"]


def test_render_lines_includes_all_three_windows_and_pivot():
    rows = [_row(trade_date=f"2026-11-{d:02d}", decision_id=f"DA-r{i}",
                 symbol=f"US.S{i}") for i, d in enumerate(range(2, 14))]
    block = scoring.collect_and_score(rows, rows, rows, as_of="2026-11-17",
                                      era_where="AND 1=1")
    text = "\n".join(scoring.render_lines(block))
    for token in ("OKR-2026-11", "KR1 DA", "KR2 DA 死单率", "§12.3 三窗并列",
                  "whole", "pre", "post", "§6 Kill/Pivot", "§5 结算日",
                  "达标日双速率外推", "不设 trade_date 上界", "日等权",
                  "不是名义加权净 bps 的严格区间", "不得重复扣"):
        assert token in text, f"缺 {token}"


def test_render_lines_survives_broken_block():
    assert "判分不可用" in scoring.render_lines({})[0]
    assert "判分不可用" in scoring.render_lines(None)[0]


def test_module_is_pure_no_db_or_disk_writes_on_import(monkeypatch):
    """纯函数模块钉子: import 不得跑 SQL / 不得写盘 (与 okr_status.py 的模块级执行相反)。

    行为验证而非源码字面量匹配 — 重新 import 本模块, 期间 sqlite3.connect 与写盘一律炸;
    不炸即证明模块级零副作用, 判分逻辑可被安全 import 进契约测试。
    """
    import importlib
    import sqlite3

    def _boom_connect(*_args, **_kwargs):
        raise AssertionError("import 期间不得连库")

    monkeypatch.setattr(sqlite3, "connect", _boom_connect)
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("import 期间不得写盘")))
    reloaded = importlib.reload(scoring)
    assert reloaded.COHORT_START == "2026-11-01"
    importlib.reload(scoring)   # 还原, 免得污染同会话其它测试


def test_cli_entry_is_guarded_by_main():
    src = (REPO / "scripts" / "okr_2026_11_scoring.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in src


def test_schema_and_doc_are_versioned():
    block = scoring.collect_and_score([_row()], [_row()], [_row()], as_of="2026-11-17",
                                      era_where="AND 1=1")
    assert block["schema"] == "okr_2026_11_scoring_v1"
    assert block["okr_doc"] == "docs/OKR-2026-11.md"
    assert json.dumps(block, default=str), "block 必须可 JSON 序列化"
