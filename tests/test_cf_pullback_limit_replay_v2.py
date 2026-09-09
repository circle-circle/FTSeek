"""配对反事实 v2 汇总口径契约测试 (2026-09-07, DEB-20260907 建议⑥).

v1 有两处方向相反的口径缺陷 (反事实臂只近似 SL/EOD ⇒ 低估; cf 毛比 actual 净 ⇒ 高估),
v2 改成双臂同尺 + 保真度前置门。本文件锁的是**口径纪律**, 不是数值:
  1. 全部合计/改善率/符号检验只在 paired 群上算 (首版把未成交行计进分子 ⇒ 符号相反)
  2. 实盘侧未结算 (pnl_net 为空) 的行不得进配对 (会被迫假平, 造出伪优势; UBER 9/3 实锤)
  3. 保真度不过门 ⇒ 四条 ADR-035 判据一律不作数, wire_routing 必须 False
     (首版 C2 通过 +153%, 拆开全是重放器把实盘赢家判成输家)
  4. 抗极值阶梯与前三大贡献笔必须附实盘真值, 使"重放伪影"当场可见
  5. 多日窗口写独立文件, 不得追加 cron 单日模式的 OUT_JSONL (否则同行重复计入配对差)
  6. 只读: DB 以 mode=ro 打开
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts import cf_pullback_limit_replay as M


def _v2row(symbol="US.TEST", date="2026-09-03", status="entered",
           cf_net=10.0, act_net=5.0, db_net=5.0, paired=True,
           cf_mfe=0.5, act_mfe=0.5, db_reason="EOD", act_reason="EOD_REPLAY",
           sens_diff=None):
    return {
        "date": date, "symbol": symbol, "status": status,
        "actual_net": db_net, "actual_exit_reason": db_reason,
        "v2": {
            "cf_net": cf_net, "cf_exit_reason": "EOD_REPLAY", "cf_exit_hm": "15:55",
            "cf_mfe_pct": cf_mfe,
            "act_replay_net": act_net, "act_replay_reason": act_reason,
            "act_replay_mfe_pct": act_mfe,
            "paired_diff": round(cf_net - act_net, 2) if paired else None,
            "paired_diff_vs_db": round(cf_net - db_net, 2) if (paired and db_net is not None) else None,
            "paired": paired,
            "replay_status": {"cf": "replayed", "actual": "replayed" if paired else None},
            # 观测口径敏感性臂 (sample_source=bar_close): build_rows 实跑时必有
            "sens_bar_close": (None if sens_diff is None else {
                "cf_net": cf_net, "act_replay_net": round(cf_net - sens_diff, 2),
                "paired": True, "paired_diff": sens_diff,
                "cf_exit_reason": "EOD_REPLAY"}),
        },
    }


def _pairs(n_match=6, n_miss=0, reason="EOD", calibers=True, same_day=True):
    """保真度配对样本。`calibers=True` 时镜像 build_rows 的 2×2 口径输出 (arm_lag ×
    sample_source) — 门口径要求两 sample_source 都有数据, 缺格即 fail-closed。"""
    rows = [{"replay_reason": reason, "replay_net": 1.0, "db_reason": reason,
             "db_net": 1.0, "same_day": same_day} for _ in range(n_match)]
    rows += [{"replay_reason": "EOD_REPLAY", "replay_net": -5.0, "db_reason": "SL_TRAIL_LOCK",
              "db_net": 8.0, "same_day": same_day} for _ in range(n_miss)]
    if not calibers:
        return rows
    out = []
    for lag in M.ARM_LAG_CALIBERS:
        for src in M.SAMPLE_CALIBERS:
            out += [dict(r, arm_lag_bars=lag, sample_source=src) for r in rows]
    return out


# ── 1. 符号检验 ───────────────────────────────────────────────────────────

def test_sign_test_one_sided():
    assert M._sign_test_one_sided(0, 0) == 1.0            # 无样本 = 无证据
    assert M._sign_test_one_sided(0, 5) == 1.0
    assert M._sign_test_one_sided(5, 5) == pytest.approx(1 / 32)
    assert M._sign_test_one_sided(3, 4) == pytest.approx(5 / 16)
    assert M._sign_test_one_sided(-1, 4) == 1.0           # 负 k 不得崩


# ── 2. 配对群纪律 ─────────────────────────────────────────────────────────

def test_sums_only_over_paired_group():
    """未成交行 (cf_net 有值但无实盘臂) 不得进合计 — 首版据此得出符号相反的改善率。"""
    rows = [_v2row(symbol="US.A", cf_net=10.0, act_net=4.0),
            _v2row(symbol="US.B", cf_net=-2.0, act_net=1.0),
            _v2row(symbol="US.C", status="expired_unfilled", cf_net=999.0,
                   act_net=None, db_net=None, paired=False)]
    s = M.summarize_v2(rows, _pairs())
    assert s["n_rows"] == 3 and s["n_paired"] == 2
    assert s["n_unpaired_no_actual_fill"] == 1
    assert s["cf_net_sum"] == pytest.approx(8.0)          # 999 被排除
    assert s["act_replay_net_sum"] == pytest.approx(5.0)
    assert s["paired_diff_sum"] == pytest.approx(3.0)
    assert "population_note" in s


def test_unsettled_actual_row_is_not_paired():
    """实盘侧未结算 (pnl_net 空) ⇒ paired=False, 不得贡献配对差 (UBER 9/3 实锤)。"""
    row = _v2row(symbol="US.UBER", cf_net=152.22, act_net=None, db_net=None, paired=False)
    s = M.summarize_v2([row], _pairs())
    assert s["n_paired"] == 0
    assert s["paired_diff_sum"] is None
    assert s["caliber_b_vs_ledger"] is None
    assert s["adr035_criteria"]["C1_n_ge_20"]["pass"] is False


def test_caliber_a_and_b_reported_separately():
    """口径 a (两臂同尺) 与口径 b (cf vs 账本真值) 必须并报, 不得只给单点。"""
    rows = [_v2row(cf_net=10.0, act_net=4.0, db_net=-20.0)]
    s = M.summarize_v2(rows, _pairs())
    assert s["caliber_a_same_replayer"]["sum"] == pytest.approx(6.0)
    assert s["caliber_b_vs_ledger"]["sum"] == pytest.approx(30.0)
    assert s["caliber_a_same_replayer"]["note"] and s["caliber_b_vs_ledger"]["note"]


# ── 3. 保真度前置门 ───────────────────────────────────────────────────────

def test_fidelity_gate_blocks_verdict_even_when_criteria_pass():
    """弱桶 (n≥5 且复刻率<0.5) ⇒ 判据全过也不作数, wire_routing 必须 False。"""
    rows = [_v2row(symbol=f"US.S{i}", cf_net=20.0, act_net=5.0, cf_mfe=0.0, act_mfe=0.5)
            for i in range(25)]
    weak = _pairs(n_match=0, n_miss=6)                    # SL_TRAIL_LOCK 复刻 0/6
    s = M.summarize_v2(rows, weak)
    assert s["evaluability"]["status"] == "NOT_EVALUABLE_FIDELITY"
    assert s["evaluability"]["weak_buckets"]
    assert s["adr035_verdict"]["blocked_by"] == "fidelity_gate"
    assert s["adr035_verdict"]["wire_routing"] is False
    assert s["adr035_criteria"]["C1_n_ge_20"]["pass"] is True   # 判据照输出但不作数


def test_fidelity_gate_passes_when_replay_faithful():
    # 四条判据全过: C1 n=25 / C2 improve=(500-125)/125=3.0 / C3 fill_rate=1.0 /
    #   C4 mfe0 占比 cf(0.0) < actual(1.0) — 低吸臂改善了"从未浮盈"的比例
    rows = [_v2row(symbol=f"US.S{i}", cf_net=20.0, act_net=5.0, cf_mfe=0.5, act_mfe=0.0)
            for i in range(25)]
    s = M.summarize_v2(rows, _pairs(n_match=10, n_miss=0))
    assert s["evaluability"]["status"] == "EVALUABLE"
    assert s["adr035_verdict"]["blocked_by"] is None
    assert s["adr035_verdict"]["wire_routing"] is True
    assert s["adr035_verdict"]["n_pass"] == s["adr035_verdict"]["n_total"]
    assert s["adr035_criteria"]["C4_mfe0_share_down"]["pass"] is True


def test_fidelity_gate_needs_minimum_match_rate():
    """整体复刻率 <0.80 即使无弱桶也不过门。"""
    rows = [_v2row(symbol=f"US.S{i}") for i in range(25)]
    # 失配行同样按 2×2 口径铺开 — 否则混口径的 reason_match_rate 会被"命中行×4"稀释,
    #   本例实测 0.903 (假过门)。任何手工构造的配对样本都必须带口径标签。
    pairs = _pairs(n_match=7, n_miss=0) + [
        dict({"replay_reason": "EOD_REPLAY", "replay_net": 0.0, "db_reason": f"X{i}",
              "db_net": 0.0, "same_day": True}, arm_lag_bars=lag, sample_source=src)
        for i in range(3) for lag in M.ARM_LAG_CALIBERS for src in M.SAMPLE_CALIBERS]
    s = M.summarize_v2(rows, pairs)
    assert s["evaluability"]["reason_match_rate"] < 0.80
    assert s["evaluability"]["status"] == "NOT_EVALUABLE_FIDELITY"


# ── 4. ADR-035 四判据 ─────────────────────────────────────────────────────

def test_c1_n_threshold_boundary():
    crit19 = M.summarize_v2([_v2row(symbol=f"US.S{i}") for i in range(19)], _pairs())
    crit20 = M.summarize_v2([_v2row(symbol=f"US.S{i}") for i in range(20)], _pairs())
    assert crit19["adr035_criteria"]["C1_n_ge_20"]["pass"] is False
    assert crit20["adr035_criteria"]["C1_n_ge_20"]["pass"] is True


def test_c3_fill_rate_uses_entered_over_entered_plus_expired():
    rows = [_v2row(symbol="US.A", status="entered"),
            _v2row(symbol="US.B", status="entered"),
            _v2row(symbol="US.C", status="expired_unfilled", cf_net=0.0,
                   act_net=None, db_net=None, paired=False),
            _v2row(symbol="US.D", status="no_bars", cf_net=None,
                   act_net=None, db_net=None, paired=False)]
    s = M.summarize_v2(rows, _pairs())
    assert s["fill_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert s["adr035_criteria"]["C3_fill_rate_ge_50pct"]["pass"] is True


def test_c4_mfe0_share_direction():
    """C4 = 反事实臂的 mfe=0 占比要**低于**实盘臂 (低吸入场应改善"从未浮盈"的比例)。"""
    good = [_v2row(symbol=f"US.S{i}", cf_mfe=0.0, act_mfe=0.0) for i in range(4)]
    good += [_v2row(symbol=f"US.G{i}", cf_mfe=0.6, act_mfe=0.0) for i in range(6)]
    s = M.summarize_v2(good, _pairs())
    assert s["mfe0_share"]["cf"] < s["mfe0_share"]["actual"]
    assert s["adr035_criteria"]["C4_mfe0_share_down"]["pass"] is True

    bad = [_v2row(symbol=f"US.B{i}", cf_mfe=0.0, act_mfe=0.7) for i in range(10)]
    s2 = M.summarize_v2(bad, _pairs())
    assert s2["adr035_criteria"]["C4_mfe0_share_down"]["pass"] is False


# ── 5. 抗极值与贡献笔归因 ─────────────────────────────────────────────────

def test_trim_ladder_and_top_contributors_expose_replay_artifacts():
    rows = [_v2row(symbol="US.GTLB", cf_net=0.05, act_net=-173.46, db_net=0.05),
            _v2row(symbol="US.UBER", cf_net=0.0, act_net=-152.22, db_net=0.0),
            _v2row(symbol="US.META", cf_net=1.0, act_net=-53.5, db_net=1.0)]
    rows += [_v2row(symbol=f"US.S{i}", cf_net=1.0, act_net=1.0) for i in range(5)]
    s = M.summarize_v2(rows, _pairs())
    assert set(s["trim_ladder"]) == {"drop_1_largest_abs", "drop_3_largest_abs",
                                     "drop_5_largest_abs"}
    assert s["trim_ladder"]["drop_3_largest_abs"]["n"] == 5
    top = s["top_contributors"]
    assert top[0]["symbol"] == "US.GTLB"
    # 关键: 贡献笔必须带"重放 vs 账本"缺口, 否则重放伪影会被读成入场方式优势
    assert top[0]["replay_vs_db_gap"] == pytest.approx(-173.51, abs=0.01)
    assert top[0]["actual_net_db"] == pytest.approx(0.05)


def test_robust_drop3_present_for_small_n():
    rows = [_v2row(symbol=f"US.S{i}", cf_net=float(i), act_net=0.0) for i in range(4)]
    s = M.summarize_v2(rows, _pairs())
    assert s["robust_drop3_largest_abs"]["n"] == 4        # n<=6 时不裁剪, 但要报出 n


# ── 6. 文件与只读契约 ─────────────────────────────────────────────────────

def test_multiday_output_files_are_separate_from_cron_single_day():
    """多日窗口必须写独立文件: 追加到 cron 的 OUT_JSONL 会让同一行重复计入配对差。"""
    assert M.OUT_V2_JSONL != M.OUT_JSONL
    assert M.OUT_V2_LATEST != M.OUT_LATEST
    assert M.OUT_V2_JSONL.name == "pullback_limit_replay_v2.jsonl"
    assert M.OUT_JSONL.name == "pullback_limit_replay.jsonl"
    assert M.OUT_V2_JSONL.parent == M.OUT_JSONL.parent     # 同在 data/shadow/


def test_db_opened_read_only():
    src = Path(inspect.getfile(M)).read_text()
    assert 'sqlite3.connect(f"file:{DB}?mode=ro", uri=True)' in src
    assert "sqlite3.connect(DB)" not in src


def test_v2_replay_arm_guards_do_not_raise():
    """重放器异常/缺输入 → 该臂 None, 不抛 (尺子不得阻断 cron)。"""
    assert M._v2_replay_arm([], "LONG", 100.0, "09:37", 95.0, 0, 10, {}) is None
    assert M._v2_replay_arm(None, "LONG", 100.0, "09:37", 95.0, 0, 10, {}) is None
    assert M._v2_replay_arm([["09:38", 1, 1, 1, 1]], "LONG", 0, "09:37", 95.0, 0, 10, {}) is None
    assert M._v2_replay_arm([["09:38", 1, 1, 1, 1]], "LONG", 100.0, "", 95.0, 0, 10, {}) is None
    assert M._v2_replay_arm([["09:38", 1, 1, 1, 1]], "LONG", 100.0, "09:37", 95.0, 0, 0, {}) is None


def test_v2_replay_arm_returns_replayed_dict():
    bars = [["2026-09-04 09:37:00", 100, 100, 100, 100],
            ["2026-09-04 09:38:00", 100, 100.2, 99.9, 100.1]]
    out = M._v2_replay_arm(bars, "LONG", 100.0, "09:37", 95.0, 0, 10, {})
    assert out and out["status"] == "replayed"
    assert out["exit_reason"] == "EOD_REPLAY"


def test_summarize_v2_self_describes_caliber_and_cfg():
    s = M.summarize_v2([_v2row()], _pairs())
    assert "双臂同尺" in s["caliber"]
    assert s["replay_cfg"]["rules_omitted"]                # 未复刻规则必须披露
    # 步骤③ (2026-09-07): DEAD_SIGNAL/MFE_STALL/TIME_EXIT 已复刻 ⇒ 披露面必须搬家,
    #   否则判读者会以为它们仍是共模缺失, 从而把可归因的失配读成"尺子天生看不见"。
    for trk in ("dead_signal_exit", "mfe_stall_exit", "time_exit"):
        assert trk in s["replay_cfg"]["rules_replicated"]
        assert trk not in s["replay_cfg"]["rules_omitted"]
    assert "overnight_gap_stop" in s["replay_cfg"]["rules_omitted"]   # 真需跨日 bar 的仍披露


# ── 7. 双口径与域切分 (2026-09-07 保真度专项步骤①③) ─────────────────────

def test_fidelity_gate_reads_both_sample_calibers():
    """门要求 arm_lag=1 的**两个** sample_source 都过 — 只报过门那格 = 挑口径。"""
    rows = [_v2row(symbol=f"US.S{i}", cf_net=20.0, act_net=5.0, cf_mfe=0.5, act_mfe=0.0)
            for i in range(25)]
    ok = M.summarize_v2(rows, _pairs(n_match=10))
    assert ok["evaluability"]["status"] == "EVALUABLE"
    assert sorted(ok["evaluability"]["gate_calibers"]) == ["lag1|bar_close", "lag1|bar_extreme"]
    assert ok["fidelity_matrix"]                            # 2×2 全格披露
    # 缺 bar_close 格 (legacy 样本 / 采集失败) ⇒ fail-closed, 不得退化成单口径过门
    legacy = M.summarize_v2(rows, _pairs(n_match=10, calibers=False))
    assert legacy["evaluability"]["status"] == "NOT_EVALUABLE_FIDELITY"
    assert legacy["evaluability"]["fidelity_gate"]["lag1|bar_close"]["n_same_day"] == 0


def test_fidelity_gate_domain_is_same_day_and_ceiling_disclosed():
    """跨日仓 (实盘次日出场) 不计入门, 但必须作为**数学上限**披露 — 不是悄悄扔掉。"""
    rows = [_v2row(symbol=f"US.S{i}", cf_net=20.0, act_net=5.0, cf_mfe=0.5, act_mfe=0.0)
            for i in range(25)]
    pairs = _pairs(n_match=8, same_day=True) + _pairs(n_match=0, n_miss=2, same_day=False)
    s = M.summarize_v2(rows, pairs)
    ev = s["evaluability"]
    assert ev["gate_domain"].startswith("same_day_only")
    assert ev["ceiling_cross_day"] is not None and ev["ceiling_cross_day"] < 1.0
    # 当日域全命中 ⇒ 过门; 跨日失配只压低 ceiling, 不改门读数
    assert ev["fidelity_gate"]["lag1|bar_extreme"]["match_rate"] == 1.0
    assert s["fidelity"]["n_cross_day"] > 0 and s["fidelity"]["match_rate_cross_day"] == 0.0


def test_caliber_split_blocks_verdict_even_when_fidelity_passes():
    """两口径配对差中位数**异号** ⇒ 结论由观测模型决定, 判据全过也不作数。"""
    rows = [_v2row(symbol=f"US.S{i}", cf_net=20.0, act_net=5.0, cf_mfe=0.5, act_mfe=0.0,
                   sens_diff=-3.0) for i in range(25)]
    s = M.summarize_v2(rows, _pairs(n_match=10))
    assert s["caliber_sensitivity"]["bar_extreme"]["median"] > 0
    assert s["caliber_sensitivity"]["bar_close"]["median"] < 0
    assert s["caliber_sensitivity"]["median_sign_agree"] is False
    assert s["evaluability"]["status"] == "NOT_EVALUABLE_CALIBER_SPLIT"
    assert s["adr035_verdict"]["blocked_by"] == "caliber_split"
    assert s["adr035_verdict"]["wire_routing"] is False


def test_caliber_agreement_allows_verdict():
    rows = [_v2row(symbol=f"US.S{i}", cf_net=20.0, act_net=5.0, cf_mfe=0.5, act_mfe=0.0,
                   sens_diff=12.0) for i in range(25)]
    s = M.summarize_v2(rows, _pairs(n_match=10))
    assert s["caliber_sensitivity"]["median_sign_agree"] is True
    assert s["evaluability"]["status"] == "EVALUABLE"
    assert s["adr035_verdict"]["wire_routing"] is True


def test_dead_epoch_ceiling_is_internally_consistent():
    """已关闭纪年轨 (现网 mfe_stall_exit_enabled=False) 的历史真值行 ⇒ 上限必须 <1 并披露。

    不断言具体数值 (那取决于生产配置与窗口), 只钉**一致性**: 有死轨 ⇒ 上限<1 且给出原因;
    无死轨 ⇒ 上限为 None/1。这条上限是"全窗读数为何到不了 0.8"的唯一合法解释之一。
    """
    pairs = _pairs(n_match=4) + [
        dict({"replay_reason": "EOD_REPLAY", "replay_net": 0.0, "db_reason": "MFE_STALL_EXIT",
              "db_net": -3.0, "same_day": True}, arm_lag_bars=lag, sample_source=src)
        for lag in M.ARM_LAG_CALIBERS for src in M.SAMPLE_CALIBERS]
    s = M.summarize_v2([_v2row()], pairs)
    ev = s["evaluability"]
    from scripts.exit_path_replay import cfg_snapshot
    if cfg_snapshot().get("mfe_stall_exit_enabled"):
        assert ev["dead_epoch_tracks"] == {}
    else:
        assert "MFE_STALL_EXIT" in ev["dead_epoch_tracks"]
        assert ev["ceiling_dead_epoch"] is not None and ev["ceiling_dead_epoch"] < 1.0
        assert ev["ceiling_dead_epoch"] == pytest.approx(0.8, abs=0.01)   # 4/5 可达


# ══════════════════════════════════════════════════════════════════════════
# 8. 尺子三级许可卡 + 纪年守卫 + 人话读数卡 (2026-09-07 判据换轨)
#    为什么必须钉: 许可卡决定"哪些话可以说"。发放错 = 拿钝尺子下结论 (过松),
#    或永远发不出许可 = 纪律从防错变瘫痪 (过严)。两个方向都要有钉子。
# ══════════════════════════════════════════════════════════════════════════
# |db_net| 排序 [12,15,18,20,25,30] ⇒ median_abs_db_net = 20.0 (许可卡分母)
NETS = [20.0, -15.0, 30.0, -25.0, 12.0, -18.0]


def _money_pairs(nets=None, bias=0.0, reason="EOD", replay_reason=None,
                 date="2026-09-09", same_day=True):
    """钱口径可控的保真度配对群 (镜像 build_rows 的 2×2 口径输出)。

    bias: 标量 (两口径同值) / 列表 (逐笔循环) / {sample_source: 标量或列表}
          — 最后一种用于钉"单口径不过 ⇒ 整级不许可"(不得挑口径)。
    """
    nets = NETS if nets is None else nets
    out = []
    for lag in M.ARM_LAG_CALIBERS:
        for src in M.SAMPLE_CALIBERS:
            b = bias.get(src, 0.0) if isinstance(bias, dict) else bias
            for i, net in enumerate(nets):
                bi = b[i % len(b)] if isinstance(b, (list, tuple)) else b
                out.append({"replay_reason": replay_reason or reason,
                            "replay_net": round(net + bi, 2),
                            "db_reason": reason, "db_net": net,
                            "same_day": same_day, "date": date,
                            "arm_lag_bars": lag, "sample_source": src})
    return out


def _rowsN(n=25, date="2026-09-09", **kw):
    return [_v2row(symbol=f"US.S{i}", date=date, **kw) for i in range(n)]


def _rows25(date="2026-09-09", **kw):
    return _rowsN(25, date=date, **kw)


# 三种尺子形态 (阈值均为预注册值, 断言的是**分级行为**不是数值):
#   A 噪声主导: bias 逐笔 ±8 ⇒ 偏差 0 / MAE 8 ⇒ L0❌(8/20=0.4) L1a✅ L1b✅ L2✅
#   B 偏差主导: bias 恒 +8   ⇒ 偏差/MAE=1.0     ⇒ L0❌        L1a✅ L1b❌ L2✅
#   C 方向失明: bias 恒 −25  ⇒ 符号判对 4/6     ⇒ L0❌        L1a❌ L1b❌ L2✅
def test_ruler_license_denies_per_trade_but_grants_paired_diff():
    """L0 不许可 + L1a 许可必须能同时成立 — 单一二元门表达不了"部分可用"。

    这正是本系统当前真实处境 (9/1–9/4 实测 L0 比值 1.25–1.88, 即尺子单笔误差比被测
    单笔盈亏还大): 逐笔归因不可判读, 但两臂同尺的配对差因共模抵消仍可用。
    """
    lic = M.summarize_v2(_rows25(), _money_pairs(bias=[8.0, -8.0] * 3))["ruler_license"]
    lv = lic["levels"]
    assert lv["L0_per_trade_attribution"]["granted"] is False
    assert lv["L0_per_trade_attribution"]["worst"] == pytest.approx(0.4)
    assert lv["L1a_paired_diff_same_replayer"]["granted"] is True
    assert lv["L1b_aggregate_vs_ledger"]["granted"] is True
    assert lv["L2_exit_path_attribution"]["granted"] is True
    assert lic["status"] == "EVALUABLE_AT_AGGREGATE_LEVEL"
    assert "L1a" in lic["granted_levels"] and "L0" not in lic["granted_levels"]


def test_ruler_license_splits_paired_diff_from_ledger_reconciliation():
    """偏差主导时 L1a 仍许可、L1b 不许可 — 这就是必须拆两级的原因。

    合成一级会连带否掉**本次唯一要裁的读数** (caliber_a 配对差)。实测 9/1–9/4
    bar_close 口径 bias +$6.58/笔 = 其 MAE 的 44%, 而待裁效应中位仅 −$9.63/笔 ⇒
    对账本真值时偏差与效应同量级 (不可用), 两臂相减时偏差抵消 (可用)。
    """
    lic = M.summarize_v2(_rows25(), _money_pairs(bias=8.0))["ruler_license"]
    lv = lic["levels"]
    assert lv["L1a_paired_diff_same_replayer"]["granted"] is True
    assert lv["L1b_aggregate_vs_ledger"]["granted"] is False
    assert lv["L1b_aggregate_vs_ledger"]["worst"]["bias_to_mae_ratio"] == 1.0
    assert lic["status"] == "EVALUABLE_AT_AGGREGATE_LEVEL"      # 由 L1a 支撑
    assert "caliber_a_same_replayer" in lv["L1a_paired_diff_same_replayer"]["reading"]
    assert "caliber_b_vs_ledger" in lv["L1b_aggregate_vs_ledger"]["reading"]


def test_ruler_license_single_caliber_failure_denies_whole_level():
    """只一个 sample_source 不过 ⇒ 整级不许可 (挑口径防线, 与既有保真度门同纪律)。"""
    lic = M.summarize_v2(_rows25(), _money_pairs(
        bias={"bar_extreme": [8.0, -8.0] * 3, "bar_close": 8.0}))["ruler_license"]
    lv = lic["levels"]
    obs = lv["L1b_aggregate_vs_ledger"]["observed"]["bias_to_mae_ratio"]
    assert obs["lag1|bar_extreme"] == 0.0 and obs["lag1|bar_close"] == 1.0
    assert lv["L1b_aggregate_vs_ledger"]["granted"] is False
    assert lv["L1a_paired_diff_same_replayer"]["granted"] is True   # 符号两口径都判对


def test_ruler_license_denies_all_money_levels_when_direction_blind():
    """方向判对率 <0.80 ⇒ L1a/L1b 全撤, 只剩 L2 (路径归因) 可用。"""
    lic = M.summarize_v2(_rows25(), _money_pairs(bias=-25.0))["ruler_license"]
    lv = lic["levels"]
    assert lv["L0_per_trade_attribution"]["granted"] is False
    assert lv["L1a_paired_diff_same_replayer"]["granted"] is False
    assert lv["L1b_aggregate_vs_ledger"]["granted"] is False
    assert lv["L2_exit_path_attribution"]["granted"] is True
    assert lic["granted_levels"] == ["L2"]
    assert lic["status"] == "EVALUABLE_AT_PATH_LEVEL"


def test_ruler_license_missing_metric_is_fail_closed():
    """门域缺一个口径样本 ⇒ 指标 None ⇒ 不许可 (不得退化成单口径发放许可)。"""
    only_ext = [p for p in _money_pairs(bias=[8.0, -8.0] * 3)
                if p["sample_source"] == "bar_extreme"]
    lic = M.summarize_v2(_rows25(), only_ext)["ruler_license"]
    assert lic["levels"]["L1a_paired_diff_same_replayer"]["granted"] is False
    assert lic["status"] != "EVALUABLE_AT_AGGREGATE_LEVEL"


def test_money_criteria_not_binding_before_preregistered_date():
    """生效日纪律: 钱口径判据在 9/1–9/4 窗上设计 ⇒ 用它裁该窗 = 凑门, 只出指示性读数。"""
    lic = M.summarize_v2(_rows25(date="2026-09-03"),
                         _money_pairs(bias=[8.0, -8.0] * 3, date="2026-09-03"))["ruler_license"]
    assert lic["status"] == "PROVISIONAL_INDICATIVE_ONLY"
    assert lic["provisional"] is True and lic["money_criteria_binding"] is False
    assert lic["money_criteria_binding_from"] == "2026-09-08"
    # 指示性 ≠ 许可作废: 层级照常发放, 只是不具裁决效力
    assert lic["levels"]["L1a_paired_diff_same_replayer"]["granted"] is True
    # 边界: 生效日当天起即为正式 (>= 而非 >)
    lic2 = M.summarize_v2(_rows25(date=M.MONEY_CRITERIA_BINDING_FROM),
                          _money_pairs(bias=[8.0, -8.0] * 3))["ruler_license"]
    assert lic2["money_criteria_binding"] is True
    assert lic2["status"] == "EVALUABLE_AT_AGGREGATE_LEVEL"


def test_epoch_guard_flags_zero_match_track_and_gives_window_floor():
    """门域内 n≥5 且复刻率=0 的出场轨 ⇒ 跨纪年 + 换窗起点 (末次日期+1), 状态 NOT_SINGLE_EPOCH。

    守卫从数据算, 不写死 config 名单: 再关一条轨也能自动判出 (旧 dead_epoch_tracks 是人工点名)。
    """
    pairs = (_money_pairs(bias=[8.0, -8.0] * 3, date="2026-08-19")
             + _money_pairs(nets=[10.0] * 3, reason="MFE_STALL_EXIT",
                            replay_reason="EOD_REPLAY", date="2026-08-20"))
    s = M.summarize_v2(_rows25(date="2026-08-18"), pairs)
    ep = s["ruler_license"]["epoch"]
    assert ep["single_epoch"] is False
    assert "MFE_STALL_EXIT" in ep["unreachable_tracks"]
    assert ep["unreachable_tracks"]["MFE_STALL_EXIT"]["last_date"] == "2026-08-20"
    assert ep["epoch_floor"] == "2026-08-21"
    assert s["ruler_license"]["status"] == "NOT_SINGLE_EPOCH"
    # 旧字段连续性: 许可卡不改 evaluability 的既有语义
    assert s["evaluability"]["status"] == "NOT_EVALUABLE_FIDELITY"


def test_epoch_guard_fail_closed_without_gate_domain_samples():
    """门域零样本 ⇒ single_epoch=None (不宣称同纪年, 也不宣称跨纪年)。"""
    ep = M.summarize_v2(_rows25(), _money_pairs(bias=8.0, same_day=False))["ruler_license"]["epoch"]
    assert ep["single_epoch"] is None
    assert ep["tracks"] == {}
    assert "fail-closed" in ep["note"]


def test_epoch_guard_discloses_domain_masked_zero_match_track():
    """域收窄会藏坏轨 (实测 SL_INITIAL: 全口径 n=8 复刻 0, 当日域仅 n=4 ⇒ 躲过弱桶规则)。

    披露但不改判 — "无弱桶" ≠ "无坏轨", 读数卡要把这句写在明面上。
    """
    pairs = (_money_pairs(bias=[8.0, -8.0] * 3, date="2026-09-09")
             + _money_pairs(nets=[10.0, 12.0], reason="SL_INITIAL",
                            replay_reason="EOD_REPLAY", date="2026-09-09"))
    s = M.summarize_v2(_rows25(), pairs)
    ep = s["ruler_license"]["epoch"]
    m = ep["masked_zero_match_tracks"]["SL_INITIAL"]
    assert m["n_all_calibers"] == 8 and m["match_rate_all_calibers"] == 0.0
    assert m["n_in_gate_domain"] == 4 < M.EPOCH_ZERO_MATCH_MIN_N
    assert ep["single_epoch"] is True
    # 且必须上读数卡 (承诺在测试名里: 披露不只落 JSON, 要落到人读的那一屏)
    assert any("域外零复刻轨" in ln and "SL_INITIAL" in ln for ln in s["readout_card"])


def test_fidelity_pairs_carry_date_for_epoch_guard():
    """守卫靠配对样本的 date 定"换窗起点" ⇒ build_rows 必须写 date (源码钉子)。"""
    assert '"date": date' in inspect.getsource(M.build_rows)


def test_fidelity_gate_discloses_money_metrics_per_caliber():
    s = M.summarize_v2(_rows25(), _money_pairs(bias=8.0))
    for _k, g in s["evaluability"]["fidelity_gate"].items():
        for f in ("sign_agreement_rate", "bias_to_mae_ratio", "median_abs_db_net",
                  "mae_over_median_abs_net"):
            assert f in g
    assert "当日出场" in s["ruler_license"]["license_domain"]   # 许可域必须写明 = 门域


def test_legacy_evaluability_fields_preserved_alongside_license():
    """许可卡是**新增**判读层, 不得替换既有 evaluability 字段 (历史 JSON 连续性)。"""
    s = M.summarize_v2(_rows25(), _money_pairs(bias=8.0))
    ev = s["evaluability"]
    for k in ("status", "gate_domain", "gate_calibers", "fidelity_gate", "reason_match_rate",
              "weak_buckets", "ceiling_cross_day", "ceiling_dead_epoch", "dead_epoch_tracks",
              "caliber_median_sign_agree"):
        assert k in ev
    assert ev["status"] in ("EVALUABLE", "NOT_EVALUABLE_FIDELITY", "NOT_EVALUABLE_CALIBER_SPLIT")
    assert "ruler_license" in s and "readout_card" in s
    assert s["window_start"] == s["window_end"] == "2026-09-09"


def test_preregistered_thresholds_are_pinned():
    """门槛值本身钉死: 悄悄挪门必须打红 CI, 不能只改常量就生效 (预注册纪律的最后一道)。"""
    assert M.MONEY_CRITERIA_BINDING_FROM == "2026-09-08"   # 生效日: 之前的窗只出指示性
    assert M.MIN_N_FOR_CONCLUSION == 30                    # 宪章「n<30 不结论」
    assert M.ADR035_VERDICT_MIN_N_PAIRED == 40             # 本裁决预注册门槛 (与宪章地板分开)
    assert M.L0_MAE_TO_MEDIAN_NET_MAX == 0.25
    assert M.L1_BIAS_TO_MAE_MAX == 0.25
    assert M.L1_SIGN_AGREEMENT_MIN == 0.80
    assert M.L2_REASON_MATCH_MIN == 0.80
    assert M.EPOCH_ZERO_MATCH_MIN_N == 5


def test_readout_card_states_what_the_ruler_can_and_cannot_answer():
    s = M.summarize_v2(_rows25(), _money_pairs(bias=8.0))
    card = s["readout_card"]
    assert isinstance(card, list) and len(card) >= 7
    text = "\n".join(card)
    for token in ("L0", "L1a", "L1b", "L2", "结论", "效力", "配对 n="):
        assert token in text
    assert "✅ 许可" in text and "❌ 不许可" in text
    assert "维持关闭" in text          # 四判据未全过 ⇒ 接线开关不得打开


def test_readout_card_marks_provisional_window():
    card = "\n".join(M.summarize_v2(_rows25(date="2026-09-03"),
                                    _money_pairs(bias=8.0, date="2026-09-03"))["readout_card"])
    assert "指示性" in card and M.MONEY_CRITERIA_BINDING_FROM in card
    assert "n≥40" in card              # 正式裁决点的样本门槛必须写在卡上, 不能只埋在文档里


def test_readout_card_separates_point_estimate_from_significance():
    """"中位数为负"不得写成"劣于"的结论 — 点估计与统计证据必须分开说。"""
    s = M.summarize_v2(_rowsN(30, cf_net=5.0, act_net=20.0), _money_pairs(bias=8.0))
    assert s["n_paired"] >= M.MIN_N_FOR_CONCLUSION          # 否则走的是"n<30 不结论"分支
    assert s["paired_diff_median"] < 0
    line = [x for x in s["readout_card"] if x.startswith("│ 结论:")][0]
    assert "点估计劣于" in line
    assert "没有统计学证据判定优劣" in line


def test_license_separates_ruler_resolution_from_verdict_eligibility():
    """许可卡发的是**尺子分辨率**, 不是**裁决资格** — 两道 n 闸必须同屏且分开。

    缺这条 ⇒ 9/8 之后单日 cron (9/4 实测 n=6, 四级许可全发放) 会打出一张自称
    "效力: 正式"、甚至"有统计学证据"的卡, 把小样本读数冒充成裁决依据。
    第二道闸 (n≥40) 防的是另一种挪门: 宪章地板 30 一达到就裁 ADR-035 接线,
    而预注册的裁决点是 n≥40 ⇒ 30≤n<40 必须显式标"可说方向, 不得裁开关"。
    """
    pairs = _money_pairs(bias=[8.0, -8.0] * 3, date="2026-09-09")
    s = M.summarize_v2(_rowsN(6, date="2026-09-09"), pairs)
    lic = s["ruler_license"]
    assert lic["status"] == "EVALUABLE_AT_AGGREGATE_LEVEL"      # 尺子够细
    assert lic["sample_size"] == {"n_paired": 6, "min_for_conclusion": 30,
                                  "sufficient": False,
                                  "min_for_adr035_verdict": 40,
                                  "verdict_sufficient": False}
    assert lic["verdict_eligible"] is False                     # 但 n 不够 ⇒ 无裁决资格
    card = "\n".join(s["readout_card"])
    assert "样本不足" in card and "n<30 不结论" in card and "无裁决资格" in card
    assert "有统计学证据" not in card                            # 6/6 同向 p=0.016 也不算证据
    # 中间态 30≤n<40: 宪章闸过 (不再报"样本不足"), 但裁决闸未过
    s2 = M.summarize_v2(_rowsN(30, date="2026-09-09"), pairs)
    card2 = "\n".join(s2["readout_card"])
    assert s2["ruler_license"]["sample_size"]["sufficient"] is True
    assert s2["ruler_license"]["verdict_eligible"] is False
    assert "样本不足" not in card2
    assert "未达本裁决门槛" in card2 and "不得裁 entry_archetype_routing_enabled" in card2
    assert "禁跨窗拼接" in card2               # 不得把生效日之前的窗并进来凑数
    # n≥40 ⇒ 裁决资格发放, 两道警告行都消失
    s3 = M.summarize_v2(_rowsN(40, date="2026-09-09"), pairs)
    assert s3["ruler_license"]["verdict_eligible"] is True
    card3 = "\n".join(s3["readout_card"])
    assert "无裁决资格" not in card3 and "未达本裁决门槛" not in card3


def test_readout_card_says_nothing_can_be_concluded_without_pairs():
    """无配对样本 ⇒ 卡片必须说"没测到", 不得说"无效" (语义相反, 见 v1 影子未采集教训)。"""
    row = _v2row(symbol="US.UBER", cf_net=152.22, act_net=None, db_net=None, paired=False)
    card = "\n".join(M.summarize_v2([row], _money_pairs(bias=8.0))["readout_card"])
    assert "什么都不能下结论" in card and "没测到" in card
