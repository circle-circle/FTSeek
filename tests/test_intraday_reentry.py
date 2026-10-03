"""intraday_reentry.evaluate_candidate 纯函数单测 (2026-08-06)."""
from app.playbook.intraday_reentry import _DEFAULTS, evaluate_candidate


def _cfg(**over):
    c = dict(_DEFAULTS)
    c.update(over)
    return c


VWAP = 100.0
# LONG 回踩带: dev ∈ [-0.3%, +1.0%] → price ∈ [99.7, 101.0]
# SHORT 回踩带: dev ∈ [-1.0%, +0.3%] → price ∈ [99.0, 100.3]
GOOD = dict(slope_5m=0.2, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0)


def test_long_pass_in_band_turned_up():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **GOOD)
    assert ok, reasons


def test_long_pass_at_band_edges():
    for price in (99.7, 101.0):
        ok, reasons = evaluate_candidate("BUY", price, VWAP, cfg=_cfg(), **GOOD)
        assert ok, (price, reasons)


def test_long_reject_extended_above_band():
    ok, reasons = evaluate_candidate("BUY", 101.5, VWAP, cfg=_cfg(), **GOOD)
    assert not ok and any("vwap_dev" in r for r in reasons)


def test_long_reject_broken_below_band():
    ok, reasons = evaluate_candidate("BUY", 99.5, VWAP, cfg=_cfg(), **GOOD)
    assert not ok and any("vwap_dev" in r for r in reasons)


def test_long_reject_slope_not_turned():
    for slope in (0.0, -0.3):
        ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": slope})
        assert not ok and any("slope_5m" in r for r in reasons)


def test_short_pass_symmetric():
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 99.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": -0.2})
    assert ok, reasons


def test_short_reject_slope_up():
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 99.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": 0.1})
    assert not ok and any("slope_5m" in r for r in reasons)


def test_short_reject_above_band():
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "slope_5m": -0.2})
    assert not ok and any("vwap_dev" in r for r in reasons)


def test_missing_indicators_reject_no_failopen():
    for kwargs in (dict(slope_5m=None), dict(atr_pct=None), dict(vol_ratio=None), dict(spread_bps=None)):
        merged = {**GOOD, **kwargs}
        ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **merged)
        assert not ok, kwargs
        assert reasons


def test_wide_spread_reject():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "spread_bps": 40.0})
    assert not ok and any("spread_bps" in r for r in reasons)


def test_low_atr_reject():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "atr_pct": 1.0})
    assert not ok and any("atr_pct" in r for r in reasons)


def test_low_vol_ratio_reject():
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=_cfg(), **{**GOOD, "vol_ratio": 0.5})
    assert not ok and any("vol_ratio" in r for r in reasons)


def test_zero_price_or_vwap_reject():
    for price, vwap in ((0, VWAP), (100.0, 0)):
        ok, reasons = evaluate_candidate("BUY", price, vwap, cfg=_cfg(), **GOOD)
        assert not ok and reasons == ["no_price_or_vwap"]


# ── vwap_band_long_high_pct 拆键闸 (PROPOSAL-20260906 R1, 2026-09-06 用户裁决直接生效) ──
# 证据: LONG dev>0 追高桶 9 笔 −96.95 (唯一出血桶) vs dev≤0 回踩桶 +24.24。
# 不变量: LONG-only (SHORT 共享带零变) / 缺键回退 hi=旧行为恒等 / 回滚=删键或置 1.0。

def test_long_chase_rejected_with_zero_long_high():
    cfg = _cfg(vwap_band_long_high_pct=0.0)
    # dev=+0.5% 追高 → 拒 (现役出血桶形态)
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=cfg, **GOOD)
    assert not ok and any("vwap_dev" in r for r in reasons)
    # dev=0.0 (贴 VWAP 不越位) → 边界放行
    ok, reasons = evaluate_candidate("BUY", 100.0, VWAP, cfg=cfg, **GOOD)
    assert ok, reasons


