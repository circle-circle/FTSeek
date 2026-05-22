#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ExitStateEngine — 止损/止盈/闪崩/追踪 状态机引擎

架构位置:
  DataSubscriber → [ExitStateEngine] → risk_pipeline (IExitStateProvider)

设计目的:
  将止损/止盈/闪崩/追踪 状态管理从 God Class (data_subscriber.py 3615 行) 提取为独立引擎。

职责:
  1. 管理退出状态字典 (trailing_stop_cache, peak_price_cache, flash_snapshots 等)
  2. 执行止损/止盈/分批止盈
  3. 提供只读 Provider 接口给 risk_pipeline (IExitStateProvider)

约束:
  - 热路径: K_1M 退出检查 < 50ms
  - 线程安全: _stop_loss_inflight 锁保护并发止损执行
  - 依赖注入: trade_executor, position_manager, notifier 通过构造函数注入

最终归宿:
  DataSubscriber 通过依赖注入持有 ExitStateEngine 实例,
  Adapter 退役后直接由 ExitStateEngine 实现 IExitStateProvider 接口。

版本历史:
  2026-05-19 v1: 从 data_subscriber.py 提取状态字典和 Provider 接口
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple, Any

from app.core.interfaces import IExitStateProvider

logger = logging.getLogger("ftseek.engine.exit_state")


