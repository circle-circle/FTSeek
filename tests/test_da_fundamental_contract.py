"""DA 基本面键契约测试 (2026-08-26, v0.70 chainaudit 反思防再发).

教训 (8/26 另一 agent 审计发现, 我方复核实弹复现):
  prompt 渲染曾读幻键 (pe_ttm/market_cap/change_pct/eps/dividend_yield/high_52w/low_52w),
  而 data_collector.collect_snapshot 产的是真键 (pe_ttm_ratio/total_market_val/
  highest52weeks_price/lowest52weeks_price), 且不产 change_pct/eps/dividend_yield
  → 100% 分析里模型看到 涨跌幅+0.0%/PE=0/市值$0.0B, 叠加 USER_PROMPT
  "PE为负或0必须在bear_case提及" = 每只股票被注入虚假"亏损公司"看空论据。
  且当时 35 个契约测试全绿 — 它们只测输出契约, 输入保真不在测试面内。

本测试用真实 OpenD 快照 fixture 钉住 "渲染路径 ↔ 真键" 契约:
  谁再把视图改成读幻键 (本 bug 的实际发生方向) → 立红。

已知天花板 (ponytail 标注): fixture 是 2026-08-26 AAPL 静态快照 —
  collector 侧改键名需重新生成 fixture (fixture._fixture_note 记有生成方法)。
  本测试覆盖方向 = 视图读错 (原 bug 方向); collector 侧漂移靠 fixture 再生兜底。
"""
import json
import sys
from pathlib import Path

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)
try:
    import llm_synth as L
except Exception as _e:  # skill 不可用即跳过 (同 test_da_output_contract 惯例)
    pytest.skip(f"skill 不可用: {_e}", allow_module_level=True)

_FIXTURE = Path(__file__).parent / "fixtures" / "da_real_snapshot_20260826.json"
if not _FIXTURE.exists():
    pytest.skip("真实快照 fixture 缺失", allow_module_level=True)
SNAP = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _rendered() -> str:
    return L._format_data_for_prompt({"snapshot": SNAP})


def test_fixture_is_real():
    """fixture 卫生: 真键必须有真实非零值, 否则后续断言无意义."""
    assert SNAP.get("pe_ttm_ratio", 0) > 0
    assert SNAP.get("total_market_val", 0) > 0
    assert SNAP.get("highest52weeks_price", 0) > 0
    assert SNAP.get("last_price", 0) > 0 and SNAP.get("prev_close", 0) > 0


def test_fundamental_view_reads_real_keys():
    """视图必须从真键取值 (改回幻键读法 → 得 0 → 本断言红)."""
    v = L._fundamental_view(SNAP)
    assert v["pe_ttm"] == SNAP["pe_ttm_ratio"] and v["pe_ttm"] > 0
    assert v["market_cap"] == SNAP["total_market_val"] and v["market_cap"] > 0
    assert v["high_52w"] == SNAP["highest52weeks_price"]
    assert v["low_52w"] == SNAP["lowest52weeks_price"]
    # 有效价字段优先；盘前/盘后不得退回 last/prev_close 的伪零涨跌。
    assert v["change_pct"] == SNAP["effective_chg_pct"]
    assert v["effective_price"] == SNAP["effective_price"]

    regular = dict(SNAP, session="regular")
    regular.pop("effective_chg_pct")
    regular.pop("effective_price")
    expect = round((SNAP["last_price"] - SNAP["prev_close"]) / SNAP["prev_close"] * 100, 2)
    assert L._fundamental_view(regular)["change_pct"] == expect


def test_rendered_prompt_has_no_zero_facade():
    """症状守卫: 渲染文本里不得出现 8/26 前的 0 假象 (原病灶的可观测指纹)."""
    text = _rendered()
    assert "PE(TTM): 0" not in text, "PE 渲染回落到幻键 0 假象"
    assert "$0.0B" not in text, "市值渲染回落到幻键 0 假象"
    # 真实值必须可见 (AAPL fixture: PE≈35.5, 市值≈4522.7B)
    assert f"PE(TTM): {SNAP['pe_ttm_ratio']}" in text
    assert f"{SNAP['total_market_val']/1e9:.1f}B" in text


def test_uncollected_dims_render_na_not_zero():
    """数据源不提供的维度 (eps/股息率) 必须显式 N/A, 不得伪装 0 诱导模型编造."""
    v = L._fundamental_view(SNAP)
    assert v["eps"] is None and v["dividend_yield"] is None
    text = _rendered()
    assert "EPS: 数据未采集(N/A)" in text
    assert "EPS: 0" not in text