def test_long_pullback_still_passes_with_zero_long_high():
    # dev=−0.1% 回踩区 (策略论点形态) → 闸不误伤
    cfg = _cfg(vwap_band_long_high_pct=0.0)
    ok, reasons = evaluate_candidate("BUY", 99.9, VWAP, cfg=cfg, **GOOD)
    assert ok, reasons


def test_short_unaffected_by_long_high_key():
    # SHORT 带 [−hi,+lo]=[−1.0,+0.3] 由共享键决定: dev=−0.5 带内 → 放行,
    # 即使 LONG 专用键=0.0 (拆键不倒置 SHORT 语义, SNDK dev=−0.84 型不受影响)
    cfg = _cfg(vwap_band_long_high_pct=0.0)
    ok, reasons = evaluate_candidate(
        "SELL_SHORT", 99.5, VWAP, cfg=cfg, **{**GOOD, "slope_5m": -0.2})
    assert ok, reasons


def test_missing_long_high_key_fallback_preserves_old_behavior():
    # 手工 cfg 缺新键 → 回退 hi=1.0: dev=+0.5 仍放行 (与拆键前恒等)
    cfg = _cfg()
    del cfg["vwap_band_long_high_pct"]
    ok, reasons = evaluate_candidate("BUY", 100.5, VWAP, cfg=cfg, **GOOD)
    assert ok, reasons


# ── overnight_loser_exempt (2026-08-06 用户裁决: 再入场水下仓首夜豁免 EOD) ──

from datetime import timedelta

import app.playbook.intraday_reentry as ir


def _cfg_on(**over):
    return _cfg(overnight_hold_loser_enabled=True, **over)


def _today_entry():
    from app.utils.timezone import now_et
    return now_et().replace(hour=11, minute=30).isoformat()


def _yesterday_entry():
    from app.utils.timezone import now_et
    return (now_et() - timedelta(days=1)).isoformat()


YES = lambda sym, date: True   # noqa: E731
NO = lambda sym, date: False   # noqa: E731


