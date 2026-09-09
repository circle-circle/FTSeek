"""离线退出重放器契约测试 (2026-09-07, DEB-20260907 建议⑥ 配对反事实的尺子).

覆盖点按"最容易出事且已出过事"排序:
  1. 入场那根 bar 整根跳过 — USAR 9/4 实锤回归 (db=SL_INITIAL −158.05 曾被误判成
     SL_TRAIL_LOCK +11.98, 根因=用入场 bar 的下影线假武装保本)
  2. SL 成交价双口径: 保本未武装取 bar 不利极值 / 已武装取限价地板 active_sl
     (8/20 方案A: 净保本由订单类型物理保证, 不能被 bar 下影线打成市价滑点)
  3. SHORT 与 LONG 对称
  4. TP 的 min_tp_rr 门禁 (RR 不足则忽略指令 TP)
  5. PRE_CLOSE 窗与 cohort B/long horizon 盈利仓豁免
  6. 阈值全部来自传入 cfg (证明没有硬编码平行常量)
  7. 零文件写入 (刻意不调 _check_playbook_exit_rules, 它会写生产影子文件)
  8. fidelity_report 的口径与净偏差符号约定
"""
from __future__ import annotations

import builtins
import inspect
from pathlib import Path

import pytest

from scripts import exit_path_replay as R


def _cfg(**over):
    """最小可用配置 — 与 load_cfg() 同键集, 但阈值由测试指定 (不依赖生产配置)。"""
    base = {
        "factor_exit_rules": {"default": {
            "trailing_enabled": True, "breakeven_at_pct": 0.4,
            "drawdown_from_peak_pct": 0.8, "trailing_step_pct": 2.0,
            "time_exit_hours": 6.0, "time_exit_only_if_loss": True, "max_hold_days": 1}},
        "breakeven_fee_cushion_pct": 0.15,
        "breakeven_slip_cushion_pct": 0.0,
        "breakeven_fee_aware_enabled": False,
        "cooldown_sl_pct": 4.0,
        "early_tight_sl_enabled": False,
        "early_tight_sl_minutes": 30.0,
        "early_tight_sl_pct": 1.0,
        "early_tight_sl_mfe_gate": 0.3,
        "early_tight_sl_atr_enabled": False,
        "early_tight_sl_atr_mult": 0.5,
        "early_tight_sl_pct_max": 2.5,
        "min_tp_rr": 1.5,
        # 步骤③ 三轨 (与 load_cfg() 同键集; 默认关闭 = 不干扰既有 28 例的期望出场)
        "dead_signal_exit_enabled": False,
        "dead_signal_exit_minutes": 60.0,
        "dead_signal_exit_mfe_pct": 0.2,
        "dead_signal_reentry_extension_min": 60.0,
        "mfe_stall_exit_enabled": False,
        "mfe_stall_exit_minutes": 150.0,
        "mfe_stall_exit_mfe_pct": 0.5,
        "mfe_stall_exit_shallow_hold_enabled": False,
        "mfe_stall_exit_shallow_pct": 1.0,
        "overnight_master_switch": False,
    }
    base.update(over)
    return base


def _bar(hm, o, h, l, c):
    return [f"2026-09-04 {hm}:00", o, h, l, c]


def _replay(**kw):
    kw.setdefault("cfg", _cfg())
    kw.setdefault("qty", 10)
    return R.replay_exit(**kw)


# ── 1. 入场 bar 整根跳过 (USAR 9/4 回归) ──────────────────────────────────

def test_entry_bar_spike_does_not_arm_breakeven():
    """入场那根 bar 的上影线不得武装保本 — 该 bar 的价格可能全部发生在 fill 之前。"""
    bars = [
        _bar("09:37", 18.9, 19.60, 18.20, 18.95),   # 入场 bar: 巨幅上下影线
        _bar("09:38", 18.90, 18.92, 18.85, 18.88),  # 之后平淡 (+0.19% < 保本阈 0.4%)
    ]
    rep = _replay(direction="LONG", entry_price=18.885, entry_hm="09:37",
                  initial_sl=17.50, take_profit=0, bars=bars)
    assert rep["status"] == "replayed"
    assert rep["exit_reason"] == "EOD_REPLAY"
    assert rep["bars_used"] == 1                      # 入场 bar 不计入评估
    # 09:37 的上影线 (+3.8%) 若被采信, 保本会武装 ⇒ 出场原因会落到 SL_* 而非 EOD
    assert rep["exit_reason"] not in ("SL_BREAKEVEN", "SL_TRAIL_LOCK")
    assert rep["mfe_pct"] < 0.4                       # 未被入场 bar 的假浮盈污染
    assert rep["exit_price"] == pytest.approx(18.88)


def test_entry_bar_adverse_wick_does_not_trigger_sl():
    """入场那根 bar 的下影线不得触发止损 (与上影线对称, 两臂同尺)。"""
    bars = [
        _bar("09:37", 100.0, 100.2, 50.0, 100.0),   # 下影线远低于 SL
        _bar("09:38", 100.0, 100.2, 99.9, 100.1),   # 之后平淡 (+0.2% < 0.4%)
    ]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars)
    assert rep["exit_reason"] == "EOD_REPLAY"
    assert rep["exit_price"] == pytest.approx(100.1)
    assert rep["mae_pct"] > -1.0                      # 未把 50.0 记成本仓 MAE


