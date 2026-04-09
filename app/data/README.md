"""
数据订阅模块微服务化重构
包含以下子模块:
- cooldown_manager: 冷却时间管理
- position_tracker: 持仓追踪管理
- adx_manager: ADX 趋势管理
- kline_processor: K 线数据处理
- exit_manager: 退出执行管理
"""

from app.data.cooldown_manager import (
    CooldownManager,
    get_cooldown_manager,
)
from app.data.position_tracker import (
    PositionTracker,
    get_position_tracker,
)
from app.data.adx_manager import (
    ADXManager,
    MultiTimeframeTrendManager,
    get_adx_manager,
    get_mtf_manager,
)
from app.data.kline_processor import (
    KlineProcessor,
    get_kline_processor,
)
from app.data.exit_manager import (
    ExitManager,
    create_exit_manager,
)

__all__ = [
    # Cooldown Manager
    'CooldownManager',
    'get_cooldown_manager',

    # Position Tracker
    'PositionTracker',
    'get_position_tracker',

    # ADX Manager
    'ADXManager',
    'MultiTimeframeTrendManager',
    'get_adx_manager',
    'get_mtf_manager',

    # Kline Processor
    'KlineProcessor',
    'get_kline_processor',

    # Exit Manager
    'ExitManager',
    'create_exit_manager',
]