def test_overnight_exempt_positive(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    assert ir.overnight_loser_exempt("AVGO", _today_entry(), -0.8, is_reentry=YES)


def test_overnight_flag_disabled(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg)  # _DEFAULTS: False
    assert not ir.overnight_loser_exempt("AVGO", _today_entry(), -0.8, is_reentry=YES)


def test_overnight_profitable_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    for pnl in (0.0, 0.5, 2.3):
        assert not ir.overnight_loser_exempt("AVGO", _today_entry(), pnl, is_reentry=YES)


def test_overnight_second_night_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    assert not ir.overnight_loser_exempt("AVGO", _yesterday_entry(), -0.8, is_reentry=YES)


# ── quick_sl_pct (2026-08-07 用户裁决 SL 放宽/ATR 相对; 2026-10-03 DEB-20261003 quick_tp 退役) ──

def test_quick_sl_mid_range_atr():
    # TSM 类 ATR 3%: SL=0.6×3=1.8→floor 2.0
    assert ir.quick_sl_pct(3.0, _cfg()) == 2.0


def test_quick_sl_high_atr_scaling():
    # AVGO 类 ATR 5%: SL=3.0 (clamp 内, 体现 ATR 相对)
    assert ir.quick_sl_pct(5.0, _cfg()) == 3.0


def test_quick_sl_low_atr_floor():
    # AAPL 类 ATR 1.8%: SL=1.08→floor 2.0
    assert ir.quick_sl_pct(1.8, _cfg()) == 2.0


def test_quick_sl_extreme_atr_ceil():
    assert ir.quick_sl_pct(12.0, _cfg()) == 3.5   # 触顶


def test_quick_sl_missing_atr_fallback():
    for bad in (None, 0, -1.5, "x"):
        assert ir.quick_sl_pct(bad, _cfg()) is None


def test_quick_sl_per_symbol_differentiation():
    # 同一配置, 不同标的 ATR → 不同 SL (用户要求"基于不同的标的股")
    assert ir.quick_sl_pct(5.0, _cfg()) > ir.quick_sl_pct(1.8, _cfg())


# ── quick_tp 退役契约 (DEB-20261003 用户裁「按建议执行」, 行为中性版) ──
# 原 DEB §五 守卫「断言 fallback TP RR<1.5 被门禁吞」前提错误: tp_rr=1.6 ≥ 门 1.5,
# sl_pct×tp_rr 填充会激活未验证的固定 TP 腿。已裁行为中性: ATR 有效 ⇒ TP=0。
# 以下守卫钉死「零交易行为变更」的实现选择。

def test_quick_tp_params_gone_from_defaults():
    """三键从 _DEFAULTS 物理删除 (复活须新 DEB + 用户裁 + 影子先行)。"""
    for k in ("quick_tp_atr_mult", "quick_tp_floor_pct", "quick_tp_ceil_pct"):
        assert k not in ir._DEFAULTS


def test_arm_atr_valid_take_profit_zero(monkeypatch):
    """ATR 有效 ⇒ take_profit=0 (无固定 TP 腿, exit_engine 规则1.5 要求 TP>0 才进)."""
    fake = _FakePDBCapture()
    _patch_pdb(monkeypatch, lambda: fake)
    for direction in ("BUY", "SELL_SHORT"):
        ir._arm_instruction("US.AMD", direction, 100.0, _cfg(), 1451, atr_pct=5.0)
        assert fake.saved["take_profit"] == 0.0, direction
        # SL 仍走 quick_sl (放宽给跨夜空间, 未动)
        assert fake.saved["stop_loss"] == (97.0 if direction == "BUY" else 103.0)


def test_arm_atr_missing_tp_unchanged(monkeypatch):
    """ATR 缺失回退路径输出不变 (sl_pct×tp_rr=RR1.6, 该 TP 腿历史上就是活的 — 不动)."""
    fake = _FakePDBCapture()
    _patch_pdb(monkeypatch, lambda: fake)
    ir._arm_instruction("US.AMD", "BUY", 100.0, {"sl_pct": 1.5, "tp_rr": 1.6}, 1451,
                        atr_pct=None)
    assert fake.saved["stop_loss"] == 98.5          # SL 1.5%
    assert fake.saved["take_profit"] == 102.4       # TP = 1.5×1.6 = 2.4%


def test_overnight_non_reentry_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    assert not ir.overnight_loser_exempt("AVGO", _today_entry(), -0.8, is_reentry=NO)


def test_overnight_bad_timestamp_not_exempt(monkeypatch):
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    for bad in ("", "not-a-date", None):
        assert not ir.overnight_loser_exempt("AVGO", bad, -0.8, is_reentry=YES)


def test_overnight_naive_timestamp_treated_as_et(monkeypatch):
    # trade_records 历史行为: entry_time 可能无时区 — 按 ET 当日处理不豁免错位
    monkeypatch.setattr(ir, "_cfg", _cfg_on)
    from app.utils.timezone import now_et
    naive_today = now_et().replace(hour=11, minute=30, tzinfo=None).isoformat()
    assert ir.overnight_loser_exempt("AVGO", naive_today, -0.8, is_reentry=YES)


# ── prior_exit_reenterable (2026-08-07: exit_reason 替代纯 pnl 代理) ──

def test_prior_hard_stop_reasons_excluded():
    for r in ("SL_INITIAL", "SL_ATR_STOP", "SL_FIXED_PCT", "OVERNIGHT_GAP_STOP"):
        assert not ir.prior_exit_reenterable(r, -0.003, 1.0)   # 浅亏硬止损也排
        assert not ir.prior_exit_reenterable(r, -0.05, 1.0)


def test_prior_benign_reasons_allowed_regardless_of_pnl():
    for r in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "TAKE_PROFIT", "TRAILING_STOP",
              "TRAILING_STOP_BY_MAIN", "PRE_CLOSE_EXIT", "EOD_FORCE_CLOSE"):
        assert ir.prior_exit_reenterable(r, 0.012, 1.0)
        assert ir.prior_exit_reenterable(r, -0.015, 1.0)  # 良性深亏也放行 (如 BE 滑点)


