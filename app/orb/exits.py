"""
ORB 辅线退出栈 — RFC-056

退出逻辑 (2026-06-23 重构, 盈利 OKR 对齐 + 消除逻辑冲突):

  Layer 1: ORBFloorStop — 硬止损 (区间中点 或 ATR×1.5, 取近者)
  Layer 2: ORBEODExit    — 盘末退出 (15:55 ET 强制全平)

止盈 (2R) 与 突破失败 (breakdown) 由 ORBEngine._monitor_exits 直接判定,
  优先级高于本栈 — 因为它们依赖 sl_distance / orb_mid (engine 持有).

退出优先级 (全链路):
  1. 2R 止盈      (engine, profit ≥ take_profit_r_mult × sl_distance)
  2. Breakdown    (engine, 价格回到 orb_mid, 60s grace 后)
  3. FloorStop    (本栈, 硬止损)
  4. EOD          (本栈, 15:55 强制全平)

2026-06-23 删除项 (减法, 消除冲突/异常):
  - ORBTrailingExit: 未经验证 (SSOT 主线 trailing 84笔 -$3,773), 且 ratchet
    字段 pos.trail_stop 从不更新 → 实际每 tick 重算, 与 2R TP 抢优先级.
  - ORBTakeProfit + take_profit_atr_mult: 死代码, 2R 在 engine 先触发, 永不到达.
  - _signed_profit_atr / _breached / compute_trail_stop: 随之删除的死辅助.

设计原则:
  - 纯函数, 无 I/O, 可单测
  - ATR-relative 止损, 适应不同标的波动特征
  - 极简: 仅保留 ORB 实际生效的退出路径
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
#  退出决策数据结构
# ============================================================

@dataclass
class ORBExitDecision:
    """退出决策"""
    reason: str           # ORB_FLOOR_STOP / ORB_EOD_EXIT (止盈/突破失败在 engine 层)
    close_pct: float      # 平仓比例 (0-100)
    detail: str           # 人类可读说明

    @property
    def should_exit(self) -> bool:
        return self.close_pct > 0


# ============================================================
#  Layer 1: 硬止损 — 区间中点 或 ATR×N (取近者)
# ============================================================

class ORBFloorStop:
    """硬止损 — 区间中点 + ATR 兜底"""

    def __init__(self, atr_mult: float = 1.5):
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
#  Layer 2: 盘末退出 — 15:55 ET 强制全平
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
#  组合退出栈
# ============================================================

class ORBExitStack:
    """ORB 退出栈 — 硬止损 + 盘末退出

    止盈 (2R) 与突破失败 (breakdown) 在 ORBEngine._monitor_exits 中先行判定,
    到达本栈的仓位仅剩硬止损 / 盘末退出两条路径.

    优先级: FloorStop > EODExit (硬止损 > 时间退出)
    """

    def __init__(
        self,
        sl_atr_mult: float = 1.5,
        eod_time: str = "15:55",
    ):
        self._floor = ORBFloorStop(atr_mult=sl_atr_mult)
        self._eod = ORBEODExit(eod_time=eod_time)

    def evaluate(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        atr: float,
        orb_mid: float,
        current_time: dt_time,
    ) -> Optional[ORBExitDecision]:
        """评估退出条件, 返回最先触发的

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

        return None