def test_bars_at_or_before_entry_are_all_skipped():
    bars = [_bar("09:31", 99, 130, 70, 100), _bar("09:35", 100, 130, 70, 100),
            _bar("09:37", 100, 130, 70, 100), _bar("09:38", 100, 100.2, 99.9, 100.05)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars)
    assert rep["bars_used"] == 1
    assert rep["exit_reason"] == "EOD_REPLAY"


# ── 2. SL 成交价双口径 ────────────────────────────────────────────────────

def test_unarmed_sl_fills_at_bar_adverse_extreme():
    """保本未武装 = 真止损, 无限价保护 ⇒ 取 bar 不利极值 (对齐 exit_engine 7/16 P0)。"""
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 100.1, 90.0, 91.0)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars)
    assert rep["exit_reason"] == "SL_INITIAL"
    assert rep["exit_price"] == pytest.approx(90.0)   # 不是 SL 价 95.0
    assert rep["exit_hm"] == "09:38"
    assert rep["net_pnl"] < rep["gross_pnl"]          # 费用照扣


def test_armed_breakeven_fills_at_limit_floor_not_bar_extreme():
    """保本已武装 ⇒ monitor 挂限价单@净保本线, 成交价物理上不低于该线。"""
    bars = [
        _bar("09:37", 100, 100, 100, 100),
        _bar("09:38", 100, 101.0, 100.5, 100.8),     # +1.0% ⇒ 武装保本 (阈值 0.4%)
        _bar("09:39", 100.8, 100.9, 80.0, 81.0),     # 深下影线
    ]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars)
    assert rep["exit_reason"] in ("SL_BREAKEVEN", "SL_TRAIL_LOCK")
    assert rep["exit_price"] >= 100.0                 # 限价地板保证净保本
    assert rep["exit_price"] > 80.0                   # 没被下影线打成市价滑点
    assert rep["net_pnl"] >= -rep["fee"] - 1e-6


def test_armed_short_breakeven_fills_at_limit_ceiling():
    bars = [
        _bar("09:37", 100, 100, 100, 100),
        _bar("09:38", 100, 100.5, 99.0, 99.2),       # 空方 +0.8% ⇒ 武装
        _bar("09:39", 99.2, 130.0, 99.0, 129.0),     # 深上影线
    ]
    rep = _replay(direction="SHORT", entry_price=100.0, entry_hm="09:37",
                  initial_sl=105.0, take_profit=0, bars=bars)
    assert rep["exit_reason"] in ("SL_BREAKEVEN", "SL_TRAIL_LOCK")
    assert rep["exit_price"] <= 100.0
    assert rep["exit_price"] < 130.0


# ── 3. SHORT 对称 ─────────────────────────────────────────────────────────

def test_short_unarmed_sl_uses_bar_high():
    # 09:38 的 low 99.9 只给 +0.1% 浮盈 (< 保本阈 0.4%) ⇒ 保持"未武装"分支
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 110.0, 99.9, 109.0)]
    rep = _replay(direction="SHORT", entry_price=100.0, entry_hm="09:37",
                  initial_sl=105.0, take_profit=0, bars=bars)
    assert rep["exit_reason"] == "SL_INITIAL"
    assert rep["exit_price"] == pytest.approx(110.0)  # 空方不利极值 = high
    assert rep["gross_pnl"] < 0


def test_short_peak_tracks_low():
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 100.2, 96.0, 97.0),
            _bar("09:39", 97, 98, 96.5, 97.5)]
    rep = _replay(direction="SHORT", entry_price=100.0, entry_hm="09:37",
                  initial_sl=105.0, take_profit=0, bars=bars)
    assert rep["peak_profit_pct"] == pytest.approx(4.0, abs=0.01)
    assert rep["mfe_pct"] == pytest.approx(4.0, abs=0.01)


# ── 4. TP 的 RR 门禁 ──────────────────────────────────────────────────────

def test_tp_ignored_when_rr_below_min():
    """RR = |TP−entry| / |entry−SL| < min_tp_rr ⇒ 忽略指令 TP (与 exit_engine 同门禁)。"""
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 101.5, 99.8, 101.0)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=99.0, take_profit=101.2, bars=bars)   # RR=1.2 < 1.5
    assert rep["tp_armed"] is False
    assert rep["tp_rr"] == pytest.approx(1.2, abs=0.01)
    assert rep["exit_reason"] != "TAKE_PROFIT"


def test_tp_fires_when_rr_passes():
    # low 102.5 必须高于锁利线 (peak 103 × (1−0.8%) = 102.18), 否则 SL 先于 TP 触发
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 103.0, 102.5, 102.8)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=98.0, take_profit=103.0, bars=bars)   # RR=1.5 ≥ 1.5
    assert rep["tp_armed"] is True
    assert rep["exit_reason"] == "TAKE_PROFIT"
    assert rep["exit_price"] == pytest.approx(103.0)              # 取有利极值


