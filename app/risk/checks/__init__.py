#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RiskCheck 自动发现与注册

架构位置:
  app/risk/risk_pipeline.py (17 个 RiskCheck 类)
    ↓ 拆分为
  app/risk/checks/ (每个 Check 独立文件)

设计目的:
  将 17 个 RiskCheck 从硬编码 DEFAULT_CHECKS 改为可配置热插拔，
  支持配置驱动的检查项启用/禁用/排序。

职责:
  1. 显式导入所有 Check 类 (保持顺序)
  2. 提供配置驱动的注册机制
  3. 保持向后兼容 (DEFAULT_CHECKS 顺序不变)

约束:
  - 检查顺序至关重要：第一个拒绝即终止
  - 必须保持与原始 DEFAULT_CHECKS 相同的顺序
  - 配置变更需要重启 (不支持运行时热加载)

版本历史:
  2026-05-19 v1: 创建 checks 包，实现自动发现机制
  2026-05-19 v2: 显式导入所有 Check 类，消除动态发现的不确定性
"""

import logging
from typing import List, Type, Optional, Dict, Any

logger = logging.getLogger("ftseek.risk.checks")


# ============================================================
#  显式导入所有 Check 类 (保持顺序)
# ============================================================
from app.risk.checks.decision_buffer import DecisionBufferCheck
from app.risk.checks.quality import QualityCheck
from app.risk.checks.trading_hours import TradingHoursCheck
from app.risk.checks.late_session import LateSessionGateCheck
from app.risk.checks.circuit_breaker import CircuitBreakerCheck
from app.risk.checks.midday_recommendation import MiddayRecommendationCheck
from app.risk.checks.adx_consistency import ADXConsistencyCheck
from app.risk.checks.multi_timeframe import MultiTimeframeCheck
from app.risk.checks.trailing_cooldown import TrailingCooldownCheck
from app.risk.checks.signal_cooldown import SignalCooldownCheck
from app.risk.checks.symbol_loss_cooldown import SymbolLossCooldownCheck
from app.risk.checks.weak_signal import WeakSignalCheck
from app.risk.checks.concentration import ConcentrationCheck
from app.risk.checks.daily_trade_limit import DailyTradeLimitCheck
from app.risk.checks.risk_reward import RiskRewardCheck
from app.risk.checks.min_hold_time import MinHoldTimeCheck
from app.risk.checks.open_dedup import OpenDedupCheck


def discover_risk_checks(
    config: Optional[Dict[str, Any]] = None,
    check_order: Optional[List[str]] = None,
) -> List[Type]:
    """
    自动发现并排序 RiskCheck 子类

    Args:
        config: 配置字典 (可选，用于启用/禁用特定检查)
        check_order: 显式指定检查顺序 (可选，None 时使用默认顺序)

    Returns:
        List[Type[RiskCheck]]: 按顺序排列的 RiskCheck 类列表
    """
    # 默认顺序 (必须与原始 DEFAULT_CHECKS 一致)
    DEFAULT_ORDER = [
        "DecisionBufferCheck",
        "QualityCheck",
        "TradingHoursCheck",
        "LateSessionGateCheck",
        "CircuitBreakerCheck",
        "MiddayRecommendationCheck",
        "ADXConsistencyCheck",
        "MultiTimeframeCheck",
        "TrailingCooldownCheck",
        "SignalCooldownCheck",
        "SymbolLossCooldownCheck",
        "WeakSignalCheck",
        "ConcentrationCheck",
        "DailyTradeLimitCheck",
        "RiskRewardCheck",
        "MinHoldTimeCheck",
        "OpenDedupCheck",
    ]

    # 使用显式顺序或默认顺序
    order = check_order if check_order is not None else DEFAULT_ORDER

    # 构建名称到类的映射
    name_to_class = {
        "DecisionBufferCheck": DecisionBufferCheck,
        "QualityCheck": QualityCheck,
        "TradingHoursCheck": TradingHoursCheck,
        "LateSessionGateCheck": LateSessionGateCheck,
        "CircuitBreakerCheck": CircuitBreakerCheck,
        "MiddayRecommendationCheck": MiddayRecommendationCheck,
        "ADXConsistencyCheck": ADXConsistencyCheck,
        "MultiTimeframeCheck": MultiTimeframeCheck,
        "TrailingCooldownCheck": TrailingCooldownCheck,
        "SignalCooldownCheck": SignalCooldownCheck,
        "SymbolLossCooldownCheck": SymbolLossCooldownCheck,
        "WeakSignalCheck": WeakSignalCheck,
        "ConcentrationCheck": ConcentrationCheck,
        "DailyTradeLimitCheck": DailyTradeLimitCheck,
        "RiskRewardCheck": RiskRewardCheck,
        "MinHoldTimeCheck": MinHoldTimeCheck,
        "OpenDedupCheck": OpenDedupCheck,
    }

    # 按顺序构建结果列表
    result = []
    for check_name in order:
        if check_name in name_to_class:
            # 检查配置是否启用
            if config is not None:
                check_config = config.get(check_name, {})
                if check_config.get("enabled", True) is False:
                    logger.info(f"[CheckDiscovery] {check_name} 已禁用 (配置驱动)")
                    continue
            result.append(name_to_class[check_name])
        else:
            logger.warning(f"[CheckDiscovery] {check_name} 未找到，跳过")

    logger.info(
        f"[CheckDiscovery] 发现 {len(result)}/{len(order)} 个 RiskCheck: "
        f"{[c.__name__ for c in result]}"
    )
    return result


# 向后兼容：DEFAULT_CHECKS 通过自动发现生成
try:
    # 从配置中心读取 risk_checks 配置
    try:
        from config.config_center import get_config_center as _gcc
        _check_config = _gcc().get('risk_checks', {})
    except Exception:
        _check_config = {}
    DEFAULT_CHECKS = discover_risk_checks(config=_check_config)
except Exception as e:
    logger.error(f"[CheckDiscovery] 自动发现失败：{e}")
    # fallback: 空列表 (防止启动失败)
    DEFAULT_CHECKS = []

__all__ = [
    "DecisionBufferCheck",
    "QualityCheck",
    "TradingHoursCheck",
    "LateSessionGateCheck",
    "CircuitBreakerCheck",
    "MiddayRecommendationCheck",
    "ADXConsistencyCheck",
    "MultiTimeframeCheck",
    "TrailingCooldownCheck",
    "SignalCooldownCheck",
    "SymbolLossCooldownCheck",
    "WeakSignalCheck",
    "ConcentrationCheck",
    "DailyTradeLimitCheck",
    "RiskRewardCheck",
    "MinHoldTimeCheck",
    "OpenDedupCheck",
    "discover_risk_checks",
    "DEFAULT_CHECKS",
]
