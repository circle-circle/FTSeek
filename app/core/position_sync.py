#!/usr/bin/env python3
"""
US Position Sync Module (FTSeek-US)
- Runtime position reconciliation between Futu SSOT and local DB
- Runs as background thread, 30s interval
- Handles both LONG and SHORT positions
"""

import json
import logging
import threading
import time
import sqlite3
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

logger = logging.getLogger("ftseek_us.position_sync")

def _get_db_path():
    from app.core.db_utils import get_main_db
    return str(get_main_db())

# Legacy module-level constant - replaced by _get_db_path() for mode isolation
DB_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'ftseek_us.db'))


class USPositionSync:
    """美股持仓同步器"""

    def __init__(self, sync_interval: int = 30, position_store=None, broker_adapter=None):
        self.sync_interval = sync_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_sync_time: Optional[datetime] = None
        self._broker_adapter = broker_adapter  # 复用执行引擎的 broker 连接
        self._sync_count = 0
        self._error_count = 0
        self.position_store = position_store  # P0 修复 2026-04-25: PositionStore 内存引用
        self._futu_ctx = None  # P2 修复: 复用 Futu 连接，避免每 30 秒新建/断开
        # P0 修复 (2026-05-11): 添加已知持仓缓存，防止连接瞬断时误删幽灵仓
        self._last_known_positions: Dict[str, Dict] = {}
        self._consecutive_empty_queries = 0
        self._max_consecutive_empty = 3  # 连续3次空查询才确认真实空仓
        # P0-2 修复 (2026-05-19): tombstone 防止刚平仓被 sync 反向恢复
        # 5/18 ARM 紊乱: 22:02:54 ghost 清理 → 22:03:25 又"从 DB 恢复" → 死循环
        # 根因: broker 异步确认延迟期间, sync 看到 broker 暂无该仓位 → 走 ghost 清理,
        # 下一轮 broker 又报有 → 走"从 DB 恢复" → 内存又有了 ARM → 引发新 close 循环
        # 解决: 主流程平仓后在 mark_closed_tombstone(), sync 看到 tombstone 就跳过 restore
        self._closed_tombstones: Dict[str, float] = {}  # symbol → close_at_ts
        self._tombstone_ttl_sec = 60.0  # 60s 内不允许 sync 恢复

    def start(self):
        if self._running:
            logger.warning("US position sync already running")
            return
        
        # P0 修复 2026-04-25: 启动时立即执行一次完整对账
        # 确保 PositionStore 与 Broker 100% 一致，避免恢复遗漏
        logger.info("🔍 [US Sync] 启动时立即执行 Broker 对账...")
        try:
            self._sync_once()
            logger.info("✅ [US Sync] 启动对账完成")
        except Exception as e:
            logger.warning(f"⚠️ [US Sync] 启动对账失败 (将继续后台同步): {e}")
        
        self._running = True
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info("US position sync started (interval=%ds)", self.sync_interval)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("US position sync stopped")

    # ─── P0-2 (2026-05-19): tombstone API — 防止 sync 反向恢复刚平仓 ───
    def mark_closed_tombstone(self, symbol: str) -> None:
        """主流程平仓成功后调用. sync 在 ttl 内不允许从 DB 恢复此 symbol."""
        if not symbol:
            return
        self._closed_tombstones[symbol] = time.time()
        logger.debug(f"[US Sync] tombstone 标记 {symbol} (ttl={self._tombstone_ttl_sec:.0f}s)")

    def _is_tombstoned(self, symbol: str) -> bool:
        """检查 symbol 是否在 tombstone ttl 内 (期间不允许 sync 恢复). 顺带清理过期项."""
        ts = self._closed_tombstones.get(symbol)
        if ts is None:
            return False
        if time.time() - ts > self._tombstone_ttl_sec:
            self._closed_tombstones.pop(symbol, None)
            return False
        return True

    def _sync_loop(self):
        """同步循环 (后台线程运行) — 根据市场阶段动态调整频率"""
        consecutive_skips = 0
        max_skips_before_log = 120  # 每 30 分钟 (15s * 120) 输出一次跳过日志

        while self._running:
            try:
                phase, interval = self._get_market_phase()
                if phase == "CLOSED":
                    consecutive_skips += 1
                    if consecutive_skips == 1:
                        logger.info("[US Sync] Market closed, sync paused (will resume next session)")
                    elif consecutive_skips >= max_skips_before_log:
                        logger.debug("[US Sync] Skipped %d cycles (market closed)", consecutive_skips)
                        consecutive_skips = 0
                    # 休眠 interval 秒
                    for _ in range(interval * 10):
                        if not self._running:
                            break
                        time.sleep(0.1)
                    continue

                if consecutive_skips > 0:
                    logger.info(f"[US Sync] Market {phase}, sync resumed (interval={interval}s)")
                consecutive_skips = 0
                self._sync_once()
                self._sync_count += 1
                # 休眠 interval 秒
                for _ in range(interval * 10):
                    if not self._running:
                        break
                    time.sleep(0.1)
            except Exception as e:
                self._error_count += 1
                logger.error("US position sync failed: %s", e, exc_info=True)
                # 异常后等 60 秒再重试
                for _ in range(600):
                    if not self._running:
                        break
                    time.sleep(0.1)

    def _get_market_phase(self) -> tuple:
        """
        判断当前市场阶段并返回 (phase, sync_interval)

        美股时间 (ET, UTC-4 夏令时 / UTC-5 冬令时):
        - PRE_MARKET:  04:00 - 09:30 ET → 同步间隔 300s (5 分钟)
        - TRADING:     09:30 - 16:00 ET → 同步间隔 30s (默认)
        - AFTER_MARKET: 16:00 - 20:00 ET → 同步间隔 300s (5 分钟)
        - CLOSED:      20:00 - 04:00 ET → 暂停同步

        北京时间 (CST, UTC+8):
        - 夏令时 (3月-11月): ET+12h
          PRE:  16:00-21:30 CST
          TRADE: 21:30-04:00 CST (次日)
          AFTER: 04:00-08:00 CST
          CLOSED: 08:00-16:00 CST
        - 冬令时 (11月-3月): ET+13h
          PRE:  17:00-22:30 CST
          TRADE: 22:30-05:00 CST (次日)
          AFTER: 05:00-09:00 CST
          CLOSED: 09:00-17:00 CST
        """
        from datetime import datetime
        import pytz

        now = datetime.now()
        weekday = now.weekday()

        # 周末判定
        if weekday == 5:  # 周六
            return ("CLOSED", 1800)  # 30 分钟检查一次
        if weekday == 4:  # 周五 08:00 CST 之后到周一 16:00 CST 之间关闭
            if now.hour >= 8:
                return ("CLOSED", 1800)
        if weekday == 6:  # 周日
            if now.hour < 16:
                return ("CLOSED", 1800)

        # 节假日检查
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            cal = get_holiday_calendar()
            if not cal.is_trading_day("US", now.date()):
                # 周一凌晨 05:00 前可能是周五夜盘延续
                if weekday == 0 and now.hour < 5:
                    pass
                elif weekday == 0 and now.hour < 8:
                    # 冬令时周一 08:00 CST = 周五 19:00 ET (盘后)
                    pass
                else:
                    return ("CLOSED", 1800)
        except Exception:
            pass

        # 转换为美东时间进行精确判断
        try:
            et_tz = pytz.timezone("US/Eastern")
            et_now = now.astimezone(et_tz)
            et_hour = et_now.hour
            et_minute = et_now.minute
            et_time = et_hour * 60 + et_minute  # 分钟数
        except Exception:
            # fallback: 使用 CST 估算 (假设夏令时)
            et_hour = (now.hour - 12) % 24
            et_time = et_hour * 60 + now.minute

        # 市场阶段判断 (ET)
        pre_market_start = 4 * 60       # 04:00 ET
        market_open = 9 * 60 + 30       # 09:30 ET
        market_close = 16 * 60          # 16:00 ET
        after_market_end = 20 * 60      # 20:00 ET

        if pre_market_start <= et_time < market_open:
            return ("PRE_MARKET", 300)     # 5 分钟
        elif market_open <= et_time < market_close:
            return ("TRADING", self.sync_interval)  # 默认 30 秒
        elif market_close <= et_time < after_market_end:
            return ("AFTER_MARKET", 300)  # 5 分钟
        else:
            return ("CLOSED", 1800)        # 30 分钟

    def _get_futu_context(self, opend_host: str, opend_port: int, trade_env, trd_market, security_firm):
        """获取或复用 Futu 交易上下文 (避免每 30 秒新建/断开连接)"""
        from futu import OpenSecTradeContext, TrdEnv, RET_OK
        
        if self._futu_ctx is None:
            try:
                self._futu_ctx = OpenSecTradeContext(
                    filter_trdmarket=trd_market,
                    host=opend_host,
                    port=opend_port,
                    is_encrypt=False,
                )
                # 实盘模式需要解锁交易
                if trade_env == TrdEnv.REAL:
                    import os
                    pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
                    if pwd_md5:
                        ret, data = self._futu_ctx.unlock_trade(
                            password_md5=pwd_md5, is_unlock=True,
                        )
                        if ret != RET_OK:
                            logger.error(f"[US Sync] 实盘解锁失败: {data}")
                            self._futu_ctx = None
                            return None
                        logger.info("[US Sync] ✅ 实盘交易已解锁")
                    else:
                        logger.error("[US Sync] 实盘模式但 FUTU_UNLOCK_PWD_MD5 未设置")
                        self._futu_ctx = None
                        return None
            except Exception as e:
                logger.warning(f"[US Sync] 创建 Futu 连接失败: {e}")
                return None
        return self._futu_ctx

    def query_broker_positions(self) -> Dict[str, Dict]:
        """
        公共方法: 查询 Broker 持仓 (复用连接，不创建/关闭新连接)
        
        供 _reconcile_position_store 等外部模块调用，避免各自创建 OpenSecTradeContext。
        优先复用 broker_adapter 的 trade_ctx (已解锁)，失败时回退到自建连接。
        
        Returns:
            {symbol: {'qty': float, 'cost': float, 'direction': 'LONG'|'SHORT'}}
        """
        from futu import RET_OK, TrdEnv, TrdMarket, SecurityFirm
        import os
        from app.core.config_container import get_config
        
        _trade_env = get_config().trade_env_int
        trade_env = TrdEnv.SIMULATE if _trade_env == 1 else TrdEnv.REAL
        
        # 优先复用 broker_adapter 的已解锁连接
        ctx = None
        if self._broker_adapter and getattr(self._broker_adapter, 'connected', False):
            ctx = getattr(self._broker_adapter, 'trade_ctx', None)
            if ctx:
                logger.debug("[US Sync] 复用 broker_adapter trade_ctx")
        
        # 回退: 自建连接
        if ctx is None:
            opend_host = os.getenv("FUTU_OPEND_HOST", os.getenv("OPEND_HOST", "127.0.0.1"))
            opend_port = int(os.getenv("FUTU_OPEND_PORT", os.getenv("OPEND_PORT", "11111")))
            ctx = self._get_futu_context(opend_host, opend_port, trade_env, TrdMarket.US, SecurityFirm.FUTUINC)
            if ctx is None:
                return {}
        
        try:
            ret, data = ctx.position_list_query(trd_env=trade_env)
            if ret != RET_OK:
                logger.warning(f"[US Sync] query_broker_positions 查询失败 (ret={ret})，重置连接")
                self._futu_ctx = None
                return {}
            if data is None or data.empty:
                # 空持仓可能是真实的，不重置连接
                return {}
            
            positions = {}
            for _, row in data.iterrows():
                qty = float(row.get('qty', 0))
                if qty != 0:
                    code = str(row['code'])
                    # 2026-06-11: 跳过非美股标的 (如 HK.00700)
                    if not code.startswith('US.') or code.count('.') > 1:
                        continue
                    if not code.startswith('US.'):
                        code = 'US.' + code
                    side = str(row.get('position_side', ''))
                    is_short = (qty < 0) or (side == 'SHORT')
                    positions[code] = {
                        'qty': abs(qty),
                        'cost': float(row.get('cost_price', 0)),
                        'direction': 'SHORT' if is_short else 'LONG'
                    }
            return positions
        except Exception as e:
            logger.warning(f"[US Sync] query_broker_positions 异常: {e}")
            self._futu_ctx = None
            return {}

    def _query_today_futu_orders(self) -> Dict[str, Dict]:
        """
        查询 Futu 当日已成交委托，用于区分策略开仓 vs 手动开仓。
        
        Returns:
            {symbol: {'order_id': str, 'price': float, 'qty': float, 'direction': str, 'time': str}}
        """
        from futu import RET_OK, TrdEnv
        from app.core.config_container import get_config
        import os
        
        _trade_env = get_config().trade_env_int
        trade_env = TrdEnv.SIMULATE if _trade_env == 1 else TrdEnv.REAL
        
        # 复用已有的 _futu_ctx（不新建连接）
        if self._futu_ctx is None:
            return {}
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        result = {}
        
        try:
            ret, data = self._futu_ctx.history_order_list_query(
                start=today_str,
                end=today_str,
                trd_env=trade_env,
            )
            if ret != RET_OK or data is None or data.empty:
                return {}
            
            for _, order in data.iterrows():
                code = str(order.get('code', ''))
                # 2026-06-11: 跳过非美股标的
                if not code.startswith('US.') or code.count('.') > 1:
                    continue
                if not code.startswith('US.'):
                    code = 'US.' + code
                dealt_qty = float(order.get('dealt_qty', 0))
                if dealt_qty <= 0:
                    continue  # 未成交的跳过
                
                order_type = str(order.get('order_type', ''))
                trd_side = str(order.get('trd_side', ''))
                # BUY = LONG开仓 or SHORT平仓; SELL = SHORT开仓 or LONG平仓
                # 简化处理：记录所有已成交委托，后续用 direction 匹配
                price = float(order.get('price', 0))
                order_id = str(order.get('order_id', ''))
                create_time = str(order.get('create_time', ''))
                
                # 保留最新的一笔（可能被多次 fill 覆盖）
                if code not in result or create_time > result[code].get('time', ''):
                    result[code] = {
                        'order_id': order_id,
                        'price': price,
                        'qty': dealt_qty,
                        'trd_side': trd_side,
                        'time': create_time,
                    }
            
            if result:
                logger.info(f"[US Sync·P3] Futu 当日委托: {len(result)} 只标的有成交记录")
        except Exception as e:
            logger.debug(f"[US Sync·P3] 查询 Futu 当日委托失败: {e}")
        
        return result

    def _sync_once(self):
        from futu import RET_OK, TrdEnv, TrdMarket, SecurityFirm
        import os

        # P0 修复 2026-04-25: 使用环境变量而非硬编码
        from app.core.config_container import get_config
        _trade_env = get_config().trade_env_int
        trade_env = TrdEnv.SIMULATE if _trade_env == 1 else TrdEnv.REAL
        opend_host = os.getenv("FUTU_OPEND_HOST", os.getenv("OPEND_HOST", "127.0.0.1"))
        opend_port = int(os.getenv("FUTU_OPEND_PORT", os.getenv("OPEND_PORT", "11111")))

        # 1. Get Futu positions (复用连接)
        futu_positions = {}
        ctx = self._get_futu_context(opend_host, opend_port, trade_env, TrdMarket.US, SecurityFirm.FUTUINC)
        if ctx is None:
            return
        try:
            # P0 修复 (2026-05-27): 使用 refresh_cache=False (默认) — 缓存数据更可靠
            # Futu SDK 的 refresh_cache=True 可能因服务端延迟返回空结果，
            # 导致真实持仓被误判为幽灵仓。缓存由 Futu OpenD 维护，数据可靠。
            # 真正的根因修复: _closed_symbols 过滤已改为 exit_price > 0 (排除假 ghost 记录)
            ret, data = ctx.position_list_query(trd_env=trade_env)
            # P0 修复: 查询失败或返回异常 → 重置连接 + 跳过本轮，防止误删持仓
            if ret != 0:
                logger.warning(f"[US Sync] Futu 持仓查询失败 (ret={ret})，重置连接，跳过本轮")
                self._futu_ctx = None
                return
            # P0 修复 (2026-05-11): 空持仓处理 — 使用连续空查询计数器 + 已知持仓缓存
            # 之前问题: 连接瞬断时返回空持仓列表，导致真实持仓被误删为幽灵仓
            if data is None or data.empty:
                self._consecutive_empty_queries += 1
                if self._consecutive_empty_queries < self._max_consecutive_empty:
                    # 使用上次已知的持仓数据继续同步，不跳过
                    if self._last_known_positions:
                        # P0 修复 (2026-05-27): 移除 _closed_symbols_cache 过滤器
                        # 同主路径逻辑: broker 是 SSOT，缓存是 broker 快照，不应按历史交易记录过滤。
                        # 已平仓的 symbol 不会出现在 broker 持仓中，自然不在缓存里。
                        logger.warning(
                            f"[US Sync] Futu 返回空持仓列表 (连续{self._consecutive_empty_queries}次), "
                            f"使用上次已知持仓({len(self._last_known_positions)}只)继续同步"
                        )
                        futu_positions = dict(self._last_known_positions)
                    else:
                        logger.warning(
                            f"[US Sync] Futu 返回空持仓列表 (连续{self._consecutive_empty_queries}次), "
                            f"无已知持仓缓存，跳过本轮"
                        )
                        self._futu_ctx = None
                        return
                else:
                    # 连续多次空查询，认为真实空仓
                    logger.warning(
                        f"[US Sync] Futu 连续{self._max_consecutive_empty}次返回空持仓列表, "
                        f"确认真实空仓，清理已知持仓缓存"
                    )
                    self._last_known_positions = {}
                    self._consecutive_empty_queries = 0
                    # 继续执行: futu_positions 保持空，将清理所有幽灵仓
            else:
                # 非空查询，重置计数器
                self._consecutive_empty_queries = 0
                # P0 修复 (2026-05-27): 移除 _closed_symbols 过滤器
                # 根因: QCOM/SNDK 有真实历史交易 (exit_price>0, TRAILING_STOP/sl_hit)，
                # 导致被永久排除 → position_sync 看不到 → 误判为幽灵仓 → 死循环。
                # Broker 是 SSOT (唯一事实源): 如果 broker 当前持有某 symbol，就必须跟踪。
                # PRICE_FAIL 期权已由独立的 _price_fail_closed 机制保护 (line 463-480)。
                for _, row in data.iterrows():
                    qty = float(row.get('qty', 0))
                    if qty != 0:
                        code = str(row['code'])
                        # 2026-06-11: 跳过非美股标的 (如 HK.00700)
                        if not code.startswith('US.') or code.count('.') > 1:
                            continue
                        if not code.startswith('US.'):
                            code = 'US.' + code
                        futu_positions[code] = {
                            'qty': abs(qty),
                            'cost': float(row.get('cost_price', 0)),
                            'direction': 'SHORT' if ((qty < 0) or (str(row.get('position_side', '')) == 'SHORT')) else 'LONG'
                        }
                # 更新已知持仓缓存
                self._last_known_positions = dict(futu_positions)
        except Exception as e:
            logger.warning("Failed to get Futu US positions: %s", e)
            # 连接异常时重置，下次同步会重新创建
            self._futu_ctx = None
            return

        # 2. Get DB positions
        if not os.path.exists(_get_db_path()):
            logger.warning("US DB not found: %s", _get_db_path())
            return

        # P0-4 修复 (2026-06-12): timeout=10 + busy_timeout 防止并发写时 "database is locked"
        conn = sqlite3.connect(_get_db_path(), timeout=10)
        # M12 (2026-06-03): 与 TradeStore 对齐 WAL 模式, 防止 journal mode 冲突
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        cursor = conn.cursor()

        # Ensure trader_type column exists
        try:
            cursor.execute("ALTER TABLE open_positions ADD COLUMN trader_type TEXT DEFAULT 'MANUAL'")
            conn.commit()
            logger.info("[US Sync] Added trader_type column")
        except sqlite3.OperationalError:
            pass  # 列已存在，正常跳过

        # Get current DB positions
        # P0 (2026-06-05): 过滤已关闭记录，避免 ghost 仓每 30s 重复检测刷屏
        cursor.execute(
            "SELECT symbol, size, direction, COALESCE(owner,'auto'), COALESCE(sync_status,'') FROM open_positions "
            "WHERE sync_status IS NULL "
            "OR sync_status NOT IN ("
            "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
            "'closed_emergency','stale_not_in_broker')"
        )
        db_positions = {}
        for row in cursor.fetchall():
            db_positions[row[0]] = {'size': row[1], 'direction': row[2], 'owner': row[3], 'sync_status': row[4]}
        # P3 修复 (2026-06-12): DB 中已有 owner='manual' 的符号，PositionStore 同步时保持 manual
        self._db_manual_symbols = {sym for sym, info in db_positions.items() if info['owner'] == 'manual'}

        futu_symbols = set(futu_positions.keys())
        db_symbols = set(db_positions.keys())

        # 3. Close ghost positions (DB has, Futu doesn't)
        # P0 修复 (2026-05-04): 新创建不足60秒的持仓跳过清理，等待 Futu 确认成交
        _now = datetime.now()
        for symbol in db_symbols - futu_symbols:
            # P0 修复 2026-04-25: 检查是否为 pending_close 标记 (通过 action_log)
            cursor.execute("SELECT action_log_json, entry_time FROM open_positions WHERE symbol=?", (symbol,))
            row = cursor.fetchone()
            is_pending_close = False
            if row and row[0]:
                try:
                    actions = json.loads(row[0])
                    if actions and isinstance(actions, list):
                        last_action = actions[-1].get("action", "")
                        is_pending_close = (last_action == "pending_close")
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

            # P0 修复 (2026-05-04 / 2026-05-12): 新持仓保护窗口 — 给 Futu 180s 确认成交
            # 2026-05-12 延长 60s→180s: Scalping 开仓后 Futu SIMULATE 延迟明显，60s 不足
            if row and row[1]:
                try:
                    entry_dt = datetime.fromisoformat(str(row[1]))
                    age_seconds = (_now - entry_dt).total_seconds()
                    if age_seconds < 180 and not is_pending_close:
                        logger.info(
                            "[US Sync] 跳过新持仓 %s (创建 %.0fs 前, 等待 Futu 确认成交)",
                            symbol, age_seconds,
                        )
                        continue
                except (ValueError, TypeError):
                    pass

            if is_pending_close:
                logger.info(f"[US Sync] ✅ 确认平仓 {symbol} (Futu 已无，pending_close 完成)")
            else:
                logger.warning("[US Sync] Ghost position: %s (DB only, closing)", symbol)
            # M10 (2026-06-03): 软删除保留审计记录, 替代硬 DELETE
            cursor.execute(
                "UPDATE open_positions SET sync_status='closed_by_sync', updated_at=? WHERE symbol=?",
                (datetime.now().isoformat(), symbol)
            )

        # 4. Update/Insert Futu positions
        # P0 fix (2026-05-21): Load PRICE_FAIL-closed set to skip re-insert/update
        _pf_closed = set()
        try:
            import json as _pfj
            _pf_p = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
            if os.path.exists(_pf_p):
                _pf_closed = set(_pfj.load(open(_pf_p)))
        except Exception:
            pass

        # P3 修复 (2026-06-09): 查询 Futu 当日委托 (DB INSERT 和 position_store 共用)
        _today_orders = self._query_today_futu_orders()
        self._today_orders = _today_orders  # 供 _sync_to_position_store_locked 使用

        # P3 修复 (2026-06-12): 查询 manual_order_audit 今日手动订单，防止被误标为 auto_main
        _manual_today = set()
        try:
            today_et = (datetime.now(timezone(timedelta(hours=-4)))).strftime('%Y-%m-%d')
            cursor.execute(
                "SELECT DISTINCT symbol FROM manual_order_audit "
                "WHERE status='submitted' AND date(timestamp) = ?",
                (today_et,)
            )
            _manual_today = {row[0] for row in cursor.fetchall()}
            if _manual_today:
                logger.info(f"[US Sync·P3] 今日手动订单标的: {_manual_today}")
            self._manual_today = _manual_today
        except Exception as e:
            logger.debug(f"[US Sync·P3] manual_order_audit 查询失败: {e}")
            self._manual_today = set()

        # 实盘隔离: 自管持仓不纳入系统管理 (yaml + DB owner='manual' 合并)
        try:
            from app.core.config_container import get_all_manual_symbols
            _self_managed = get_all_manual_symbols()
        except Exception:
            _self_managed = set()

        for symbol, info in futu_positions.items():
            # 2026-06-11: 跳过非美股标的 (防御性检查)
            if not symbol.startswith('US.') or symbol.count('.') > 1:
                continue
            # P0 fix: Skip PRICE_FAIL-closed options entirely (no DB update or insert)
            if symbol in _pf_closed:
                # Also clean from open_positions if present (prevents _restore_position_from_db)
                if symbol in db_positions:
                    cursor.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
                    logger.warning(f"[US Sync·P0] 清理 PRICE_FAIL 已关闭期权(DB): {symbol}")
                continue

            # 实盘隔离: 跳过自管持仓，不纳入系统管理 (保留 DB 记录，owner='manual')
            if _self_managed and symbol in _self_managed:
                if symbol in db_positions:
                    db_info = db_positions[symbol]
                    db_owner = db_info.get('owner', 'auto')
                    # 更新 owner + 修正 stub 行的 direction/size/sync_status
                    needs_owner_fix = db_owner != 'manual'
                    needs_data_fix = (db_info.get('sync_status') == 'pending_sync' or
                                      abs(db_info['size'] - info['qty']) > 0.1 or
                                      db_info['direction'] != info['direction'])
                    if needs_owner_fix or needs_data_fix:
                        cursor.execute(
                            "UPDATE open_positions SET owner='manual', direction=?, size=?, "
                            "avg_entry_price=?, sync_status='synced', updated_at=? WHERE symbol=?",
                            (info['direction'], info['qty'], info['cost'],
                             datetime.now().isoformat(), symbol))
                        if needs_owner_fix:
                            logger.info(f"[US Sync·隔离] {symbol} owner→manual + 数据修正")
                else:
                    # 首次发现自管持仓，写入 DB 标 manual
                    # P0-4 修复 (2026-06-12): UPSERT 防止与 manual_order_service stub 行 UNIQUE 冲突
                    cursor.execute(
                        "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, owner, sync_status, updated_at) "
                        "VALUES (?, ?, ?, ?, 'manual', 'synced', ?) "
                        "ON CONFLICT(symbol) DO UPDATE SET "
                        "direction=excluded.direction, size=excluded.size, "
                        "avg_entry_price=excluded.avg_entry_price, owner='manual', "
                        "sync_status='synced', updated_at=excluded.updated_at",
                        (symbol, info['direction'], info['qty'], info['cost'], datetime.now().isoformat()))
                    logger.info(f"[US Sync·隔离] 录入自管持仓: {symbol} ({info['direction']} {info['qty']}股)")
                continue

            if symbol in db_positions:
                # Update existing
                db_info = db_positions[symbol]
                size_changed = abs(db_info['size'] - info['qty']) > 0.1
                # P0 修复 (2026-06-03): 方向翻转检测 — 原代码仅检查 size 变化
                # 根因: XLF 从 SHORT -11202 翻转为 LONG +11202 时 abs size 不变，
                # UPDATE 被跳过 → DB 方向过期 → 平仓单变成加仓单
                direction_changed = db_info.get('direction', '') != info['direction']
                if size_changed or direction_changed:
                    if direction_changed:
                        logger.warning(
                            f"[US Sync·方向翻转] {symbol} {db_info.get('direction','?')}→{info['direction']} "
                            f"(size={info['qty']}, cost=${info['cost']:.2f})"
                        )
                    else:
                        logger.info("[US Sync] Updating %s: size %s -> %s", symbol, db_info['size'], info['qty'])
                    cursor.execute(
                        "UPDATE open_positions SET size=?, direction=?, avg_entry_price=?, updated_at=?, sync_status='synced' WHERE symbol=?",
                        (info['qty'], info['direction'], info['cost'], datetime.now().isoformat(), symbol)
                    )
            else:
                # P2 修复 (2026-04-30): 插入新持仓时验证仓位占比 + 使用 ATR 动态止损
                # 1. 仓位占比验证
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    capital = get_capital_safety_system()
                    total_equity = capital._state.current_equity if capital._state.current_equity > 0 else 100000
                    position_value = info['qty'] * info['cost']
                    position_pct = position_value / total_equity
                    
                    # 获取配置限制
                    try:
                        from app.core.config_container import get_config
                        cfg = get_config()
                        max_per_symbol = float(cfg.get_dotted("risk.position.max_per_symbol", 0.25))
                    except Exception:
                        max_per_symbol = 0.25
                    
                    if position_pct > max_per_symbol:
                        # 根因修复 (2026-06-05): 超限仓位触发自动减仓
                        # 计算需要平掉的股数
                        max_value = total_equity * max_per_symbol
                        excess_value = position_value - max_value
                        excess_shares = int(excess_value / info['cost'])
                        
                        if excess_shares > 0:
                            logger.warning(
                                f"🚫 [US Sync] {symbol} 占比 {position_pct:.1%} 超限 {max_per_symbol:.0%}，"
                                f"触发自动减仓: {excess_shares}股 (${excess_value:,.0f}) | "
                                f"当前 {info['qty']}股(${position_value:,.0f}) → 目标 {info['qty']-excess_shares}股(${max_value:,.0f})"
                            )
                            # 标记需要减仓，在 sync 循环结束后统一处理
                            if not hasattr(self, '_pending_trims'):
                                self._pending_trims = {}
                            self._pending_trims[symbol] = {
                                'shares': excess_shares,
                                'direction': info['direction'],
                                'reason': f'sync_over_limit_{max_per_symbol:.0%}',
                            }
                        else:
                            logger.warning(
                                f"⚠️ [US Sync] {symbol} 占比 {position_pct:.1%} 超限 {max_per_symbol:.0%}，"
                                f"但计算减仓股数为 0 (市值 ${position_value:,.0f} / 净值 ${total_equity:,.0f})"
                            )
                except Exception as e:
                    logger.debug(f"[US Sync] 仓位验证失败 (不影响同步): {e}")
                
                # 2. ATR 动态止损 (根因修复 2026-06-05)
                # SL 与 exit_policy.yaml atr_stop.multiplier=4.0 对齐
                # 1分钟 ATR 值小, 4× 给出合理日内止损距离 (ETF≈4%, 个股≈3-5%)
                # TP 用百分比而非 ATR×N (ATR×4 对 ETF 是幻影目标)
                try:
                    atr = self._get_symbol_atr(symbol, info['cost'])
                    atr_mult = self._get_atr_stop_mult()  # SSOT: exit_policy.yaml
                    # ETF vs 个股: 不同 TP 目标
                    from app.risk.position_limits import ETF_EXPOSURE_DISCOUNT
                    _is_etf = symbol in ETF_EXPOSURE_DISCOUNT
                    _tp_pct = 0.03 if _is_etf else 0.05  # ETF 3%, 个股 5%
                    if info['direction'] == 'LONG':
                        initial_sl = info['cost'] - atr * atr_mult
                        initial_tp = info['cost'] * (1 + _tp_pct)
                    else:
                        initial_sl = info['cost'] + atr * atr_mult
                        initial_tp = info['cost'] * (1 - _tp_pct)
                    
                    # 硬底限保护: 与 exit_policy.yaml max_loss_pct=15% 对齐
                    _max_loss_pct = 0.15  # exit_policy global max_loss_pct
                    try:
                        from app.risk.exit_policy_guardian import ExitPolicyGuardian as _EPG
                        _pol = _EPG()._load_exit_policy_dict()
                        _ml = float(_pol.get("exit_rules", {}).get(
                            "atr_stop", {}).get("max_loss_pct", 15.0))
                        _max_loss_pct = _ml / 100.0
                    except Exception:
                        pass
                    hard_stop = info['cost'] * (1 - _max_loss_pct) if info['direction'] == 'LONG' else info['cost'] * (1 + _max_loss_pct)
                    if info['direction'] == 'LONG':
                        initial_sl = max(initial_sl, hard_stop)
                    else:
                        initial_sl = min(initial_sl, hard_stop)
                    
                    _sl_pct = abs(initial_sl - info['cost']) / info['cost'] * 100
                    _tp_pct_actual = abs(initial_tp - info['cost']) / info['cost'] * 100
                    logger.info(
                        f"[US Sync] {symbol} SL=${initial_sl:.2f} ({_sl_pct:.1f}%) "
                        f"TP=${initial_tp:.2f} ({_tp_pct_actual:.1f}%) "
                        f"ATR=${atr:.2f}×{atr_mult:.0f} hard_stop=${hard_stop:.2f}"
                    )
                except Exception as e:
                    # Fallback: 固定百分比 (与 atr_stop 4×ATR 等效的距离)
                    logger.debug(f"[US Sync] {symbol} ATR 获取失败，使用固定止损: {e}")
                    atr = 0
                    _fb_sl = 0.06  # 6% 止损 (约等于 4× 日均 ATR)
                    _fb_tp = 0.05  # 5% 止盈 (ETF/个股通用)
                    if info['direction'] == 'LONG':
                        initial_sl = info['cost'] * (1 - _fb_sl)
                        initial_tp = info['cost'] * (1 + _fb_tp)
                    else:
                        initial_sl = info['cost'] * (1 + _fb_sl)
                        initial_tp = info['cost'] * (1 - _fb_tp)

                logger.info("[US Sync] New position: %s %s %s @ %s", symbol, info['direction'], info['qty'], info['cost'])
                # P3 修复 (2026-06-12): 三层手动判断: DB已有manual > 今日手动审计 > 富途委托
                _db_already_manual = symbol in getattr(self, '_db_manual_symbols', set())
                if _db_already_manual:
                    _db_trader_type = "MANUAL"
                elif symbol in _manual_today:
                    _db_trader_type = "MANUAL"
                elif symbol in _today_orders:
                    _db_trader_type = "AUTO"
                else:
                    _db_trader_type = "MANUAL"
                _db_strategy_id = "auto_main" if _db_trader_type == "AUTO" else "futu_sync"
                # P0-4 修复 (2026-06-12): UPSERT — 防止与 stub 行/隔离自管行 UNIQUE 冲突
                # 根因: 之前裸 INSERT 触发 "UNIQUE constraint failed: open_positions.symbol" 持续刷屏
                cursor.execute("""
                    INSERT INTO open_positions (
                        symbol, direction, size, avg_entry_price,
                        initial_sl, current_sl, initial_tp,
                        entry_time, updated_at, strategy_id, trader_type, owner_strategy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        direction=excluded.direction,
                        size=excluded.size,
                        avg_entry_price=excluded.avg_entry_price,
                        initial_sl=COALESCE(open_positions.initial_sl, excluded.initial_sl),
                        current_sl=COALESCE(open_positions.current_sl, excluded.current_sl),
                        initial_tp=COALESCE(open_positions.initial_tp, excluded.initial_tp),
                        updated_at=excluded.updated_at,
                        strategy_id=COALESCE(open_positions.strategy_id, excluded.strategy_id),
                        trader_type=COALESCE(open_positions.trader_type, excluded.trader_type),
                        owner_strategy=COALESCE(open_positions.owner_strategy, excluded.owner_strategy)
                """, (
                    symbol, info['direction'], info['qty'], info['cost'],
                    initial_sl, initial_sl, initial_tp,
                    datetime.now().isoformat(), datetime.now().isoformat(),
                    _db_strategy_id, _db_trader_type, _db_strategy_id
                ))

        conn.commit()
        conn.close()

        # ═══ 根因修复 (2026-06-05): 执行超限减仓 + 扫描所有持仓 ═══
        self._execute_pending_trims_and_check_all_oversized()
        # P0 修复 2026-04-25: 同步到 PositionStore 内存
        if self.position_store:
            self._sync_to_position_store(futu_positions)

        self._last_sync_time = datetime.now()
        _db_count = self.position_store.count() if self.position_store else len(futu_positions)
        logger.info("[US Sync] Done | Futu: %d | DB: %d", len(futu_positions), _db_count)

        # P2 新增 (2026-04-29): 一致性校验 + 资本安全同步
        self._verify_and_sync_capital(futu_positions)

    def _sync_to_position_store(self, futu_positions: Dict):
        """P0 修复 2026-04-25: 将 Futu Broker 持仓同步到 PositionStore 内存
        P0-4a 修复 2026-05-03: 整个 all() → diff → add/remove 序列在 position_store._lock 内原子执行
        防止交易循环在 snapshot 和 mutation 之间插入新持仓

        这是消除「手动操作无止损保护」的关键修复。
        场景:
        - 用户在 Futu App 手动开仓 → 30s 后此方法发现新持仓 → 写入 PositionStore → ExitMonitor 开始监控
        - 用户在 Futu App 手动平仓 → 30s 后此方法发现持仓消失 → 从 PositionStore 移除 → 停止监控幽灵仓
        """
        from app.core.models import Position, Direction, MarketRegime

        # P0-4a 修复: 整个序列原子执行
        with self.position_store._lock:
            self._sync_to_position_store_locked(futu_positions)

    def _sync_to_position_store_locked(self, futu_positions: Dict):
        """在 position_store._lock 内执行的同步逻辑 (P0-4a)"""
        from app.core.models import Position, Direction, MarketRegime

        # 实盘隔离: 自管持仓列表 (yaml + DB owner='manual' 合并)
        try:
            from app.core.config_container import get_all_manual_symbols
            _self_managed = get_all_manual_symbols()
        except Exception:
            _self_managed = set()

        mem_positions = list(self.position_store._positions.values())  # 已持有锁，直接读取
        mem_symbols = set(p.symbol for p in mem_positions)
        futu_symbols = set(futu_positions.keys())

        # P1 修复 (2026-06-07): 查 DB 已有仓位，区分"重启恢复"和"真正新发现"
        # 重启恢复: DB 有 synced 记录 → 只加载到内存，不发通知
        # 真正新发现: DB 没有 → 发通知
        _db_known_symbols = set()
        try:
            conn = sqlite3.connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT symbol FROM open_positions "
                "WHERE sync_status IS NULL OR sync_status NOT IN ("
                "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
                "'closed_emergency','stale_not_in_broker')"
            )
            _db_known_symbols = set(row[0] for row in cursor.fetchall())
            conn.close()
        except Exception:
            pass

        # Broker-first (2026-06-03): 不再预读 DB，所有仓位以 Futu 为准

        # 1. 新增持仓 (Futu 有，内存没有) → 手动开仓检测
        # P0 fix (2026-05-21): Skip re-importing options that were force-closed via PRICE_FAIL.
        # These positions still exist in Futu (user opened manually, SELL may have failed),
        # but re-importing them triggers the infinite PRICE_FAIL re-import loop.
        _price_fail_closed = set()
        try:
            import json as _pf_json
            _pf_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
            if os.path.exists(_pf_path):
                _price_fail_closed = set(_pf_json.load(open(_pf_path)))
        except Exception:
            pass
        new_symbols = futu_symbols - mem_symbols
        
        # _today_orders 已在上方查询 (DB INSERT 共用)
        _today_orders = getattr(self, '_today_orders', {})
        
        for symbol in new_symbols:
            # P0 fix: Skip PRICE_FAIL-closed options
            if symbol in _price_fail_closed:
                logger.warning(f"[US Sync·P0] 跳过 PRICE_FAIL 已关闭期权 {symbol} (防止重导入循环)")
                continue
            # 实盘隔离: 跳过自管持仓 (startup reconcile 也需检查)
            if _self_managed and symbol in _self_managed:
                logger.info(f"[US Sync·隔离] 跳过自管持仓(startup): {symbol}")
                continue
            # H6 (2026-06-03): tombstone 检查 — 防止刚平仓的 position 被重新创建
            if self._is_tombstoned(symbol):
                logger.warning(f"[US Sync·H6] 跳过 tombstoned {symbol} (刚平仓, 防止重入)")
                continue
            # Broker-first (2026-06-03): 不再从 DB 恢复，新发现的仓位统一用全新计算的 SL/TP

            info = futu_positions[symbol]
            direction = Direction.SHORT if info['direction'] == 'SHORT' else Direction.LONG
            cost = info['cost']
            size = info['qty']
            
            # P2 修复 (2026-04-30): 使用 ATR 动态止损替代固定百分比
            try:
                atr = self._get_symbol_atr(symbol, cost)
                atr_mult = self._get_atr_stop_mult()  # P1: SSOT 替代硬编码 2.5
                if info['direction'] == 'LONG':
                    sl_price = cost - atr * atr_mult
                    tp_price = cost + atr * atr_mult * 2.0
                else:
                    sl_price = cost + atr * atr_mult
                    tp_price = cost - atr * atr_mult * 2.0
                
                # 硬底限保护
                hard_stop = cost * 0.85 if info['direction'] == 'LONG' else cost * 1.15
                if info['direction'] == 'LONG':
                    sl_price = max(sl_price, hard_stop)
                else:
                    sl_price = min(sl_price, hard_stop)
                    
            except Exception as e:
                logger.debug(f"[US Sync] {symbol} ATR 获取失败，使用固定止损: {e}")
                sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                tp_mult = 0.85 if info['direction'] == 'SHORT' else 1.15
                sl_price = cost * sl_mult
                tp_price = cost * tp_mult

            # Broker-first (2026-06-03): SL 方向校验 — 方向错误直接重算
            if info['direction'] == 'SHORT' and sl_price <= cost:
                logger.error(f"[SL校验·修复] {symbol} SHORT SL=${sl_price:.2f} < entry=${cost:.2f}, 重算")
                sl_price = cost * 1.08  # fallback 8%
            elif info['direction'] == 'LONG' and sl_price >= cost:
                logger.error(f"[SL校验·修复] {symbol} LONG SL=${sl_price:.2f} > entry=${cost:.2f}, 重算")
                sl_price = cost * 0.92  # fallback 8%

            # P0 修复 (2026-05-20): 期权仓位归主线策略管理，避免误标为手动仓
            # 期权代码格式：US.XXX26XXXXP/C (含年月 + Put/Call)
            is_option = any(x in symbol for x in ['26', '27', '28']) and any(x in symbol for x in ['P', 'C'])
            
            # P3 修复 (2026-06-12): 三层手动判断优先级: DB已有manual > 今日手动审计 > 富途委托
            _is_db_manual = symbol in getattr(self, '_db_manual_symbols', set())
            _is_manual_today = symbol in getattr(self, '_manual_today', set())
            _has_today_order = symbol in _today_orders and not _is_manual_today and not _is_db_manual
            if is_option:
                strategy_id = "voting_llm_option"
                trader_type = "OPTION"
            elif _is_db_manual:
                strategy_id = "manual_sync"
                trader_type = "MANUAL"
                logger.info(
                    f"[US Sync·P3] {symbol} DB已有owner=manual，保持手动管理"
                )
            elif _is_manual_today:
                strategy_id = "manual_sync"
                trader_type = "MANUAL"
                logger.info(
                    f"[US Sync·P3] {symbol} 今日有手动订单，标记为 manual_sync"
                )
            elif _has_today_order:
                strategy_id = "auto_main"
                trader_type = "AUTO"
                _order_info = _today_orders[symbol]
                logger.info(
                    f"[US Sync·P3] {symbol} 匹配到当日委托 (order_id={_order_info['order_id']}), "
                    f"标记为 auto_main (非手动)"
                )
            else:
                strategy_id = "manual_sync"
                trader_type = "MANUAL"

            position = Position(
                strategy_id=strategy_id,
                symbol=symbol,
                direction=direction,
                size=size,
                avg_entry_price=cost,
                initial_sl=sl_price,
                current_sl=sl_price,
                initial_tp=tp_price,
                take_profit_levels=[],
                time_stop_hours=72,
                market_regime=MarketRegime.RANGE,
            )
            position.owner_strategy = strategy_id
            position.trail_state = {
                "highest_price": cost,
                "lowest_price": cost,
                "prev_scan_price": cost,
            }
            # Broker-first (2026-06-03): 如实标记来源，不再伪装 broker_sync
            # P1 归因修复 (2026-06-06): 补充 trace_id + entry_reason，防止 metadata 全空
            import uuid as _sync_uuid
            _sync_trace = f"sync-{symbol}-{int(time.time())}-{_sync_uuid.uuid4().hex[:4]}"
            if is_option:
                position.metadata = {
                    "source": "voting_llm_option",
                    "is_option": True,
                    "trace_id": _sync_trace,
                    "entry_reason": "option_discovery",
                    "strategy_id": strategy_id,
                }
            elif _has_today_order:
                # P3: 策略开仓恢复 — 标记为 auto_main 恢复
                _order_info = _today_orders[symbol]
                position.metadata = {
                    "source": "auto_main",
                    "origin": "sync_recovery",
                    "sync_origin": "position_sync",
                    "trace_id": _sync_trace,
                    "entry_reason": "sync_recovery_matched_order",
                    "strategy_id": strategy_id,
                    "futu_order_id": _order_info.get('order_id', ''),
                    "futu_order_time": _order_info.get('time', ''),
                }
            else:
                position.metadata = {
                    "source": "futu_discovery",
                    "origin": "external",
                    "sync_origin": "position_sync",
                    "trace_id": _sync_trace,
                    "entry_reason": "futu_discovery",
                    "strategy_id": strategy_id,
                }
            self.position_store.add(symbol, position)
            # P3: 日志区分策略恢复 vs 手动开仓
            if _has_today_order:
                logger.info(
                    f"🔄 [US Sync·P3] 策略仓位恢复 {symbol} ({info['direction']} {size}股 @${cost:.2f})，"
                    f"匹配当日委托 → auto_main | SL=${sl_price:.2f}"
                )
            else:
                logger.warning(
                    f"⚠️ [US Sync] 发现手动开仓 {symbol} ({info['direction']} {size}股 @${cost:.2f})，"
                    f"已加入 ExitMonitor 监控 | SL=${sl_price:.2f}"
                )
            
            # P1 修复 (2026-06-07): 只在 DB 中不存在时发通知（真正新发现）
            # 重启恢复的仓位已在 DB 中，不发通知避免刷屏
            if symbol not in _db_known_symbols:
                try:
                    from app.notifier.feishu import notify_order_filled
                    dir_str = "BUY" if info['direction'] == 'LONG' else "SELL"
                    notify_order_filled(
                        symbol=symbol,
                        direction=dir_str,
                        filled_price=cost,
                        quantity=size,
                        stop_loss=sl_price,
                        take_profit=[tp_price] if tp_price else [],
                        signal_strength=0,
                        signal_confidence=0,
                        strategy="manual_sync",
                        source_type="POSITION_SYNC",  # 明确标记为历史仓位同步
                    )
                    logger.info(f"[US Sync] 飞书成交通知已发送: {symbol} (DB 中不存在，新发现)")
                except Exception as e:
                    logger.warning(f"[US Sync] 飞书成交通知发送失败: {e}")
            else:
                logger.debug(f"[US Sync] {symbol} 已在 DB 中，跳过通知 (重启恢复)")

        # 2. 幽灵持仓 (内存有，Futu 没有) → 手动平仓检测
        # P0 修复 2026-04-28: 期权持仓不在 broker 中，不应被误判为幽灵仓
        # P0-4c 修复 2026-05-03: 查询失败返回 None，跳过清理
        # P0 修复 2026-05-04: 新创建不足60秒的持仓跳过，等待 Futu 确认成交
        ghost_symbols = mem_symbols - futu_symbols
        option_symbols = self._get_option_position_symbols()
        _now_sync = datetime.now()
        if option_symbols is None:
            # 查询失败，无法区分期权和股票，保守跳过所有清理
            logger.warning("[US Sync] 期权查询失败，跳过本轮幽灵仓清理")
        else:
            for symbol in ghost_symbols:
                if symbol in option_symbols:
                    logger.debug(f"[US Sync] 跳过期权仓 {symbol} (不在 broker 中，正常)")
                    continue
                # P0 修复 (2026-05-04 / 2026-05-12 / 2026-05-13): 新持仓保护窗口
                # 2026-05-13: Scalping 持仓使用动态保护窗口 (基于 max_hold_minutes)
                # 根因: 模拟盘 Futu 延迟可达 10 分钟，固定 180s 不够
                existing_pos = self.position_store.get(symbol)
                if existing_pos and hasattr(existing_pos, 'entry_time'):
                    try:
                        age_seconds = (_now_sync - existing_pos.entry_time).total_seconds()
                        # Scalping 持仓: 保护窗口 = max(max_hold_minutes * 2, 5min)
                        # 例: max_hold=5min → 保护 10min = 600s
                        grace_period = 180  # 默认 (主管线)
                        # Task #7 (2026-05-31): owner_strategy 优先, strategy_id 仅作向后兼容 fallback
                        _owner = (
                            getattr(existing_pos, 'owner_strategy', '')
                            or getattr(existing_pos, 'strategy_id', '')
                            or ''
                        )
                        if _owner == 'scalping':
                            meta = getattr(existing_pos, 'metadata', {}) or {}
                            max_hold_min = meta.get('max_hold_minutes', 5)
                            grace_period = max(max_hold_min * 60 * 2, 300)
                        if age_seconds < grace_period:
                            logger.info(
                                "[US Sync] 跳过新持仓 %s (创建 %.0fs 前 < %.0fs 保护窗口, 等待 Futu 确认成交)",
                                symbol, age_seconds, grace_period,
                            )
                            continue
                    except (TypeError, ValueError):
                        pass
                self.position_store.remove(symbol)
                logger.info(f"🧹 [US Sync] 清理幽灵仓 {symbol} (Futu 已平仓)")
                
                # P0 修复 (2026-05-27): 幽灵通知冷却 — 同一 symbol 30分钟内仅通知一次
                # 防止 position_monitor 重导入→position_sync 再清理的死循环通知轰炸
                _ghost_cooldown = getattr(self, '_ghost_notify_cooldown', {})
                _last_notify = _ghost_cooldown.get(symbol, 0)
                _now_cd = time.time()
                if _now_cd - _last_notify < 1800:  # 30 min cooldown
                    logger.debug(f"[US Sync] 幽灵通知冷却中: {symbol} (距上次 {_now_cd - _last_notify:.0f}s)")
                    continue
                _ghost_cooldown[symbol] = _now_cd
                self._ghost_notify_cooldown = _ghost_cooldown
                
                # P0 修复 (2026-05-07 / 2026-05-12): 幽灵仓清理不发零值通知
                # 2026-05-12: exit_price=0 说明无真实成交确认，通知是误导
                if existing_pos and existing_pos.avg_entry_price > 0:
                    try:
                        from app.notifier.feishu import notify_exit
                        notify_exit(
                            symbol=symbol,
                            direction="BUY" if existing_pos and existing_pos.direction.name == "SHORT" else "SELL",
                            entry_price=existing_pos.avg_entry_price,
                            exit_price=0,  # 未知平仓价 — 非真实退出
                            quantity=existing_pos.size if existing_pos else 0,
                            pnl=0,
                            pnl_pct=0,
                            exit_reason="ghost_cleaned",
                            holding_hours=0,
                            source_type="POSITION_SYNC",
                        )
                        logger.info(f"[US Sync] 飞书幽灵仓清理通知已发送: {symbol}")
                    except Exception as e:
                        logger.warning(f"[US Sync] 飞书幽灵仓清理通知发送失败: {e}")
                else:
                    logger.debug(f"[US Sync] 幽灵仓清理跳过通知: {symbol} (无有效 entry_price)")

        # 3. 数量变化 (部分成交/加仓) → 更新内存
        common_symbols = mem_symbols & futu_symbols
        for symbol in common_symbols:
            info = futu_positions[symbol]
            existing = self.position_store.get(symbol)
            if existing is None:
                continue

            # P0 修复 (2026-06-04): 方向翻转检测 — Broker-first 覆盖内存方向
            # 根因: 6/3 XLE SHORT→DB残留LONG→平仓变加仓→仓位翻倍→亏$7255
            #        6/3 XLF LONG→DB残留SHORT→退出方向错误→亏$4458
            # 场景: 重启后 position_monitor 从 DB 恢复方向, 但 DB 方向已过期.
            #       position_sync 读到 Broker 正确方向, 更新了 DB 但未修正内存.
            # 修复: 内存方向与 Broker 不一致时, 强制以 Broker 为准覆盖.
            broker_dir = Direction.SHORT if info['direction'] == 'SHORT' else Direction.LONG
            mem_dir = existing.direction
            if broker_dir != mem_dir:
                old_dir_str = mem_dir.value if hasattr(mem_dir, 'value') else str(mem_dir)
                new_dir_str = info['direction']
                logger.critical(
                    f"🔴🔴🔴 [US Sync·方向翻转·P0] {symbol} "
                    f"内存={old_dir_str} vs Broker={new_dir_str} "
                    f"(size={info['qty']}, cost=${info['cost']:.2f}) "
                    f"→ 强制以 Broker 为准覆盖内存! "
                    f"如果正在平仓, 平仓方向将自动修正."
                )
                existing.direction = broker_dir
                existing.avg_entry_price = info['cost']
                existing.size = info['qty']
                # 重算 SL/TP (方向翻转后旧 SL 方向错误)
                try:
                    atr = self._get_symbol_atr(symbol, info['cost'])
                    atr_mult = self._get_atr_stop_mult()  # P1: SSOT 替代硬编码 2.5
                    if info['direction'] == 'LONG':
                        existing.initial_sl = info['cost'] - atr * atr_mult
                        existing.current_sl = existing.initial_sl
                    else:
                        existing.initial_sl = info['cost'] + atr * atr_mult
                        existing.current_sl = existing.initial_sl
                except Exception:
                    sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                    existing.initial_sl = info['cost'] * sl_mult
                    existing.current_sl = existing.initial_sl
                # P0 修复 (2026-06-04): 清理旧方向 trail_state
                # 旧 peak/highest/lowest 基于反方向计算, 方向翻转后全部失效
                # 不清理会导 trailing stop 方向错误 (SHORT 按 LONG peak 回撤)
                existing.trail_state = {
                    "highest_price": info['cost'],
                    "lowest_price": info['cost'],
                    "prev_scan_price": info['cost'],
                }
                # 同步 DB
                try:
                    import sqlite3 as _sqlite3
                    db_path = _get_db_path()
                    conn = _sqlite3.connect(db_path, timeout=10)
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute(
                        "UPDATE open_positions SET direction=?, avg_entry_price=?, size=?, "
                        "initial_sl=?, current_sl=?, updated_at=? WHERE symbol=?",
                        (info['direction'], info['cost'], info['qty'],
                         existing.initial_sl, existing.current_sl,
                         datetime.now().isoformat(), symbol)
                    )
                    conn.commit()
                    conn.close()
                except Exception as _dbe:
                    logger.warning(f"[US Sync·方向翻转] DB 同步失败 (不影响内存修正): {_dbe}")
                # 同步 daily_active_pool
                try:
                    from app.utils.daily_active_pool import get_daily_active_pool
                    pool = get_daily_active_pool(market_prefix="US.")
                    pool.add_position(symbol, metadata={
                        "direction": new_dir_str,
                        "quantity": info['qty'],
                    })
                except Exception:
                    pass
                # 飞书告警
                try:
                    from app.notifier.feishu import send_text
                    send_text(
                        f"🔴 [方向翻转·P0] {symbol}\n"
                        f"内存={old_dir_str} → Broker={new_dir_str}\n"
                        f"size={info['qty']}, cost=${info['cost']:.2f}\n"
                        f"SL 已重算, DB 已同步"
                    )
                except Exception:
                    pass
                continue  # 方向已修正, 跳过后续 qty 逻辑

            qty_changed = abs(existing.size - info['qty']) > 0.1
            # P0-1 修复 (2026-05-19, Karpathy 二审 v2): 区分加仓 vs 减仓, 不一刀切.
            #
            # 5/18 ARM 紊乱根因: paper broker 在 partial_close 后短暂报错 cost=$216.11,
            # 替换真实 $203.53 → PortfolioDrawdown 误判 -2.5% 触发 7×FLAT_ALL.
            #
            # 修复策略 (3 分支, 显式不重叠):
            #   1. size 增加 (加仓 / 漏仓补回): broker cost 是新加权均价, 必须更新
            #   2. size 减少 (部分平仓): cost 是入场加权均价, 不应改, 保留 SSOT
            #   3. cost == 0 (本地 avg 缺失): 用 broker cost 兜底初始化
            if qty_changed:
                size_increased = info['qty'] > existing.size

                # ── P0 (2026-06-12): Scale-out 保护 ──
                # 根因: SNDK 灾难 — 分批止盈刚减仓, position_sync 读 broker 尚未结算,
                #        误判为"加仓"覆盖回大值, 导致下一 tick 又触发分批止盈
                # 修法: 如果 position 有 _scale_out_until 且 broker size > 本地 size,
                #        跳过覆盖 (这是结算延迟, 不是真加仓)
                _exist_meta = getattr(existing, "metadata", {}) or {}
                _so_until = _exist_meta.get("_scale_out_until", 0)
                if size_increased and _so_until and time.time() < _so_until:
                    logger.warning(
                        f"⚠️ [US Sync·scale-out保护] {symbol} 跳过加仓覆盖: "
                        f"本地size={existing.size} broker={info['qty']} "
                        f"(分批止盈冷却中, broker结算延迟)"
                    )
                    continue

                if size_increased:
                    # 加仓: 用 broker 端的新加权均价 (broker 是加仓事件的 SSOT)
                    if existing.avg_entry_price > 0 and abs(existing.avg_entry_price - info['cost']) > 0.5:
                        logger.warning(
                            f"⚠️ [US Sync·加仓] {symbol} size {existing.size}→{info['qty']}, "
                            f"avg_cost ${existing.avg_entry_price:.2f}→${info['cost']:.2f} (broker 加权均价覆盖)"
                        )
                    existing.avg_entry_price = info['cost']
                elif not existing.avg_entry_price or existing.avg_entry_price <= 0:
                    # cost 缺失兜底
                    logger.warning(
                        f"⚠️ [US Sync] {symbol} avg_entry_price=0 → 用 broker cost=${info['cost']:.2f} 兜底"
                    )
                    existing.avg_entry_price = info['cost']
                else:
                    # 减仓: 保留 SSOT cost. 若漂移显著 → WARNING 让 daily_review grep 出来
                    if abs(existing.avg_entry_price - info['cost']) > 0.5:
                        logger.warning(
                            f"⚠️ [US Sync·减仓] {symbol} size {existing.size}→{info['qty']}, "
                            f"local_cost=${existing.avg_entry_price:.2f} broker_cost=${info['cost']:.2f} "
                            f"(broker 漂移, 保留 SSOT, 不覆盖. 若是真实分红/拆股事件请人工处理)"
                        )

                existing.size = info['qty']
                # initial_sl/current_sl 仅在缺失时初始化, 不覆盖
                if not existing.initial_sl or existing.initial_sl == 0:
                    sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                    existing.initial_sl = existing.avg_entry_price * sl_mult
                if not existing.current_sl or existing.current_sl == 0:
                    sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                    existing.current_sl = existing.avg_entry_price * sl_mult
                existing.trail_state = existing.trail_state or {}
                existing.trail_state["prev_scan_price"] = info['cost']
                logger.info(
                    f"📊 [US Sync] 更新 {symbol}: size={info['qty']}, "
                    f"avg_cost=${existing.avg_entry_price:.2f} ({'加仓' if size_increased else '减仓'})"
                )

    # ═══════════════════════════════════════════════════════════════════
    #  P2 新增: 一致性校验 + 资本安全同步 (2026-04-29)
    # ═══════════════════════════════════════════════════════════════════

    def _verify_and_sync_capital(self, futu_positions: Dict):
        """
        持仓同步后的一致性校验与资本安全更新。

        职责:
        1. 校验 Broker/DB/Memory 三层持仓一致性
        2. 从 Broker 实时获取账户资金数据 (equity/cash/market_value)
        3. 更新 Capital Safety 状态，确保风控基于真实数据
        4. 检测持仓变动 (手动开/平仓) 并触发风控刷新
        """
        try:
            # --- 1. 三层一致性校验 ---
            db_positions = self._get_db_positions()
            mem_positions = {}
            if self.position_store:
                for p in self.position_store.all():
                    mem_positions[p.symbol] = {
                        'size': p.size,
                        'direction': p.direction.value if hasattr(p.direction, 'value') else str(p.direction),
                    }

            futu_set = set(futu_positions.keys())
            db_set = set(db_positions.keys())
            mem_set = set(mem_positions.keys())

            # 检查不一致
            issues = []
            if futu_set != db_set:
                missing_db = futu_set - db_set
                missing_futu = db_set - futu_set
                if missing_db:
                    issues.append(f"DB 缺少 Broker 持仓: {missing_db}")
                if missing_futu:
                    issues.append(f"DB 有多余持仓 (Broker 已无): {missing_futu}")

            if futu_set != mem_set and self.position_store:
                missing_mem = futu_set - mem_set
                missing_mem_ghost = mem_set - futu_set
                opt_symbols = self._get_option_position_symbols()
                missing_mem_ghost = {s for s in missing_mem_ghost if s not in opt_symbols}
                if missing_mem:
                    issues.append(f"Memory 缺少 Broker 持仓: {missing_mem}")
                if missing_mem_ghost:
                    issues.append(f"Memory 有幽灵仓: {missing_mem_ghost}")

            if issues:
                logger.warning(
                    "⚠️ [US Sync Consistency] 检测到 %d 个不一致:\n  %s",
                    len(issues), "\n  ".join(issues)
                )
            else:
                logger.debug(
                    "[US Sync Consistency] ✅ 三层一致 | 持仓: %s",
                    ", ".join(f"{s}({futu_positions[s]['qty']})" for s in futu_set) if futu_set else "空仓"
                )

            # --- 2. 从 Broker 获取实时资金数据 ---
            # 注意: futu-opend 部分版本不支持 acc_cash_info_query，
            # 此时降级使用 position_monitor 的资金更新逻辑，不在此处重复估算。
            from futu import TrdEnv, TrdMarket, SecurityFirm
            from app.core.config_container import get_config
            trade_env = TrdEnv.SIMULATE if get_config().trade_env_int == 1 else TrdEnv.REAL

            ctx = self._get_futu_context(
                os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                int(os.getenv("FUTU_OPEND_PORT", "11111")),
                trade_env,
                TrdMarket.US,
                SecurityFirm.FUTUINC,
            )
            if ctx is None:
                logger.debug("[US Sync Capital] Futu 连接不可用，跳过资金更新")
                return

            broker_data = {}
            try:
                ret, funds_data = ctx.acc_cash_info_query(trd_env=trade_env)
                if ret == 0 and funds_data is not None and not funds_data.empty:
                    row = funds_data.iloc[0]
                    broker_data = {
                        'cash': float(row.get('cash', 0)),
                        'market_value': float(row.get('market_val', 0)),
                        'total_assets': float(row.get('total_assets', 0)),
                        'buying_power': float(row.get('max_power_short', 0)),
                    }
            except (AttributeError, Exception) as e:
                logger.debug("[US Sync Capital] acc_cash_info_query 不可用 (%s)，"
                             "由 position_monitor 统一更新资金", e)

            # Fallback: 如果 API 不支持，跳过资金更新 (由 position_monitor 统一处理)
            if not broker_data.get('total_assets', 0):
                logger.debug("[US Sync Capital] 无 Broker 资金数据，跳过 (由 position_monitor 处理)")
                return

            # --- 3. 更新 Capital Safety ---
            if broker_data.get('total_assets', 0) > 0:
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    capital = get_capital_safety_system()
                    old_equity = capital._state.current_equity
                    old_level = capital._state.water_level.value

                    capital.update_account(
                        current_equity=broker_data['total_assets'],
                        cash=broker_data['cash'],
                        market_value=broker_data['market_value'],
                        buying_power=broker_data['buying_power'],
                    )

                    new_equity = capital._state.current_equity
                    new_level = capital._state.water_level.value

                    # 只在变动时输出 INFO 级别日志
                    if abs(new_equity - old_equity) > 100 or new_level != old_level:
                        logger.info(
                            "💰 [US Sync Capital] 更新: 净值=$%.0f | "
                            "现金=$%.0f | "
                            "市值=$%.0f | "
                            "水位=%s (旧=%s)",
                            new_equity, broker_data['cash'],
                            broker_data['market_value'],
                            new_level, old_level
                        )
                    else:
                        logger.debug(
                            "[US Sync Capital] 未显著变化: 净值=$%.0f, 现金=$%.0f",
                            new_equity, broker_data['cash']
                        )
                except Exception as e:
                    logger.debug(f"[US Sync Capital] Capital Safety 更新失败 (不影响主流程): {e}")

        except Exception as e:
            logger.debug(f"[US Sync Consistency] 校验异常 (已忽略): {e}")

    def _get_trade_env(self) -> int:
        """获取当前交易环境 (0=实盘, 1=模拟)"""
        try:
            from app.core.config_container import get_config
            return get_config().trade_env_int
        except Exception:
            return 1

    def _get_option_position_symbols(self) -> Optional[set]:
        """从 option_positions DB 表读取期权持仓标的 (2026-04-28)
        P0-4c 修复 (2026-05-03): 返回 None 表示查询失败，而非空集
        空集 = 无期权持仓；None = 查询出错，应跳过清理

        期权持仓不在 broker 中 (模拟/真实期权)，不应被误判为幽灵仓。

        2026-05-20 修复: 使用 option_code 列 (合约代码) 而非 symbol 列 (标的).
        open_positions 中存的是合约代码 (如 US.QQQ260602P710000).

        2026-05-20 补充: 额外扫描 open_positions 中的期权合约代码 (启发式匹配).
        捕获 option_positions 表中遗漏的期权 (如 SPY260602P730000).
        """
        try:
            conn = sqlite3.connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT option_code FROM option_positions")
            symbols = {row[0] for row in cursor.fetchall()}

            # 补充: 扫描 open_positions 中的期权合约代码 (启发式匹配)
            import re
            cursor.execute("SELECT symbol FROM open_positions")
            for row in cursor.fetchall():
                sym = row[0]
                if re.match(r'^US\.[A-Z]+\d{6}[PC]\d+$', sym):
                    symbols.add(sym)

            conn.close()
            return symbols
        except Exception as e:
            logger.warning(f"[US Sync] 读取期权持仓失败: {e}")
            return None  # P0-4c: None 表示错误，调用方应跳过清理

    def _get_db_positions(self) -> Dict[str, dict]:
        """从 DB 读取当前持仓 (用于判断是否为已存在仓位)"""
        import sqlite3
        if not os.path.exists(_get_db_path()):
            return {}
        try:
            conn = sqlite3.connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            # 根因修复 (2026-06-05): 与 _sync_once 保持相同过滤条件
            # 排除已关闭/stale 行, 避免一致性检查重复报告已处理的 ghost 仓
            cursor.execute(
                "SELECT symbol, direction, size, avg_entry_price, initial_sl, current_sl "
                "FROM open_positions "
                "WHERE sync_status IS NULL "
                "OR sync_status NOT IN ("
                "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
                "'closed_emergency','stale_not_in_broker')"
            )
            positions = {}
            for row in cursor.fetchall():
                positions[row[0]] = {
                    'direction': row[1],
                    'size': row[2],
                    'avg_entry_price': row[3],
                    'initial_sl': row[4],
                    'current_sl': row[5],
                }
            conn.close()
            return positions
        except Exception as e:
            logger.debug(f"[US Sync] 读取 DB 持仓失败: {e}")
            return {}

    def _get_symbol_atr(self, symbol: str, fallback_price: float) -> float:
        """P2 修复 (2026-04-30): 获取股票 ATR 用于动态止损计算
        
        优先级:
        1. TwelveData 历史数据 (盘前/盘后可用)
        2. Futu LV3 K 线 (交易时段可用)
        3. Fallback: 2% 价格 (保守估计)
        """
        # P0 修复 (2026-05-21): 期权代码不走 TwelveData/MarketDataRouter（不支持期权格式）
        from app.data.futu_lv3_provider import FutuLV3Provider
        if FutuLV3Provider._is_option_code(symbol):
            # 期权无传统 ATR，使用 3% 价格作为保守估计（期权波动率通常更高）
            default_atr = fallback_price * 0.03
            logger.debug(f"[US Sync ATR] {symbol} 期权代码，使用默认 ATR=${default_atr:.2f} (3%)")
            return default_atr

        # 尝试从 TwelveData 获取
        try:
            from app.data.twelvedata_provider import TwelveDataProvider
            provider = TwelveDataProvider()
            ts_data = provider.get_historical_data(symbol, interval="1day", outputsize=20)
            if ts_data and 'atr' in ts_data:
                atr = float(ts_data['atr'])
                if atr > 0:
                    logger.debug(f"[US Sync ATR] {symbol} ATR=${atr:.2f} (TwelveData)")
                    return atr
        except Exception as e:
            logger.debug(f"[US Sync ATR] TwelveData 获取失败: {e}")
        
        # 尝试从市场数据路由器获取
        try:
            from app.data.market_data_router import get_market_data_router
            router = get_market_data_router()
            context = router.get_full_market_context(symbol)
            if context and context.get('atr', 0) > 0:
                atr = float(context['atr'])
                logger.debug(f"[US Sync ATR] {symbol} ATR=${atr:.2f} (MarketDataRouter)")
                return atr
        except Exception as e:
            logger.debug(f"[US Sync ATR] MarketDataRouter 获取失败: {e}")
        
        # Fallback: 使用 2% 价格作为保守 ATR 估计
        default_atr = fallback_price * 0.02
        logger.debug(f"[US Sync ATR] {symbol} 使用默认 ATR=${default_atr:.2f} (2% 价格)")
        return default_atr

    def _get_atr_stop_mult(self) -> float:
        """P1 修复 (2026-06-07): SSOT 读取 ATR 止损倍数

        从 exit_policy.yaml 读取 atr_stop.multiplier_by_profit.initial,
        消除 position_sync 中 3 处硬编码 (2.5/4.0) 的不一致。
        默认 4.0 (与 exit_policy.yaml SSOT 对齐)。
        """
        try:
            from app.risk.exit_policy_guardian import ExitPolicyGuardian
            _ep = ExitPolicyGuardian()
            _policy = _ep._load_exit_policy_dict()
            return float(_policy.get("exit_rules", {}).get(
                "atr_stop", {}).get("multiplier_by_profit", {}).get("initial", 4.0))
        except Exception:
            return 4.0

    def _execute_pending_trims_and_check_all_oversized(self):
        """根因修复 (2026-06-05): 执行超限减仓 + 扫描所有持仓
        
        职责:
        1. 执行 _pending_trims 中标记的减仓 (新发现的超限仓位)
        2. 扫描所有 open_positions，检测已有超限仓位并触发减仓
        3. 通过 ExecutionEngine 提交部分平仓单
        """
        import sqlite3
        
        # 1. 收集所有需要减仓的 symbol + shares
        trims_to_execute = {}
        
        # 1a. 从 _pending_trims 获取 (新发现的)
        if hasattr(self, '_pending_trims') and self._pending_trims:
            trims_to_execute.update(self._pending_trims)
            self._pending_trims.clear()
        
        # 1b. 扫描所有 open_positions 检测已有超限仓位
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            total_equity = capital._state.current_equity if capital._state.current_equity > 0 else 100000
            
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                max_per_symbol = float(cfg.get_dotted("risk.position.max_per_symbol", 0.25))
            except Exception:
                max_per_symbol = 0.25
            
            # 🔴 P0 (2026-06-13): 周期扫描必须过滤 self_managed, 防止对手动仓位触发减仓
            try:
                from app.utils.watchlist_manager import get_all_manual_symbols
                _scan_self_managed = get_all_manual_symbols()
            except Exception:
                _scan_self_managed = set()

            conn = sqlite3.connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol, size, avg_entry_price, direction, trader_type
                FROM open_positions
                WHERE sync_status IS NULL OR sync_status NOT IN (
                    'closed_by_daemon','closed_by_scheduler','closed_by_sync',
                    'closed_emergency','stale_not_in_broker')
            """)
            # 告警冷却: 同一 symbol 5 分钟内只打一次 warning (不影响减仓执行)
            if not hasattr(self, '_last_scan_alert_time'):
                self._last_scan_alert_time: Dict[str, float] = {}
            _alert_now = time.time()
            _alert_cooldown = 300  # 5 minutes
            for row in cursor.fetchall():
                sym, size, price, direction, trader_type = row[0], row[1], row[2], row[3], (row[4] if len(row) > 4 else None)
                # 2026-06-11: 跳过非美股标的
                if not sym.startswith('US.') or sym.count('.') > 1:
                    continue
                # 🔴 P0 (2026-06-13): 跳过 self_managed / manual 仓位 — 系统不碰手动仓
                if _scan_self_managed and sym in _scan_self_managed:
                    continue
                if trader_type and str(trader_type).upper() == 'MANUAL':
                    continue
                if sym in trims_to_execute:
                    continue  # 已在 pending_trims 中
                notional = size * price
                pct = notional / total_equity
                if pct > max_per_symbol:
                    max_value = total_equity * max_per_symbol
                    excess_value = notional - max_value
                    excess_shares = int(excess_value / price) if price > 0 else 0
                    if excess_shares > 0:
                        # 告警冷却: 同一 symbol 5 分钟内只打一次 log
                        if _alert_now - self._last_scan_alert_time.get(sym, 0) >= _alert_cooldown:
                            self._last_scan_alert_time[sym] = _alert_now
                            logger.warning(
                                f"🚫 [仓位扫描] {sym} 占比 {pct:.1%} 超限 {max_per_symbol:.0%}，"
                                f"触发减仓: {excess_shares}/{size}股 (${excess_value:,.0f})"
                            )
                        trims_to_execute[sym] = {
                            'shares': excess_shares,
                            'direction': direction,
                            'reason': f'periodic_scan_over_limit_{max_per_symbol:.0%}',
                        }
            conn.close()
        except Exception as e:
            logger.debug(f"[仓位扫描] 扫描失败: {e}")
        
        # 2. 执行减仓
        if not trims_to_execute:
            return
        
        # 冷却机制: 同一 symbol 10 分钟内不重复减仓
        if not hasattr(self, '_last_trim_time'):
            self._last_trim_time: Dict[str, float] = {}
        _now = time.time()
        _trim_cooldown = 600  # 10 minutes
        
        # 准备 Futu 交易上下文
        try:
            from futu import TrdEnv, TrdMarket, SecurityFirm, OrderType as FutuOrderType, TrdSide
            from app.core.config_container import get_config as _te_cfg
            _trade_env_int = int(_te_cfg().get_dotted("trade_env", 1))
            trade_env = TrdEnv.SIMULATE if _trade_env_int == 1 else TrdEnv.REAL
        except Exception as e:
            logger.error(f"[减仓] Futu 初始化失败: {e}")
            return
        
        # P1-1 修复 (2026-06-12): 永久跳过名单 (业务级错误后避免反复重试至重启)
        if not hasattr(self, '_trim_permanent_skip'):
            self._trim_permanent_skip: set = set()

        # P1-1 修复 (2026-06-12): symbol 正则白名单, 拦截 US.HK.00700 这种非法代码
        _us_symbol_re = re.compile(r'^US\.[A-Z][A-Z0-9.]*$')

        for symbol, trim_info in trims_to_execute.items():
            # P1-1: 非法 symbol 永久跳过 (US.HK.00700, US.NA, etc.)
            if not _us_symbol_re.match(symbol) or symbol.count('.') > 1:
                if symbol not in self._trim_permanent_skip:
                    self._trim_permanent_skip.add(symbol)
                    logger.error(f"[减仓] {symbol} 非法美股代码, 加入永久跳过名单")
                continue
            # P1-1: 已知业务级错误的 symbol 直到下次重启都不再尝试
            if symbol in self._trim_permanent_skip:
                logger.debug(f"[减仓] {symbol} 已在永久跳过名单, 不再尝试")
                continue
            # 冷却检查
            if _now - self._last_trim_time.get(symbol, 0) < _trim_cooldown:
                logger.debug(f"[减仓] {symbol} 冷却中，跳过")
                continue
            
            shares = trim_info['shares']
            direction = trim_info['direction']
            reason = trim_info.get('reason', 'sync_over_limit')
            try:
                # 通过 Futu trade_ctx 直接提交市价平仓单
                ctx = self._get_futu_context(
                    os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                    int(os.getenv("FUTU_OPEND_PORT", "11111")),
                    trade_env, TrdMarket.US, SecurityFirm.FUTUINC,
                )
                if ctx is None:
                    logger.error(f"[减仓] {symbol} Futu 连接不可用")
                    continue
                
                # 平仓方向: LONG→SELL, SHORT→BUY (COVER)
                close_side = TrdSide.SELL if direction == "LONG" else TrdSide.BUY
                
                # 获取当前价格 (市价单需要参考价格)
                try:
                    from app.data.market_data_router import get_market_data_router
                    _md = get_market_data_router().get_latest(symbol)
                    _price = float(_md.get('price', 0) or 0) if _md else 0
                except Exception:
                    _price = 0
                if _price <= 0:
                    _price = trim_info.get('price', 0)
                
                ret, data = ctx.place_order(
                    price=_price,
                    qty=shares,
                    code=symbol,
                    trd_side=close_side,
                    order_type=FutuOrderType.MARKET,
                    trd_env=trade_env,
                    remark=f"OVER_LIMIT_TRIM:{reason}",
                )
                if ret == 0:
                    _oid = data.iloc[0].get("order_id", "?") if hasattr(data, 'iloc') else "?"
                    logger.info(
                        f"✅ [减仓] {symbol} {close_side} {shares}股 已提交 "
                        f"(order_id={_oid}, reason={reason})"
                    )
                    self._last_trim_time[symbol] = _now  # 更新冷却时间
                else:
                    # P1-1 修复 (2026-06-12): 失败也写冷却 + 业务错误识别
                    self._last_trim_time[symbol] = _now
                    _err_text = str(data).lower()
                    _business_keywords = (
                        "找不到", "not found", "invalid", "交叉交易", "相反方向",
                        "不支持", "no such", "permission", "禁止", "不存在",
                    )
                    if any(kw in _err_text for kw in _business_keywords):
                        self._trim_permanent_skip.add(symbol)
                        logger.error(
                            f"[减仓] {symbol} 业务级错误, 加入永久跳过名单: ret={ret}, data={data}"
                        )
                    else:
                        logger.error(f"[减仓] {symbol} 提交失败 (10min 冷却): ret={ret}, data={data}")
            except Exception as e:
                # 异常路径同样写冷却, 防止异常驱动的快速重试
                self._last_trim_time[symbol] = _now
                logger.error(f"[减仓] {symbol} 执行异常 (10min 冷却): {e}")

    # Broker-first (2026-06-03): _restore_position_from_db 已删除
    # 不再从 DB 恢复持仓到内存 — broker 是唯一真相源
    # 新发现的仓位统一走 _sync_to_position_store_locked 的全新计算路径


# Global instance
_us_sync: Optional[USPositionSync] = None
_us_sync_lock = threading.Lock()  # P0-3b 修复 (2026-05-03): 防止多实例竞争


def get_us_position_sync() -> USPositionSync:
    global _us_sync
    if _us_sync is None:
        with _us_sync_lock:
            # 双重检查: 可能在等待锁期间其他线程已初始化
            if _us_sync is None:
                _us_sync = USPositionSync()
    return _us_sync


def start_us_position_sync(sync_interval: int = 30, position_store=None, broker_adapter=None):
    sync = get_us_position_sync()
    sync.sync_interval = sync_interval
    sync.position_store = position_store  # P0 修复 2026-04-25
    sync._broker_adapter = broker_adapter  # 复用执行引擎 broker 连接
    sync.start()
    return sync