def test_sl_beats_tp_within_same_bar():
    """bar 内 high/low 先后不可知 ⇒ 次序固定为 SL 先判 (保守, 两臂同尺)。"""
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 110.0, 90.0, 100.0)]
    # 保本阈抬到 99% ⇒ 该 bar 的 +10% 不武装保本, 隔离出"SL vs TP 次序"这一个变量
    cfg = _cfg()
    cfg["factor_exit_rules"] = {"default": dict(cfg["factor_exit_rules"]["default"],
                                                breakeven_at_pct=99.0)}
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=108.0, bars=bars, cfg=cfg)
    assert rep["exit_reason"] == "SL_INITIAL"
    assert rep["exit_price"] == pytest.approx(90.0)


# ── 5. PRE_CLOSE 窗与豁免 ─────────────────────────────────────────────────

def test_pre_close_exit_inside_window():
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("15:55", 100, 100.3, 99.9, 100.2)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars)
    assert rep["exit_reason"] == "PRE_CLOSE_EXIT"
    assert rep["exit_price"] == pytest.approx(100.2)


def test_pre_close_skipped_for_cohort_b_long_horizon_winner():
    """cohort B / long horizon 的盈利仓跳过 PRE_CLOSE (需隔夜总开关开)。"""
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("15:55", 100, 100.3, 99.9, 100.2)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars,
                  cohort="B", horizon="long", cfg=_cfg(overnight_master_switch=True))
    assert rep["skip_allowed"] is True
    assert rep["exit_reason"] == "EOD_REPLAY"


def test_pre_close_not_skipped_for_loser_even_in_cohort_b():
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("15:55", 100, 100.2, 99.0, 99.0)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=0, bars=bars,
                  cohort="B", horizon="long", cfg=_cfg(overnight_master_switch=True))
    assert rep["exit_reason"] == "PRE_CLOSE_EXIT"


# ── 6. 阈值来自 cfg, 无硬编码平行常量 ─────────────────────────────────────

def test_breakeven_threshold_read_from_cfg():
    """同一批 bar, 只改 breakeven_at_pct ⇒ 武装时点改变 (证明阈值非硬编码)。"""
    bars = [_bar("09:37", 100, 100, 100, 100),
            _bar("09:38", 100, 100.6, 100.0, 100.5),      # +0.6% 浮盈
            _bar("09:39", 100.5, 100.6, 90.0, 91.0)]
    loose = _cfg()
    loose["factor_exit_rules"] = {"default": dict(loose["factor_exit_rules"]["default"],
                                                  breakeven_at_pct=2.0)}
    r_loose = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                      initial_sl=95.0, take_profit=0, bars=bars, cfg=loose)
    r_tight = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                      initial_sl=95.0, take_profit=0, bars=bars, cfg=_cfg())
    assert r_loose["exit_reason"] == "SL_INITIAL"
    assert r_tight["exit_reason"] in ("SL_BREAKEVEN", "SL_TRAIL_LOCK")


def test_min_tp_rr_read_from_cfg():
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 101.5, 99.8, 101.0)]
    strict = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                     initial_sl=99.0, take_profit=101.2, bars=bars)
    lax = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=99.0, take_profit=101.2, bars=bars, cfg=_cfg(min_tp_rr=1.0))
    assert strict["tp_armed"] is False and lax["tp_armed"] is True


def test_reuses_exit_engine_ssot_not_a_parallel_copy():
    """军规: 不造平行层 — 保本/锁利数学必须来自 exit_engine, 本模块不得自己实现。"""
    src = Path(inspect.getfile(R)).read_text()
    assert "from app.playbook.exit_engine import _compute_dynamic_effective_sl" in src
    assert "_get_factor_exit_config" in src
    assert "def _compute_dynamic_effective_sl" not in src      # 没有本地重定义
    assert "_check_playbook_exit_rules" not in src.replace(
        "刻意**不**调用 `_check_playbook_exit_rules`", "").replace(
        "不调 _check_playbook_exit_rules", "")                  # 刻意不调用有副作用的实盘函数


# ── 7. 零文件写入 ─────────────────────────────────────────────────────────

def test_replay_writes_nothing(monkeypatch):
    """离线重放不得写任何文件 (实盘退出函数会写生产影子文件, 故本模块刻意不调它)。"""
    writes: list = []
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **kw):
        if any(ch in str(mode) for ch in "wax+"):
            writes.append(str(file))
        return real_open(file, mode, *a, **kw)

    def guarded_write_text(self, *a, **kw):
        writes.append(str(self))
        raise AssertionError("重放器不得写文件")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    bars = [_bar("09:37", 100, 100, 100, 100), _bar("09:38", 100, 101, 99, 100.5)]
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:37",
                  initial_sl=95.0, take_profit=110.0, bars=bars)
    R.fidelity_report([{"replay_reason": rep["exit_reason"], "replay_net": rep["net_pnl"],
                        "db_reason": "EOD", "db_net": 1.0, "same_day": True}])
    assert writes == []


# ── 8. 状态守卫与保真度报告 ───────────────────────────────────────────────

@pytest.mark.parametrize("kw,expected", [
    ({"bars": []}, "no_bars"),
    ({"bars": None}, "no_bars"),
    ({"entry_hm": "not-a-time"}, "bad_entry_hm"),
    ({"qty": 0}, "bad_input"),
    ({"entry_price": 0}, "bad_input"),
])
def test_status_guards(kw, expected):
    base = dict(direction="LONG", entry_price=100.0, entry_hm="09:37",
                initial_sl=95.0, take_profit=0, qty=10,
                bars=[_bar("09:38", 100, 101, 99, 100.5)])
    base.update(kw)
    rep = _replay(**base)
    assert rep["status"] == expected
    assert rep["rules_omitted"]                        # 未复刻规则必须自描述披露


