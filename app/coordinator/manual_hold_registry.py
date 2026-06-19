"""
ManualHoldRegistry — 手动持仓标记注册表 (全局单例)

设计目标:
  - 记录所有手动交易中的标的 (持仓 + 挂单)
  - 自动交易 (signal_pipeline / orb_engine) 在扫描前查询此表，命中则跳过
  - 实时更新，无延迟 (不依赖 position_sync 30s 周期)
  - 线程安全 (使用 RLock)

使用方式:
  from app.coordinator.manual_hold_registry import manual_hold_registry

  # 手动开仓/挂单成功后:
  manual_hold_registry.register(symbol, direction, qty, reason)

  # 手动全部平仓后:
  manual_hold_registry.unregister(symbol)

  # 自动管线扫描时:
  if manual_hold_registry.is_manual_held(symbol):
      continue
"""
from __future__ import annotations

import threading
import logging
from typing import Dict, Set, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ManualHoldInfo:
    symbol: str
    direction: str  # "LONG" / "SHORT"
    quantity: int
    reason: str
    registered_at: datetime = field(default_factory=datetime.now)


class ManualHoldRegistry:
    """
    手动持仓标记注册表 (全局单例)
    - 记录所有手动交易中的标的 (持仓 + 挂单)
    - 自动交易在扫描前查询此表, 命中则跳过
    - 实时更新, 无延迟
    """
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._holdings = {}
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._holdings: Dict[str, ManualHoldInfo] = {}
            self._initialized = True

    def register(self, symbol: str, direction: str, quantity: int, reason: str = ""):
        """手动开仓/挂单时立即注册"""
        with self._lock:
            self._holdings[symbol] = ManualHoldInfo(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                reason=reason,
            )
            logger.info(
                f"[ManualHoldRegistry] REGISTERED: {symbol} {direction} "
                f"qty={quantity} reason={reason}"
            )

    def unregister(self, symbol: str):
        """手动全部平仓后注销"""
        with self._lock:
            if symbol in self._holdings:
                info = self._holdings.pop(symbol)
                logger.info(
                    f"[ManualHoldRegistry] UNREGISTERED: {symbol} "
                    f"(was {info.direction} qty={info.quantity})"
                )
            else:
                logger.warning(
                    f"[ManualHoldRegistry] UNREGISTER called but {symbol} not found"
                )

    def is_manual_held(self, symbol: str) -> bool:
        """自动交易调用: 该标的是否被手动占用"""
        with self._lock:
            return symbol in self._holdings

    def get_all_manual_symbols(self) -> Set[str]:
        """获取所有手动占用的标的列表"""
        with self._lock:
            return set(self._holdings.keys())

    def get_hold_info(self, symbol: str) -> Optional[ManualHoldInfo]:
        """获取手动持仓详情 (供 Dashboard 展示)"""
        with self._lock:
            return self._holdings.get(symbol)

    def get_all_holdings(self) -> Dict[str, ManualHoldInfo]:
        """获取所有手动持仓详情"""
        with self._lock:
            return dict(self._holdings)

    def get_manual_count(self) -> int:
        """获取手动持仓数量"""
        with self._lock:
            return len(self._holdings)

    def clear(self):
        """清空所有注册 (仅用于测试 / 重置)"""
        with self._lock:
            self._holdings.clear()
            logger.info("[ManualHoldRegistry] CLEARED all holdings")

    def sync_from_positions(self):
        """
        启动时从 PositionStore 恢复手动持仓到 Registry

        遍历当前所有持仓, 若 source 为 MANUAL (含 metadata.source='manual' /
        owner_strategy='manual_sync' / DB open_positions.owner='manual'),
        则注册到 Registry. 确保重启后 auto pipeline 不会误触手动标的.

        必须在 position_sync 启动对账完成之后、scheduler.run_forever()
        (signal_pipeline / orb_engine 启动) 之前调用.
        """
        try:
            from app.positions.source import PositionSource
            from app.core.position_sync import get_us_position_sync

            pos_sync = get_us_position_sync()
            if not pos_sync or not pos_sync.position_store:
                logger.warning(
                    "[ManualHoldRegistry] Cannot sync: position_sync.position_store not available"
                )
                return

            try:
                positions = pos_sync.position_store.all()
            except Exception as _ae:
                logger.warning(
                    f"[ManualHoldRegistry] position_store.all() failed: {_ae}"
                )
                return

            # 兜底: 从 DB open_positions 读取 owner='manual' 的标的
            db_manual_symbols: Set[str] = set()
            try:
                import sqlite3
                from app.core.db_utils import get_main_db
                _conn = sqlite3.connect(str(get_main_db()), timeout=5)
                _cur = _conn.execute(
                    "SELECT symbol FROM open_positions WHERE owner='manual'"
                )
                db_manual_symbols = {row[0] for row in _cur.fetchall()}
                _conn.close()
            except Exception as _dbe:
                logger.debug(
                    f"[ManualHoldRegistry] DB owner=manual scan skipped: {_dbe}"
                )

            restored_count = 0
            for pos in positions:
                try:
                    symbol = getattr(pos, 'symbol', None) or str(pos)

                    # 1) 优先 metadata.source
                    source_str = ""
                    meta = getattr(pos, 'metadata', None)
                    if isinstance(meta, dict):
                        source_str = str(meta.get('source', '') or '')
                    # 2) 退化到 owner_strategy / strategy_id
                    if not source_str:
                        source_str = (
                            getattr(pos, 'owner_strategy', None)
                            or getattr(pos, 'strategy_id', None)
                            or ''
                        )

                    is_manual = False
                    # 严格匹配: PositionSource.MANUAL
                    if PositionSource.from_str(source_str).is_manual():
                        is_manual = True
                    # 宽松匹配: position_sync 写入的 manual_sync 实际语义为手动
                    elif str(source_str).lower() in (
                        'manual', 'manual_sync', 'futu_manual'
                    ):
                        is_manual = True
                    # DB 兜底: open_positions.owner='manual'
                    elif symbol in db_manual_symbols:
                        is_manual = True

                    if not is_manual:
                        continue

                    direction = getattr(pos, 'direction', 'UNKNOWN')
                    if hasattr(direction, 'value'):
                        direction = direction.value
                    direction = str(direction).upper()

                    quantity = (
                        getattr(pos, 'size', None)
                        or getattr(pos, 'quantity', 0)
                        or 0
                    )
                    try:
                        quantity = int(quantity)
                    except Exception:
                        quantity = 0

                    self.register(
                        symbol, direction, quantity,
                        reason="restored_on_startup",
                    )
                    restored_count += 1
                except Exception as _pe:
                    logger.warning(
                        f"[ManualHoldRegistry] restore single position failed: {_pe}"
                    )

            logger.info(
                f"[ManualHoldRegistry] Startup sync: restored "
                f"{restored_count} manual positions (scanned {len(positions)} total)"
            )
        except Exception as e:
            logger.error(f"[ManualHoldRegistry] Startup sync failed: {e}")


# 全局单例
manual_hold_registry = ManualHoldRegistry()
