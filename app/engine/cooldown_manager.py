#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CooldownManager — 信号/止损/标的 冷却管理器

架构位置:
  DataSubscriber → [CooldownManager] → risk_pipeline (ICooldownProvider)

设计目的:
  将冷却管理逻辑从 God Class (data_subscriber.py) 提取为独立模块。

职责:
  1. 管理信号冷却 (_cooldown): 推送后冷却
  2. 管理止损冷却 (_stop_loss_cooldown): 止损后冷却
  3. 管理开仓原子锁 (_open_position_ts): 防重复开仓
  4. 管理标的冷却 (_symbol_loss_cooldowns): 连续亏损后 24h 冷却

约束:
  - 线程安全: 所有写入操作持有锁
  - 热路径: 读取方法是 dict 查询 (< 0.1ms)
  - 依赖注入: 无外部依赖 (纯状态管理)

最终归宿:
  DataSubscriber 通过依赖注入持有 CooldownManager 实例,
  Adapter 退役后直接由 CooldownManager 实现 ICooldownProvider 接口。

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取冷却状态字典
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Set

from app.core.interfaces import ICooldownProvider

logger = logging.getLogger("ftseek.engine.cooldown")


class CooldownManager:
    """
    信号/止损/标的 冷却管理器

    状态字典 (从 data_subscriber 迁移):
      - _signal_cooldown: Dict[str, float] — 信号推送冷却
      - _stop_loss_cooldown: Dict[str, float] — 止损冷却时间戳
      - _open_position_ts: Dict[str, float] — 开仓原子锁 (code -> last_open_ts)
      - _symbol_loss_cooldowns: Dict[str, float] — 连续亏损冷却 (code -> cooldown_until)

    ICooldownProvider (只读):
      - is_signal_cooled(code)
      - get_last_open_ts(code)
      - get_symbol_loss_cooldown_until(code)
    """

    def __init__(self):
        """初始化冷却管理器"""

        # =====================================================================
        #  状态字典 (从 data_subscriber 迁移)
        # =====================================================================

        # 信号推送冷却: code -> last_push_time
        # 防止同一标的在短时间内重复推送
        self._signal_cooldown: Dict[str, float] = {}

        # 止损冷却时间戳: code -> last_stop_loss_time
        # 防止频繁触发止损 (30s → 60s → 120s → 300s → 300s 递增)
        self._stop_loss_cooldown: Dict[str, float] = {}

        # 开仓原子锁: code -> last_open_timestamp
        # 防止同一标的在同一秒内重复开仓 (5s 冷却)
        self._open_position_ts: Dict[str, float] = {}

        # 标的连续亏损冷却: code -> cooldown_until (epoch seconds)
        # 连续 2 笔亏损后 24h 禁止交易
        self._symbol_loss_cooldowns: Dict[str, float] = {}

        # =====================================================================
        #  锁 (保护写入操作)
        # =====================================================================
        self._signal_lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._loss_lock = threading.Lock()

        # =====================================================================
        #  配置项
        # =====================================================================
        self._signal_cooldown_sec: float = 5.0  # 信号冷却 (秒)
        self._open_cooldown_sec: float = 5.0  # 开仓冷却 (秒)
        self._symbol_loss_cooldown_hours: int = 24  # 连续亏损冷却 (小时)

        logger.info(
            f"[CooldownManager] 初始化完成: "
            f"signal={self._signal_cooldown_sec}s, "
            f"open={self._open_cooldown_sec}s, "
            f"loss={self._symbol_loss_cooldown_hours}h"
        )

    # =========================================================================
    #  ICooldownProvider 接口实现 (只读)
    # =========================================================================

    def is_signal_cooled(self, code: str) -> bool:
        """
        信号是否已冷却 (距离上次推送足够久)

        Args:
            code: 股票代码

        Returns:
            True=已冷却, False=仍在冷却中
        """
        last_push = self._signal_cooldown.get(code, 0)
        elapsed = time.time() - last_push
        return elapsed >= self._signal_cooldown_sec

    def get_last_open_ts(self, code: str) -> float:
        """
        获取标的最近一次开仓时间戳 (0=从未开仓)

        Args:
            code: 股票代码

        Returns:
            时间戳 (epoch seconds), 0 表示从未开仓
        """
        return self._open_position_ts.get(code, 0)

    def get_symbol_loss_cooldown_until(self, code: str) -> float:
        """
        获取标的连续亏损冷却截止时间戳 (0=未冷却)

        Args:
            code: 股票代码

        Returns:
            截止时间戳 (epoch seconds), 0 表示未冷却
        """
        return self._symbol_loss_cooldowns.get(code, 0)

    # =========================================================================
    #  写入方法 (从 data_subscriber 迁移)
    # =========================================================================

    def record_signal(self, code: str) -> None:
        """
        记录信号推送时间 (用于信号冷却判断)

        Args:
            code: 股票代码
        """
        with self._signal_lock:
            self._signal_cooldown[code] = time.time()
            logger.debug(f"[CooldownManager] {code} 信号冷却已记录")

    def record_open(self, code: str) -> None:
        """
        记录开仓时间戳 (用于开仓去重和信号冷却)

        Args:
            code: 股票代码
        """
        with self._open_lock:
            self._open_position_ts[code] = time.time()
            logger.debug(f"[CooldownManager] {code} 开仓时间戳已记录")

    def record_stop_loss(self, code: str) -> None:
        """
        记录止损时间戳 (用于止损冷却)

        Args:
            code: 股票代码
        """
        self._stop_loss_cooldown[code] = time.time()
        logger.debug(f"[CooldownManager] {code} 止损冷却已记录")

    def set_symbol_loss_cooldown(self, code: str, hours: float = None) -> None:
        """
        设置标的连续亏损冷却 (默认 24h)

        Args:
            code: 股票代码
            hours: 冷却时长 (小时), 默认 24h
        """
        if hours is None:
            hours = self._symbol_loss_cooldown_hours

        with self._loss_lock:
            cooldown_until = time.time() + hours * 3600
            self._symbol_loss_cooldowns[code] = cooldown_until
            logger.info(
                f"[CooldownManager] {code} 连续亏损冷却已设置: "
                f"冷却至 {datetime.fromtimestamp(cooldown_until).strftime('%Y-%m-%d %H:%M')}"
            )

    # =========================================================================
    #  清理方法
    # =========================================================================

    def cleanup_expired(self, max_age_seconds: float = 86400) -> None:
        """
        清理过期冷却记录 (每日调用一次, 默认 24h 过期)

        Args:
            max_age_seconds: 过期时长 (秒), 默认 24h
        """
        now = time.time()
        cleaned = 0

        # 清理信号冷却
        with self._signal_lock:
            expired = [c for c, ts in self._signal_cooldown.items() if now - ts > max_age_seconds]
            for code in expired:
                self._signal_cooldown.pop(code, None)
            cleaned += len(expired)

        # 清理开仓时间戳
        with self._open_lock:
            expired = [c for c, ts in self._open_position_ts.items() if now - ts > max_age_seconds]
            for code in expired:
                self._open_position_ts.pop(code, None)
            cleaned += len(expired)

        # 清理止损冷却
        expired = [c for c, ts in self._stop_loss_cooldown.items() if now - ts > max_age_seconds]
        for code in expired:
            self._stop_loss_cooldown.pop(code, None)
        cleaned += len(expired)

        # 清理标的连续亏损冷却
        with self._loss_lock:
            expired = [c for c, ts in self._symbol_loss_cooldowns.items() if now > ts]
            for code in expired:
                self._symbol_loss_cooldowns.pop(code, None)
            cleaned += len(expired)

        if cleaned > 0:
            logger.info(f"[CooldownManager] 清理 {cleaned} 条过期冷却记录")

    # =========================================================================
    #  调试方法
    # =========================================================================

    def get_stats(self) -> Dict:
        """获取冷却管理器统计信息"""
        return {
            "signal_cooldown_count": len(self._signal_cooldown),
            "open_position_ts_count": len(self._open_position_ts),
            "stop_loss_cooldown_count": len(self._stop_loss_cooldown),
            "symbol_loss_cooldowns_count": len(self._symbol_loss_cooldowns),
        }
