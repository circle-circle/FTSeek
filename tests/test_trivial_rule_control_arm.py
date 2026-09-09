"""笨规则对照臂契约测试 (2026-09-07, DEB-20260907 建议③).

锁的是**度量纪律**, 不是某窗口的读数:
  1. 配对结构: 两臂共用同一收益, 只差方向 ⇒ 离群裁剪必须同时作用于两臂
  2. SHORT 基准与 signal_ic_tracker 同式 ((t0/tn)−1), 不是 −long_basis
  3. 主指标是按日聚类的配对差 + MDE@80% (宪章七律 #9), 且必须自报"无法分辨"
  4. 每条臂都声明信息集与"两链决策时是否已知" — 缺声明的臂不得存在
  5. 方向无定义 (sign=0) 的行从配对剔除, 不得记 0 收益 (会把差值拉向 0)
  6. 只读: 快照库 mode=ro, 日K只读缓存零拉取, 输出落 data/shadow/
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts import trivial_rule_control_arm as C


_MISSING = object()


def _row(date="2026-09-03", chain="deep_analysis", symbol="US.TEST", llm_sign=1,
         raw_t1=0.01, raw_t5=0.02, llm_ret=_MISSING, llm_ret_t5=_MISSING,
         prev=None, gap=None, t0=None, alpha=0.005):
    return {
        "date": date, "chain": chain, "symbol": symbol, "llm_sign": llm_sign,
        "raw_ret_t1": raw_t1, "raw_ret_t5": raw_t5,
        "llm_ret": (llm_sign * raw_t1) if llm_ret is _MISSING else llm_ret,
        "llm_ret_t5": (llm_sign * raw_t5) if llm_ret_t5 is _MISSING else llm_ret_t5,
        "alpha_t1": alpha, "alpha_t5": alpha,
        "prev_day_ret": prev, "overnight_gap": gap, "t0_intraday_ret": t0,
    }


# ── 1. 符号与收益基准 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("x,expected", [(0.01, 1), (-0.01, -1), (0.0, 0), (None, 0),
                                        ("bad", 0), (0.0, 0)])
def test_sign_of(x, expected):
    assert C.sign_of(x) == expected


def test_long_and_short_basis_are_not_simply_negated():
    """SHORT = (t0/tn) − 1, 与 −long_basis 不同 (库内 SSOT 口径, 不可自创)。"""
    lng = C.long_basis_ret(100.0, 90.0)
    sht = C.short_basis_ret(100.0, 90.0)
    assert lng == pytest.approx(-0.10)
    assert sht == pytest.approx(100.0 / 90.0 - 1.0)
    assert sht != pytest.approx(-lng)


@pytest.mark.parametrize("t0,tn", [(0, 100), (100, 0), (None, 100), (100, None), (-1, 100)])
def test_basis_ret_rejects_bad_prices(t0, tn):
    assert C.long_basis_ret(t0, tn) is None
    assert C.short_basis_ret(t0, tn) is None


# ── 2. 离群策略作用于两臂 (配对不被破坏) ──────────────────────────────────

def test_policy_raw_is_identity():
    rows = [_row(raw_t1=3.0, llm_ret=3.0), _row(raw_t1=-3.0, llm_ret=-3.0)]
    assert C.apply_policy(rows, "raw") == rows


def test_policy_abs_filter_drops_both_arms_together():
    rows = [_row(llm_ret=0.01), _row(llm_ret=0.60), _row(llm_ret=-0.55)]
    out = C.apply_policy(rows, "abs_lt_0_5")
    assert len(out) == 1 and out[0]["llm_ret"] == pytest.approx(0.01)


def test_policy_winsor_clips_arm_ret_too():
    """关键契约: winsor 必须同时裁 arm_ret, 否则两臂被不同规则裁剪 ⇒ 差值失去配对含义。"""
    rows = [_row(llm_ret=0.01) for _ in range(100)]
    rows.append(_row(llm_ret=4.0))
    rows[-1]["arm_ret"] = -4.0                            # 同一行的对照臂是反向大值
    out = C.apply_policy(rows, "winsor_1_99")
    extreme = out[-1]
    assert extreme["llm_ret"] < 4.0
    assert extreme["arm_ret"] > -4.0                      # 臂侧同样被裁
    assert abs(extreme["llm_ret"]) == pytest.approx(abs(extreme["arm_ret"]))


def test_policy_excl_top3_drops_three_largest_abs():
    rows = [_row(llm_ret=0.001 * i) for i in range(1, 11)]
    rows += [_row(llm_ret=1.0), _row(llm_ret=-2.0), _row(llm_ret=3.0)]
    out = C.apply_policy(rows, "excl_top3")
    assert len(out) == 10
    assert all(abs(r["llm_ret"]) < 1.0 for r in out)


def test_policy_excl_top3_needs_more_than_three_rows():
    assert C.apply_policy([_row(llm_ret=1.0), _row(llm_ret=2.0)], "excl_top3") == []


def test_unknown_policy_raises():
    with pytest.raises(ValueError):
        C.apply_policy([_row()], "nope")


def test_clip_row_does_not_mutate_input():
    r = {"llm_ret": 5.0, "arm_ret": -5.0, "date": "2026-09-03"}
    out = C.clip_row(r, -1.0, 1.0)
    assert r["llm_ret"] == 5.0 and r["arm_ret"] == -5.0
    assert out["llm_ret"] == 1.0 and out["arm_ret"] == -1.0
    assert out["date"] == r["date"]


# ── 3. 按日聚类统计 ───────────────────────────────────────────────────────

def test_daily_cluster_stats_hand_computable_case():
    pairs = [("d1", 0.02, 0.01), ("d1", 0.00, 0.01), ("d2", -0.01, 0.01), ("d3", 0.03, -0.01)]
    st = C.daily_cluster_stats(pairs, n_perm=0)
    # 日差: d1 = 0.01−0.01 = 0; d2 = −0.01−0.01 = −0.02; d3 = 0.03−(−0.01) = +0.04
    assert st["n_rows"] == 4 and st["n_days"] == 3
    assert st["daily_mean_diff"] == pytest.approx(0.02 / 3, abs=1e-6)
    assert st["daily_sd_diff"] == pytest.approx(0.0305505, abs=1e-6)
    assert st["t_daily"] == pytest.approx(0.378, abs=0.01)
    assert st["days_positive"] == 1
    assert st["mde80"] == pytest.approx(2.8 * 0.0305505 / 3 ** 0.5, abs=1e-6)
    assert st["resolvable"] is False                      # |mu| << MDE ⇒ 必须自报无法分辨
    assert st["n_lt_30_days_flag"] is True
    # pooled 与 clustered 必须同时给出 (pooled 会被日内相关夸大)
    assert st["pooled_mean_diff"] == pytest.approx(0.005, abs=1e-6)
    assert st["perm_p_two_sided"] is None                 # n_perm=0 ⇒ 不出 p


def test_daily_cluster_stats_resolvable_flag_can_be_true():
    """效应远大于日间噪声时必须判为可分辨 (否则门永远关着 = 纪律变瘫痪)。"""
    pairs = [(f"d{i}", 0.05, 0.0) for i in range(40)]
    st = C.daily_cluster_stats(pairs, n_perm=200)
    assert st["daily_mean_diff"] == pytest.approx(0.05)
    assert st["daily_sd_diff"] == pytest.approx(0.0, abs=1e-9)
    assert st["mde80"] == pytest.approx(0.0, abs=1e-9)
    assert st["resolvable"] is True
    assert st["n_lt_30_days_flag"] is False


def test_daily_cluster_stats_empty():
    assert C.daily_cluster_stats([])["n_days"] == 0


def test_sign_flip_perm_p_is_seeded_and_bounded():
    diffs = [0.01, -0.02, 0.03, -0.01, 0.02, 0.04]
    p1 = C.sign_flip_perm_p(diffs, sum(diffs) / len(diffs), 2000, seed=42)
    p2 = C.sign_flip_perm_p(diffs, sum(diffs) / len(diffs), 2000, seed=42)
    p3 = C.sign_flip_perm_p(diffs, sum(diffs) / len(diffs), 2000, seed=7)
    assert p1 == p2                                      # 同种子可复现
    assert 0.0 <= p1 <= 1.0
    assert p3 is not None
    assert C.sign_flip_perm_p([0.01, 0.02], 0.015, 100) is None      # 日数 <3 不出 p
    assert C.sign_flip_perm_p(diffs, 0.01, 0) is None


def test_sign_flip_perm_p_extreme_observation_gives_small_p():
    diffs = [0.05] * 12                                    # 12 天全为正且等幅
    p = C.sign_flip_perm_p(diffs, 0.05, 4000, seed=1)
    assert p == pytest.approx(2 / 4096, abs=0.005)         # 只有全同号一种排列更极端


def test_days_needed_to_resolve():
    # MDE@80% = 2.8·sd/√K ≤ |mu|  ⇒  K ≥ (2.8·sd/|mu|)²
    assert C.days_needed_to_resolve(0.001, 0.01) == int(-(-1 * ((2.8 * 0.01 / 0.001) ** 2) // 1))
    assert C.days_needed_to_resolve(0.0, 0.01) is None
    assert C.days_needed_to_resolve(0.001, 0.0) is None
    assert C.days_needed_to_resolve(None, 0.01) is None


# ── 4. 臂定义的信息集声明 (诚实性契约) ────────────────────────────────────

def test_every_arm_spec_declares_info_set_and_availability():
    for spec in C.ARM_SPECS:
        assert spec["key"] and spec["rule"] and spec["info_set"] and spec["arm_class"]
        assert set(spec["known_at_decision"]) == {"deep_analysis", "universe_expansion"}
        assert all(isinstance(v, bool) for v in spec["known_at_decision"].values())


def test_every_arm_spec_has_a_direction_handler():
    """孤儿臂 (有 spec 无实现) 会在运行期抛 ValueError — 这里静态锁死一一对应。"""
    row = _row(prev=0.01, gap=0.01, t0=0.01)
    for spec in C.ARM_SPECS:
        assert C.arm_direction(spec["key"], row) in (1, -1)
    with pytest.raises(ValueError):
        C.arm_direction("NOT_AN_ARM", row)


def test_fair_arm_is_marked_available_to_both_chains():
    """主对照臂必须在两链决策时都已知, 否则它不是"公平的一行规则"而是优势臂。"""
    fair = {s["key"]: s for s in C.ARM_SPECS if s["arm_class"] == "fair_trivial_rule"}
    assert fair, "必须至少有一条信息集弱于 LLM 的公平对照臂"
    for s in fair.values():
        assert all(s["known_at_decision"].values())
    adv = [s for s in C.ARM_SPECS if s["arm_class"].startswith("info_advantaged")]
    assert adv and all(not any(s["known_at_decision"].values()) for s in adv)
    assert all(s.get("note") for s in adv)                  # 优势臂必须自带解释


def test_arm_direction_semantics():
    assert C.arm_direction("ALWAYS_LONG", _row()) == 1
    assert C.arm_direction("CONTRARIAN_LLM", _row(llm_sign=1)) == -1
    assert C.arm_direction("CONTRARIAN_LLM", _row(llm_sign=-1)) == 1
    assert C.arm_direction("PREV_DAY_MOMENTUM", _row(prev=0.02)) == 1
    assert C.arm_direction("PREV_DAY_MOMENTUM", _row(prev=-0.02)) == -1
    assert C.arm_direction("OVERNIGHT_GAP", _row(gap=0.0)) is None      # 平开 = 无方向
    assert C.arm_direction("T0_CLOSE_MOMENTUM", _row(t0=None)) is None


# ── 5. 配对构造 ───────────────────────────────────────────────────────────

def test_paired_rows_self_comparison_gives_zero_arm():
    rows = [_row(raw_t1=0.02, llm_ret=0.02)]
    out = C.paired_rows(rows, None, "t1")
    assert out[0]["arm_ret"] == 0.0 and out[0]["llm_ret"] == pytest.approx(0.02)


def test_paired_rows_drops_rows_where_arm_has_no_direction():
    """sign=0 的行必须剔除, 不得记 0 收益 — 记 0 会把配对差系统性拉向 0。"""
    rows = [_row(prev=0.01), _row(prev=0.0), _row(prev=None)]
    out = C.paired_rows(rows, "PREV_DAY_MOMENTUM", "t1")
    assert len(out) == 1


def test_paired_rows_arm_return_is_direction_times_shared_raw():
    rows = [_row(raw_t1=0.03, prev=-1.0)]                   # 前日下跌 ⇒ 该行做空
    out = C.paired_rows(rows, "PREV_DAY_MOMENTUM", "t1")
    assert out[0]["llm_ret"] == pytest.approx(0.03)         # LLM 做多
    assert out[0]["arm_ret"] == pytest.approx(-0.03)        # 对照臂做空, 同一收益


def test_paired_rows_horizon_t5_uses_t5_columns():
    rows = [_row(raw_t1=0.01, raw_t5=0.09, prev=1.0)]
    t1 = C.paired_rows(rows, "PREV_DAY_MOMENTUM", "t1")
    t5 = C.paired_rows(rows, "PREV_DAY_MOMENTUM", "t5")
    assert t1[0]["llm_ret"] == pytest.approx(0.01)
    assert t5[0]["llm_ret"] == pytest.approx(0.09)


def test_paired_rows_skips_rows_missing_the_horizon():
    rows = [_row(raw_t5=None, llm_ret_t5=None, prev=1.0)]
    assert C.paired_rows(rows, "PREV_DAY_MOMENTUM", "t5") == []


def test_evaluate_reports_all_arms_and_self_comparison():
    rows = [_row(date=f"2026-09-{d:02d}", raw_t1=0.01 * (i - 2), prev=(-1) ** i,
                 gap=(-1) ** i, t0=(-1) ** i)
            for d in (1, 2, 3) for i in range(5)]
    out = C.evaluate(rows, "t1", "abs_lt_0_5", "all", n_perm=200, seed=1)
    assert set(out["arms"]) == {s["key"] for s in C.ARM_SPECS}
    assert out["llm_vs_zero"]["n_days"] == 3
    for st in out["arms"].values():
        assert "mde80" in st and "resolvable" in st and "perm_p_two_sided" in st


def test_evaluate_scope_filters_by_chain():
    rows = [_row(chain="deep_analysis"), _row(chain="universe_expansion")]
    da = C.evaluate(rows, "t1", "raw", "deep_analysis", n_perm=0, seed=1)
    assert da["llm_vs_zero"]["n_rows"] == 1


def test_cross_policy_sign_table_flags_unstable_conclusions():
    """raw 与稳健口径反向 ⇒ all_same_sign False (极值行驱动的结论一票否决)。

    场景照抄实盘: LLM 臂本身是微负均值, 一行 +400% 脏数据 (US.ADBT 2026-09-02 SHORT
    fwd_ret_t1=+4.0) 就足以把 raw/winsor 抬成正, 而 abs 过滤与 excl_top3 仍留负 ⇒
    四策略不同号 ⇒ 该结论不得采信。这就是"金额均值被极值绑架"的一票否决用途。
    """
    # 值必须互不相同: excl_top3 用严格 < 阈值, 全等值会被整批剔掉 (n_policy 少一档)
    stable = [_row(date=f"2026-09-{d:02d}", raw_t1=-(0.01 + 0.001 * d),
                   llm_ret=-(0.01 + 0.001 * d), prev=-1.0) for d in range(1, 6)]
    tbl = C.cross_policy_sign_table(stable, "t1", "all", 0, seed=1)
    assert tbl["PREV_DAY_MOMENTUM"]["n_policy"] == len(C.POLICIES)

    hijacked = stable + [_row(date="2026-09-06", raw_t1=4.0, llm_ret=4.0, prev=-1.0)]
    tbl2 = C.cross_policy_sign_table(hijacked, "t1", "all", 0, seed=1)
    key = "LLM_vs_ZERO"
    assert tbl2[key]["by_policy"]["raw"] > 0 > tbl2[key]["by_policy"]["excl_top3"]
    assert tbl2[key]["by_policy"]["abs_lt_0_5"] < 0            # 稳健口径未被极值收买
    assert tbl2[key]["all_same_sign"] is False


def test_alpha_readout_uses_ledger_column_and_no_pairing():
    rows = [_row(date=f"2026-09-{d:02d}", alpha=0.01) for d in range(1, 6)]
    out = C.alpha_readout(rows, "t1", "all", n_perm=0, seed=1)
    assert out["metric"] == "alpha_t1"
    assert out["daily_mean_diff"] == pytest.approx(0.01)     # 对照恒为 0 ⇒ 差=alpha 本身


# ── 6. 只读与落点契约 ─────────────────────────────────────────────────────

def test_snapshot_db_opened_read_only_and_output_under_shadow():
    src = Path(inspect.getfile(C)).read_text()
    assert '?mode=ro' in src
    assert 'OUT_LATEST = REPO / "data" / "shadow"' in src


def test_daily_bars_are_cache_only_no_network():
    """日K只读缓存, 零拉取 (补拉是 t5cf 06:53 的职责) — 不得出现 futu/requests 调用。"""
    src = Path(inspect.getfile(C)).read_text()
    assert "replay_bars_daily" in src
    for banned in ("OpenQuoteContext", "request_history_kline", "import futu", "requests.get"):
        assert banned not in src


def test_load_daily_returns_none_for_unknown_symbol():
    assert C.load_daily("US.__NOPE_NOT_A_SYMBOL__") is None
    assert C.bar_context("US.__NOPE_NOT_A_SYMBOL__", "2026-09-03") == {
        "prev_day_ret": None, "overnight_gap": None, "t0_intraday_ret": None}


def test_bar_context_needs_two_prior_bars():
    """prev_day_ret 需要 t0−1 与 t0−2 两根 ⇒ 序列头部三项全 None (不给假方向)。

    实现是"全有或全无": i<2 时连 overnight_gap / t0_intraday_ret 也不给。这是刻意的
    保守选择 — 让三条平庸规则落在同一行集上, 不出现"某臂多几行"的口径差。
    代价只落在日K缓存序列的头两天 (2021 年), 对本项目窗口无影响。
    """
    sym = "US.__TEST_HEAD__"
    C._daily_cache[sym] = [["2026-09-01", 10, 11, 9, 10.5], ["2026-09-02", 10.5, 11, 10, 10.8]]
    C._daily_index[sym] = {"2026-09-01": 0, "2026-09-02": 1}
    try:
        assert C.bar_context(sym, "2026-09-01")["prev_day_ret"] is None
        out = C.bar_context(sym, "2026-09-02")
        assert out == {"prev_day_ret": None, "overnight_gap": None, "t0_intraday_ret": None}
    finally:
        C._daily_cache.pop(sym, None)
        C._daily_index.pop(sym, None)


def test_bar_context_computes_all_three_when_history_is_long_enough():
    sym = "US.__TEST_OK__"
    C._daily_cache[sym] = [["2026-08-31", 10, 11, 9, 10.0],
                           ["2026-09-01", 10, 11, 9, 10.5],
                           ["2026-09-02", 10.5, 11, 10, 10.8]]
    C._daily_index[sym] = {"2026-08-31": 0, "2026-09-01": 1, "2026-09-02": 2}
    try:
        out = C.bar_context(sym, "2026-09-02")
        assert out["prev_day_ret"] == pytest.approx(10.5 / 10.0 - 1.0)
        assert out["overnight_gap"] == pytest.approx(10.5 / 10.5 - 1.0)
        assert out["t0_intraday_ret"] == pytest.approx(10.8 / 10.5 - 1.0)
    finally:
        C._daily_cache.pop(sym, None)
        C._daily_index.pop(sym, None)


def test_bar_context_rejects_non_positive_prices():
    sym = "US.__TEST_ZERO__"
    C._daily_cache[sym] = [["2026-09-01", 0, 0, 0, 0], ["2026-09-02", 0, 0, 0, 0],
                           ["2026-09-03", 0, 1, 0, 5]]
    C._daily_index[sym] = {"2026-09-01": 0, "2026-09-02": 1, "2026-09-03": 2}
    try:
        out = C.bar_context(sym, "2026-09-03")
        assert out["prev_day_ret"] is None                   # 昨收 0 ⇒ 不出假方向
    finally:
        C._daily_cache.pop(sym, None)
        C._daily_index.pop(sym, None)