def test_prior_zero_wr_and_ops_reasons_excluded():
    # 数据依据 (08-07 全量 trade_records, pnl_net): MFE_STALL 28笔 0%WR -$1169,
    #   TIME_EXIT 33笔 0%WR -$1151 → 0胜率纯亏损禁入; 操作类 (GHOST/EXIT_MONITOR/
    #   OKR_AUDIT) 非 thesis 信号 → 禁入 (fail-closed).
    for r in ("MFE_STALL_EXIT", "TIME_EXIT", "BROKER_GHOST_CLOSE",
              "EXIT_MONITOR_CLOSE", "FORCED_CLOSE_OKR_AUDIT"):
        assert not ir.prior_exit_reenterable(r, -0.003, 1.0)
        assert not ir.prior_exit_reenterable(r, 0.02, 1.0)  # 0-WR/操作类: 盈利也排


def test_prior_unknown_reasons_fall_back_to_pnl():
    # 真正未知 reason → pnl 代理: -1% 内放行, 超则排
    for r in (None, "", "WEIRD_UNKNOWN_REASON"):
        assert ir.prior_exit_reenterable(r, -0.008, 1.0)
        assert not ir.prior_exit_reenterable(r, -0.012, 1.0)


# ── cumulative_exposure_ok (2026-08-07 审查#1: 同标的当日累计暴露护栏) ──

def test_cumulative_exposure_ok_basic():
    # equity=100k, max_pos_pct=0.06, mult=1.5 → 护栏线 = 0.5×0.06×100k = $3,000
    assert ir.cumulative_exposure_ok(2999, 0.06, 100_000, 1.5)
    assert not ir.cumulative_exposure_ok(3001, 0.06, 100_000, 1.5)
    assert ir.cumulative_exposure_ok(3000, 0.06, 100_000, 1.5)  # 边界: ≤


def test_cumulative_exposure_ok_edge():
    assert ir.cumulative_exposure_ok(0, 0.06, 100_000, 1.5)      # 首笔名义 0 → 放行
    assert ir.cumulative_exposure_ok(999_999, 0.06, 0, 1.5)      # equity=0 → fail-open 放行
    assert ir.cumulative_exposure_ok(999_999, 0.0, 100_000, 1.5)  # max_pos_pct=0 → fail-open
    # mult 越大越宽: 2.0 允许首笔 ≤ 6%×equity
    assert ir.cumulative_exposure_ok(5999, 0.06, 100_000, 2.0)
    assert not ir.cumulative_exposure_ok(6001, 0.06, 100_000, 2.0)


def test_prior_pnl_boundary():
    assert ir.prior_exit_reenterable(None, -0.0099, 1.0)
    assert not ir.prior_exit_reenterable(None, -0.01, 1.0)   # 恰好 -1% 排 (非严格大于)


# ── choose_direction (2026-08-12 用户裁决: 方向 slope_5m 顺势裁决, 直接落地) ──

def test_choose_direction_adaptive_flip():
    # 前笔 LONG 但 slope 转负 → 顺势改空 (adaptive 标记)
    assert ir.choose_direction("LONG", -0.15, True) == ("SELL_SHORT", "adaptive")
    # 前笔 SHORT 但 slope 转正 → 顺势改多
    assert ir.choose_direction("SHORT", 0.22, True) == ("BUY", "adaptive")


def test_choose_direction_adaptive_same_as_prior():
    # slope 与前笔同向 → 方向不变, 标记 inherited (无改向)
    assert ir.choose_direction("LONG", 0.3, True) == ("BUY", "inherited")
    assert ir.choose_direction("SHORT", -0.3, True) == ("SELL_SHORT", "inherited")
    # 别名归一化
    assert ir.choose_direction("BUY", 0.1, True)[0] == "BUY"
    assert ir.choose_direction("SELL_SHORT", -0.1, True)[0] == "SELL_SHORT"


def test_choose_direction_fallback_inherit():
    # slope 缺失 / 为 0 / 开关关闭 → 继承前笔 (与改动前行为一致)
    assert ir.choose_direction("LONG", None, True) == ("BUY", "inherited")
    assert ir.choose_direction("SHORT", None, True) == ("SELL_SHORT", "inherited")
    assert ir.choose_direction("LONG", 0.0, True) == ("BUY", "inherited")
    assert ir.choose_direction("LONG", -0.5, False) == ("BUY", "inherited")
    assert ir.choose_direction("SHORT", 0.5, False) == ("SELL_SHORT", "inherited")



