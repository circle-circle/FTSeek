"""盘前榜质量门单测 (2026-08-29, 用户裁"按建议执行" 盘前榜过滤规则)
================================================
背景: 富途全市场盘前榜 (get_us_pre_market_rank) 默认按涨跌幅排序,
实弹探针证明榜单天然是小票/垃圾票 (33/50 盘前成交额<$1M, 混杠杆 ETF)。
质量门: 成交额≥$5M → 价格≥$5 → 市值≥$2B(顺带剔ETF) → |涨幅|>25% 打标不入围
→ 财报3日内标 EVENT_RISK → 每向≤5 候选。

本测试钉住的是【行为】不是文本:
  1. 各门拦截方向正确 (成交额/价格/市值/极端带)
  2. ETF (mcap=-1 哨兵) 被剔除
  3. 极端涨幅带打标不入围但保留展示 (影子攒样)
  4. 财报交叉标记
  5. 每向候选上限 5
  6. 边界值语义 (≥ 含等号, > 不含)
回滚契约: 质量门阈值改动需同步本测试 (红) — 防静默漂移。
"""
import pytest

from app.data.premarket_enrichment import apply_rank_quality_gates


def _row(sym, chg, to=10_000_000.0, px=20.0):
    return {"symbol": sym, "price": px, "change_pct": chg, "turnover_usd": to}


def _mcap(*syms, val=5e9):
    return {f"US.{s}": val for s in syms}


def test_turnover_gate_blocks_low_liquidity():
    """实弹根因: 盘前成交额<$5M 全是噪音 (探针 33/50 <$1M, BESS 仅 $515)。"""
    gated = apply_rank_quality_gates(
        [_row("NOISY", 8.0, to=999_999)], [], _mcap("NOISY"), set())
    assert gated["gainers"]["candidates"] == []
    assert gated["gainers"]["rejected"][0]["reason"] == "turnover"


def test_turnover_gate_boundary_inclusive():
    """≥$5M 含等号 — 边界恰好 5,000,000 应通过。"""
    gated = apply_rank_quality_gates(
        [_row("EDGE", 8.0, to=5_000_000)], [], _mcap("EDGE"), set())
    assert len(gated["gainers"]["candidates"]) == 1


def test_price_gate_blocks_penny():
    """仙股门: 价格<$5 剔除 (对齐 factor_engine 既有闸值)。"""
    gated = apply_rank_quality_gates(
        [_row("PENNY", 12.0, px=0.30)], [], _mcap("PENNY"), set())
    assert gated["gainers"]["candidates"] == []
    assert gated["gainers"]["rejected"][0]["reason"] == "price"


def test_mcap_gate_and_etf_sentinel():
    """市值<$2B 剔除; ETF 哨兵 -1 (快照市值 NaN) 同路剔除。"""
    m = _mcap("SMALL", val=1e9)
    m["US.ETFSPY"] = -1.0
    gated = apply_rank_quality_gates(
        [_row("SMALL", 8.0), _row("ETFSPY", 6.0)], [], m, set())
    reasons = {r["symbol"]: r["reason"] for r in gated["gainers"]["rejected"]}
    assert reasons["SMALL"] == "mcap"
    assert reasons["ETFSPY"] == "etf_or_smallcap"
    assert gated["gainers"]["candidates"] == []


def test_missing_mcap_treated_as_reject():
    """市值缺失 (fail-close): 按剔除处理, 不放行。"""
    gated = apply_rank_quality_gates([_row("NOMAP", 8.0)], [], {}, set())
    assert gated["gainers"]["candidates"] == []
    assert gated["gainers"]["rejected"][0]["reason"] == "etf_or_smallcap"


def test_extreme_band_flagged_not_candidate():
    """|涨幅|>25% 打标不入围但保留展示 (影子攒样, 非硬禁)。"""
    gated = apply_rank_quality_gates(
        [_row("ROCKET", 30.0), _row("CALM", 12.0)],
        [], _mcap("ROCKET", "CALM"), set())
    assert [c["symbol"] for c in gated["gainers"]["candidates"]] == ["CALM"]
    assert [e["symbol"] for e in gated["gainers"]["extreme"]] == ["ROCKET"]
    assert "EXTREME" in gated["gainers"]["extreme"][0]["tags"]


def test_extreme_boundary_25_not_flagged():
    """>25% 不含等号 — 恰好 +25% 正常入围。"""
    gated = apply_rank_quality_gates([_row("AT25", 25.0)], [], _mcap("AT25"), set())
    assert [c["symbol"] for c in gated["gainers"]["candidates"]] == ["AT25"]


def test_earnings_event_risk_tag():
    """未来3日财报标的打 EVENT_RISK 标记 (不剔除, 只提示)。"""
    gated = apply_rank_quality_gates(
        [_row("ERNSTOCK", 8.0)], [], _mcap("ERNSTOCK"), {"ERNSTOCK"})
    assert gated["gainers"]["candidates"][0]["tags"] == ["EVENT_RISK"]


def test_candidates_capped_at_5_per_side():
    """每向候选上限 5 (防稀释下游注意力)。"""
    rows = [_row(f"S{i}", 10.0 - i) for i in range(12)]
    gated = apply_rank_quality_gates(
        rows, [], _mcap(*[f"S{i}" for i in range(12)]), set())
    assert len(gated["gainers"]["candidates"]) == 5
    assert [c["symbol"] for c in gated["gainers"]["candidates"]] == [f"S{i}" for i in range(5)]


def test_losers_side_symmetric():
    """领跌侧同规则对称生效 (负涨幅)。"""
    gated = apply_rank_quality_gates(
        [], [_row("FALLER", -13.0), _row("FALLER2", -40.0)],
        _mcap("FALLER", "FALLER2"), set())
    assert [c["symbol"] for c in gated["losers"]["candidates"]] == ["FALLER"]
    assert [e["symbol"] for e in gated["losers"]["extreme"]] == ["FALLER2"]


def test_rank_numbering_skips_rejected():
    """rank 只计过 成交额+价格+市值 门的标的 (剔除的不占位)。"""
    gated = apply_rank_quality_gates(
        [_row("A", 9.0, to=100), _row("B", 8.0)],  # A 成交额不足
        [], _mcap("B"), set())
    assert gated["gainers"]["candidates"][0]["rank"] == 1
    assert gated["gainers"]["candidates"][0]["symbol"] == "B"