def test_hm_to_minutes_accepts_both_formats():
    assert R._hm_to_minutes("09:37") == 9 * 60 + 37
    assert R._hm_to_minutes("2026-09-04 09:37:00") == 9 * 60 + 37
    with pytest.raises(Exception):
        R._hm_to_minutes("??")


def test_fidelity_report_empty():
    out = R.fidelity_report([])
    assert out["n"] == 0 and "note" in out


def test_fidelity_report_match_rate_and_bias_sign():
    pairs = [
        {"replay_reason": "SL_INITIAL", "replay_net": -100.0, "db_reason": "SL_INITIAL",
         "db_net": -90.0, "same_day": True},
        {"replay_reason": "EOD_REPLAY", "replay_net": -5.0, "db_reason": "SL_TRAIL_LOCK",
         "db_net": 12.0, "same_day": True},
        {"replay_reason": "SL_TRAIL_LOCK", "replay_net": 8.0, "db_reason": "SL_TRAIL_LOCK",
         "db_net": 10.0, "same_day": False},
    ]
    out = R.fidelity_report(pairs)
    assert out["n"] == 3
    assert out["reason_match_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert out["net_bias"] < 0                        # 重放比真值保守 = 负偏差
    assert out["n_cross_day"] == 1
    assert out["by_db_reason"]["SL_TRAIL_LOCK"]["n"] == 2
    assert out["by_db_reason"]["SL_TRAIL_LOCK"]["match_rate"] == pytest.approx(0.5)
    # 分桶按 n 降序 (弱桶要先被看见)
    assert list(out["by_db_reason"])[0] == "SL_TRAIL_LOCK"


def test_fidelity_report_ignores_rows_without_db_net():
    out = R.fidelity_report([{"replay_reason": "EOD_REPLAY", "replay_net": 1.0,
                              "db_reason": None, "db_net": None, "same_day": True}])
    assert out["n"] == 0


# ── 8b. 钱口径四指标 (2026-09-07 判据换轨) ────────────────────────────────
#   为什么必须钉: 这四个数是"尺子三级许可卡"的**唯一输入** (L0/L1a/L1b)。指标算错 =
#   许可发放错 = 拿一把钝尺子下结论。判别力方向也在此钉死: 钱口径与标签口径各有盲区,
#   不得互相替换 (实证见 fidelity_report 内注释与 DEB §4.9)。
def _mp(replay_net, db_net, reason="EOD", replay_reason=None, same_day=True):
    return {"replay_reason": replay_reason or reason, "replay_net": replay_net,
            "db_reason": reason, "db_net": db_net, "same_day": same_day}


def test_fidelity_money_metrics_values():
    out = R.fidelity_report([_mp(24.0, 20.0), _mp(-24.0, -20.0),
                             _mp(10.0, 10.0), _mp(-10.0, -10.0)])
    # 误差对称 (+4/-4/0/0) ⇒ 偏差 0、MAE 2; |db_net| 排序 [10,10,20,20] 取中位 20
    assert out["sign_agreement_rate"] == 1.0
    assert out["bias_to_mae_ratio"] == 0.0
    assert out["median_abs_db_net"] == pytest.approx(20.0)
    assert out["mae_over_median_abs_net"] == pytest.approx(0.1)   # 2/20: 逐笔可判读


def test_fidelity_money_metrics_sign_agreement_counts_flips():
    """方向判对率: 重放把赢判成输 (或反之) 必须被计为不一致 — 这是 L1a 的唯一判据。"""
    out = R.fidelity_report([_mp(20.0, 20.0), _mp(-20.0, -20.0),
                             _mp(5.0, -20.0), _mp(-3.0, 20.0)])
    assert out["sign_agreement_rate"] == 0.5
    assert out["mae_over_median_abs_net"] > 0.5      # 单笔误差达被测量级一半以上


def test_fidelity_money_metrics_perfect_replay_is_not_fail_closed():
    """零误差不得退化成 None: 许可卡把 None 判为"不许可", 完美尺子反被否 (回归钉子)。"""
    out = R.fidelity_report([_mp(10.0, 10.0), _mp(-5.0, -5.0)])
    assert out["net_mae"] == 0.0
    assert out["bias_to_mae_ratio"] == 0.0           # 不是 None
    assert out["mae_over_median_abs_net"] == 0.0
    assert out["sign_agreement_rate"] == 1.0


def test_fidelity_money_metrics_cannot_replace_label_gate():
    """判别力钉子 (实证): 钱口径在"路径全错"的窗上照样满分 ⇒ 不得用它替换标签门。

    对应 2026-09-07 烂窗 7/11–8/14 实测: 标签一致率仅 0.384/0.444, 而钱口径
    |bias|/MAE = 0.054/0.009、符号一致 0.852/0.875 —— 钱口径看不出路径归因已经失效。
    """
    pairs = [_mp(101.0 if i % 2 else 99.0, 100.0, reason="SL_TRAIL_LOCK",
                 replay_reason="EOD_REPLAY") for i in range(6)]
    out = R.fidelity_report(pairs)
    assert out["reason_match_rate"] == 0.0           # 路径归因全错
    assert out["sign_agreement_rate"] == 1.0         # 钱口径却满分
    assert out["bias_to_mae_ratio"] == 0.0
    assert out["mae_over_median_abs_net"] == pytest.approx(0.01)


def test_fidelity_money_metrics_economic_equivalence_is_label_gate_blindspot():
    """反方向盲区: 经济等价标签 (SL_BREAKEVEN vs SL_TRAIL_LOCK) 钱差近零, 标签门判失配。

    ⇒ 两级必须并列: 标签门不许可时仍可许可 L0/L1 (钱读得准, 只是路径名说不准)。
    """
    pairs = [_mp(10.0, 12.0, reason="SL_BREAKEVEN", replay_reason="SL_TRAIL_LOCK")
             for _ in range(6)]
    out = R.fidelity_report(pairs)
    assert out["reason_match_rate"] == 0.0
    assert out["median_abs_db_net"] == pytest.approx(12.0)
    assert out["mae_over_median_abs_net"] == pytest.approx(0.17)   # 2/12 < 0.25 ⇒ L0 可许可


def test_cfg_snapshot_is_self_describing():
    snap = R.cfg_snapshot()
    assert set(R.RULES_REPLICATED) <= set(snap["rules_replicated"])
    # 步骤③ (2026-09-07): 三轨已从"未复刻"移入"已复刻" — 披露面必须跟着搬家,
    #   否则判读者会以为 DEAD_SIGNAL 仍是共模缺失而低估全窗失配的可归因性。
    assert "dead_signal_exit" in snap["rules_replicated"]
    assert "mfe_stall_exit" in snap["rules_replicated"]
    assert "time_exit" in snap["rules_replicated"]
    assert "dead_signal_exit" not in snap["rules_omitted"]
    assert "overnight_gap_stop" in snap["rules_omitted"]   # 真需跨日 bar 的仍须披露
    assert "default_exit_cfg" in snap
    assert "factor_exit_rules" not in snap               # 大字典不落库


def test_test_cfg_keyset_matches_load_cfg():
    """本文件的 `_cfg()` 必须与 `load_cfg()` **同键集** (键漂移钉子)。

    为什么值得钉: replay_exit 对 cfg 用**严格下标**取值 (军规"阈值全部来自 cfg, 不留
    平行默认常量"), 故 load_cfg 每加一个键, 手搓 cfg 的调用方 (本文件 / 任何离线探针)
    就会 KeyError。步骤③ 加 9 个键时正是这样炸的 — 与其事后修, 不如让键集漂移在 CI 就红。
    """
    assert set(_cfg()) == set(R.load_cfg(refresh=True))


# ══════════════════════════════════════════════════════════════════════════
# 9. 步骤③ 三轨 (DEAD_SIGNAL_EXIT / MFE_STALL_EXIT / TIME_EXIT)
#    为什么必须钉: 8/15+ 单纪年窗 63 笔失配里 DEAD_SIGNAL_EXIT n=20 复刻 0/20
#    (规则在实盘活着、重放器没实现) ⇒ 该窗剩余失配的最大单块。
# ══════════════════════════════════════════════════════════════════════════
def _flat_bars(hm_from, hm_to, h=100.10, l=99.60, c=99.70):
    """生成 [hm_from, hm_to] 每分钟一根的平盘 bar (peak 恒 0.10%, 收盘恒 −0.30%)。"""
    def _m(hm):
        hh, mm = hm.split(":")
        return int(hh) * 60 + int(mm)
    return [_bar(f"{(_m(hm_from) + i) // 60:02d}:{(_m(hm_from) + i) % 60:02d}", 100.0, h, l, c)
            for i in range(_m(hm_to) - _m(hm_from) + 1)]


def _ds_cfg(**over):
    return _cfg(dead_signal_exit_enabled=True, **over)


def test_dead_signal_exit_fires_when_peak_never_reaches_threshold():
    """held≥60min ∧ 生命周期 peak<0.2% → DEAD_SIGNAL_EXIT, 成交价取该根收盘价。"""
    bars = _flat_bars("09:32", "10:40")
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                  initial_sl=90.0, take_profit=0, bars=bars, cfg=_ds_cfg())
    assert rep["exit_reason"] == "DEAD_SIGNAL_EXIT"
    assert rep["exit_hm"] == "10:31"                      # held 首次 ≥60 的那根
    assert rep["exit_price"] == pytest.approx(99.70)      # 时间类规则取收盘价, 不取 bar 极值
    assert rep["dead_signal_minutes_effective"] == 60.0


