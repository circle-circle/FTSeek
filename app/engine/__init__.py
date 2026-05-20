"""
FTSeek Engine Package — 聚焦引擎模块

架构位置:
  app/data_subscriber.py (God Class, 3615 行)
    ↓ 拆解为
  app/engine/ (6 个聚焦引擎 + 1 个薄编排层)

引擎清单:
  - exit_state_engine: 止损/止盈/闪崩/追踪 状态机
  - cooldown_manager: 信号/止损/标的 冷却管理
  - market_data_store: 行情数据 CQRS (写入 K 线，只读查询)
  - indicator_cache: ADX/ATR/峰值/MTF 计算缓存
  - futu_bridge: Futu SDK 连接/回调/重连
  - trade_orchestrator: consensus → risk → execute 编排

设计原则:
  1. 单一职责: 每个引擎只做一件事
  2. 依赖倒置: 引擎通过 Provider 接口暴露，不直接暴露给 risk_pipeline
  3. CQRS: 写入由 Worker 线程调用 (持有锁), 读取由 RiskCheck/API 调用 (无锁或读锁)
  4. 热路径保护: 所有 Provider 调用是 dict 查询 (< 0.1ms 开销)

版本历史:
  2026-05-19 v1: 创建 engine 包，添加 __init__.py
"""

from app.engine.exit_state_engine import ExitStateEngine
from app.engine.cooldown_manager import CooldownManager
from app.engine.market_data_store import MarketDataStore
from app.engine.indicator_cache import IndicatorCache
from app.engine.futu_bridge import FutuBridge

__all__ = ['ExitStateEngine', 'CooldownManager', 'MarketDataStore', 'IndicatorCache', 'FutuBridge']
