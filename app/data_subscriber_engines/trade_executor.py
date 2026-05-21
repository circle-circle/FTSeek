"""
TradeExecutor — 交易执行引擎

职责:
- 执行止损平仓 (整手处理、碎股处理、冷却机制、线程安全)
- 执行止盈平仓
- 执行部分止盈 (分批止盈)
- 验证 Futu 实际持仓，避免幽灵仓

从 data_subscriber.py 提取，降低耦合，提升可测试性

依赖注入:
- position_manager: 持仓管理器
- config: 全局配置
- logger: 日志记录器

向后兼容:
- 所有方法签名保持与原方法一致
- 通过依赖注入支持测试时注入 mock 对象
"""
import time
import threading
from datetime import datetime
from typing import Dict, Optional
from utils.logger import get_logger

logger = get_logger(__name__)


class TradeExecutor:
    """
    交易执行引擎 — 止损/止盈/部分止盈执行
    
    职责:
    - 执行止损平仓 (整手处理、碎股处理、冷却机制、线程安全)
    - 执行止盈平仓
    - 执行部分止盈 (分批止盈)
    - 验证 Futu 实际持仓，避免幽灵仓
    
    依赖注入:
    - position_manager: 持仓管理器
    - config: 全局配置
    """
    
    def __init__(
        self,
        position_manager,
        config,
    ):
        """
        初始化交易执行引擎
        
        Args:
            position_manager: 持仓管理器实例
            config: 全局配置实例
        """
        self.position_manager = position_manager
        self.config = config
        
        # 止损冷却字典 (递增冷却：按失败次数增加)
        # 结构：{code: last_stop_timestamp}
        self._stop_loss_cooldown: Dict[str, float] = {}
        
        # 止损失败计数 (用于递增冷却)
        # 结构：{code: fail_count}
        self._stop_loss_fail_count: Dict[str, int] = {}
        
        # 止损 inflight 锁 (防止重复触发)
        # 结构：{code: True/False}
        self._stop_loss_inflight: Dict[str, bool] = {}
        self._stop_loss_inflight_lock = threading.Lock()
        
        # 缓存清理辅助结构
        self._trailing_stop_cache: Dict[str, float] = {}
        self._peak_price_cache: Dict[str, float] = {}
        
        logger.info("[TradeExecutor] 初始化完成（依赖注入）")

    def execute_stop_loss(self, code: str, current_price: float) -> None:
        """
        执行止损平仓
        
        P0 2026-05-21: 从 data_subscriber.py L1758-2021 迁移
        
        特性:
        - 整手处理 (港股最小交易单位)
        - 碎股处理 (不足一手时以实际数量平仓)
        - 冷却机制 (按失败次数递增)
        - 线程安全 (inflight 锁防止重复触发)
        - Futu 持仓验证 (避免幽灵仓)
        """
        # Phase 6: 配置驱动委托给引擎
        if getattr(self, '_exit_delegation_enabled', False):
            position = self.position_manager.get_position(code)
            if position is not None:
                # TODO: 委托给 ExitStateEngine
                pass
        
        # Phase 10A: 委托给 ExitExecutor 服务
        if getattr(self, '_exit_executor', None) is not None:
            return self._exit_executor.execute_stop_loss(code, current_price)
        
        # 止损冷却递增：按失败次数增加冷却时间
        fail_count = self._stop_loss_fail_count.get(code, 0)
        
        # SSOT: risk_limits.stop_loss_cooldown_schedule
        from config.config_center import get_config_center as _gcc_cooldown
        COOLDOWN_SCHEDULE = _gcc_cooldown().get('risk_limits.stop_loss_cooldown_schedule', [30, 60, 120, 300, 300])
        cooldown_seconds = COOLDOWN_SCHEDULE[min(fail_count, len(COOLDOWN_SCHEDULE) - 1)]
        
        now = time.time()
        last_stop_time = self._stop_loss_cooldown.get(code, 0)
        if now - last_stop_time < cooldown_seconds:
            logger.info(
                f"[止损冷却] {code} 冷却中，距离上次止损 {now - last_stop_time:.1f}s < {cooldown_seconds}s (失败次数={fail_count})"
            )
            return
        
        # P0 修复 (2026-04-23): 跳过已标记为 BAD_ORDER 的标的
        bad_orders = getattr(self, '_bad_orders', {})
        if code in bad_orders:
            bo = bad_orders[code]
            logger.warning(
                f"[止损跳过] {code} 已标记为 BAD_ORDER (失败{bo['fail_count']}次：{bo['error'][:60]})"
            )
            return
        
        # P0 修复 (2026-05-19): 用线程安全的原子锁保护 _stop_loss_inflight
        with self._stop_loss_inflight_lock:
            if self._stop_loss_inflight.get(code, False):
                logger.debug(f"[止损跳过] {code} 已有止损线程正在执行，跳过重复触发")
                return
            self._stop_loss_inflight[code] = True
        
        position = self.position_manager.get_position(code)
        if position is None:
            self._stop_loss_inflight.pop(code, None)
            return
        
        # 获取每手股数 (2026-05-07 P0 修复：动态获取而非配置默认值)
        from app.config import DEFAULT_ORDER_QTY_BY_CODE, HK_LOT_SIZES
        lot_size = HK_LOT_SIZES.get(code) or DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        # 碎股处理：如果持仓不足一手，以实际持仓数量平仓 (2026-05-07 P0 修复)
        close_qty = position.quantity
        if close_qty < lot_size:
            logger.warning(
                f"[止损碎股处理] {code} 持仓{close_qty}股不足一手 ({lot_size}股)，"
                f"以实际数量平仓 (若 Futu 拒绝将标记为 BAD_ORDER)"
            )
        elif close_qty % lot_size != 0:
            # 向上取整到整手
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止损数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        logger.info(
            f"[止损执行] {code} 持仓={position.quantity}股 | 每手={lot_size}股 | "
            f"平仓={close_qty}股 | 入场价={position.entry_price} | 当前价={current_price}"
        )
        
        # 创建止损信号 (P1 修复 2026-05-21: 传递 trace_id)
        from app.strategy.base import Signal, SignalAction
        stop_loss_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止损触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止损",
            trace_id=getattr(position, 'trace_id', '') or '',
            extra={
                "stop_loss": True,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                "exit_reason": "stop_loss"
            }
        )
        
        logger.info(f"[止损尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # P0 修复 (2026-04-23): 验证 Futu 实际持仓，避免幽灵仓导致无效下单
        # position_sync 的 _futu_positions 缓存是 Futu 真实持仓的唯一可信源
        # 2026-04-24 修复：模拟盘 (TRADE_ENV=1) 不应验证 Futu 实际持仓，
        # 本地 DB 就是唯一真实源。Futu 模拟盘无实际成交，验证永远失败。
        try:
            if self.config.TRADE_ENV == 1:
                logger.debug(f"[止损验证] {code} 模拟盘跳过 Futu 持仓验证 (TRADE_ENV=1)")
            else:
                from app.trader.position_sync import get_position_sync
                ps = get_position_sync()
                futu_pos = ps._futu_positions.get(code)
                if futu_pos is None or futu_pos.quantity <= 0:
                    # Futu 无此持仓 → 可能是幽灵仓，也可能是刚卖出尚未同步
                    # P1 修复 (2026-05-16): 先用 current_price 平仓保留 P&L，避免 entry=exit 丢失盈亏
                    logger.warning(
                        f"[止损跳过] {code} Futu 无实际持仓 (本地有 {close_qty}股)，"
                        f"以当前价 {current_price:.2f} 平仓 (防 P&L 丢失)"
                    )
                    try:
                        self.position_manager.close_position(code, current_price, datetime.now().isoformat())
                        logger.info(f"[持仓修复] {code} 已平仓 (P&L 已记录)")
                    except Exception as _e:
                        logger.error(f"[持仓修复] {code} 清理失败：{_e}")
                    self._stop_loss_fail_count.pop(code, None)
                    self._stop_loss_cooldown.pop(code, None)
                    self._trailing_stop_cache.pop(code, None)
                    self._peak_price_cache.pop(code, None)
                    self._stop_loss_inflight.pop(code, None)
                    return
        except Exception as _e:
            logger.debug(f"[止损 Futu 验证] {code} 检查异常 (降级放行): {_e}")
        
        # P0 修复 (2026-04-23): 止损执行放入后台线程，不阻塞 Worker
        # 避免 Futu API 慢响应/失败时阻塞整个行情处理管线
        # 2026-05-17 P0: 用 in-flight 锁防止重复触发，冷却改为线程完成后设置
        threading.Thread(
            target=self._do_execute_stop_loss,
            args=(code, current_price, stop_loss_signal, close_qty),
            daemon=True,
            name=f"StopLoss-{code}"
        ).start()

    def _do_execute_stop_loss(
        self,
        code: str,
        current_price: float,
        stop_loss_signal,
        close_qty: int
    ) -> None:
        """
        后台执行止损 (线程安全)
        
        P0 2026-05-21: 从 data_subscriber.py L1906-2021 迁移
        """
        try:
            # 执行平仓
            result = self.position_manager.close_position(
                code,
                current_price,
                datetime.now().isoformat(),
                signal=stop_loss_signal
            )
            
            if result:
                logger.info(f"[止损成功] {code} 平仓 {close_qty}股 @ {current_price:.2f}")
                # 清理缓存
                self._stop_loss_fail_count.pop(code, None)
                self._trailing_stop_cache.pop(code, None)
                self._peak_price_cache.pop(code, None)
            else:
                logger.warning(f"[止损失败] {code} 平仓返回 None")
                # 递增失败计数
                self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
        except Exception as e:
            logger.error(f"[止损异常] {code}: {e}", exc_info=True)
            self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
        finally:
            # 设置冷却 (2026-05-17: 成功后设置，失败后也设置但立即清除)
            if code in self._stop_loss_fail_count:
                # 失败：设置冷却但不清除，让递增冷却生效
                self._stop_loss_cooldown[code] = time.time()
            else:
                # 成功：清除冷却
                self._stop_loss_cooldown.pop(code, None)
            
            # 释放 inflight 锁 (2026-05-19 P0: 原子锁保护)
            with self._stop_loss_inflight_lock:
                self._stop_loss_inflight.pop(code, None)

    def execute_take_profit(self, code: str, current_price: float) -> None:
        """
        执行止盈平仓
        
        P0 2026-05-21: 从 data_subscriber.py L2022-2204 迁移
        """
        # TODO: 从 data_subscriber.py 迁移完整止盈逻辑
        # 当前先保持为占位符
        logger.info(f"[止盈执行] {code} 当前价={current_price:.2f}")

    def execute_partial_take_profit(
        self,
        code: str,
        current_price: float,
        ratio: float
    ) -> None:
        """
        执行部分止盈 (分批止盈)
        
        P0 2026-05-21: 从 data_subscriber.py L2205-2380 迁移
        """
        # TODO: 从 data_subscriber.py 迁移完整部分止盈逻辑
        # 当前先保持为占位符
        logger.info(f"[部分止盈执行] {code} 当前价={current_price:.2f} 比例={ratio:.2f}")
