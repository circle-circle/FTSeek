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
    持仓同步器 (支持港股/美股双市场)

    定期从 Futu OpenD 同步持仓数据到本地数据库
    """

    def __init__(self, sync_interval: int = 30, market: str = 'HK', trade_context: Optional[object] = None):
        """
        初始化持仓同步器

        Args:
            sync_interval: 同步间隔 (秒)，默认 30 秒
            market: 市场标识 ('HK' 或 'US')
            trade_context: (P0修复) 共享的 OpenSecTradeContext 实例，避免频繁创建/销毁连接
        """
        self.sync_interval = sync_interval
        self.market = market.upper()  # HK or US
        self._trade_context = trade_context
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

        logger.info(f"持仓同步器已初始化 (基准间隔：{sync_interval}秒, 市场：{self.market}, 空仓自动降频)")

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
        """判断是否在交易时段内 (含盘前准备 + 盘后缓冲)

        根据 self.market 属性判断对应市场的时间 (均为 CST UTC+8)。

        港股时间 (HK):
        - 盘前准备: 09:00-09:30 (同步确保开市前DB对齐)
        - 上午: 09:30-12:00
        - 午休: 12:00-13:00 (不同步，避免午间数据异常)
        - 下午: 13:00-16:00
        - 盘后缓冲: 16:00-16:15 (处理尾盘成交)
        - 周末/节假日: 不同步

        美股时间 (US):
        - 盘前准备: 21:00-21:30 (夏令时) / 22:00-22:30 (冬令时)
        - 交易: 21:30-04:00 (夏令时) / 22:30-05:00 (冬令时)
        - 盘后缓冲: 04:00-05:30 (夏令时) / 05:00-06:00 (冬令时)
        - 周末/节假日: 不同步
        """
        from datetime import datetime
        from app.utils.holiday_calendar import get_holiday_calendar

        now = datetime.now()
        weekday = now.weekday()

        # 判断当前是夏令时还是冬令时 (简单估算: 3月中旬到11月初为夏令时)
        month = now.month
        is_dst = 3 <= month <= 10  # 近似判断，精确判断可用 pytz

        # 确定对应的市场前缀和假日日历
        market_for_calendar = "HK" if self.market == "HK" else "US"
        
        # 周末判断
        if market_for_calendar == "HK" and weekday >= 5:
            return False
        if market_for_calendar == "US" and (weekday == 5 or weekday == 0): 
            # 美股周一到周五交易，周日晚上(周日晚CST)是周一盘前，周六晚上(周六晚CST)是周日无交易
            # 注意：CST 的周一 21:30 是美东周一上午
            # CST 的周日 21:30 是美东周日下午，无交易。
            # CST 的周五 21:30 是美东周五上午，有交易。
            # CST 的周六 04:00 (周五收盘后) 结束。
            # 所以美股交易涉及 CST 的 周日、周一、周二、周三、周四、周五
            # 排除 周六 (5) 全天。
            # 周日 (6) 是盘前，允许。
            if weekday == 5: # Saturday
                return False

        # 节假日判断
        try:
            cal = get_holiday_calendar()
            if not cal.is_trading_day(market_for_calendar, now.date()):
                return False
        except Exception:
            pass

        current_minutes = now.hour * 60 + now.minute

        if self.market == "HK":
            # 港股时间
            morning_start = 9 * 60         # 09:00
            morning_end = 12 * 60          # 12:00
            afternoon_start = 13 * 60      # 13:00
            afternoon_end = 16 * 60 + 15   # 16:15
            return (morning_start <= current_minutes < morning_end or
                    afternoon_start <= current_minutes < afternoon_end)
        else:
            # 美股时间 (CST)
            # 夏令时 (DST): 21:30 - 04:00 + 盘前 21:00 / 盘后 05:30
            # 冬令时 (Standard): 22:30 - 05:00 + 盘前 22:00 / 盘后 06:00
            
            if is_dst:
                pre_start = 21 * 60        # 21:00
                pre_end = 21 * 60 + 30     # 21:30
                trade_start = 21 * 60 + 30 # 21:30
                trade_end = 4 * 60         # 04:00 (next day)
                post_start = 4 * 60        # 04:00
                post_end = 5 * 60 + 30     # 05:30
            else:
                pre_start = 22 * 60        # 22:00
                pre_end = 22 * 60 + 30     # 22:30
                trade_start = 22 * 60 + 30 # 22:30
                trade_end = 5 * 60         # 05:00 (next day)
                post_start = 5 * 60        # 05:00
                post_end = 6 * 60          # 06:00

            # 跨越午夜的处理
            if trade_start > trade_end: # Normal case: 21:30 to 04:00
                # 晚上交易段
                is_night_trading = (current_minutes >= trade_start or current_minutes < trade_end)
                # 盘前段 (21:00-21:30) -> pre_start <= current_minutes < trade_start
                is_pre = (pre_start <= current_minutes < trade_start)
                # 盘后段 (04:00-05:30) -> trade_end <= current_minutes < post_end
                is_post = (trade_end <= current_minutes < post_end)
                return is_night_trading or is_pre or is_post
            else:
                # This case shouldn't happen for US market in CST, but handle safely
                return trade_start <= current_minutes < trade_end

    def _sync_once(self):
        """执行一次同步"""
        from app.trader.position_manager import PositionManager

        position_manager = PositionManager.get_instance()

        # 1. 获取本地持仓
        local_positions = position_manager.get_all_positions()
        
        # P0 根治 (2026-05-06): 检测本地DB重复持仓并告警
        code_counts = {}
        for p in local_positions:
            code_counts[p.code] = code_counts.get(p.code, 0) + 1
        duplicates = {c: cnt for c, cnt in code_counts.items() if cnt > 1}
        if duplicates:
            logger.warning(f"[防呆] 本地持仓存在重复: {duplicates}。请检查数据一致性。")
        
        # 构建映射 (保留最后一条)
        self._local_positions = {p.code: p for p in local_positions}

        # 2. 获取 Futu 持仓 (复用注入的 _trade_context 或临时创建)
        try:
            futu_positions = self._fetch_from_futu()
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
        """从 Futu OpenD 拉取持仓 (根据 market 属性自动选择港股/美股)
        
        P0 修复 (2026-05-22): 共享 context 僵尸引用防护
        - unsubscribe_all() 关闭 context 后，PositionSync._trade_context 仍指向已关闭对象
        - 本修复在查询失败时自动清除共享引用，下次同步创建临时 context
        """
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket
        from app import config
        
        positions = {}
        trd_ctx = self._trade_context
        try:
            # 如果未注入共享 context，则临时创建一个
            should_close = False
            if trd_ctx is None:
                trd_ctx = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.US if self.market == 'US' else TrdMarket.HK
                )
                should_close = True
            
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            ret, data = trd_ctx.position_list_query(trd_env=trd_env)
            if should_close:
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
                logger.info(f"[{self.market}Futu同步] 获取到 {len(positions)} 个持仓")
                self._last_position_count = len(positions)
            elif ret != 0:
                logger.warning(f"[{self.market}Futu同步] 查询失败: ret={ret}")
            else:
                if self._last_position_count is not None and self._last_position_count > 0:
                    logger.info(f"[{self.market}Futu同步] 持仓已全部清仓，当前 0 个")
                else:
                    logger.debug(f"[{self.market}Futu同步] 当前空仓")
                self._last_position_count = 0
        except Exception as e:
            logger.error(f"[{self.market}Futu同步] 异常: {e}")
            
            # P0 修复 (2026-05-22): 共享 context 僵尸引用防护
            # 如果查询失败且使用的是共享 context，清除引用防止下次同步继续用僵尸对象
            # 场景: unsubscribe_all() 关闭了 context → PositionSync._trade_context 仍指向已关闭对象
            #       → 每次同步都失败 → 幽灵仓无法被清理
            if not should_close and self._trade_context is not None:
                logger.warning(
                    f"[{self.market}Futu同步] 共享 context 查询失败，清除引用 "
                    f"(可能是非交易时段 context 已关闭，下次同步将创建临时 context)"
                )
                self._trade_context = None
            
            # 确保临时创建的 context 在异常时也被关闭
            if 'should_close' in locals() and should_close:
                try:
                    trd_ctx.close()
                except Exception:
                    pass
        
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
            # Futu 无持仓，但本地有 → 幽灵仓 (订单未在富途成交或已被外部删除)
            if local_pos and local_pos.status == 'OPEN':
                logger.warning(f"[持仓差异] {code}: 本地有持仓但 Futu 无持仓，清理幽灵仓")
                from datetime import datetime
                try:
                    # P0 修复 (2026-05-08): 改为 CLOSE 而非 DELETE，保留 exit_price 和 realized_pnl
                    # 用 entry_price 作为 exit_price (零盈亏平仓)，避免 exit_price=NULL 污染统计
                    exit_price = getattr(local_pos, 'entry_price', 0) or 0
                    exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    position_manager.close_position(code, exit_price, exit_time)
                    logger.info(f"[持仓修复] {code} 幽灵仓已平仓 (id={local_pos.id}, exit_price={exit_price:.4f}, 零盈亏)")
                    # 钉钉通知：幽灵仓清理
                    try:
                        from app.notifier import get_dingtalk_notifier
                        entry = getattr(local_pos, 'entry_price', 0) or 0
                        qty = getattr(local_pos, 'quantity', 0) or 0
                        get_dingtalk_notifier().send_text(
                            f"🧹 [持仓同步] 清理幽灵仓\n"
                            f"标的: {code}\n"
                            f"数量: {qty} 股\n"
                            f"开仓价: {entry:.4f}\n"
                            f"原因: 富途无此持仓 (订单可能未成交)\n"
                            f"处理: 已按 entry_price 平仓 (保留 exit_price 记录)"
                        )
                    except Exception as dt_err:
                        logger.debug(f"[持仓修复] 钉钉通知发送失败: {dt_err}")
                except Exception as e:
                    logger.error(f"[持仓修复] {code} 幽灵仓清理失败: {e}")
            return

        if local_pos is None:
            # P0 修复 (2026-05-06): 检查是否已有相同code的OPEN持仓，防止重复创建
            existing_open = position_manager.get_all_positions()
            existing_for_code = [p for p in existing_open if p.code == code]
            if existing_for_code:
                logger.warning(
                    f"[持仓差异] {code}: 本地已有 {len(existing_for_code)} 个OPEN持仓，跳过重复创建"
                )
                # 只对齐数量，不创建新记录
                for ep in existing_for_code:
                    if abs(ep.quantity - futu_pos.quantity) > 0:
                        try:
                            position_manager.update_position_fields(
                                position_id=ep.id,
                                quantity=futu_pos.quantity,
                                entry_price=futu_pos.cost_price
                            )
                            logger.info(f"[持仓修复] {code} (id={ep.id}) 数量已对齐")
                        except Exception as e:
                            logger.error(f"[持仓修复] {code} 数量更新失败: {e}")
                return
            
            # 本地无持仓，但 Futu 有 → Futu 是权威源，必须以 Futu 为准
            # P0 根治 (2026-05-22): 不再用时间窗口跳过
            # 旧逻辑: 10min 内有关闭记录 → 无限跳过 → 幽灵持仓永远不被修复
            # 新逻辑: Futu 有持仓 → 本地必须同步，无论何时关闭过
            # 真正的修复在上游: trade_executor 必须先发 Futu 订单，成功后才更新 DB
            # 如果本地确实刚刚成功平仓（Futu 订单已发），Futu 不可能仍然持有
            # 因此 Futu 仍持有 = 本地平仓未真实执行 → 必须纠正
            import sqlite3
            from datetime import datetime, timedelta
            try:
                db_path = position_manager._db_path if hasattr(position_manager, '_db_path') else 'data/ftseek.db'
                with sqlite3.connect(db_path) as conn:
                    recent_close = conn.execute(
                        "SELECT COUNT(*) FROM positions WHERE code=? AND status='CLOSED' "
                        "AND exit_time IS NOT NULL",
                        (code,)
                    ).fetchone()[0]
                if recent_close > 0:
                    logger.warning(
                        f"[持仓差异·幽灵仓修复] {code}: Futu 持有 {futu_pos.quantity}股 "
                        f"但本地已标记 CLOSED (共{recent_close}条历史记录)。"
                        f"本地平仓未真实执行到 Futu，强制同步创建"
                    )
                else:
                    logger.warning(f"[持仓差异] {code}: Futu 有持仓但本地无记录，自动创建")
            except Exception as check_err:
                logger.debug(f"[持仓差异] {code}: 历史记录检查失败: {check_err}")
                logger.warning(f"[持仓差异] {code}: Futu 有持仓但本地无记录，自动创建")
            try:
                from app.trader.position_manager import Position
                from datetime import datetime
                # P0 修复 (2026-05-11): 同步创建的持仓必须有默认止损止盈
                # 之前：不传 stop_loss/take_profit → SL=0, TP=0 → 裸奔无保护
                # 现在：根据 entry_price 计算默认 SL(3.5%) 和 TP(5.0%)
                entry_px = futu_pos.cost_price
                default_sl = round(entry_px * 0.965, 3)  # -3.5%
                default_tp = round(entry_px * 1.050, 3)  # +5.0%
                logger.info(
                    f"[持仓修复·默认风控] {code} entry=${entry_px:.2f} "
                    f"SL=${default_sl:.2f}(-3.5%) TP=${default_tp:.2f}(+5.0%)"
                )
                # P0 修复 (2026-05-22): force=True 跳过防呆检查
                # 原因: 此处已确认 Futu 持有该持仓，本地必须创建对应 OPEN 记录
                # 防呆检查 (5min 同价 CLOSED) 是为正常交易信号设计的，
                # 幽灵仓修复路径不应受其限制
                new_id = position_manager.create_position(
                    code=code,
                    action='BUY',
                    quantity=futu_pos.quantity,
                    entry_price=futu_pos.cost_price,
                    entry_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    stop_loss=default_sl,
                    take_profit=default_tp,
                    strategy='futu_sync',
                    trader_type='SYNC',
                    strategy_tags='sync',  # P2 修复 (2026-05-21): 标记同步来源
                    force=True,  # 跳过防呆，确保 OPEN 记录被创建
                )
                # 验证实际创建的是 OPEN 记录 (而非防呆返回 CLOSED ID)
                import sqlite3 as _sqlite3
                _db_path = position_manager._db_path if hasattr(position_manager, '_db_path') else 'data/ftseek.db'
                with _sqlite3.connect(_db_path) as _conn:
                    _row = _conn.execute(
                        "SELECT status FROM positions WHERE id = ?", (new_id,)
                    ).fetchone()
                if _row and _row[0] == 'OPEN':
                    logger.info(f"[持仓修复] {code} 已自动创建 (id={new_id}, Futu→DB 同步)")
                else:
                    logger.error(
                        f"[持仓修复·致命] {code} create_position 返回 id={new_id} "
                        f"但状态={_row[0] if _row else 'NOT_FOUND'}，幽灵仓未修复！"
                    )

                
                # P0 修复 (2026-05-06): 自动同步创建持仓后发送钉钉通知
                try:
                    from app.notifier import get_dingtalk_notifier
                    get_dingtalk_notifier().send_text(
                        f"📥 [持仓同步] 发现新持仓 (Futu→DB)\n"
                        f"标的: {code}\n"
                        f"数量: {futu_pos.quantity} 股\n"
                        f"成本价: {futu_pos.cost_price:.4f}\n"
                        f"来源: 持仓同步器自动创建 (trader_type=MANUAL)\n"
                        f"说明: 本地漏记，已从Futu同步"
                    )
                    logger.info(f"[持仓同步] {code} 钉钉通知已发送")
                except Exception as dt_err:
                    logger.debug(f"[持仓同步] {code} 钉钉通知发送失败: {dt_err}")
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
        比对并修复所有持仓的差异 (仅处理当前市场相关的持仓)
        """
        # Filter local positions to only include those matching self.market
        # e.g., if market is US, only look at US.* codes in local positions
        prefix = f"{self.market}."
        
        # 1. Get relevant local positions
        local_market_pos = {
            code: pos for code, pos in self._local_positions.items()
            if code.startswith(prefix) or not '.' in code  # fallback for codes without prefix
        }
        
        # 2. Get all codes to check
        all_codes = set(local_market_pos.keys()) | set(self._futu_positions.keys())

        for code in all_codes:
            local_pos = local_market_pos.get(code)
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


# 全局单例池 (支持多市场)
_syncers: Dict[str, PositionSync] = {}


def get_position_sync(market: str = 'HK', trade_context: Optional[object] = None) -> PositionSync:
    """获取持仓同步器单例 (按市场)"""
    market = market.upper()
    if market not in _syncers:
        _syncers[market] = PositionSync(market=market, trade_context=trade_context)
    elif trade_context is not None:
        # 如果已有实例但传入了 context，更新它 (支持延迟注入)
        _syncers[market]._trade_context = trade_context
    return _syncers[market]


def start_position_sync(sync_interval: int = 30, market: str = 'HK'):
    """
    启动持仓同步 (便捷函数)

    Args:
        sync_interval: 同步间隔 (秒)
        market: 市场标识 ('HK' 或 'US')
    """
    sync = get_position_sync(market=market)
    sync.sync_interval = sync_interval
    sync.start()
    return sync