def test_dead_signal_exit_disabled_by_default_is_eod():
    """同一批 bar、dead_signal 关闭 (现网 True / 测试默认 False) → EOD_REPLAY: 证明该轨
    确实由 cfg 驱动, 而非被硬编码进重放器。"""
    bars = _flat_bars("09:32", "10:40")
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                  initial_sl=90.0, take_profit=0, bars=bars, cfg=_cfg())
    assert rep["exit_reason"] == "EOD_REPLAY"


def test_dead_signal_exit_suppressed_once_peak_reaches_threshold():
    """09:40 那根把 peak 抬到 0.25% (≥0.2%) ⇒ 方向已兑现, 60min 检查点不得再杀。"""
    bars = _flat_bars("09:32", "10:40")
    bars[8] = _bar("09:40", 100.0, 100.30, 99.90, 100.05)   # peak 0.30% ≥ 阈值
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                  initial_sl=90.0, take_profit=0, bars=bars, cfg=_ds_cfg())
    assert rep["exit_reason"] != "DEAD_SIGNAL_EXIT"
    assert rep["peak_profit_pct"] >= 0.2


def test_dead_signal_reentry_extension_widens_breathing_window():
    """Phase0 (8/23): decision_id 前缀 intraday_reentry → 呼吸窗 60→120min。

    实锤依据: reentry 前 6 笔中 4 笔被 60min 口径杀掉 (CRM/TSM/AVGO/CSCO), 全在入场后
    1–2h; 全窗 19 笔 reentry ⇒ 这条判据不是装饰, 是 5% 样本的生死线。
    """
    bars = _flat_bars("09:32", "11:40")
    plain = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                    initial_sl=90.0, take_profit=0, bars=bars, cfg=_ds_cfg())
    reentry = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                      initial_sl=90.0, take_profit=0, bars=bars, cfg=_ds_cfg(),
                      decision_id="intraday_reentry-1250-1110")
    assert plain["exit_reason"] == "DEAD_SIGNAL_EXIT" and plain["exit_hm"] == "10:31"
    assert reentry["exit_reason"] == "DEAD_SIGNAL_EXIT" and reentry["exit_hm"] == "11:31"
    assert reentry["dead_signal_minutes_effective"] == 120.0
    # 非 reentry 前缀不得误加宽 (判据是前缀, 不是"包含")
    other = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                    initial_sl=90.0, take_profit=0, bars=bars, cfg=_ds_cfg(),
                    decision_id="da-1250-1110")
    assert other["dead_signal_minutes_effective"] == 60.0


