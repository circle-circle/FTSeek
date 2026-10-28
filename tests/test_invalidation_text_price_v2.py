"""文本失效价解析: 方向感知 + 最近锚价优先 (2026-10-28 接管修复, DEB-20261028 §15.8).

修前病灶 (生产 670 笔实测):
  · 13 条被采纳的文本价离入场价 15%–833% (跨资产/日期/窗口误读);
  · 12 条判 "degenerate" 的文本里有 5 条其实含可用价, 被第一处匹配丢掉。
修法: 剥非价格数字 → 候选价 (方向对应动词 > 关键位词) → 取离入场锚最近且 ≤30% 的。
这些断言钉住的是"预注册规则本身", 放宽 30% 或改回 first-match 都必须先改文档。
"""
import pytest

from app.playbook.invalidation_contract import (
    _MAX_TEXT_PRICE_DISTANCE,
    compile_invalidation_contract,
    compile_text_rule,
    extract_text_price,
    text_price_candidates,
)


# ── 1. 第一处匹配是日期/窗口, 真实价在后面 → 必须取真实价 ──────────────────────
@pytest.mark.parametrize(
    "text,entry,expected",
    [
        ("跌破10-21低点237.23及MA10(240.14)，W底形态失效", 247.22, 237.23),   # CRM 1721
        ("若跌破7/29低点$372.72且3日内未收回，或下一份财报指引下调", 407.65, 372.72),
        ("收盘价跌破08-25突破日最低价$92.80且次日无法收回", 95.0, 92.80),
        ("股价跌破10/9跳空缺口下沿$428且连续3日收盘无法收回", 436.93, 428.0),
        ("股价放量跌破10-09低点62.78，或下次财报EPS不及预期", 66.39, 62.78),
        ("跌破52周低点$147.74或连续2日收盘低于MA60($176.91)下方10%", 158.4, 147.74),
    ],
)
def test_date_and_window_fragments_are_not_prices(text, entry, expected):
    value = extract_text_price(text, entry_price=entry)
    assert value == pytest.approx(expected)


# ── 2. 跨资产/指数价不得被采纳 (旧实现按 0.1–10× 粗门漏过 9.3×) ────────────────
def test_cross_asset_price_is_rejected_not_adopted():
    text = "QQQ跌破$680关键支撑；Uber单日放量跌破$69.80且收盘未收回"
    assert extract_text_price(text, entry_price=72.9) == pytest.approx(69.80)


@pytest.mark.parametrize(
    "text,entry",
    [
        ("《遗忘之海》首月流水低于预期；恒生科技指数跌破24000点", 123.04),
        ("口服PCSK9商业化首季销售额<$2亿；SPY跌破$720引发系统性抛售", 125.38),
        ("量比跌破0.8或5日涨跌转负则失效", 449.61),
    ],
)
def test_non_price_numbers_yield_no_price_rule(text, entry):
    rule, status = compile_text_rule(text, entry_price=entry)
    assert rule is None
    assert status == "degenerate"
    assert extract_text_price(text, entry_price=entry) is None


def test_percent_invalidation_is_not_misread_as_price():
    """毛利率跌破58% —— 58 是百分比, 不是 $58 的价格位。"""
    rule, status = compile_text_rule("毛利率跌破58%", entry_price=431.69)
    assert status in ("unparseable", "parseable")
    if rule is not None:                     # 若命中, 只能是 percent 规则
        assert rule["unit"] == "percent"
        assert rule["value"] == pytest.approx(58.0)


def test_thousand_separator_is_parsed():
    assert extract_text_price("单日放量跌破$1,140且换手率>3%", entry_price=1214.69) == pytest.approx(1140.0)


# ── 3. 方向感知: SHORT 的失效是向上破位, 不得记成 "<" 死规则 ──────────────────
def test_short_breakout_rule_uses_greater_than():
    rule, status = compile_text_rule(
        "价格放量突破14.45(MA10)且站稳，空头逻辑失效", entry_price=13.41, direction="SHORT"
    )
    assert status == "parseable"
    assert rule["value"] == pytest.approx(14.45)
    assert rule["operator"] == ">"


def test_long_breakdown_rule_uses_less_than():
    rule, status = compile_text_rule(
        "跌破$237.23低点", entry_price=247.22, direction="LONG"
    )
    assert status == "parseable"
    assert rule["operator"] == "<"


def test_short_direction_does_not_reuse_long_verb():
    """SHORT 文本里的 '跌破x' 不是失效条件 (那是顺向), 不能产出规则。"""
    verb_hits, _level = text_price_candidates("跌破$30", direction="SHORT")
    assert verb_hits == []


# ── 4. 预注册上限是硬约束 ────────────────────────────────────────────────────
def test_registered_band_is_the_rejection_threshold():
    assert _MAX_TEXT_PRICE_DISTANCE == 0.30
    # 29% 内的价格必须被采纳, 31% 外必须拒绝
    inside = extract_text_price("跌破$70.00", entry_price=100.0)
    outside = extract_text_price("跌破$60.00", entry_price=100.0)
    assert 70.0 == pytest.approx(inside)
    assert outside is None


def test_contract_exposes_candidates_for_audit():
    contract = compile_invalidation_contract(
        invalidation="跌破10-21低点237.23及MA10(240.14)，W底形态失效",
        raw_instruction_json="{}",
        direction="LONG",
        entry_price=247.22,
    )
    assert contract["text_parse_status"] == "parseable"
    assert 237.23 in contract["text_price_candidates"]
    assert contract["action"] == "REVIEW_ONLY"


def test_text_rule_stays_review_only():
    rule, _status = compile_text_rule("跌破$237.23", entry_price=247.22)
    assert rule["action"] == "REVIEW_ONLY"