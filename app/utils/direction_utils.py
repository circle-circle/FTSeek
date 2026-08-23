"""交易方向词表规范化工具 (SSOT for direction vocabulary)

本系统同时存在两套方向词表, 且落库在不同表:
  - 指令侧 (playbook_instructions.direction / UE 注入 / Instruction 对象):
      BUY / SELL_SHORT / SKIP
  - 成交侧 (trade_records.direction / 退出链 / 影子台账):
      LONG / SHORT

两套词表之间的映射此前在各站点就地重写 (app/playbook/shadow_tracker.py:452 /
app/analysis/plan_effectiveness.py:232 / app/playbook/intraday_reentry.py:485,766 /
app/playbook/pipeline.py:1276 ...), 没有单一真源 —— 直接后果是
2026-08-23 UE 同标的冷却门用指令词汇 'BUY' 去查只存 'LONG' 的 trade_records,
全表 0 命中, 门上线即死且静默 (无告警、无日志、测试查不出)。

本模块是该映射的唯一真源。跨这两个词表比较方向时一律走这里, 不要就地写 dict。
"""
from typing import Optional

# 指令词表 → 成交词表。键覆盖历史上出现过的全部写法 (SELL 是早期别名)。
_TO_TRADE = {
    "BUY": "LONG",
    "LONG": "LONG",
    "SELL_SHORT": "SHORT",
    "SHORT": "SHORT",
    "SELL": "SHORT",
}

# 成交词表 → 指令词表。
_TO_INSTRUCTION = {
    "LONG": "BUY",
    "BUY": "BUY",
    "SHORT": "SELL_SHORT",
    "SELL_SHORT": "SELL_SHORT",
    "SELL": "SELL_SHORT",
}


def to_trade_direction(direction: Optional[str]) -> Optional[str]:
    """指令/任意词表 → 成交词表 (LONG/SHORT)。

    用于查询 trade_records、影子台账、退出链等成交侧数据。
    无法识别 (含 SKIP / None / 空串) 返回 None —— 调用方必须显式处理,
    不得静默当作"无匹配"跳过 (那正是冷却门死门的失效模式)。
    """
    return _TO_TRADE.get(str(direction or "").strip().upper())


def to_instruction_direction(direction: Optional[str]) -> Optional[str]:
    """成交/任意词表 → 指令词表 (BUY/SELL_SHORT)。无法识别返回 None。"""
    return _TO_INSTRUCTION.get(str(direction or "").strip().upper())


def is_short(direction: Optional[str]) -> Optional[bool]:
    """方向是否为做空。无法识别返回 None (不要默认 False —— 会把未知方向当多头)。"""
    t = to_trade_direction(direction)
    return None if t is None else (t == "SHORT")
