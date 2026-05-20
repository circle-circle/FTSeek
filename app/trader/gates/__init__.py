#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁系统 (Gates System)

交易前门禁检查抽象层。

导出:
    - GateInterface: 门禁接口
    - GateChain: 门禁链管理器
    - TimeGate: 时段门禁
    - FeeGate: 费用门禁
    - MaxPositionGate: 最大持仓数门禁
    - LLMSignalImpactGate: LLM 信号门禁
    - MarketRegimeGate: 市场趋势门禁

使用示例:
    from app.trader.gates import GateChain, TimeGate, FeeGate

    chain = GateChain()
    chain.add_gate(TimeGate())
    chain.add_gate(FeeGate())

    passed, reason = chain.check_all(signal, context)
    if not passed:
        return TradeResult(success=False, message=reason)

版本历史:
  2026-05-19: 创建
"""

from app.trader.gates.base_gate import GateInterface
from app.trader.gates.gate_chain import GateChain
from app.trader.gates.time_gate import TimeGate
from app.trader.gates.fee_gate import FeeGate
from app.trader.gates.max_position_gate import MaxPositionGate
from app.trader.gates.llm_signal_impact_gate import LLMSignalImpactGate
from app.trader.gates.market_regime_gate import MarketRegimeGate

__all__ = [
    'GateInterface',
    'GateChain',
    'TimeGate',
    'FeeGate',
    'MaxPositionGate',
    'LLMSignalImpactGate',
    'MarketRegimeGate',
]
