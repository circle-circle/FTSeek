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
logger.propagate = True  # 2026-04-22: 同步日志输出到主日志


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

        # 自适应频率：空仓时降频减少 OpenD 连接开销
        self._last_position_count = None  # 上次同步的持仓数
        self._effective_interval = sync_interval  # 当前生效的同步间隔

        logger.info(f"持仓同步器已初始化 (基准间隔：{sync_interval}秒, 空仓自动降频)")

    def start(self):
        """启动后台同步线程"""
        if self._running:
            logger.warning("持仓同步器已在运行中")
            return

        # P0 修复 (2026-04-23): 启动时立即执行一次对账，确保开市前本地DB与Futu对齐
        # 避免系统启动后到首次同步前的窗口期内幽灵仓触发止损
        try:
            self._sync_once()
            logger.info("[持仓同步] 启动时立即对账完成")
        except Exception as e:
            logger.warning(f"[持仓同步] 启动时对账失败: {e}")

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
        """同步循环 (后台线程运行) — 仅在交易时段内执行，空仓时自动降频"""
        from datetime import datetime
        consecutive_skips = 0
        max_skips_before_log = 120  # ~1 hour of skips before logging

        while self._running:
            try:
                if self._is_trading_period():
                    consecutive_skips = 0
                    self._sync_once()
                    self._sync_count += 1
                    # 自适应频率：空仓5分钟，有持仓30秒
                    has_positions = len(self._futu_positions) > 0
                    # P0 修复 (2026-04-23): 如果本地有持仓但Futu无 → 幽灵仓，高频对账
                    local_has = len(self._local_positions) > 0
                    has_ghost = local_has and not has_positions
                    if has_ghost:
                        new_interval = 30  # 幽灵仓存在时强制高频对账
                    else:
                        new_interval = 30 if has_positions else 300
                    if new_interval != self._effective_interval:
                        self._effective_interval = new_interval
                        mode = "高频(有持仓)" if has_positions else ("高频(幽灵仓清理)" if has_ghost else "低频(空仓)")
                        logger.info(f"[持仓同步] 同步频率切换为 {self._effective_interval}s ({mode})")
                else:
                    consecutive_skips += 1
                    if consecutive_skips == 1:
                        logger.info("[持仓同步] 非交易时段，同步已暂停 (盘前/开盘自动恢复)")
                    elif consecutive_skips >= max_skips_before_log:
                        logger.debug("[持仓同步] 非交易时段已暂停 %d 次", consecutive_skips)
                        consecutive_skips = 0  # reset to avoid log spam
            except Exception as e:
                self._error_count += 1
                logger.error(f"持仓同步失败：{e}", exc_info=True)

            # 等待下次同步（使用自适应间隔）
            for _ in range(self._effective_interval * 10):
                if not self._running:
                    break
                time.sleep(0.1)

    def _is_trading_period(self) -> bool:
        """判断是否在港股交易时段内 (含盘前准备 + 盘后缓冲)

        港股时间 (CST UTC+8):
        - 盘前准备: 09:00-09:30 (同步确保开市前DB对齐)
        - 上午: 09:30-12:00
        - 午休: 12:00-13:00 (不同步，避免午间数据异常)
        - 下午: 13:00-16:00
        - 盘后缓冲: 16:00-16:15 (处理尾盘成交)
        - 周末/节假日: 不同步
        """
        from datetime import datetime
        from app.utils.holiday_calendar import get_holiday_calendar

        now = datetime.now()
        weekday = now.weekday()

        # 周末不同步
        if weekday >= 5:
            return False

        # 节假日不同步
        try:
            cal = get_holiday_calendar()
            if not cal.is_trading_day("HK", now.date()):
                return False
        except Exception:
            pass  # 日历加载失败时降级为仅周末判断

        # 交易时段: 09:00 - 16:15 (含盘前+盘后缓冲)
        current_minutes = now.hour * 60 + now.minute
        morning_start = 9 * 60         # 09:00 (盘前准备)
        morning_end = 12 * 60          # 12:00
        afternoon_start = 13 * 60      # 13:00
        afternoon_end = 16 * 60 + 15   # 16:15 (盘后缓冲)

        return (morning_start <= current_minutes < morning_end or
                afternoon_start <= current_minutes < afternoon_end)

    def _sync_once(self):
        """执行一次同步"""
        from app.trader.position_manager import PositionManager
        from app.trader.executor import TradeExecutor

        position_manager = PositionManager.get_instance()

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
        return self._fetch_from_futu()

    def _fetch_from_futu(self) -> Dict[str, FutuPosition]:
        """从 Futu OpenD 拉取持仓 (直接调用 Futu API)"""
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket
        from app import config
        
        positions = {}
        try:
            trd_ctx = OpenSecTradeContext(
                host=config.OPEND_HOST,
                port=config.OPEND_PORT,
                filter_trdmarket=TrdMarket.HK
            )
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = trd_ctx.position_list_query(trd_env=trd_env)
            trd_ctx.close()
            
            if ret == 0 and not data.empty:
                for _, row in data.iterrows():
                    qty = float(row.get('qty', 0))
                    if qty > 0:
                        code = str(row['code'])
                        positions[code] = FutuPosition(
                            code=code,
                            quantity=int(qty),
                            available_qty=int(row.get('available_qty', 0)),
                            cost_price=float(row.get('cost_price', 0)),
                            market_price=float(row.get('nominal_price', 0)),
                            pnl=float(row.get('pl_val', 0)),
                            pnl_rate=float(row.get('pl_ratio', 0))
                        )
                logger.info(f"[Futu同步] 获取到 {len(positions)} 个港股持仓")
                self._last_position_count = len(positions)
            elif ret != 0:
                # 查询失败
                logger.warning(f"[Futu同步] 查询失败: ret={ret}")
            else:
                # 查询成功但无数据 (空仓)
                if len(positions) != self._last_position_count:
                    if self._last_position_count is not None and self._last_position_count > 0:
                        logger.info(f"[Futu同步] 持仓已全部清仓，当前 0 个")
                    else:
                        logger.debug("[Futu同步] 当前空仓，跳过冗余日志")
                    self._last_position_count = 0
                else:
                    logger.debug("[Futu同步] 当前空仓，跳过冗余日志")
        except Exception as e:
            logger.error(f"[Futu同步] 异常: {e}")
        
        return positions

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
            # Futu 无持仓，但本地有 → 已平仓但未更新，自动关闭
            if local_pos and local_pos.status == 'OPEN':
                logger.warning(f"[持仓差异] {code}: 本地有持仓但 Futu 无持仓，自动关闭")
                from datetime import datetime
                try:
                    position_manager.close_position(code, local_pos.entry_price, datetime.now().isoformat())
                    logger.info(f"[持仓修复] {code} 已自动关闭 (幽灵仓清理)")
                except Exception as e:
                    logger.error(f"[持仓修复] {code} 自动关闭失败: {e}")
            return

        if local_pos is None:
            # 本地无持仓，但 Futu 有 → 漏记，自动创建
            logger.warning(f"[持仓差异] {code}: Futu 有持仓但本地无记录，自动创建")
            try:
                from app.trader.position_manager import Position
                from datetime import datetime
                position_manager.create_position(
                    code=code,
                    action='BUY',
                    quantity=futu_pos.quantity,
                    entry_price=futu_pos.cost_price,
                    entry_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    strategy='futu_sync',
                    trader_type='MANUAL'
                )
                logger.info(f"[持仓修复] {code} 已自动创建 (Futu→DB 同步)")
            except Exception as e:
                logger.error(f"[持仓修复] {code} 自动创建失败: {e}")
            return

        # 双方都有持仓，检查数量是否一致
        if abs(local_pos.quantity - futu_pos.quantity) > 0:
            logger.warning(
                f"[持仓差异] {code}: 本地数量={local_pos.quantity}, Futu 数量={futu_pos.quantity}"
            )
            # 以 Futu 为准更新本地
            try:
                position_manager.update_position_fields(
                    position_id=local_pos.id,
                    quantity=futu_pos.quantity,
                    entry_price=futu_pos.cost_price
                )
                logger.info(f"[持仓修复] {code} 数量已对齐: {local_pos.quantity} → {futu_pos.quantity}")
            except Exception as e:
                logger.error(f"[持仓修复] {code} 数量更新失败: {e}")

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
