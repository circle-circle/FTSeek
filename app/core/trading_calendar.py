#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 交易日历门禁 (Trading Calendar Gate)

P0 修复 (2026-04-25): 统一非交易时间静默逻辑

功能:
- 提供 should_poll_market() 判断是否应该请求行情API
- 提供 should_execute_trades() 判断是否应该执行交易
- 提供 should_run_llm_scan() 判断是否应该运行LLM扫描

时段规则 (美东时间):
- WEEKEND (周末): 完全静默，不调用任何API
- OVERNIGHT (深夜 20:00-04:00): 完全静默
- PRE_MARKET (盘前 04:00-09:30): 仅新闻采集，不请求行情
- TRADING (交易 09:30-16:00): 完整功能
- AFTER_HOURS (盘后 16:00-20:00): 新闻+LLM，不请求行情
"""

import logging
from typing import Tuple

from app.core.scheduler_triggers import get_market_phase, MarketPhase

logger = logging.getLogger(__name__)


def should_poll_market() -> bool:
    """
    判断是否应该请求行情API (价格、VIX、SPY等)
    
    仅交易时段返回 True，其他时段均返回 False
    """
    phase = get_market_phase()
    if phase == MarketPhase.TRADING:
        return True
    
    logger.debug(f"[交易日历] {phase.value} → 跳过行情轮询")
    return False


def should_execute_trades() -> bool:
    """
    判断是否应该执行交易 (下单、平仓等)
    
    仅交易时段返回 True
    """
    return get_market_phase() == MarketPhase.TRADING


def should_run_llm_scan() -> bool:
    """
    判断是否应该运行LLM扫描 (新闻分析、策略生成)
    
    周末返回 False，其他时段返回 True (盘前/盘后/深夜均可运行)
    """
    phase = get_market_phase()
    if phase == MarketPhase.WEEKEND:
        logger.debug(f"[交易日历] {phase.value} → 跳过LLM扫描")
        return False
    return True


def should_monitor_positions() -> bool:
    """
    判断是否应该监控持仓 (需要行情数据)
    
    仅交易时段和盘后返回 True
    """
    phase = get_market_phase()
    if phase in (MarketPhase.TRADING, MarketPhase.AFTER_HOURS):
        return True
    
    logger.debug(f"[交易日历] {phase.value} → 跳过持仓监控")
    return False


def get_gate_status() -> dict:
    """
    获取当前门禁状态 (用于诊断和日志)
    
    Returns:
        dict: 包含当前阶段和各门禁状态
    """
    phase = get_market_phase()
    return {
        "phase": phase.value,
        "poll_market": should_poll_market(),
        "execute_trades": should_execute_trades(),
        "run_llm_scan": should_run_llm_scan(),
        "monitor_positions": should_monitor_positions(),
    }
