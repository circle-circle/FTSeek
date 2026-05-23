"""
回测模块

提供 Walk-Forward 回测引擎和统计验证门禁。
"""
from app.backtest.walk_forward import WalkForwardEngine, WalkForwardResult
from app.backtest.statistical_gate import StatisticalGate, GateResult

__all__ = [
    'WalkForwardEngine',
    'WalkForwardResult', 
    'StatisticalGate',
    'GateResult',
]