def test_dead_signal_exit_symmetric_for_short():
    """SHORT 侧: peak 用有利极值 (低), 收盘 −0.30% 变成 +0.30% ⇒ 仍满足 peak<0.2%。"""
    bars = _flat_bars("09:32", "10:40", h=100.40, l=99.90, c=100.30)
    rep = _replay(direction="SHORT", entry_price=100.0, entry_hm="09:31",
                  initial_sl=110.0, take_profit=0, bars=bars, cfg=_ds_cfg())
    assert rep["exit_reason"] == "DEAD_SIGNAL_EXIT"
    assert rep["exit_hm"] == "10:31"


def test_pre_close_outranks_dead_signal_same_bar():
    """次序钉子: 同一根同时满足 PRE_CLOSE 与 DEAD_SIGNAL → PRE_CLOSE 赢。

    实盘序为 PRE_CLOSE(exit_engine.py:668) → DEAD_SIGNAL(:891) → MFE_STALL(:932) →
    TIME_EXIT(:972)。次序错一位就会把"尾盘收尾"记成"死信号", 而两者的语义与后续
    归因完全不同 (前者是日终纪律, 后者是信号质量证据)。
    """
    bars = _flat_bars("14:51", "15:59")
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="14:50",
                  initial_sl=90.0, take_profit=0, bars=bars,
                  cfg=_ds_cfg(dead_signal_exit_minutes=62))
    assert rep["exit_reason"] == "PRE_CLOSE_EXIT"
    assert rep["exit_hm"] == "15:52"                      # mtc=8 窗首根, held=62 同根成立


def test_mfe_stall_exit_config_driven_and_shallow_hold_branch():
    """MFE_STALL 三重条件 + 8/04「浅亏转持有」分支: 浅亏不出场, 深亏出场。

    现网 mfe_stall_exit_enabled=False (末次触发 8/11), 但实现必须配置驱动 —
    否则跨纪年窗 (7/11–8/14, 含 31 笔 MFE_STALL) 永远无法重放, 保真度门只能读单窗。
    """
    bars = _flat_bars("09:32", "15:20")                  # peak 0.10%<0.5%, 收盘 −0.30% (浅亏)
    #   窗口刻意止于 15:20: 既不达 TIME_EXIT(6h → 15:32) 也不达 PRE_CLOSE(15:52),
    #   于是"浅亏转持有"的落点只能是 EOD_REPLAY, 读数不被别的轨接手。
    base = dict(direction="LONG", entry_price=100.0, entry_hm="09:31",
                initial_sl=90.0, take_profit=0, bars=bars)
    stall = _cfg(mfe_stall_exit_enabled=True)
    shallow = _replay(cfg=_cfg(mfe_stall_exit_enabled=True,
                               mfe_stall_exit_shallow_hold_enabled=True,
                               mfe_stall_exit_shallow_pct=1.0), **base)
    deep = _replay(cfg=stall, **base)                     # shallow_hold 默认 False ⇒ 立即退
    assert deep["exit_reason"] == "MFE_STALL_EXIT"
    assert deep["exit_hm"] == "12:01"                     # held 首次 ≥150
    assert shallow["exit_reason"] == "EOD_REPLAY"         # 浅亏转持有 → 不市价卖, 持有到日终
    off = _replay(cfg=_cfg(), **base)
    assert off["exit_reason"] == "EOD_REPLAY"             # 关闭时该轨不存在


