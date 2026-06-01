"""
ORB 辅线退出栈 — RFC-056

三层独立退出, 不走主线 exit_policy:

  Layer 1: ORBFloorStop     — 硬止损 (区间中点 或 ATR×1.0, 取近者)
  Layer 2: ORBTrailingExit  — 追踪止损 (盈利>1ATR 激活, trail=1.2×ATR)
  Layer 3: ORBEODExit       — 盘末退出 (15:55 ET 强制全平)

设计原则:
  - 纯函数, 无 I/O, 可单测
  - ATR-relative 止损, 适应不同标的波动特征
  - ratchet: 追踪线只往有利方向移, 永不回退
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================
#  退出决策数据结构
# ============================================================

@dataclass
class ORBExitDecision:
    """退出决策"""
    reason: str           # ORB_FLOOR_STOP / ORB_TRAILING_STOP / ORB_EOD_EXIT / ORB_TAKE_PROFIT
    close_pct: float      # 平仓比例 (0-100)
    detail: str           # 人类可读说明

    @property
    def should_exit(self) -> bool:
        return self.close_pct > 0


# ============================================================
#  辅助函数
# ============================================================

def _signed_profit_atr(direction: str, entry: float, current: float, atr: float) -> float:
    """计算有向 ATR 盈利: LONG 正=盈利, SHORT 正=盈利"""
    if atr <= 0 or entry <= 0 or current <= 0:
        return 0.0
    if direction == "LONG":
        return (current - entry) / atr
    else:
        return (entry - current) / atr


def _breached(direction: str, current: float, stop_level: float) -> bool:
    """判断价格是否突破止损/止盈线"""
    if direction == "LONG":
        return current <= stop_level
    else:
        return current >= stop_level


# ============================================================
#  Layer 1: 硬止损 — 区间中点 或 ATR×N (取近者)
# ============================================================

class ORBFloorStop:
    """硬止损 — 区间中点 + ATR 兜底"""

    def __init__(self, atr_mult: float = 1.0):
        self._atr_mult = atr_mult

    def check(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        atr: float,
        orb_mid: float,
    ) -> Optional[ORBExitDecision]:
        """检查是否触发硬止损

        Args:
            direction: "LONG" / "SHORT"
            entry_price: 入场均价
            current_price: 当前价
            atr: ATR 值
            orb_mid: 开盘区间中点 (metadata 中保存)

        Returns:
            ORBExitDecision 或 None
        """
        if atr <= 0 or entry_price <= 0:
            return None

        # LONG: stop = max(orb_mid, entry - ATR*mult), 取更高者 (更保守)
        if direction == "LONG":
            atr_stop = entry_price - atr * self._atr_mult
            stop = max(orb_mid, atr_stop) if orb_mid > 0 else atr_stop
            if current_price <= stop:
                return ORBExitDecision(
                    reason="ORB_FLOOR_STOP",
                    close_pct=100.0,
                    detail=f"价格 {current_price:.2f} ≤ 止损 {stop:.2f} "
                           f"(orb_mid={orb_mid:.2f}, atr_stop={atr_stop:.2f})",
                )
        # SHORT: stop = min(orb_mid, entry + ATR*mult), 取更低者 (更保守)
        else:
            atr_stop = entry_price + atr * self._atr_mult
            stop = min(orb_mid, atr_stop) if orb_mid > 0 else atr_stop
            if current_price >= stop:
                return ORBExitDecision(
                    reason="ORB_FLOOR_STOP",
                    close_pct=100.0,
                    detail=f"价格 {current_price:.2f} ≥ 止损 {stop:.2f} "
                           f"(orb_mid={orb_mid:.2f}, atr_stop={atr_stop:.2f})",
                )

        return None


# ============================================================
#  Layer 2: 追踪止损 — 盈利>1ATR 激活, ratchet
# ============================================================

class ORBTrailingExit:
    """追踪止损 — Chandelier-style, ratchet-only"""

    def __init__(
        self,
        activation_profit_atr: float = 1.0,
        trail_multiplier: float = 1.2,
    ):
        self._activation = activation_profit_atr
        self._trail_mult = trail_multiplier

    def check(
        self,
        direction: str,
        current_price: float,
        atr: float,
        peak_price: float,     # LONG=持仓期间最高, SHORT=持仓期间最低
        trail_stop: float,     # 上一次计算的追踪止损线 (ratchet 用)
    ) -> Optional[ORBExitDecision]:
        """检查是否触发追踪止损

        Args:
            direction: "LONG" / "SHORT"
            current_price: 当前价
            atr: ATR 值
            peak_price: LONG 持仓最高 / SHORT 持仓最低
            trail_stop: 上一次追踪止损线 (用于 ratchet)

        Returns:
            ORBExitDecision 或 None
        """
        if atr <= 0 or current_price <= 0:
            return None

        # LONG 追踪
        if direction == "LONG":
            # 计算候选追踪线
            candidate = peak_price - atr * self._trail_mult

            # ratchet: 只上移, 不下移
            new_trail = max(trail_stop, candidate) if trail_stop > 0 else candidate

            # 当前价低于追踪线 → 触发
            if current_price <= new_trail:
                return ORBExitDecision(
                    reason="ORB_TRAILING_STOP",
                    close_pct=100.0,
                    detail=f"价格 {current_price:.2f} ≤ 追踪线 {new_trail:.2f} "
                           f"(peak={peak_price:.2f}, trail={self._trail_mult}×ATR)",
                )

        # SHORT 追踪
        else:
            candidate = peak_price + atr * self._trail_mult
            new_trail = min(trail_stop, candidate) if trail_stop > 0 else candidate

            if current_price >= new_trail:
                return ORBExitDecision(
                    reason="ORB_TRAILING_STOP",
                    close_pct=100.0,
                    detail=f"价格 {current_price:.2f} ≥ 追踪线 {new_trail:.2f} "
                           f"(peak={peak_price:.2f}, trail={self._trail_mult}×ATR)",
                )

        return None


# ============================================================
#  Layer 3: 盘末退出 — 15:55 ET 强制全平
# ============================================================

class ORBEODExit:
    """盘末退出 — 固定时间强制全平"""

    def __init__(self, eod_time: str = "15:55"):
        parts = eod_time.split(":")
        self._eod = dt_time(int(parts[0]), int(parts[1]))

    def check(self, current_time: dt_time) -> Optional[ORBExitDecision]:
        """检查是否到达盘末退出时间"""
        if current_time >= self._eod:
            return ORBExitDecision(
                reason="ORB_EOD_EXIT",
                close_pct=100.0,
                detail=f"盘末退出: {current_time.strftime('%H:%M')} ≥ {self._eod.strftime('%H:%M')}",
            )
        return None


# ============================================================
#  止盈 — ATR×N 全平 (简单直接)
# ============================================================

class ORBTakeProfit:
    """固定止盈 — 盈利达 ATR×N 全平"""

    def __init__(self, tp_atr_mult: float = 2.0):
        self._tp_mult = tp_atr_mult

    def check(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        atr: float,
    ) -> Optional[ORBExitDecision]:
        """检查是否触发止盈"""
        profit_atr = _signed_profit_atr(direction, entry_price, current_price, atr)
        if profit_atr >= self._tp_mult:
            return ORBExitDecision(
                reason="ORB_TAKE_PROFIT",
                close_pct=100.0,
                detail=f"止盈触发: profit={profit_atr:.1f}ATR ≥ {self._tp_mult}ATR "
                       f"(entry={entry_price:.2f}, current={current_price:.2f})",
            )
        return None


# ============================================================
#  组合退出栈
# ============================================================

class ORBExitStack:
    """ORB 三层退出栈 — 按优先级依次检查

    优先级: FloorStop > EODExit > TakeProfit > TrailingExit
    (硬止损 > 时间退出 > 止盈 > 追踪)
    """

    def __init__(
        self,
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 2.0,
        trail_activation_atr: float = 1.0,
        trail_multiplier: float = 1.2,
        eod_time: str = "15:55",
    ):
        self._floor = ORBFloorStop(atr_mult=sl_atr_mult)
        self._trail = ORBTrailingExit(
            activation_profit_atr=trail_activation_atr,
            trail_multiplier=trail_multiplier,
        )
        self._eod = ORBEODExit(eod_time=eod_time)
        self._tp = ORBTakeProfit(tp_atr_mult=tp_atr_mult)
        self._trail_activation = trail_activation_atr

    def evaluate(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        atr: float,
        orb_mid: float,
        peak_price: float,
        trail_stop: float,
        current_time: dt_time,
    ) -> Optional[ORBExitDecision]:
        """评估所有退出条件, 返回最先触发的

        Returns:
            ORBExitDecision (最先触发的退出) 或 None (不退出)
        """
        # 1. 硬止损 (最高优先级)
        floor = self._floor.check(direction, entry_price, current_price, atr, orb_mid)
        if floor:
            return floor

        # 2. 盘末退出
        eod = self._eod.check(current_time)
        if eod:
            return eod

        # 3. 止盈
        tp = self._tp.check(direction, entry_price, current_price, atr)
        if tp:
            return tp

        # 4. 追踪止损 (基于 peak 激活, 非 current — 价格可能已从高位回落)
        peak_profit_atr = _signed_profit_atr(direction, entry_price, peak_price, atr)
        if peak_profit_atr >= self._trail_activation:
            trail = self._trail.check(
                direction, current_price, atr, peak_price, trail_stop,
            )
            if trail:
                return trail

        return None

    def compute_trail_stop(
        self,
        direction: str,
        atr: float,
        peak_price: float,
        current_trail_stop: float,
        entry_price: float,
    ) -> float:
        """计算并返回更新后的追踪止损线 (ratchet, 用于持久化)

        即使不触发退出, 也需要每轮更新 trail_stop 并保存到 trail_state。
        """
        if atr <= 0 or peak_price <= 0:
            return current_trail_stop

        if direction == "LONG":
            candidate = peak_price - atr * self._trail._trail_mult
            return max(current_trail_stop, candidate) if current_trail_stop > 0 else candidate
        else:
            candidate = peak_price + atr * self._trail._trail_mult
            return min(current_trail_stop, candidate) if current_trail_stop > 0 else candidate
