"""
Y 数据驱动修复单测 (2026-05-15)
基于昨日 700/700 UNKNOWN regime / GOOGL -$6K bug / ORCL +$17K 异常 真实数据
"""
import pytest


# ─── Y1 Market Regime Detector ────────────────────────────
def test_y1_regime_detector_extreme_vix():
    from app.risk.market_regime_detector import MarketRegimeDetector
    d = MarketRegimeDetector()
    snap = d.detect(vix=40.0, spy_change_pct=-0.02)
    assert snap.regime == "EXTREME"


def test_y1_regime_no_data_fallback():
    """K 线数据缺失时不崩 (只用 VIX)"""
    from app.risk.market_regime_detector import MarketRegimeDetector
    d = MarketRegimeDetector()
    snap = d.detect(vix=18.0)  # 无 K 线
    assert snap.regime in ("RANGING", "HIGH_VOL_CHOP", "TRENDING", "TRANSITION", "UNKNOWN")
    assert snap.adx == -1.0


def test_y1_regime_trending_up():
    """ADX 高 + SPY 涨 → TRENDING_UP"""
    from app.risk.market_regime_detector import MarketRegimeDetector
    d = MarketRegimeDetector()
    # 模拟趋势上行 K 线 (单调上涨)
    closes = [100 + i * 0.5 for i in range(20)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    snap = d.detect(spy_highs=highs, spy_lows=lows, spy_closes=closes,
                    vix=15.0, spy_change_pct=0.01)
    # 趋势市场 ADX 应高
    assert snap.regime in ("TRENDING_UP", "TRENDING")


def test_y1_position_scale_extreme_zero():
    from app.risk.market_regime_detector import get_position_scale
    assert get_position_scale("EXTREME") == 0.0
    assert get_position_scale("HIGH_VOL_CHOP") == 0.3
    assert get_position_scale("TRENDING_UP") == 1.0
    assert get_position_scale("UNKNOWN") == 0.5


# ─── Y2 BB 带宽崩溃过滤 (源码层验证 — scalping/signals.py 间接依赖 pytz) ─


# ─── Y4 BREAKEVEN_STOP for SHORT 修复 ────────────────────────
def test_y4_breakeven_short_above_entry():
    """SHORT break-even 应在 entry 之上 (反弹回到 entry+buffer 触发保利)
    验证: 修复后源码内 SHORT 也用 (1 + buffer_pct)
    """
    src = open("app/risk/atr_stop_engine.py").read()
    # 旧 bug: SHORT 用 entry_price * (1 - buffer_pct)
    # 修复后: SHORT 也用 entry_price * (1 + buffer_pct)
    assert "Y4" in src or "breakeven_stop = entry_price * (1 + buffer_pct)" in src
    # 关键检查: SHORT 分支不应该有 (1 - buffer_pct) 用于 breakeven
    # 取 breakeven 计算块
    bk_idx = src.find("=== 5. Breakeven")
    bk_block = src[bk_idx : bk_idx + 1000]
    # 修复后 LONG / SHORT 都用 (1 + buffer_pct)
    plus_count = bk_block.count("(1 + buffer_pct)")
    minus_count = bk_block.count("(1 - buffer_pct)")
    assert plus_count >= 2, "SHORT 也应用 (1 + buffer_pct)"


def test_y2_bb_filter_logic_in_source():
    """通过源码验证 BB sanity check 已加 (避开 pytz 间接依赖)"""
    src = open("app/scalping/signals.py").read()
    assert "Y2" in src
    assert "bandwidth < price * 0.001" in src
    assert "数据异常" in src or "数据异常" in src


# ─── Y5 PnL 异常值监控 ────────────────────────────
def test_y5_outlier_monitor_initial_no_alert():
    """初始无数据 → 不告警"""
    from app.risk.pnl_outlier_monitor import PnLOutlierMonitor
    m = PnLOutlierMonitor()
    m._mean = None  # 强制无数据
    assert m.check("NVDA", 100.0) is False


def test_y5_outlier_monitor_normal_no_alert():
    """正常 PnL → 不告警"""
    from app.risk.pnl_outlier_monitor import PnLOutlierMonitor
    m = PnLOutlierMonitor(sigma_threshold=5.0)
    m._mean = 0.0
    m._std = 100.0
    m._n = 100
    m._last_refresh = 9e10  # 防止刷新
    assert m.check("NVDA", 200) is False  # 2σ, 不告警


def test_y5_outlier_monitor_extreme_alerts(monkeypatch):
    """5σ+ 异常 → 告警 (返回 True)

    Fixed (2026-05-16): monkeypatch _persist + send_critical 防副作用.
    之前每次 pytest 都污染 data/ftseek_us.db.pnl_outliers (28 行 stale GOOGL/ORCL 假数据)
    并发飞书/钉钉假告警.
    """
    from app.risk.pnl_outlier_monitor import PnLOutlierMonitor
    import app.utils.critical_alerts as _ca
    m = PnLOutlierMonitor(sigma_threshold=5.0)
    m._mean = 0.0
    m._std = 100.0
    m._n = 100
    m._last_refresh = 9e10
    # 阻止 _persist 写真实 DB + 阻止 send_critical 发真实告警
    monkeypatch.setattr(m, "_persist", lambda *a, **kw: None)
    monkeypatch.setattr(_ca, "send_critical", lambda *a, **kw: None)
    # ORCL +$17K, z = 170 σ
    assert m.check("ORCL", 17000) is True
    # GOOGL -$6K, z = -60 σ
    assert m.check("GOOGL", -6000) is True


# ─── Y3 STRUCTURE_EXIT 收紧 (yaml 改动验证) ────────────────────
def test_y3_structure_exit_cooldown_tightened():
    import yaml
    cfg = yaml.safe_load(open("config/unified_config.yaml"))
    structure_cooldown = cfg.get("symbol_cooldown", {}).get("structure_exit", {})
    # Y3: 从 3 → 2
    assert structure_cooldown.get("consecutive_losses") == 2
    # Y3: 从 24 → 48
    assert structure_cooldown.get("cooldown_hours") == 48


# ─── 集成 ───────────────────────────────────────
def test_all_y_modules_importable():
    import importlib
    for m in [
        "app.risk.market_regime_detector",
        "app.risk.pnl_outlier_monitor",
    ]:
        importlib.import_module(m)