# ── _day_first_direction (2026-08-27 当日反手硬闸数据源, 8/26 LLY 教训) ──

class _FakeRowResult:
    def __init__(self, row): self._row = row
    def fetchone(self): return self._row


class _FakeConn:
    def __init__(self, row): self._row = row
    def execute(self, sql, params): return _FakeRowResult(self._row)


class _FakePDB:
    def __init__(self, row=None, raise_err=False):
        self._row, self._raise = row, raise_err
    def _conn(self):
        if self._raise:
            raise RuntimeError("db down")
        return _FakeConn(self._row)


def _patch_pdb(monkeypatch, fake):
    import app.playbook.db as pdb_mod
    monkeypatch.setattr(pdb_mod, "PlaybookDB", fake)


def test_day_first_direction_returns_non_reentry_first(monkeypatch):
    # 第一轮是 DA 指令 (model_used≠reentry) → 返回其方向, 供反手闸比对
    _patch_pdb(monkeypatch, lambda: _FakePDB(row=("BUY", "qwen3.7-max")))
    assert ir._day_first_direction("2026-08-27", "US.LLY") == "BUY"


def test_day_first_direction_ignores_reentry_rows(monkeypatch):
    # ORDER BY id ASC 取到的若是 reentry 行自身族 → None (不拿再入场当第一轮基准)
    _patch_pdb(monkeypatch, lambda: _FakePDB(row=("SELL_SHORT", "intraday_reentry_v1")))
    assert ir._day_first_direction("2026-08-27", "US.LLY") is None


def test_day_first_direction_fail_open(monkeypatch):
    # DB 异常 / 当日无第一轮 → None → 不拦 (fail-open, 对齐其他证伪门约定)
    _patch_pdb(monkeypatch, lambda: _FakePDB(raise_err=True))
    assert ir._day_first_direction("2026-08-27", "US.LLY") is None
    _patch_pdb(monkeypatch, lambda: _FakePDB(row=None))
    assert ir._day_first_direction("2026-08-27", "US.LLY") is None


# ── _capital_flow_today_index 尾部增量缓存 (2026-08-30 性能修复的回归钉) ──
# fdbfcc78 引入。钉四个分支: 冷启动 / 热路径增量 / 轮转缩小回冷启动 / 跨日缓存重置,
# 外加与旧全量读算法的行为等价钉。钉子动机: 该函数在盘中热路径 (78次/日),
# 2026-08-30 复核发现原 commit 注释的容量测算错 6 倍 (窗口 1MB < 实测单日 1.88MB),
# 已改 4MB 并以本组钉子锁行为。

import json as _json


def _cf_row(symbol, item_time, large):
    # large>0 → INFLOW (super+big 同号写入); large<0 → OUTFLOW; ==0 不给方向
    v = large / 2.0
    return _json.dumps({"symbol": symbol, "item_time": item_time,
                        "super_in_flow": v, "big_in_flow": v})


def _cf_setup(monkeypatch, tmp_path, lines, today="2026-08-28"):
    p = tmp_path / "futu_capital_flow.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(ir, "_CF_PATH", p)
    monkeypatch.setattr(ir, "_today_et_str", lambda: today)
    ir._cf_cache.clear()
    return p


def test_cf_index_cold_start_full_day(monkeypatch, tmp_path):
    # 冷启动: 当日多条同标的, 后写覆盖 (最新一条定方向); 非当日/==0 不给方向
    _cf_setup(monkeypatch, tmp_path, [
        _cf_row("US.AAPL", "2026-08-27T10:00:00", 100),   # 昨日 → 忽略
        _cf_row("US.AAPL", "2026-08-28T10:00:00", 100),   # 早段 IN
        _cf_row("US.AAPL", "2026-08-28T11:00:00", -50),   # 覆盖为 OUT
        _cf_row("US.MSFT", "2026-08-28T10:30:00", 0),     # ==0 → 无方向
        _cf_row("US.BAD", "{not json", 1),                # 坏行 → 忽略
    ])
    assert ir._capital_flow_today_index() == {"US.AAPL": "OUTFLOW"}


