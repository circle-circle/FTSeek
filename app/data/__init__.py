"""
数据订阅模块微服务化重构
包含行情处理、K 线分析、退出管理、共识触发、订阅管理等子模块

重构于 2026-04-09: 拆分巨型类 DataSubscriber (2895 行) 为独立微模块
"""
from app.data.trading_hours import TradingHoursManager, get_trading_hours_manager
from app.data.kline_processor import KlineProcessor, get_kline_processor
from app.data.exit_manager import ExitManager, create_exit_manager
from app.data.consensus_trigger import ConsensusTrigger
from app.data.subscriber import DataSubscriber, create_subscriber

# 新增微模块 (2026-04-09)
from app.data.cooldown_manager import CooldownManager, get_cooldown_manager
from app.data.position_tracker import PositionTracker, get_position_tracker
from app.data.adx_manager import ADXManager, MultiTimeframeTrendManager, get_adx_manager, get_mtf_manager

__all__ = [
    # 原有模块
    'TradingHoursManager',
    'get_trading_hours_manager',
    'KlineProcessor',
    'get_kline_processor',
    'ExitManager',
    'create_exit_manager',
    'ConsensusTrigger',
    'DataSubscriber',
    'create_subscriber',

    # 新增微模块 (2026-04-09)
    'CooldownManager',
    'get_cooldown_manager',
    'PositionTracker',
    'get_position_tracker',
    'ADXManager',
    'MultiTimeframeTrendManager',
    'get_adx_manager',
    'get_mtf_manager',
]