def test_mfe_stall_shallow_boundary_is_strict():
    """亏损**恰好等于** shallow_pct 时不豁免 (实盘判据是 profit_pct > -shallow, 严格)。"""
    bars = _flat_bars("09:32", "15:20", l=98.90, c=99.00)   # 收盘 −1.00% = 边界
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                  initial_sl=90.0, take_profit=0, bars=bars,
                  cfg=_cfg(mfe_stall_exit_enabled=True,
                           mfe_stall_exit_shallow_hold_enabled=True,
                           mfe_stall_exit_shallow_pct=1.0))
    assert rep["exit_reason"] == "MFE_STALL_EXIT"


def test_time_exit_uses_gross_loss_gate_and_strict_hour_boundary():
    """TIME_EXIT: held>time_exit_hours*60 (严格) ∧ 毛口径水下。

    钉两点易错处: ① 边界是 `>` 不是 `>=` (实盘 exit_engine.py:965 同式), 差一分钟就会
    把 6h 档记成 5h59m 出场; ② 亏损门用**毛**口径 — 切净口径会把微盈仓市价卖=白跑。
    """
    bars = _flat_bars("09:32", "15:59")                  # 收盘 −0.30% (毛亏)
    losing = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                     initial_sl=90.0, take_profit=0, bars=bars, cfg=_cfg())
    assert losing["exit_reason"] == "TIME_EXIT"
    assert losing["exit_hm"] == "15:32"                  # held=361 首次 >360 (15:31 held=360 不触发)
    winning = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                      initial_sl=90.0, take_profit=0,
                      bars=_flat_bars("09:32", "15:59", h=100.60, l=100.20, c=100.40),
                      cfg=_cfg())
    assert winning["exit_reason"] == "PRE_CLOSE_EXIT"     # 盈利仓不受 TIME_EXIT 影响


def test_time_exit_hours_read_from_factor_exit_rules():
    """time_exit_hours 走 _get_factor_exit_config 命中的那一档, 不是重放器自设常量。

    below_ma20 档 = 1.0h (default 6.0h) ⇒ 同一批 bar, 换个 conditions 出场时刻就该变。
    """
    cfg = _cfg(factor_exit_rules={
        "default": {"trailing_enabled": True, "breakeven_at_pct": 0.4,
                    "drawdown_from_peak_pct": 0.8, "trailing_step_pct": 2.0,
                    "time_exit_hours": 6.0, "time_exit_only_if_loss": True,
                    "max_hold_days": 1},
        "below_ma20": {"trailing_enabled": True, "breakeven_at_pct": 0.4,
                       "drawdown_from_peak_pct": 0.8, "trailing_step_pct": 2.0,
                       "time_exit_hours": 1.0, "time_exit_only_if_loss": True,
                       "max_hold_days": 1}})
    # 用 SHORT + below_ma20 (BEARISH): _get_factor_exit_config 是**方向感知**的 (7/7 fix +
    #   P0-2 7/20) — LONG 指令带 below_ma20 会在第二轮被当作反向因子跳过而落 default,
    #   那时 fast/slow 同档, 本钉子就测不到东西了。
    bars = _flat_bars("09:32", "15:59", h=100.40, l=99.90, c=100.30)   # SHORT 毛亏 0.30%
    fast = _replay(direction="SHORT", entry_price=100.0, entry_hm="09:31",
                   initial_sl=110.0, take_profit=0, bars=bars, cfg=cfg,
                   conditions=["below_ma20"])
    slow = _replay(direction="SHORT", entry_price=100.0, entry_hm="09:31",
                   initial_sl=110.0, take_profit=0, bars=bars, cfg=cfg)
    assert fast["exit_reason"] == "TIME_EXIT" and fast["exit_hm"] == "10:32"   # 1.0h 档
    assert slow["exit_reason"] == "TIME_EXIT" and slow["exit_hm"] == "15:32"   # default 6.0h 档


def test_three_tracks_do_not_preempt_stop_loss():
    """SL 仍是最高优先级: 三轨都满足的同一根, 若 SL 先被打掉则记 SL_BREAKEVEN/SL_INITIAL。"""
    bars = _flat_bars("09:32", "15:59")
    bars[59] = _bar("10:31", 100.0, 100.10, 88.00, 89.00)   # held=60 那根同时击穿 SL=90
    rep = _replay(direction="LONG", entry_price=100.0, entry_hm="09:31",
                  initial_sl=90.0, take_profit=0, bars=bars, cfg=_ds_cfg())
    assert rep["exit_reason"] == "SL_INITIAL"               # 同根内 SL 先判 ⇒ 不是 DEAD_SIGNAL
    assert rep["exit_hm"] == "10:31"


