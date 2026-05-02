"""
交易执行阶段模块 — 盘中 Intraday

目标：将 data_subscriber.py (3600行) 拆分为职责清晰的子模块。

模块边界：
├── market_subscriber.py    — 行情订阅（Futu Push 回调、K线处理）
├── signal_processor.py     — 信号处理（共识评估、信号链记录）
├── position_manager.py     — 持仓管理（止损/止盈/追踪止盈）
├── worker_pool.py          — Worker 线程管理（数据分发、生命周期）
└── coordinator.py          — 协调器（整合所有子模块）

原则：
1. 每个模块 < 500 行
2. 模块间通过接口通信，不直接 import 实现
3. 所有配置通过 cfg.get() 统一读取

使用方式:
    from app.intraday import IntradayCoordinator
    
    coordinator = IntradayCoordinator(
        position_manager=...,
        trade_executor=...,
        consensus_engine=...
    )
    coordinator.start(battle_plan)
"""

from app.intraday.market_subscriber import MarketSubscriber
from app.intraday.signal_processor import SignalProcessor
from app.intraday.position_manager import IntradayPositionManager
from app.intraday.worker_pool import WorkerPool
from app.intraday.coordinator import IntradayCoordinator
from app.intraday.subscription_manager import SubscriptionManager, MarketHours

__all__ = [
    "MarketSubscriber",
    "SignalProcessor",
    "IntradayPositionManager",
    "WorkerPool",
    "IntradayCoordinator",
    "SubscriptionManager",
    "MarketHours",
]
