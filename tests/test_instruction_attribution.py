"""trade → playbook_instructions 归属解析契约 (2026-11-12)."""
from __future__ import annotations

from app.playbook.instruction_attribution import (
    AMBIGUOUS,
    canonical_plan_direction,
    resolve_trade_instruction_ids,
)


def _instruction(**overrides):
    row = {
        "id": 1,
        "date": "2026-11-09",
        "symbol": "US.APP",
        "direction": "BUY",
        "decision_id": "DA-APP",
    }
    row.update(overrides)
    return row


def _trade(**overrides):
    row = {
        "trade_date": "2026-11-09",
        "symbol": "US.APP",
        "direction": "LONG",
        "decision_id": "DA-APP",
        "metadata_json": None,
    }
    row.update(overrides)
    return row


def test_canonical_plan_direction():
    assert canonical_plan_direction("LONG") == "BUY"
    assert canonical_plan_direction("BUY") == "BUY"
    assert canonical_plan_direction("SHORT") == "SELL_SHORT"
    assert canonical_plan_direction("SELL_SHORT") == "SELL_SHORT"
    assert canonical_plan_direction("SKIP") == "SKIP"


def test_unique_decision_id_resolves():
    assert resolve_trade_instruction_ids(
        [_trade()], [_instruction(id=7)]
    ) == [7]


def test_metadata_instruction_id_wins():
    trade = _trade(metadata_json='{"playbook_instruction_id": 2}')
    assert resolve_trade_instruction_ids(
        [trade],
        [_instruction(id=1), _instruction(id=2)],
    ) == [2]


def test_unknown_trade_is_none_not_ambiguous():
    trade = _trade(decision_id="EXT-APP")
    assert resolve_trade_instruction_ids(
        [trade], [_instruction(id=1)]
    ) == [None]


def test_cross_day_duplicate_decision_id_disambiguated_by_trade_context():
    instructions = [
        _instruction(id=1, date="2026-11-08", decision_id="DA-REUSED"),
        _instruction(id=2, date="2026-11-09", decision_id="DA-REUSED"),
    ]
    assert resolve_trade_instruction_ids(
        [_trade(trade_date="2026-11-09", decision_id="DA-REUSED")],
        instructions,
    ) == [2]
    assert resolve_trade_instruction_ids(
        [_trade(trade_date="2026-11-08", decision_id="DA-REUSED")],
        instructions,
    ) == [1]


def test_same_day_same_symbol_same_direction_duplicates_stay_ambiguous():
    instructions = [
        _instruction(id=1, decision_id="SHARED"),
        _instruction(id=2, decision_id="SHARED"),
    ]
    resolved = resolve_trade_instruction_ids(
        [_trade(decision_id="SHARED")], instructions
    )
    assert resolved == [AMBIGUOUS]
    assert resolved[0] is not None
    assert resolved[0] != 1


def test_symbol_and_direction_context_narrows_duplicate():
    instructions = [
        _instruction(id=1, symbol="US.APP", direction="BUY", decision_id="SHARED"),
        _instruction(id=2, symbol="US.TSLA", direction="BUY", decision_id="SHARED"),
    ]
    assert resolve_trade_instruction_ids(
        [_trade(symbol="US.TSLA", direction="LONG", decision_id="SHARED")],
        instructions,
    ) == [2]


def test_duplicate_without_context_fields_stays_ambiguous_not_wrong_id():
    # 旧 caller (只传 id/decision_id) 在重复 decision_id 时必须谨慎, 不得随手取第一行。
    instructions = [{"id": 1, "decision_id": "SHARED"}, {"id": 2, "decision_id": "SHARED"}]
    assert resolve_trade_instruction_ids(
        [_trade(decision_id="SHARED")], instructions
    ) == [AMBIGUOUS]

# ── 2026-11-12 核查加固: 归因来源分层 ────────────────────────────────
def test_sources_label_precise_vs_fallback_vs_stale():
    """报表必须能分辨「精确 (metadata)」与「猜的 (decision_id 回退)」。

    metadata_stale 是最危险的一格: metadata 记过指令 id, 但该指令已不在指令表内
    (删了/重建了), 代码静默降级到 decision_id 模糊匹配 —— 结果看起来和精确归属一样,
    却是猜的。没有这个标记, 执行率里混着推断值也无从审计。
    """
    from app.playbook.instruction_attribution import (
        SOURCE_AMBIGUOUS, SOURCE_DECISION_ID, SOURCE_METADATA,
        SOURCE_METADATA_STALE, SOURCE_UNMATCHED, count_sources,
    )
    instructions = [
        _instruction(id=1, decision_id="DA-APP"),
        _instruction(id=2, decision_id="DA-APP", symbol="US.OTHER"),
        _instruction(id=3, decision_id="DA-NVDA", symbol="US.NVDA"),
    ]
    trades = [
        # ① metadata 精确命中
        _trade(metadata_json='{"playbook_instruction_id": 1}'),
        # ② metadata 指向已删除的指令 99 → 只有唯一 decision_id 候选 ⇒ stale 回退
        _trade(symbol="US.NVDA", direction="LONG", decision_id="DA-NVDA",
               metadata_json='{"playbook_instruction_id": 99}'),
        # ③ 无 metadata, 唯一 decision_id ⇒ 纯回退
        _trade(symbol="US.GOOG", direction="LONG", decision_id="DA-GOOGX"),
        # ④ 无 metadata 且无候选 ⇒ unmatched
        _trade(symbol="US.ZZZ", direction="LONG", decision_id=""),
    ]
    instructions.append(_instruction(id=4, decision_id="DA-GOOGX", symbol="US.GOOG"))
    sources: list = []
    resolved = resolve_trade_instruction_ids(trades, instructions, sources=sources)
    assert resolved[0] == 1 and sources[0] == SOURCE_METADATA
    assert resolved[1] == 3 and sources[1] == SOURCE_METADATA_STALE
    assert resolved[2] == 4 and sources[2] == SOURCE_DECISION_ID
    assert resolved[3] is None and sources[3] == SOURCE_UNMATCHED
    counts = count_sources(sources)
    assert counts[SOURCE_METADATA] == 1
    assert counts[SOURCE_METADATA_STALE] == 1
    assert counts[SOURCE_DECISION_ID] == 1
    assert counts[SOURCE_UNMATCHED] == 1
    assert counts[SOURCE_AMBIGUOUS] == 0   # 五类恒有键 (0 也输出)


def test_sources_backwards_compatible_when_not_requested():
    """不传 sources 时行为与签名完全不变 (三个报表调用方按需接入)。"""
    resolved = resolve_trade_instruction_ids(
        [_trade(metadata_json='{"playbook_instruction_id": 1}')],
        [_instruction(id=1, decision_id="DA-APP")])
    assert resolved == [1]
