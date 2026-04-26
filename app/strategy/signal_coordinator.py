#!/usr/bin/env python3
"""
信号协调器 (Signal Coordinator)

功能:
1. 同一股票多信号去重 (保留高优先级)
2. 方向冲突检测 (BUY vs SELL)
3. 持仓状态感知 (已有持仓时的信号处理)
4. 时间窗口感知 (跨市场 vs 趋势)

架构位置:
  信号生成 → [SignalCoordinator] → 决策缓冲层 → 执行
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime
from collections import defaultdict

from app.strategy.base import Signal, SignalAction
from app.strategy.cross_market import SignalPriority, CrossMarketSessionWindow

logger = logging.getLogger("ftseek.signal_coordinator")


class SignalCoordinator:
    """
    信号协调器 - 解决多策略对同一标的的冲突信号
    
    核心规则:
    1. 套利窗口 (09:30-10:00): 跨市场信号 CRITICAL > 趋势信号 MEDIUM
    2. 非套利窗口 (10:00+): 趋势信号正常处理,跨市场信号降级
    3. 同优先级: 保留高置信度
    4. 方向冲突: 反向信号忽略 (记录日志)
    5. 持仓冲突: 已有持仓时,根据策略来源决定加仓/止盈/忽略
    """
    
    def __init__(self):
        # 冲突解决日志
        self._resolution_log: List[dict] = []
        
        logger.info("信号协调器已初始化")
    
    def resolve_conflicts(
        self,
        signals: List[Signal],
        current_positions: Dict[str, dict],
        is_arb_window: bool = False,
    ) -> List[Signal]:
        """
        解决信号冲突
        
        Args:
            signals: 待处理信号列表
            current_positions: 当前持仓 {code: {action, pnl_pct, ...}}
            is_arb_window: 是否在套利窗口 (09:30-10:00)
        
        Returns:
            解决冲突后的信号列表
        """
        if not signals:
            return []
        
        # 按股票代码分组
        grouped = defaultdict(list)
        for signal in signals:
            grouped[signal.code].append(signal)
        
        resolved_signals = []
        
        for code, code_signals in grouped.items():
            position = current_positions.get(code)
            
            if len(code_signals) == 1:
                # 单信号,直接通过 (但需检查持仓冲突)
                resolved = self._check_position_conflict(
                    code_signals[0], position, is_arb_window
                )
                if resolved:
                    resolved_signals.append(resolved)
            else:
                # 多信号冲突,需要解决
                resolved = self._resolve_multi_signals(
                    code, code_signals, position, is_arb_window
                )
                if resolved:
                    resolved_signals.append(resolved)
        
        return resolved_signals
    
    def _resolve_multi_signals(
        self,
        code: str,
        signals: List[Signal],
        position: Optional[dict],
        is_arb_window: bool,
    ) -> Optional[Signal]:
        """
        解决同一股票的多信号冲突
        
        规则:
        1. 套利窗口: 跨市场信号 > 趋势信号
        2. 非套利窗口: 趋势信号 > 跨市场信号 (可能过期)
        3. 同优先级: 保留高置信度
        4. 方向冲突: 反向信号忽略
        """
        # 按优先级排序
        signals.sort(key=lambda s: getattr(s, 'priority', SignalPriority.MEDIUM), reverse=True)
        
        winner = signals[0]
        losers = signals[1:]
        
        # 检查方向冲突
        has_conflict = any(s.action != winner.action for s in losers)
        
        if has_conflict:
            # 方向冲突,记录日志
            conflict_details = [
                f"{s.strategy}({s.action.value}, p={getattr(s, 'priority', 2)})"
                for s in losers
            ]
            logger.warning(
                f"[信号冲突] {code} | "
                f"胜出: {winner.strategy}({winner.action.value}) | "
                f"冲突: {', '.join(conflict_details)}"
            )
            
            # 套利窗口内,跨市场信号优先
            if is_arb_window:
                if winner.strategy == 'cross_market_arbitrage':
                    logger.info(f"[套利窗口] {code} 跨市场信号优先")
                elif winner.strategy != 'cross_market_arbitrage':
                    # 检查是否有跨市场信号被压制
                    arb_signals = [
                        s for s in signals
                        if s.strategy == 'cross_market_arbitrage'
                    ]
                    if arb_signals:
                        logger.warning(
                            f"[套利窗口] {code} 跨市场信号被趋势信号压制, "
                            f"可能存在信息优势损失"
                        )
        
        # 记录冲突解决
        self._resolution_log.append({
            'timestamp': datetime.now().isoformat(),
            'code': code,
            'winner': f"{winner.strategy}({winner.action.value})",
            'losers': [f"{s.strategy}({s.action.value})" for s in losers],
            'arb_window': is_arb_window,
        })
        
        return winner
    
    def _check_position_conflict(
        self,
        signal: Signal,
        position: Optional[dict],
        is_arb_window: bool,
    ) -> Optional[Signal]:
        """
        检查持仓冲突
        
        场景:
        1. 已有BUY持仓,又来BUY信号 → 加仓 or 忽略?
        2. 已有BUY持仓,来SELL信号 → 平仓
        3. 无持仓,来BUY信号 → 开仓
        """
        if not position:
            return signal  # 无持仓,直接开仓
        
        pos_action = position.get('action')
        pos_pnl = position.get('pnl_pct', 0)
        
        # 场景1: 同方向信号 (可能加仓)
        if signal.action.value == pos_action:
            # 套利窗口内,跨市场信号要求加仓 → 谨慎处理
            if signal.strategy == 'cross_market_arbitrage':
                if pos_pnl >= 0.03:
                    logger.info(
                        f"[持仓冲突] {signal.code} 已有盈利 ({pos_pnl:+.1%}), "
                        f"跨市场加仓信号忽略 (让止盈引擎处理)"
                    )
                    return None  # 拒绝加仓
                elif pos_pnl <= -0.02:
                    logger.info(
                        f"[持仓冲突] {signal.code} 已有亏损 ({pos_pnl:+.1%}), "
                        f"跨市场加仓信号忽略 (让止损引擎处理)"
                    )
                    return None
                else:
                    # 盈亏不大,允许加仓 (但需仓位检查)
                    logger.info(
                        f"[持仓冲突] {signal.code} 跨市场加仓信号通过 "
                        f"(盈亏={pos_pnl:+.1%})"
                    )
                    return signal
            
            # 趋势策略加仓 → 正常处理
            return signal
        
        # 场景2: 反向信号 (平仓)
        else:
            logger.info(
                f"[持仓冲突] {signal.code} 反向信号 → 平仓 "
                f"({signal.strategy})"
            )
            return signal  # 平仓信号优先级最高
    
    def get_resolution_log(self, last_n: int = 50) -> List[dict]:
        """获取冲突解决日志"""
        return self._resolution_log[-last_n:]
    
    def clear_log(self):
        """清空日志"""
        self._resolution_log.clear()
