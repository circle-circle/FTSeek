"""settle_shadow_portfolio.settle 单测 (2026-08-09)."""
from scripts.settle_shadow_portfolio import settle


def _bars(prices, start_hm=10, start_min=0):
    """从收盘价序列合成 1-min bars ([ts,o,h,l,c]), ts 从 start 时刻逐分钟."""
    out = []
    for i, c in enumerate(prices):
        mm = start_min + i
        hh = start_hm + mm // 60
        mm = mm % 60
        ts = f"2026-08-08 {hh:02d}:{mm:02d}:00"
        out.append([ts, c, c * 1.001, c * 0.999, c])
    return out


def test_sl_hit_long():
    # 初始 SL 1.5% → 98.5; 价格跌到 ~98.4 (bar close 98.45, 采样点穿 98.5)
    bars = _bars([100, 99.5, 99.0, 98.45, 98.4])
    px, reason, _ = settle("LONG", 100.0, bars, 1.5, None, "10:00")
    assert reason == "SL_TIGHT" and px <= 98.5


def test_tp_hit_long():
    bars = _bars([100, 100.5, 101.0, 101.6, 102.1])
    px, reason, _ = settle("LONG", 100.0, bars, 1.5, 102.0, "10:00")
    assert reason == "TP" and px >= 102.0


def test_breakeven_trail_exit():
    # mfe 到 1.0% 激活保本 (线 100.4); 回落至 100.3 触发
    bars = _bars([100, 100.4, 100.8, 101.0, 100.6, 100.3])
    px, reason, mfe = settle("LONG", 100.0, bars, 1.5, None, "10:00")
    assert reason == "BE_TRAIL" and 100.2 <= px <= 100.45
    assert mfe >= 0.5


def test_dead_signal_exit():
    # 65 根 bar 全部在 ±0.1% 内 → 第 60 bar 触发死信号
    prices = [100 + (0.05 if i % 2 else -0.05) for i in range(65)]
    bars = _bars(prices)
    px, reason, mfe = settle("LONG", 100.0, bars, 1.5, None, "10:00")
    assert reason == "DEAD_SIGNAL" and mfe < 0.2


def test_eod_fallback():
    # 上行激活保本 (h 越过 0.5% mfe) 但之后任何采样点不触及 BE/trail 线 → EOD 收最后 close
    bars = _bars([100, 100.3, 100.7, 100.85, 100.95, 101.05])
    px, reason, _ = settle("LONG", 100.0, bars, 1.5, None, "10:00")
    assert reason == "EOD" and px == 101.05


def test_short_sl_symmetric():
    bars = _bars([100, 100.6, 101.6])   # SHORT: SL 1.5% → 101.5, 价格上行穿线
    px, reason, _ = settle("SHORT", 100.0, bars, 1.5, None, "10:00")
    assert reason == "SL_TIGHT" and px >= 101.5


def test_entry_hm_filter():
    # entry_hm=10:03 → 前 3 根 bar 不参与
    bars = _bars([98.0, 98.2, 98.3, 99.0, 99.5])
    px, reason, _ = settle("LONG", 99.0, bars, 1.5, None, "10:03")
    assert reason == "EOD" and px == 99.5   # 没看到前面的低价 bar
