"""SignalQualityScorer 单元测试

Phase 1 (2026-05-31): 测试 5 维度评分器的各种场景:
  - 完美多头/空头排列 → 高置信度
  - 弱信号 → 低置信度
  - 矛盾维度 → 惩罚
  - 缺失数据 → 中性范围
  - 结果可序列化
"""
import pytest
from app.strategy.signal_quality_scorer import SignalQualityScorer, QualityResult


class _MockCtx:
    """Minimal mock of SymbolContext"""
    def __init__(self, **kwargs):
        defaults = {
            "symbol": "US.AAPL", "price": 150.0, "rsi": 45.0,
            "ma5": 152.0, "ma20": 148.0, "ma50": 145.0, "ma200": 140.0,
            "macd": 0.5, "macd_signal": 0.3, "slope_5m": 0.2,
            "volume_ratio": 1.5, "volume": 1000000, "avg_volume": 800000,
            "order_flow_imbalance": 0.2, "ticker_buy_sell_ratio": 1.1,
            "vix": 18.0, "spy_price": 520.0, "spy_ma200": 500.0,
            "market_regime": "TREND_UP", "trend": "UP",
            "vwap_deviation": 0.005, "metadata": {},
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


# ── 基本场景 ──

def test_long_perfect_alignment_high_confidence():
    """完美多头排列 + 放量 + 买方主导 → 高置信度"""
    ctx = _MockCtx(price=155, ma5=153, ma20=148, ma50=145, rsi=42,
                   volume_ratio=2.0, order_flow_imbalance=0.4)
    result = SignalQualityScorer.score(ctx, "LONG")
    assert result.confidence > 0.65, f"Expected >0.65, got {result.confidence}"
    assert result.dimensions["trend"] > 0.6
    assert result.dimensions["volume"] > 0.5


def test_short_perfect_alignment():
    """完美空头排列 + 放量 + 卖方主导 → 高置信度"""
    ctx = _MockCtx(price=140, ma5=142, ma20=148, ma50=150, rsi=58,
                   market_regime="TREND_DOWN", trend="DOWN",
                   volume_ratio=1.8, order_flow_imbalance=-0.3,
                   spy_price=490, spy_ma200=500)
    result = SignalQualityScorer.score(ctx, "SHORT")
    assert result.confidence > 0.60


def test_weak_signal_low_confidence(monkeypatch):
    """均线缠绕 + 缩量 + 高 VIX + RANGE → 置信度显著低于强信号.

    语义更新: 矛盾惩罚放宽 (-0.10→-0.03, 模拟盘), 弱信号绝对值升至 ~0.55,
    但必须仍低于 0.65 高置信门槛, 且严格低于完美排列信号.
    """
    # 密封: 权重/胜率校准从 live 数据文件热加载, 会造成分数漂移
    import app.strategy.signal_quality_scorer as sqs_mod
    monkeypatch.setattr(sqs_mod, "_load_weights", lambda: None)
    monkeypatch.setattr(sqs_mod, "_load_calibration", lambda: {})

    weak_ctx = _MockCtx(price=150, ma5=150, ma20=150, ma50=150, rsi=50,
                        macd=0.0, macd_signal=0.0, slope_5m=0.0,
                        volume_ratio=0.5, order_flow_imbalance=0.0,
                        vix=35, market_regime="RANGE")
    weak = SignalQualityScorer.score(weak_ctx, "LONG")
    assert weak.confidence < 0.60, f"Expected <0.60, got {weak.confidence}"

    strong_ctx = _MockCtx(price=155, ma5=153, ma20=148, ma50=145, rsi=42,
                          volume_ratio=2.0, order_flow_imbalance=0.4)
    strong = SignalQualityScorer.score(strong_ctx, "LONG")
    assert weak.confidence < strong.confidence, "弱信号必须低于强信号"


def test_contradicting_dimensions_penalized():
    """趋势看多但量价和资金流看空 → 矛盾惩罚"""
    ctx = _MockCtx(price=155, ma5=153, ma20=148, ma50=145,  # bullish trend
                   volume_ratio=0.5, order_flow_imbalance=-0.5,  # bearish flow
                   vix=35)
    result = SignalQualityScorer.score(ctx, "LONG")
    assert result.contradiction_penalty < 0


def test_missing_data_returns_neutral():
    """关键指标缺失 → 中性范围 (不惩罚也不奖励)"""
    ctx = _MockCtx(rsi=None, macd=None, volume_ratio=None,
                   order_flow_imbalance=None, ticker_buy_sell_ratio=None)
    result = SignalQualityScorer.score(ctx, "LONG")
    assert 0.30 <= result.confidence <= 0.70, f"Expected 0.30-0.70, got {result.confidence}"


def test_result_serializable():
    """QualityResult.to_dict() 可序列化为 dict"""
    ctx = _MockCtx()
    result = SignalQualityScorer.score(ctx, "LONG")
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "confidence" in d
    assert "dimensions" in d
    assert "confluence_bonus" in d
    assert "contradiction_penalty" in d


# ── 边界场景 ──

def test_confidence_clamped():
    """置信度不会超出 [0.10, 0.95]"""
    # Best case scenario
    ctx = _MockCtx(price=155, ma5=153, ma20=148, ma50=145, rsi=42,
                   volume_ratio=3.0, order_flow_imbalance=0.5,
                   ticker_buy_sell_ratio=1.3, vix=12,
                   market_regime="TREND_UP", trend="UP")
    result = SignalQualityScorer.score(ctx, "LONG")
    assert result.confidence <= 0.95

    # Worst case
    ctx2 = _MockCtx(price=140, ma5=142, ma20=148, ma50=150, rsi=80,
                    volume_ratio=0.3, order_flow_imbalance=-0.6,
                    vix=45, market_regime="RANGE", trend="DOWN",
                    spy_price=480, spy_ma200=500)
    result2 = SignalQualityScorer.score(ctx2, "LONG")
    assert result2.confidence >= 0.10


def test_strength_range():
    """强度在 [10, 99] 范围内"""
    ctx = _MockCtx()
    result = SignalQualityScorer.score(ctx, "LONG")
    assert 10 <= result.strength <= 99


def test_all_dimensions_present():
    """结果包含所有 5 个维度"""
    ctx = _MockCtx()
    result = SignalQualityScorer.score(ctx, "LONG")
    expected_dims = {"trend", "momentum", "volume", "order_flow", "market"}
    assert set(result.dimensions.keys()) == expected_dims


def test_short_direction_scoring():
    """SHORT 方向使用 is_long=False 路径"""
    ctx = _MockCtx(price=140, ma5=142, ma20=148, ma50=150, rsi=60,
                   trend="DOWN", market_regime="TREND_DOWN",
                   volume_ratio=1.8, order_flow_imbalance=-0.4,
                   spy_price=490, spy_ma200=500)
    result = SignalQualityScorer.score(ctx, "SHORT")
    assert result.dimensions["trend"] > 0.5
    assert result.dimensions["momentum"] > 0.3


def test_confluence_bonus_applied():
    """多维度 > 0.6 时触发汇合奖励"""
    # Set up a scenario where most dimensions agree strongly
    ctx = _MockCtx(price=155, ma5=153, ma20=148, ma50=145,
                   rsi=42, macd=0.5, macd_signal=0.2, slope_5m=0.3,
                   volume_ratio=2.5, vwap_deviation=0.01,
                   order_flow_imbalance=0.4, ticker_buy_sell_ratio=1.2,
                   vix=15, spy_price=520, spy_ma200=500,
                   market_regime="TREND_UP", trend="UP")
    result = SignalQualityScorer.score(ctx, "LONG")
    assert result.confluence_bonus > 0


def test_direction_string_case_insensitive():
    """方向字符串大小写不敏感"""
    ctx = _MockCtx()
    r1 = SignalQualityScorer.score(ctx, "LONG")
    r2 = SignalQualityScorer.score(ctx, "long")
    r3 = SignalQualityScorer.score(ctx, "Long")
    assert r1.confidence == r2.confidence == r3.confidence