# ══════════════════════════════════════════════════════════════════════════
# 10. 两个**必须双报**的口径参数: arm_lag_bars (订单因果性) / sample_source (观测边界)
#     为什么单独钉: 这两个参数都不是"精度旋钮", 而是**口径声明** — 任一被悄悄改回
#     单口径, 保真度门就会退化成"报那个过门的数"(宪章: 禁挑口径)。
# ══════════════════════════════════════════════════════════════════════════
def test_arm_lag_one_forbids_same_bar_arm_and_trigger():
    """lag=1: 本根把 peak 抬过武装线, **不得**用同一根的不利极值打掉刚武装的保本线。

    这是 §4.5 那 84 笔 SL_TRAIL_LOCK→SL_BREAKEVEN 误分类的机制本体: 净保本限价单是
    武装**之后**才挂出的, 不可能被武装之前发生过的价格成交。
    """
    bars = [_bar("09:32", 100.0, 100.80, 99.90, 100.05),    # 本根 peak 0.80% ≥ 武装线 0.4%
            _bar("09:33", 100.05, 100.20, 100.00, 100.10)]   # 下一根才允许武装生效
    kw = dict(direction="LONG", entry_price=100.0, entry_hm="09:31",
              initial_sl=99.0, take_profit=0, bars=bars, cfg=_cfg())
    lag0 = _replay(arm_lag_bars=0, **kw)
    lag1 = _replay(arm_lag_bars=1, **kw)
    assert lag0["exit_reason"] == "SL_BREAKEVEN" and lag0["exit_hm"] == "09:32"  # 武装即死
    assert lag1["exit_hm"] == "09:33"                        # 同根不触发, 顺延到下一根
    assert lag1["exit_reason"] == "SL_BREAKEVEN"
    # 观测口径不受 arm_lag 影响: 它约束"哪张止损单在场", 不是"价格到过哪"
    assert lag0["mfe_pct"] == pytest.approx(lag1["mfe_pct"]) == pytest.approx(0.80)
    assert lag0["arm_lag_bars"] == 0 and lag1["arm_lag_bars"] == 1   # 输出自描述


def test_arm_lag_default_is_the_causal_bound():
    """默认值必须是 1 (因果下界) — 默认乐观会让"忘了传参"的调用方静默拿到上界读数。"""
    import inspect
    sig = inspect.signature(R.replay_exit)
    assert sig.parameters["arm_lag_bars"].default == 1
    assert sig.parameters["sample_source"].default == "bar_extreme"


def test_sample_source_bracket_changes_reason_and_price():
    """同一批 bar: 影线武装 (bar_extreme) → SL_BREAKEVEN@限价地板; 收盘未武装 (bar_close)
    → SL_INITIAL@bar 不利极值。这就是 USAR 9/4 那类"幻影保本"的机制钉子。"""
    bars = [_bar("09:32", 100.0, 100.80, 100.10, 100.20)]      # 影线 0.80%, 收盘仅 0.20%
    bars += [_bar(f"{(573 + i) // 60:02d}:{(573 + i) % 60:02d}", 100.20, 100.30, 100.20, 100.25)
             for i in range(13)]                                 # 09:33–09:45 平盘 (收盘 0.25%)
    bars.append(_bar("09:47", 100.10, 100.10, 98.50, 98.60))     # 崩盘根 (冷却窗已过)
    kw = dict(direction="LONG", entry_price=100.0, entry_hm="09:31",
              initial_sl=99.0, take_profit=0, bars=bars, cfg=_cfg())
    up = _replay(sample_source="bar_extreme", **kw)
    lo = _replay(sample_source="bar_close", **kw)
    assert up["exit_reason"] == "SL_BREAKEVEN" and up["exit_price"] == pytest.approx(100.15)
    # 下界口径的填充价 = **收盘** 98.60, 不是影线 98.50 — bar_close 是"只看到收盘档"的
    #   观测模型, SL 判据与成交价必须同源, 否则等于偷偷混用上界信息。
    assert lo["exit_reason"] == "SL_INITIAL" and lo["exit_price"] == pytest.approx(98.60)
    # 区间性质: 上界的 peak 观测 ≥ 下界 (实盘 10s 轮询真值必落两者之间)
    assert up["mfe_pct"] > lo["mfe_pct"]
    assert up["sample_source"] == "bar_extreme" and lo["sample_source"] == "bar_close"


def test_sample_source_rejects_unknown_caliber():
    """口径名写错必须炸 — 静默回落会让"双报"变成同一口径报两次。"""
    bars = [_bar("09:32", 100.0, 100.1, 99.9, 100.0)]
    with pytest.raises(ValueError):
        _replay(direction="LONG", entry_price=100.0, entry_hm="09:31", initial_sl=99.0,
                take_profit=0, bars=bars, cfg=_cfg(), sample_source="poll_10s")


def test_sample_source_bar_close_uses_close_for_sl_and_tp():
    """bar_close 下 SL/TP 也按收盘判 (同一观测模型贯穿), 不是只改 peak。"""
    # TP=100.50: 影线到 100.80 但收盘只 100.20 ⇒ 上界判 TAKE_PROFIT, 下界不判
    bars = [_bar("09:32", 100.0, 100.80, 100.10, 100.20),
            _bar("09:33", 100.20, 100.30, 100.15, 100.25)]
    kw = dict(direction="LONG", entry_price=100.0, entry_hm="09:31",
              initial_sl=99.0, take_profit=100.50, bars=bars,
              cfg=_cfg(min_tp_rr=0.1))
    assert _replay(sample_source="bar_extreme", **kw)["exit_reason"] == "TAKE_PROFIT"
    assert _replay(sample_source="bar_close", **kw)["exit_reason"] == "EOD_REPLAY"