def test_cf_index_hot_path_incremental(monkeypatch, tmp_path):
    # 热路径: 首调冷启动, 追加新行后二调只解析增量, 新旧合并正确
    p = _cf_setup(monkeypatch, tmp_path, [_cf_row("US.AAPL", "2026-08-28T10:00:00", 100)])
    assert ir._capital_flow_today_index() == {"US.AAPL": "INFLOW"}
    with open(p, "a", encoding="utf-8") as f:
        f.write(_cf_row("US.NBIS", "2026-08-28T11:00:00", -80) + "\n")
        f.write(_cf_row("US.AAPL", "2026-08-28T12:00:00", -30) + "\n")
    out = ir._capital_flow_today_index()
    assert out == {"US.AAPL": "OUTFLOW", "US.NBIS": "OUTFLOW"}


def test_cf_index_rotation_shrink_falls_back_to_cold(monkeypatch, tmp_path):
    # 轮转/缩小: 文件变小 → 缓存 size > 实际 size → 回冷启动重扫, 不残留旧索引
    p = _cf_setup(monkeypatch, tmp_path, [_cf_row("US.AAPL", "2026-08-28T10:00:00", 100)])
    assert ir._capital_flow_today_index() == {"US.AAPL": "INFLOW"}
    p.write_text(_cf_row("US.TSLA", "2026-08-28T13:00:00", -70) + "\n", encoding="utf-8")
    assert ir._capital_flow_today_index() == {"US.TSLA": "OUTFLOW"}


def test_cf_index_cross_day_cache_reset(monkeypatch, tmp_path):
    # 跨日: 换日后缓存 miss → 冷启动, 只采信新当日记录 (防昨日方向串场)
    p = _cf_setup(monkeypatch, tmp_path, [_cf_row("US.AAPL", "2026-08-28T10:00:00", 100)])
    assert ir._capital_flow_today_index() == {"US.AAPL": "INFLOW"}
    with open(p, "a", encoding="utf-8") as f:
        f.write(_cf_row("US.AAPL", "2026-08-31T10:00:00", -40) + "\n")
    monkeypatch.setattr(ir, "_today_et_str", lambda: "2026-08-31")
    assert ir._capital_flow_today_index() == {"US.AAPL": "OUTFLOW"}


def test_cf_index_equivalence_with_full_read(monkeypatch, tmp_path):
    # 行为等价钉: 窗口 ≥ 当日全量时, 与旧全量读算法逐符号一致 (2026-08-30 已用
    # 生产 95k 行文件独立复现 23/23 一致, 本钉用合成数据把该性质固化进回归)
    import random
    rng = random.Random(20260830)
    syms = [f"US.S{i:02d}" for i in range(20)]
    lines = []
    for day in ("2026-08-27", "2026-08-28"):
        for hour in range(9, 16):
            for s in syms:
                lines.append(_cf_row(s, f"{day}T{hour:02d}:30:00", rng.choice([0, 10, -10, 40, -40, 0.0])))
    _cf_setup(monkeypatch, tmp_path, lines, today="2026-08-28")

    ref = {}
    for l in lines:
        ir._cf_apply_line(l, "2026-08-28", ref)
    assert ir._capital_flow_today_index() == ref


def test_cf_index_partial_line_not_lost(monkeypatch, tmp_path):
    # 写入方写到一半时读取: 半行不入缓存不误解析; 写完后下一轮必须能补读到完整行
    # (旧实现把半行字节也计入缓存 offset, 写完后从 offset 续读 → 该行永久丢失)
    p = _cf_setup(monkeypatch, tmp_path, [_cf_row("US.AAPL", "2026-08-28T10:00:00", 100)])
    assert ir._capital_flow_today_index() == {"US.AAPL": "INFLOW"}
    full_line = _cf_row("US.LLY", "2026-08-28T11:00:00", -60)
    with open(p, "a", encoding="utf-8") as f:
        f.write(full_line[:20])           # 写到一半
    assert ir._capital_flow_today_index() == {"US.AAPL": "INFLOW"}   # 半行不可见
    with open(p, "a", encoding="utf-8") as f:
        f.write(full_line[20:] + "\n")    # 写完剩余部分
    assert ir._capital_flow_today_index() == {"US.AAPL": "INFLOW", "US.LLY": "OUTFLOW"}


