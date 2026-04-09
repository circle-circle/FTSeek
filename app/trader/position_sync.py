"""
持仓同步器 (Position Sync)

功能:
- 每 30 秒同步 Futu 持仓与本地数据库
- 防止订单/持仓状态不一致
- 检测并报告异常差异

使用示例:
    from app.trader.position_sync import PositionSync
    sync = PositionSync()
    sync.start()  # 启动后台同步线程
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger("ftseek.position_sync")
logger.propagate = False


@dataclass
class FutuPosition:
    """Futu 持仓数据模型"""
    code: str
    quantity: int
    available_qty: int
    cost_price: float
    market_price: float
    pnl: float
    pnl_rate: float


class PositionSync:
    """
    持仓同步器

    定期从 Futu OpenD 同步持仓数据到本地数据库
    """

    def __init__(self, sync_interval: int = 30):
        """
        初始化持仓同步器

        Args:
            sync_interval: 同步间隔 (秒)，默认 30 秒
        """
        self.sync_interval = sync_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_sync_time: Optional[datetime] = None
        self._sync_count = 0
        self._error_count = 0

        # 缓存同步结果
        self._futu_positions: Dict[str, FutuPosition] = {}
        self._local_positions: Dict[str, dict] = {}

        logger.info(f"持仓同步器已初始化 (同步间隔：{sync_interval}秒)")

    def start(self):
        """启动后台同步线程"""
        if self._running:
            logger.warning("持仓同步器已在运行中")
            return

        self._running = True
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info("持仓同步器已启动")

    def stop(self):
        """停止后台同步线程"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("持仓同步器已停止")

    def _sync_loop(self):
        """同步循环 (后台线程运行)"""
        while self._running:
            try:
                self._sync_once()
                self._sync_count += 1
            except Exception as e:
                self._error_count += 1
                logger.error(f"持仓同步失败：{e}", exc_info=True)

            # 等待下次同步
            for _ in range(self.sync_interval * 10):
                if not self._running:
                    break
                time.sleep(0.1)

    def _sync_once(self):
        """执行一次同步"""
        from app.trader.position_manager import PositionManager
        from app.trader.executor import TradeExecutor

        position_manager = PositionManager()

        # 1. 获取本地持仓
        local_positions = position_manager.get_all_positions()
        self._local_positions = {p.code: p for p in local_positions}

        # 2. 获取 Futu 持仓 (如已连接)
        try:
            futu_positions = self._fetch_futu_positions()
            self._futu_positions = futu_positions
        except Exception as e:
            logger.warning(f"获取 Futu 持仓失败：{e}")
            return

        # 3. 比对差异
        self._reconcile_positions(position_manager)

        self._last_sync_time = datetime.now()
        logger.debug(f"持仓同步完成 | 本地：{len(self._local_positions)} | Futu: {len(self._futu_positions)}")

    def _fetch_futu_positions(self) -> Dict[str, FutuPosition]:
        """
        从 Futu OpenD 获取持仓列表

        Returns:
            Dict[code, FutuPosition]
        """
        # TODO: 实现 Futu API 调用
        # 需要调用 TradeExecutor.get_position_list()
        # 目前返回空字典
        return {}

    def _reconcile_position(self, position_manager, code: str, local_pos, futu_pos: Optional[FutuPosition]):
        """
        比对并修复单个持仓的差异

        Args:
            position_manager: 持仓管理器实例
            code: 股票代码
            local_pos: 本地持仓对象
            futu_pos: Futu 持仓对象 (可能为 None)
        """
        if futu_pos is None:
            # Futu 无持仓，但本地有 → 可能已平仓但未更新
            if local_pos and local_pos.status == 'OPEN':
                logger.warning(f"[持仓差异] {code}: 本地有持仓但 Futu 无持仓")
                # 可选：自动关闭本地持仓
                # position_manager.close_position(code, local_pos.entry_price, datetime.now().isoformat())
            return

        if local_pos is None:
            # 本地无持仓，但 Futu 有 → 可能漏记
            logger.warning(f"[持仓差异] {code}: Futu 有持仓但本地无记录")
            # 可选：创建本地持仓记录
            return

        # 双方都有持仓，检查数量是否一致
        if abs(local_pos.quantity - futu_pos.quantity) > 0:
            logger.warning(
                f"[持仓差异] {code}: 本地数量={local_pos.quantity}, Futu 数量={futu_pos.quantity}"
            )
            # 可选：以 Futu 为准更新本地
            # position_manager.update_quantity(local_pos.id, futu_pos.quantity)

    def _reconcile_positions(self, position_manager):
        """
        比对并修复所有持仓的差异

        Args:
            position_manager: 持仓管理器实例
        """
        all_codes = set(self._local_positions.keys()) | set(self._futu_positions.keys())

        for code in all_codes:
            local_pos = self._local_positions.get(code)
            futu_pos = self._futu_positions.get(code)
            self._reconcile_position(position_manager, code, local_pos, futu_pos)

    def get_sync_status(self) -> dict:
        """
        获取同步状态

        Returns:
            包含同步状态的字典
        """
        return {
            "running": self._running,
            "last_sync_time": self._last_sync_time.isoformat() if self._last_sync_time else None,
            "sync_count": self._sync_count,
            "error_count": self._error_count,
            "local_position_count": len(self._local_positions),
            "futu_position_count": len(self._futu_positions),
        }


# 全局单例
position_sync: Optional[PositionSync] = None


def get_position_sync() -> PositionSync:
    """获取持仓同步器单例"""
    global position_sync
    if position_sync is None:
        position_sync = PositionSync()
    return position_sync


def start_position_sync(sync_interval: int = 30):
    """
    启动持仓同步 (便捷函数)

    Args:
        sync_interval: 同步间隔 (秒)

    Returns:
        PositionSync 实例
    """
    sync = get_position_sync()
    sync.sync_interval = sync_interval
    sync.start()
    return sync
