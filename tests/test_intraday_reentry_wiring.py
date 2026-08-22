"""证伪门接线修复回归 — ADR-032 (2026-08-22 用户裁"请修复").

背景: 73c853b9 起四个门按想象 schema 读 provider (buy_pct/sell_pct、dict 键财报、
social score、buy_strength/sell_strength 均不存在) → 恒 None → 永不触发, 而纯函数
契约测试注入参数全绿掩盖接线死亡。本文件用 **provider 真实输出形态** 做 fixture
钉死 helper 契约, 防同类 schema 臆写复发。
回滚: 删本文件 + revert intraday_reentry.py 对应 hunk。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.playbook.intraday_reentry import (
    _DEFAULTS,
    _derive_analyst_trend,
    _derive_ticker_direction,
    _find_days_to_earnings,
)


# ── analyst: 真实 schema {periods: [{strong_buy,buy,hold,sell,strong_sell}]} ──

def test_analyst_upgrade_on_bullish_revision():
    # 上月净中性 → 当月看多迁移 (Yahoo recommendationTrend 形态, periods[0]=当月)
    rt = {"periods": [
        {"period": "0m", "strong_buy": 8, "buy": 10, "hold": 5, "sell": 1, "strong_sell": 0},
        {"period": "-1m", "strong_buy": 3, "buy": 8, "hold": 10, "sell": 2, "strong_sell": 1},
    ]}
    assert _derive_analyst_trend(rt) == "upgrade"


def test_analyst_downgrade_and_deadband():
    downgrade = {"periods": [
        {"period": "0m", "strong_buy": 1, "buy": 4, "hold": 6, "sell": 8, "strong_sell": 5},
        {"period": "-1m", "strong_buy": 8, "buy": 10, "hold": 4, "sell": 1, "strong_sell": 0},
    ]}
    assert _derive_analyst_trend(downgrade) == "downgrade"
    # 微小变化 (<5pp) = 月度噪声 → 不给方向
    stable = {"periods": [
        {"period": "0m", "strong_buy": 5, "buy": 10, "hold": 8, "sell": 2, "strong_sell": 1},
        {"period": "-1m", "strong_buy": 5, "buy": 11, "hold": 7, "sell": 2, "strong_sell": 1},
    ]}
    assert _derive_analyst_trend(stable) is None


def test_analyst_insufficient_or_empty_never_fires():
    # 修复前该门对真实 schema 恒 None; 这里钉死边界输入也不误触
    assert _derive_analyst_trend({}) is None
    assert _derive_analyst_trend({"periods": [{"strong_buy": 9, "buy": 9, "hold": 0,
                                               "sell": 0, "strong_sell": 0}]}) is None
    assert _derive_analyst_trend(None) is None


# ── earnings: 真实 schema List[Dict], 行含 Futu 格式 symbol / days_until ──

def test_earnings_finds_row_in_list_of_dicts():
    # 修复前 `sym in _earn` 对 List[Dict] 恒 False — 用真实行结构钉死正确遍历
    rows = [
        {"symbol": "US.WEAV", "name": "Weave", "days_until": 6},
        {"symbol": "US.BULL", "name": "Webtoon", "days_until": 2},
    ]
    assert _find_days_to_earnings(rows, "US.BULL") == 2
    assert _find_days_to_earnings(rows, "US.MU") is None


def test_earnings_malformed_rows_safe():
    assert _find_days_to_earnings([], "US.MU") is None
    assert _find_days_to_earnings(None, "US.MU") is None
    assert _find_days_to_earnings(["junk", 42, {"symbol": "US.MU"}], "US.MU") is None
    assert _find_days_to_earnings([{"symbol": "US.MU", "days_until": None}], "US.MU") is None


# ── ticker: 真实 schema {sentiment[-1,1], buy_ratio, volume_weighted, confidence} ──

def test_ticker_direction_from_real_schema():
    assert _derive_ticker_direction(
        {"sentiment": 0.42, "buy_ratio": 0.71, "volume_weighted": 0.5, "confidence": 0.8}
    ) == "bullish"
    assert _derive_ticker_direction(
        {"sentiment": -0.31, "buy_ratio": 0.29, "volume_weighted": -0.4, "confidence": 0.8}
    ) == "bearish"


def test_ticker_neutral_band_and_no_data_default():
    # ±0.15 死区内不给方向
    assert _derive_ticker_direction(
        {"sentiment": 0.05, "buy_ratio": 0.52, "volume_weighted": 0.01, "confidence": 0.8}
    ) is None
    # get_ticker_sentiment 无数据时返回 default dict (confidence=0.0) → 必须不触方向
    no_data = {"sentiment": 0.0, "buy_ratio": 0.5, "volume_weighted": 0.0, "confidence": 0.0}
    assert _derive_ticker_direction(no_data) is None
    assert _derive_ticker_direction(None) is None


# ── social: 数据源无极性分数 → 门必须默认关断 (诚实降级, ADR-032) ──

def test_social_gate_disabled_by_default_source_has_no_polarity():
    """collect_sentiment 真实返回 {symbol, reddit[], hackernews[], polymarket[],
    summary{计数}, fetched_at} 全程无带符号 score → 本门在现源下不可诚实实现,
    默认 False。若有人改回 True 且源仍无分数, 此测试拦截。"""
    assert _DEFAULTS.get("social_sentiment_divergence_check_enabled") is False


# ── 2026-08-22c: provider 缓存调用签名死门回归钉 (门活性探针首跑抓出) ──

def test_futu_ext_no_invalid_cache_kwargs():
    """BaseDataProvider._is_cache_valid(symbol) 只收单参; 原代码传 ttl_override=3600
    → 每次 TypeError → 调用方 fail-open 吞掉 → 卖空拥挤门自上线恒死。
    钉死 provider 源码内不得再出现该臆造参数。"""
    import inspect
    from app.data.providers import futu_extended_provider as mod
    for line in inspect.getsource(mod).splitlines():
        if "_is_cache_valid(" in line:
            assert "ttl_override" not in line, f"死门回归: {line.strip()}"