# ── _arm_instruction entry_metrics 埋点 (PROPOSAL-20260906 R2①, 2026-09-06) ──
# 军规"新增落库字段必带契约测试断言": 钉透传结构/键集/零行为变更。

class _FakePDBCapture:
    """捕获 save_instruction 入参; orig 快照查询返回 None (归档失败不阻断分支)。"""

    def __init__(self):
        self.saved = None

    def _conn(self):
        return _FakeConn(None)

    def save_instruction(self, inst):
        self.saved = inst
        return 9001


def _arm_cfg():
    return {"sl_pct": 1.5, "tp_rr": 1.6}


def _metrics():
    return {
        "slope_5m_pct": 0.13, "vwap_dev_pct": -0.28, "vol_ratio": 1.52,
        "spread_bps": 3.7, "atr_pct": 4.1, "direction_mode": "inherited",
        "falsif_inputs": {"capital_flow_direction": "INFLOW",
                          "short_percent": None, "insider_signal": "buy",
                          "analyst_trend": None, "days_to_earnings": 5,
                          "sentiment_score": 0.2, "sec_event_negative": False,
                          "sec_event_13dg": False, "social_sentiment_score": None,
                          "ticker_sentiment": "bullish"},
    }


def test_arm_instruction_entry_metrics_pass_through(monkeypatch):
    fake = _FakePDBCapture()
    _patch_pdb(monkeypatch, lambda: fake)
    m = _metrics()
    new_id = ir._arm_instruction("US.AMD", "BUY", 100.0, _arm_cfg(), 1451,
                                 atr_pct=None, entry_metrics=m)
    assert new_id == 9001
    ri = fake.saved["raw_instruction"]
    assert ri["source"] == "intraday_reentry" and ri["reentry_of_trade"] == 1451
    # 透传不改写 (单位口径由生产侧注释声明: pct=百分数, spread_bps=万分数)
    assert ri["entry_metrics"] == m
    assert ri["entry_metrics"]["vwap_dev_pct"] == -0.28
    assert set(ri["entry_metrics"]["falsif_inputs"]) == {
        "capital_flow_direction", "short_percent", "insider_signal",
        "analyst_trend", "days_to_earnings", "sentiment_score",
        "sec_event_negative", "sec_event_13dg", "social_sentiment_score",
        "ticker_sentiment"}


def test_arm_instruction_entry_metrics_default_none(monkeypatch):
    fake = _FakePDBCapture()
    _patch_pdb(monkeypatch, lambda: fake)
    new_id = ir._arm_instruction("US.AMD", "BUY", 100.0, _arm_cfg(), 1451)
    assert new_id == 9001
    assert fake.saved["raw_instruction"]["entry_metrics"] is None


def test_arm_instruction_metrics_zero_behavior_change(monkeypatch):
    # 埋点不改变 SL/TP/方向/decision_id 形态 (record-only 纪律钉)
    f1, f2 = _FakePDBCapture(), _FakePDBCapture()
    _patch_pdb(monkeypatch, lambda: f1)
    ir._arm_instruction("US.AMD", "BUY", 100.0, _arm_cfg(), 1451)
    _patch_pdb(monkeypatch, lambda: f2)
    ir._arm_instruction("US.AMD", "BUY", 100.0, _arm_cfg(), 1451,
                        entry_metrics=_metrics())
    for k in ("direction", "entry_price", "stop_loss", "take_profit",
                  "confidence", "thesis", "model_used"):
        assert f1.saved[k] == f2.saved[k]
    assert f1.saved["decision_id"].startswith("intraday_reentry-1451-")