class ExitStateEngine:
    """
    止损/止盈/闪崩/追踪 状态机引擎

    状态字典 (从 data_subscriber 迁移):
      - _trailing_stop_cache: Dict[str, float] — 追踪止损价
      - _trailing_exit_time: Dict[str, float] — 追踪止盈触发时间
      - _peak_price_cache: Dict[str, float] — 持仓峰值
      - _flash_snapshots: Dict[str, Tuple[float, float]] — 闪崩快照
      - _stop_loss_cooldown: Dict[str, float] — 止损冷却时间戳
      - _stop_loss_fail_count: Dict[str, int] — 止损失败计数
      - _stop_loss_inflight: Dict[str, bool] — 止损执行锁
      - soft_stop_warnings: Dict[str, float] — 软止损预警冷却

    IExitStateProvider (只读):
      - get_trailing_exit_time(code)
      - get_trailing_stop_price(code)
      - get_peak_price(code)
    """

    def __init__(
        self,
        trade_executor: Any,
        position_manager: Any,
        notifier: Any = None,
        consensus_engine: Any = None,
    ):
        """
        初始化退出状态引擎

        Args:
            trade_executor: 交易执行器 (TradeExecutor)
            position_manager: 持仓管理器 (PositionManager)
            notifier: 通知器 (可选, 用于推送止损/止盈通知)
            consensus_engine: 共识引擎 (可选, 用于记录信号结果)
        """
        self._executor = trade_executor
        self._pm = position_manager
        self._notifier = notifier
        self._consensus = consensus_engine

        # =====================================================================
        #  状态字典 (从 data_subscriber 迁移)
        # =====================================================================

        # 追踪止损价缓存: code -> 追踪止损价
        # 多头: 盈利 >= 1% 时激活; 空头: 盈利 >= 1% 时激活
        self._trailing_stop_cache: Dict[str, float] = {}

        # 追踪止盈触发时间缓存: code -> timestamp (用于重开仓冷却判断)
        # TrailingCooldownCheck 依赖此时间戳防止短期内重新开仓
        self._trailing_exit_time: Dict[str, float] = {}

        # 持仓期间峰值价格缓存: code -> peak_price (移动止损使用)
        self._peak_price_cache: Dict[str, float] = {}

        # Flash Exit 闪崩防御快照: {code: (price, timestamp)}
        # 60s 内反向波动 >= ATR * multiplier 时立即平仓
        self._flash_snapshots: Dict[str, Tuple[float, float]] = {}

        # 止损冷却时间戳: code -> last_stop_loss_time
        # 防止频繁触发止损 (30s → 60s → 120s → 300s → 300s 递增)
        self._stop_loss_cooldown: Dict[str, float] = {}

        # 止损失败计数: code -> int (用于递增冷却时间)
        self._stop_loss_fail_count: Dict[str, int] = {}

        # 止损执行锁: code -> bool (防止同一标的多线程同时止损)
        self._stop_loss_inflight: Dict[str, bool] = {}
        self._stop_loss_inflight_lock = threading.Lock()

        # 软止损预警冷却: code -> last_warning_timestamp (防止每秒多次预警)
        self.soft_stop_warnings: Dict[str, float] = {}

        # =====================================================================
        #  配置项 (从 data_subscriber._load_exit_enhance_config 迁移)
        # =====================================================================
        self._flash_exit_enabled: bool = True
        self._flash_atr_mult: float = 1.0       # ATR 倍数阈值
        self._flash_window_seconds: float = 60.0  # 检测窗口

        self._pre_close_exit_enabled: bool = True
        self._pre_close_window_minutes: int = 15  # 距收盘触发窗口

        # =====================================================================
        #  依赖引用 (懒加载)
        # =====================================================================
        self._signal_tracker = None
        self._event_bus = None
        self._bad_orders: Dict[str, dict] = {}
        self._scenario_cache: Dict[str, Any] = {}

        logger.info(
            f"[ExitStateEngine] 初始化完成: "
            f"flash_exit={self._flash_exit_enabled}, "
            f"flash_atr_mult={self._flash_atr_mult}, "
            f"flash_window={self._flash_window_seconds}s"
        )

    # =========================================================================
    #  IExitStateProvider 接口实现 (只读)
    # =========================================================================

    def get_trailing_exit_time(self, code: str) -> float:
        """获取标的追踪止盈触发时间戳 (0=未触发)"""
        return self._trailing_exit_time.get(code, 0)

    def get_trailing_stop_price(self, code: str) -> float:
        """获取标的追踪止损价格 (0=未设置)"""
        return self._trailing_stop_cache.get(code, 0)

    def get_peak_price(self, code: str) -> float:
        """获取标的峰值价格缓存 (0=无缓存)"""
        return self._peak_price_cache.get(code, 0)

    # =========================================================================
    #  写入方法 (从 data_subscriber 迁移)
    # =========================================================================

    def update_trailing_stop(self, code: str, current_price: float, position: Any) -> None:
        """
        激活/上移追踪止盈止损价

        规则：
        - 多头：盈利 >= 1% 时激活；追踪距离 = 2×ATR；只上移不下移
        - 空头：盈利 >= 1% 时激活；追踪距离 = 2×ATR；只下移不上移
        """
        if position is None:
            return

        try:
            entry = position.entry_price
            if entry <= 0:
                return

            # 获取 ATR 百分比
            atr_pct = self._get_preferred_atr_percent(code)

            # 获取动态止损配置中的 ATR 倍数
            try:
                from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                trail_atr_mult = _dem.current_params.get('atr_stop', {}).get('multiplier', 2.0)
            except Exception:
                trail_atr_mult = 2.0

            trail_gap = current_price * atr_pct * trail_atr_mult

            # 配置项 (默认值与 data_subscriber 保持一致)
            try:
                from config.config_center import get_config_center
                cc = get_config_center()
                _trail_atr_mult = float(cc.get('risk_limits.trailing_stop_atr_mult', 3.0))
                _trail_activate = float(cc.get('risk_limits.trailing_stop_activate_pct', 0.02))
            except Exception:
                _trail_atr_mult = 3.0
                _trail_activate = 0.02

            if position.action == 'BUY':
                pnl_pct = (current_price - entry) / entry
                if pnl_pct >= _trail_activate:
                    new_trail = current_price - trail_gap * (_trail_atr_mult / 2.0)
                    old_trail = self._trailing_stop_cache.get(code, 0.0)
                    if new_trail > old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%}, gap={trail_gap*(_trail_atr_mult/2.0):.3f})"
                        )
            elif position.action == 'SELL':
                pnl_pct = (entry - current_price) / entry
                if pnl_pct >= _trail_activate:
                    new_trail = current_price + trail_gap * (_trail_atr_mult / 2.0)
                    old_trail = self._trailing_stop_cache.get(code, float('inf'))
                    if new_trail < old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈 - 空头] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%}, gap={trail_gap*1.5:.3f})"
                        )
        except Exception as e:
            logger.debug(f"[追踪止盈] 更新异常：{e}")

    def update_peak_price(self, code: str, price: float) -> None:
        """更新持仓峰值价格缓存"""
        if code not in self._peak_price_cache:
            self._peak_price_cache[code] = price
        elif price > self._peak_price_cache[code]:
            self._peak_price_cache[code] = price

    def clear_exit_state(self, code: str) -> None:
        """清理标的所有退出状态 (平仓后调用)"""
        self._trailing_stop_cache.pop(code, None)
        self._trailing_exit_time.pop(code, None)
        self._peak_price_cache.pop(code, None)
        self._flash_snapshots.pop(code, None)
        self._stop_loss_cooldown.pop(code, None)
        self._stop_loss_fail_count.pop(code, None)
        self.soft_stop_warnings.pop(code, None)
        logger.debug(f"[ExitStateEngine] {code} 退出状态已清理")

    # =========================================================================
    #  执行方法 (从 data_subscriber 迁移)
    # =========================================================================

    def execute_stop_loss(
        self,
        code: str,
        current_price: float,
        position: Any,
        signal: Any,
        trade_type: str = "STOP_LOSS",
    ) -> bool:
        """
        执行止损

        迁移自 data_subscriber._execute_stop_loss

        Args:
            code: 股票代码
            current_price: 当前价格
            position: 持仓对象
            signal: 信号对象
            trade_type: 交易类型

        Returns:
            True=执行成功，False=执行失败
        """
        # 防止并发止损
        with self._stop_loss_inflight_lock:
            if self._stop_loss_inflight.get(code, False):
                logger.warning(f"[止损] {code} 已有止损执行中，跳过")
                return False
            self._stop_loss_inflight[code] = True

        try:
            # 委托给内部实现
            return self._do_execute_stop_loss(code, signal, trade_type)
        finally:
            with self._stop_loss_inflight_lock:
                self._stop_loss_inflight.pop(code, None)

    def _do_execute_stop_loss(self, code: str, signal: Any, trade_type: str) -> bool:
        """
        内部止损执行

        迁移自 data_subscriber._do_execute_stop_loss
        """
        try:
            # 记录止损时间戳
            self._stop_loss_cooldown[code] = time.time()

            # 执行交易
            if self._executor is not None:
                result = self._executor.close_position(code, trade_type=trade_type)
                if result:
                    logger.info(f"[止损] {code} 执行成功")
                    # 通知
                    if self._notifier is not None:
                        self._notifier.send_stop_loss_notification(code, trade_type)
                    return True
                else:
                    logger.error(f"[止损] {code} 执行失败")
                    return False
            return False

        except Exception as e:
            logger.error(f"[止损] {code} 执行异常：{e}")
            return False

    def execute_take_profit(
        self,
        code: str,
        current_price: float,
        position: Any,
        signal: Any,
    ) -> bool:
        """
        执行止盈

        迁移自 data_subscriber._execute_take_profit

        Returns:
            True=执行成功，False=执行失败
        """
        try:
            # 记录追踪止盈时间
            self._trailing_exit_time[code] = time.time()

            # 执行交易
            if self._executor is not None:
                result = self._executor.close_position(code, trade_type="TAKE_PROFIT")
                if result:
                    logger.info(f"[止盈] {code} 执行成功")
                    # 清理退出状态
                    self.clear_exit_state(code)
                    return True
                else:
                    logger.error(f"[止盈] {code} 执行失败")
                    return False
            return False

        except Exception as e:
            logger.error(f"[止盈] {code} 执行异常：{e}")
            return False

    def execute_partial_take_profit(
        self,
        code: str,
        current_price: float,
        ratio: float,
        position: Any,
    ) -> bool:
        """
        执行分批止盈

        迁移自 data_subscriber._execute_partial_take_profit

        Args:
            ratio: 止盈比例 (0.0 ~ 1.0)

        Returns:
            True=执行成功，False=执行失败
        """
        try:
            if self._executor is not None:
                result = self._executor.close_position(
                    code, trade_type="PARTIAL_TAKE_PROFIT", ratio=ratio
                )
                if result:
                    logger.info(f"[分批止盈] {code} 执行成功 (比例={ratio:.0%})")
                    return True
                else:
                    logger.error(f"[分批止盈] {code} 执行失败")
                    return False
            return False

        except Exception as e:
            logger.error(f"[分批止盈] {code} 执行异常：{e}")
            return False

    def check_flash_exit(
        self,
        code: str,
        current_price: float,
        position: Any,
    ) -> bool:
        """
        检查闪崩退出

        迁移自 data_subscriber._check_flash_exit

        60s 内反向波动 >= ATR * multiplier 时立即平仓

        Returns:
            True=触发闪崩退出，False=未触发
        """
        if not self._flash_exit_enabled:
            return False

        if position is None:
            return False

        try:
            now = time.time()
            snapshot = self._flash_snapshots.get(code)

            if snapshot is None:
                # 首次记录快照
                self._flash_snapshots[code] = (current_price, now)
                return False

            last_price, last_time = snapshot
            elapsed = now - last_time

            # 更新快照
            self._flash_snapshots[code] = (current_price, now)

            # 检查时间窗口
            if elapsed > self._flash_window_seconds:
                return False

            # 计算反向波动
            if position.action == 'BUY':
                drop_pct = (last_price - current_price) / last_price
            else:  # SELL
                drop_pct = (current_price - last_price) / last_price

            # 获取 ATR 倍数阈值
            atr_pct = self._get_preferred_atr_percent(code)
            threshold = atr_pct * self._flash_atr_mult

            if drop_pct >= threshold:
                logger.warning(
                    f"[闪崩] {code} 触发闪崩退出：{drop_pct:.2%} >= {threshold:.2%} "
                    f"(elapsed={elapsed:.0f}s, atr_mult={self._flash_atr_mult})"
                )
                return True

            return False

        except Exception as e:
            logger.debug(f"[闪崩] {code} 检查异常：{e}")
            return False

    # =========================================================================
    #  内部辅助方法
    # =========================================================================

    def _get_preferred_atr_percent(self, code: str) -> float:
        """
        获取优先使用的 ATR 百分比 (默认 60M K 线)

        从 consensus_engine 或 data_subscriber 委托获取
        """
        try:
            # 尝试从 consensus_engine 获取
            if self._consensus is not None and hasattr(self._consensus, '_atr_cache'):
                return self._consensus._atr_cache.get(code, 0.02)  # 默认 2%
            # fallback: 使用默认值
            return 0.02
        except Exception:
            return 0.02

    def _get_atr(self, code: str) -> float:
        """获取标的 ATR 值 (绝对值, 非百分比)"""
        try:
            if self._consensus is not None and hasattr(self._consensus, 'get_atr'):
                return self._consensus.get_atr(code)
            # fallback: 使用峰值价格的 3% 作为默认 ATR
            peak = self._peak_price_cache.get(code, 0)
            return peak * 0.03 if peak > 0 else 1.0
        except Exception:
            return 1.0

    def _get_signal_tracker(self):
        """懒加载 signal_tracker"""
        if self._signal_tracker is None:
            try:
                from app.signal.core import get_signal_tracker
                self._signal_tracker = get_signal_tracker()
            except Exception:
                pass
        return self._signal_tracker

    def _get_event_bus(self):
        """懒加载 event_bus"""
        if self._event_bus is None:
            try:
                from app.core.event_bus import get_event_bus
                self._event_bus = get_event_bus()
            except Exception:
                pass
        return self._event_bus

    # =========================================================================
    #  生命周期管理
    # =========================================================================

    def cleanup_expired_state(self, max_age_seconds: float = 86400) -> None:
        """清理过期状态 (每日调用一次, 默认 24h 过期)"""
        now = time.time()
        expired_codes = []

        # 清理 _trailing_exit_time
        for code, ts in list(self._trailing_exit_time.items()):
            if now - ts > max_age_seconds:
                expired_codes.append(code)

        # 清理 _flash_snapshots
        for code, (price, ts) in list(self._flash_snapshots.items()):
            if now - ts > max_age_seconds:
                expired_codes.append(code)

        # 批量清理
        for code in set(expired_codes):
            self.clear_exit_state(code)

        if expired_codes:
            logger.info(f"[ExitStateEngine] 清理 {len(set(expired_codes))} 个过期标的状态")
