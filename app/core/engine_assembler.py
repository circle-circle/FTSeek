#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
引擎组装器 — 使用 DI Container 组装 DataSubscriber

架构位置:
  app/main.py (手动组装)
    ↓ 替换为
  app/core/engine_assembler.py (容器组装)

设计目的:
  1. 使用 Container 替代手动组装
  2. 统一管理引擎生命周期
  3. 支持配置驱动的组件选择

约束:
  - 单例模式：每个引擎只创建一次
  - 延迟初始化：首次 resolve 时创建
  - 循环依赖检测：防止引擎之间的循环引用

版本历史:
  2026-05-19 v1: 创建引擎组装器，支持 6 引擎 + DataSubscriber 组装
"""

import logging
from typing import Any, Optional

from app.core.container import Container

logger = logging.getLogger("ftseek.engine_assembler")


class EngineAssembler:
    """
    引擎组装器 — 使用 DI Container 组装 DataSubscriber

    使用方式:
        assembler = EngineAssembler(config=config)
        subscriber = assembler.assemble_subscriber()
    """

    def __init__(self, config: Any = None):
        """
        初始化组装器

        Args:
            config: 配置对象 (app.config)
        """
        self._config = config
        self._container = Container()

        logger.info("[EngineAssembler] 初始化完成")

    def assemble_subscriber(self, strategies: list, notifier: Any, llm_analyst: Any = None, trade_executor: Any = None) -> Any:
        """
        组装 DataSubscriber

        Args:
            strategies: 策略列表
            notifier: 通知器 (DingTalkNotifier)
            llm_analyst: LLM 分析师 (可选)
            trade_executor: 交易执行器 (可选，注入避免双实例)

        Returns:
            DataSubscriber 实例
        """
        from app.engine import (
            ExitStateEngine, CooldownManager,
            MarketDataStore, IndicatorCache, FutuBridge
        )
        from app.data_subscriber import DataSubscriber

        # 读取配置
        exit_delegation = False
        if self._config is not None:
            try:
                from config.config_center import get_config_center as _gcc
                exit_delegation = _gcc().get('engine.exit_delegation', False)
            except Exception:
                pass

        # 注册引擎
        # P0 fix (2026-05-20): 注册 trade_executor 和 position_manager 到容器
        self._container.register("futu_bridge", FutuBridge, depends=[], singleton=True)
        self._container.register("market_data_store", MarketDataStore, depends=[], singleton=True)
        self._container.register("cooldown_manager", CooldownManager, depends=[], singleton=True)
        self._container.register("indicator_cache", IndicatorCache, depends=["market_data_store"], singleton=True)

        # trade_executor + position_manager: 先注册占位，resolve 前 set_instance
        self._container.register("trade_executor", lambda: None, depends=[], singleton=True)
        if trade_executor is not None:
            self._container.set_instance("trade_executor", trade_executor)
        self._container.register("position_manager", lambda: None, depends=[], singleton=True)
        if trade_executor is not None and hasattr(trade_executor, 'position_manager'):
            self._container.set_instance("position_manager", trade_executor.position_manager)

        self._container.register("exit_engine", ExitStateEngine, depends=["trade_executor", "position_manager"], singleton=True)

        # 解析引擎
        futu_bridge = self._container.resolve("futu_bridge")
        market_data_store = self._container.resolve("market_data_store")
        exit_engine = self._container.resolve("exit_engine")
        cooldown_manager = self._container.resolve("cooldown_manager")
        indicator_cache = self._container.resolve("indicator_cache")

        # 注入依赖
        futu_bridge.set_market_data_store(market_data_store)

        # 创建 DataSubscriber
        subscriber = DataSubscriber(
            strategies=strategies,
            notifier=notifier,
            llm_analyst=llm_analyst,
            trade_executor=trade_executor,
        )

        # 注入引擎
        subscriber._futu_bridge = futu_bridge
        subscriber._market_data_store = market_data_store
        subscriber._exit_engine = exit_engine
        subscriber._cooldown_manager = cooldown_manager
        subscriber._indicator_cache = indicator_cache
        subscriber._exit_delegation_enabled = exit_delegation

        logger.info(
            f"[EngineAssembler] DataSubscriber 组装完成: "
            f"5 引擎注入 | exit_delegation={exit_delegation}"
        )

        return subscriber

    def get_container(self) -> Container:
        """获取 DI Container (用于扩展)"""
        return self._container


# 全局实例
_assembler: Optional[EngineAssembler] = None


def get_assembler(config: Any = None) -> EngineAssembler:
    """获取全局组装器实例"""
    global _assembler
    if _assembler is None:
        _assembler = EngineAssembler(config=config)
    return _assembler
