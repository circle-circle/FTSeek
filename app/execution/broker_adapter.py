# app/execution/broker_adapter.py
"""
富途券商适配器

实现富途 OpenD 真实连接：
- 模拟账户美股下单（TrdEnv.SIMULATE）
- 持仓查询
- 实时报价
"""
import logging
import os
import time
import threading
from typing import Optional, Dict, Any, List

from app.core.models import Order, OrderState, Direction, OrderType

logger = logging.getLogger(__name__)


class FutuBrokerAdapter:
    """富途券商适配器（支持模拟和真实环境）"""

    # 心跳与重连配置
    HEARTBEAT_INTERVAL_SEC = 30
    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_BACKOFF_SEC = [5, 15, 30]  # 指数退避间隔

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: int = 1,    # 0=真实，1=模拟
        account_id: str = "",
        on_disconnect_callback=None,
    ):
        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.account_id = account_id
        self.connected = False
        self.quote_ctx = None
        self.trade_ctx = None
        # 实盘解锁密码: 优先 MD5 (更安全，文件里不存明文)
        # 获取方式: echo -n "你的密码" | md5sum | awk '{print $1}'
        self._unlock_pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")   # 推荐: MD5 哈希
        self._unlock_pwd_plain = os.getenv("FUTU_UNLOCK_PWD", "")      # 备选: 明文密码
        self._unlock_pwd = self._unlock_pwd_md5 or self._unlock_pwd_plain
        self._use_md5 = bool(self._unlock_pwd_md5)  # 有 MD5 则用 MD5，否则用明文

        # 心跳与重连状态
        self._last_heartbeat_time = 0.0
        self._heartbeat_failures = 0
        self._reconnect_attempts = 0
        self._reconnect_lock = threading.Lock()
        self._on_disconnect_callback = on_disconnect_callback
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop_event = threading.Event()

    # ------------------------------------------------------------------
    # 兜底安全门禁 (2026-06-06): broker 前最后一道墙
    # ------------------------------------------------------------------

    _NOTIONAL_HARD_CAP_PCT = 0.30  # 30% equity — 粗略硬兜底，比上游 25% 宽松

    def _notional_sanity_check(self, symbol: str, qty, price: float, is_close: bool = False) -> bool:
        """
        兜底检查: 单笔名义 ≤ equity × 30%.
        不依赖 position_store, 只查 equity. 如果 equity 获取失败则 fail-close (拒绝).
        这是所有路径的最后一道墙，不是主要防线。

        P0 (2026-06-16): is_close=True 时绕过检查 — 平仓单不受名义上限约束
        根因: 6/15 SNDK $43K > 30% equity 被拒, FLAT_ALL 无法执行紧急平仓

        Returns: True = 通过, False = 拒绝
        """
        # P0 (2026-06-16): 平仓单一律放行 — 紧急退出不能被门禁阻断
        if is_close:
            return True

        if qty <= 0 or price <= 0:
            return True  # 无效参数不在此拦截（上游应已处理）

        notional = float(qty) * price
        equity = 0.0
        try:
            from app.risk.capital_safety import get_capital_safety_system
            _cap = get_capital_safety_system()
            if _cap._state.current_equity > 0:
                equity = _cap._state.current_equity
        except Exception:
            pass

        # Fail-close: equity 不可用时拒绝 (防止以虚假 $100K 放行超大订单)
        if equity <= 0:
            logger.error(
                f"🛑 [BROKER·兜底] {symbol} 拒绝: equity 不可用, fail-close "
                f"(名义=${notional:,.0f}) — capital_safety 未就绪!"
            )
            return False

        limit = equity * self._NOTIONAL_HARD_CAP_PCT
        if notional > limit:
            logger.error(
                f"🛑 [BROKER·兜底] {symbol} 拒绝: "
                f"名义=${notional:,.0f} > 兜底上限=${limit:,.0f} "
                f"(净值${equity:,.0f}×{self._NOTIONAL_HARD_CAP_PCT:.0%}) "
                f"— 上游门禁全部失效!"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """连接到富途 OpenD（美股模拟交易）"""
        # 检查是否已经连接，避免重复登录
        if self.connected and self.trade_ctx is not None:
            logger.info("[富途] 已连接，跳过重复连接")
            return True
        
        try:
            from futu import (
                OpenQuoteContext,
                OpenSecTradeContext,
                TrdMarket,
                TrdEnv,
                RET_OK,
            )

            # 行情 Context
            self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

            # 交易 Context（美股市场）
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=self.host,
                port=self.port,
                is_encrypt=False,
            )

            # 模拟环境无需解锁密码
            if self.trade_env == 1:
                logger.info("[富途] 模拟交易环境，无需解锁")
                # 自动探测模拟账户 ID（防止 OpenD 重启后 ID 变化）
                self._discover_simulate_account()
            else:
                # 实盘: 必须先解锁交易账户
                if not self._unlock_pwd:
                    logger.error(
                        "[富途·实盘] ❌ 解锁密码未设置！"
                        "无法解锁真实交易账户，下单将被拒绝。"
                        "请在 .env.us 中设置以下任一:\n"
                        "  推荐: FUTU_UNLOCK_PWD_MD5=<md5哈希> "
                        "(生成: echo -n \"密码\" | md5sum | awk '{print $1}')\n"
                        "  备选: FUTU_UNLOCK_PWD=<明文密码>"
                    )
                    self.connected = False
                    return False
                try:
                    if self._use_md5:
                        ret, data = self.trade_ctx.unlock_trade(
                            password_md5=self._unlock_pwd, is_unlock=True,
                        )
                    else:
                        ret, data = self.trade_ctx.unlock_trade(
                            password=self._unlock_pwd, is_unlock=True,
                        )
                    if ret == RET_OK:
                        logger.info("[富途·实盘] ✅ 真实交易账户已解锁")
                    else:
                        logger.error(f"[富途·实盘] ❌ 解锁失败: {data}")
                        self.connected = False
                        return False
                except Exception as unlock_err:
                    logger.error(f"[富途·实盘] ❌ 解锁异常: {unlock_err}")
                    self.connected = False
                    return False
                # 实盘也自动探测账户 ID
                self._discover_real_account()

            self.connected = True
            logger.info(f"[富途] 连接成功 {self.host}:{self.port} env={'模拟' if self.trade_env == 1 else '真实'}")
            return True

        except ImportError:
            logger.error("[富途] futu-api 未安装，请运行: pip install futu-api")
            return False
        except Exception as e:
            logger.error(f"[富途] 连接失败: {e}")
            return False

    def _discover_simulate_account(self):
        """自动探测模拟账户 ID，防止 OpenD 重启后 ID 变化导致下单失败"""
        try:
            from futu import TrdEnv, RET_OK
            ret, data = self.trade_ctx.get_acc_list()
            if ret != RET_OK:
                logger.warning(f"[富途] 无法获取账户列表，使用配置值: {self.account_id}")
                return
            sim_rows = data[data['trd_env'] == TrdEnv.SIMULATE]
            if sim_rows.empty:
                logger.warning(f"[富途] 未找到模拟账户，使用配置值: {self.account_id}")
                return
            actual_sim_id = str(sim_rows.iloc[0]['acc_id'])
            if not self.account_id:
                self.account_id = actual_sim_id
                logger.info(f"[富途] 自动探测模拟账户: {actual_sim_id}")
            elif self.account_id != actual_sim_id:
                logger.warning(
                    f"[富途] 配置的模拟账户 {self.account_id} 不存在，"
                    f"自动切换为实际账户: {actual_sim_id}"
                )
                self.account_id = actual_sim_id
            else:
                logger.info(f"[富途] 模拟账户验证通过: {self.account_id}")
        except Exception as e:
            logger.warning(f"[富途] 自动探测账户异常: {e}，使用配置值: {self.account_id}")

    def _discover_real_account(self):
        """自动探测真实账户 ID，优先使用 REAL_ACCOUNT_ID 环境变量指定的账户"""
        try:
            from futu import TrdEnv, RET_OK
            ret, data = self.trade_ctx.get_acc_list()
            if ret != RET_OK:
                logger.warning(f"[富途·实盘] 无法获取账户列表，使用配置值: {self.account_id}")
                return
            real_rows = data[data['trd_env'] == TrdEnv.REAL]
            if real_rows.empty:
                logger.error("[富途·实盘] ❌ 未找到真实账户，请确认 OpenD 已登录真实账户")
                return

            real_ids = [str(r) for r in real_rows['acc_id'].tolist()]
            # 优先使用 REAL_ACCOUNT_ID 环境变量（多账户时明确指定）
            target_id = os.getenv("REAL_ACCOUNT_ID", "").strip()
            if target_id and target_id in real_ids:
                self.account_id = target_id
                logger.info(f"[富途·实盘] ✅ 使用指定账户: {target_id}")
            elif target_id:
                logger.error(
                    f"[富途·实盘] ❌ 指定账户 {target_id} 不存在！"
                    f"可用真实账户: {real_ids}"
                )
                # 不 fallback，阻断连接
                self.connected = False
            else:
                # 未指定则用第一个（向后兼容，但多账户时不安全）
                actual_real_id = str(real_rows.iloc[0]['acc_id'])
                self.account_id = actual_real_id
                logger.warning(
                    f"[富途·实盘] ⚠️ 未设置 REAL_ACCOUNT_ID，"
                    f"自动使用第一个真实账户: {actual_real_id} "
                    f"(可用账户: {real_ids})"
                )
        except Exception as e:
            logger.warning(f"[富途·实盘] 自动探测真实账户异常: {e}，使用配置值: {self.account_id}")

    def disconnect(self):
        """断开连接"""
        try:
            if self.quote_ctx:
                self.quote_ctx.close()
                self.quote_ctx = None
            if self.trade_ctx:
                self.trade_ctx.close()
                self.trade_ctx = None
        except Exception as e:
            logger.debug(f"[富途] 断开连接: {e}")
        finally:
            self.connected = False
            logger.info("[富途] 已断开连接")

    def is_connected(self) -> bool:
        """检查连接状态（含心跳探测）"""
        if not self.connected or self.trade_ctx is None:
            return False
        # 如果距上次心跳超过 60 秒，主动探测一次
        if time.time() - self._last_heartbeat_time > 60:
            return self._heartbeat_check()
        return True

    # ------------------------------------------------------------------
    # 心跳检测与自动重连
    # ------------------------------------------------------------------

    def start_heartbeat(self, interval: int = HEARTBEAT_INTERVAL_SEC):
        """启动后台心跳检测线程"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            logger.debug("[心跳] 心跳线程已运行")
            return
        self._heartbeat_stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(interval,),
            daemon=True,
            name="futu-heartbeat",
        )
        self._heartbeat_thread.start()
        logger.info(f"[心跳] 后台心跳检测已启动 (间隔={interval}s)")

    def stop_heartbeat(self):
        """停止后台心跳检测"""
        self._heartbeat_stop_event.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=10)
        logger.info("[心跳] 后台心跳检测已停止")

    def _heartbeat_loop(self, interval: int):
        """心跳循环：每 interval 秒检查一次连接"""
        while not self._heartbeat_stop_event.is_set():
            self._heartbeat_stop_event.wait(interval)
            if self._heartbeat_stop_event.is_set():
                break
            if self.connected:
                alive = self._heartbeat_check()
                if not alive:
                    self._attempt_reconnect()

    def _heartbeat_check(self) -> bool:
        """
        执行一次心跳探测：调用轻量级 API 验证连接。
        成功时更新 _last_heartbeat_time 和 _heartbeat_failures。
        """
        try:
            if not self.trade_ctx:
                return False
            from futu import RET_OK
            # 轻量级 API：查询账户列表，开销极小
            ret, data = self.trade_ctx.get_acc_list()
            if ret == RET_OK:
                self._last_heartbeat_time = time.time()
                # 心跳恢复：之前有过失败记录
                if self._heartbeat_failures > 0:
                    logger.info(f"[心跳] 连接恢复 (之前失败 {self._heartbeat_failures} 次)")
                    self._heartbeat_failures = 0
                    self._reconnect_attempts = 0
                return True
            else:
                logger.warning(f"[心跳] 探测失败: {data}")
                self._heartbeat_failures += 1
                return False
        except Exception as e:
            logger.warning(f"[心跳] 探测异常: {e}")
            self._heartbeat_failures += 1
            return False

    def _attempt_reconnect(self):
        """尝试自动重连（指数退避，最多 MAX_RECONNECT_ATTEMPTS 次）"""
        with self._reconnect_lock:
            if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    f"[重连] 已达最大重试次数 ({self.MAX_RECONNECT_ATTEMPTS})，"
                    f"标记连接断开"
                )
                self.connected = False
                if self._on_disconnect_callback:
                    try:
                        self._on_disconnect_callback()
                    except Exception as e:
                        logger.error(f"[重连] 回调异常: {e}")
                return

            backoff = self.RECONNECT_BACKOFF_SEC[self._reconnect_attempts]
            self._reconnect_attempts += 1
            logger.info(
                f"[重连] 尝试 {self._reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS}，"
                f"等待 {backoff}s..."
            )
            time.sleep(backoff)

            # 先断开旧连接
            self.disconnect()

            # 尝试重新连接
            success = self.connect()
            if success:
                logger.info(f"[重连] 成功 (尝试 {self._reconnect_attempts} 次)")
                self._heartbeat_failures = 0
                self._reconnect_attempts = 0
                self._last_heartbeat_time = time.time()
            else:
                logger.warning(
                    f"[重连] 第 {self._reconnect_attempts} 次失败"
                )
                # 如果仍有机会，递归尝试
                if self._reconnect_attempts < self.MAX_RECONNECT_ATTEMPTS:
                    self._attempt_reconnect()
                else:
                    self.connected = False
                    if self._on_disconnect_callback:
                        try:
                            self._on_disconnect_callback()
                        except Exception as e:
                            logger.error(f"[重连] 回调异常: {e}")

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Dict[str, Any]:
        """
        提交订单到富途模拟账户

        Args:
            order: 订单对象

        Returns:
            {"order_id": str, "status": str, "futu_order_id": str}
        """
        # 2. 提交订单
        if not self.is_connected():
            # P0: 实盘模式下 Broker 断连时拒绝订单，不允许模拟成交
            if os.environ.get("TRADE_ENV", "1") == "0":
                raise RuntimeError(
                    f"[实盘安全] Broker 未连接, 拒绝提交 {order.symbol} 订单 — "
                    f"请勿在断连状态下交易"
                )
            return self._simulate_fill(order)

        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK

            # 转换方向 (确保 Order 对象有 direction 或从 action 推断)
            trd_side = order.trd_side if hasattr(order, 'trd_side') else (TrdSide.BUY if order.direction == Direction.LONG else TrdSide.SELL)
            # 转换订单类型
            futu_order_type = FutuOrderType.NORMAL
            if order.order_type == OrderType.MARKET:
                futu_order_type = FutuOrderType.MARKET
            # 转换交易环境
            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

            # 2026-04-26: 移除 TWAP 拆单逻辑，统一单笔提交
            # 风控层已充分约束订单规模 (PositionLimits / CapitalSafety / RiskStateMachine)
            # 移动止损策略 (Chandelier/ATR/Flash Exit) 独立于下单方式，不受影响
            is_sell = False
            if hasattr(order, 'trd_side'):
                is_sell = (order.trd_side == TrdSide.SELL)
            elif hasattr(order, 'direction'):
                is_sell = (order.direction != Direction.LONG)

            # 兜底门禁 (2026-06-06): broker 前最后一道墙
            # P0 (2026-06-16): 平仓单 (is_exit=True) 绕过门禁 — 紧急退出不能被阻断
            _order_meta = getattr(order, 'metadata', None) or {}
            _is_close_order = _order_meta.get('is_exit', False) if isinstance(_order_meta, dict) else False
            if not self._notional_sanity_check(order.symbol, order.quantity, order.price, is_close=_is_close_order):
                return {"success": False, "error": "broker_notional_cap", "order_id": ""}

            ret, data = self.trade_ctx.place_order(
                price=order.price,
                qty=order.quantity,
                code=order.symbol,
                trd_side=trd_side,
                order_type=futu_order_type,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )

            if ret == RET_OK:
                futu_id = str(data["order_id"].values[0])

                # P0 Fix: 订单状态快速确认 (防止幽灵仓)
                # 单次查询，不 sleep — place_order 成功返回时交易所拒单已同步返回
                # 延迟性拒单由 position_sync (30s) 兜底
                try:
                    from futu import TrdEnv, RET_OK as RET_OK_FUTU
                    
                    trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL
                    
                    ret_q, data_q = self.trade_ctx.order_list_query(
                        order_id=futu_id,
                        trd_env=trd_env
                    )
                    if ret_q == RET_OK_FUTU and not data_q.empty:
                        status = str(data_q.iloc[0].get('order_status', ''))
                        if 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
                            logger.error(f"🚨 [订单失败] {order.symbol} Status={status}")
                            raise RuntimeError(f"订单提交后失败: {order.symbol} status={status}")
                except RuntimeError:
                    raise
                except Exception as e:
                    logger.warning(f"[订单状态检查] 异常 (降级放行): {e}")

                # P0 Fix (2026-06-11): PDT 记录移至平仓时
                # 开仓时不记录 — PDT 法规只计 round trip (同日开+平)
                # 记录逻辑在 order_executor._close_position() 中调用 pdt_guard.record_close()

                logger.info(
                    f"[富途] 下单成功 {order.symbol} {trd_side} "
                    f"qty={order.quantity} price={order.price} "
                    f"futu_order_id={futu_id}"
                )
                return {
                    "order_id": order.order_id,
                    "futu_order_id": futu_id,
                    "futu_order_ids": [futu_id],  # 保持向后兼容
                    "status": "submitted",
                }
            else:
                raise RuntimeError(f"下单失败: {data}")

        except ImportError:
            if os.environ.get("TRADE_ENV", "1") == "0":
                raise RuntimeError(
                    f"[实盘安全] futu-api 未安装, 拒绝提交 {order.symbol} 订单"
                )
            return self._simulate_fill(order)
        except Exception as e:
            logger.error(f"[富途] 下单异常 {order.symbol}: {e}")
            raise

    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if not self.is_connected():
            logger.warning("[富途] 未连接，模拟取消成功")
            return True

        try:
            from futu import TrdEnv, ModifyOrderOp, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

            ret, data = self.trade_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )
            if ret == 0:  # RET_OK
                logger.info(f"[富途] 取消订单成功 {order_id}")
                return True
            else:
                logger.error(f"[富途] 取消订单失败 {order_id}: {data}")
                return False

        except ImportError:
            return True
        except Exception as e:
            logger.error(f"[富途] 取消订单异常 {order_id}: {e}")
            return False

    def amend_order(self, order_id: str, new_price: float = None, new_qty: int = None) -> bool:
        """改单 — 修改未成交订单的价格或数量 (ModifyOrderOp.MODIFY)"""
        if not self.is_connected():
            logger.warning("[富途] 未连接，模拟改单成功")
            return True

        try:
            from futu import TrdEnv, ModifyOrderOp, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

            ret, data = self.trade_ctx.modify_order(
                modify_order_op=ModifyOrderOp.MODIFY,
                order_id=order_id,
                qty=new_qty or 0,
                price=new_price or 0,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )
            if ret == RET_OK:
                logger.info(f"[富途] 改单成功 {order_id}: price={new_price}, qty={new_qty}")
                return True
            else:
                logger.error(f"[富途] 改单失败 {order_id}: {data}")
                return False

        except ImportError:
            return True
        except Exception as e:
            logger.error(f"[富途] 改单异常 {order_id}: {e}")
            return False

    def place_stop_order(self, symbol: str, qty: int, stop_price: float,
                         direction: str = "LONG") -> dict:
        """
        Broker 层止损单 — 使用 Futu STOP 订单类型

        设计:
          - LONG 持仓: SELL STOP @ stop_price (价格跌破时触发卖出)
          - SHORT 持仓: BUY STOP @ stop_price (价格突破时触发买入)
          - 模拟盘降级: Futu SIMULATE 可能不支持 STOP, 返回 mock
          - 失败时 graceful degrade (仅日志, 不阻塞主流程)
        """
        if not self.is_connected():
            logger.debug(f"[富途] 未连接, 跳过止损单 {symbol}")
            return {"success": False, "reason": "not_connected"}

        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

            # 模拟盘不支持 STOP — graceful degrade
            if trd_env == TrdEnv.SIMULATE:
                logger.debug(f"[富途] 模拟盘跳过 STOP 单 {symbol} (不支持)")
                return {"success": False, "reason": "simulate_not_supported"}

            trd_side = TrdSide.SELL if direction == "LONG" else TrdSide.BUY

            ret, data = self.trade_ctx.place_order(
                price=stop_price,
                qty=qty,
                code=symbol,
                trd_side=trd_side,
                order_type=FutuOrderType.STOP,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )

            if ret == RET_OK and not data.empty:
                order_id = str(data.iloc[0].get("order_id", ""))
                logger.info(f"[富途] STOP 单已挂 {symbol} {direction} qty={qty} stop=${stop_price} → {order_id}")
                return {"success": True, "order_id": order_id}
            else:
                logger.warning(f"[富途] STOP 单失败 {symbol}: {data}")
                return {"success": False, "reason": str(data)}

        except Exception as e:
            logger.warning(f"[富途] STOP 单异常 {symbol}: {e}")
            return {"success": False, "reason": str(e)}

    def query_order(self, order_id: str) -> Optional[Order]:
        """查询订单状态"""
        if not self.is_connected():
            return None

        try:
            from futu import TrdEnv, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL
            ret, data = self.trade_ctx.order_list_query(
                order_id=order_id,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )

            if ret == RET_OK and len(data) > 0:
                row = data.iloc[0]
                order = Order(
                    order_id=order_id,
                    symbol=str(row.get("code", "")),
                    quantity=float(row.get("qty", 0)),
                    price=float(row.get("price", 0)),
                    filled_quantity=float(row.get("dealt_qty", 0)),
                    avg_fill_price=float(row.get("dealt_avg_price", 0)),
                )
                # 转换状态
                futu_status = str(row.get("order_status", ""))
                order.state = self._convert_order_status(futu_status)
                return order

        except ImportError:
            pass
        except Exception as e:
            logger.error(f"[富途] 查询订单异常 {order_id}: {e}")

        return None

    # ------------------------------------------------------------------
    # 持仓查询
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        查询持仓列表

        Returns:
            [{"symbol", "qty", "cost_price", "current_price", "unrealized_pnl"}, ...]
        """
        if not self.is_connected():
            logger.debug("[富途] 未连接，返回空持仓")
            return []

        try:
            from futu import TrdEnv, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL
            ret, data = self.trade_ctx.position_list_query(
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )

            if ret == RET_OK and len(data) > 0:
                positions = []
                for _, row in data.iterrows():
                    positions.append({
                        "symbol": str(row.get("code", "")),
                        "qty": float(row.get("qty", 0)),
                        "cost_price": float(row.get("cost_price", 0)),
                        "current_price": float(row.get("market_val", 0)) / max(1, float(row.get("qty", 1))),
                        "unrealized_pnl": float(row.get("pl_val", 0)),
                        "unrealized_pnl_ratio": float(row.get("pl_ratio", 0)),
                    })
                return positions
            return []

        except ImportError:
            return []
        except Exception as e:
            logger.error(f"[富途] 查询持仓异常: {e}")
            return []

    # ------------------------------------------------------------------
    # 实时报价
    # ------------------------------------------------------------------

    def get_current_price(self, symbol: str) -> float:
        """
        获取当前价格（市场快照）

        Args:
            symbol: 如 "US.AAPL"

        Returns:
            当前价格，失败返回 0.0
        """
        if not self.is_connected() or not self.quote_ctx:
            return 0.0

        try:
            from futu import RET_OK

            ret, data = self.quote_ctx.get_market_snapshot([symbol])
            if ret == RET_OK and len(data) > 0:
                price = float(data["last_price"].values[0])
                return price
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"[富途] 获取价格异常 {symbol}: {e}")

        return 0.0

    @staticmethod
    def _safe_float(val, default=0.0) -> float:
        """安全转换: 处理 Futu 模拟盘返回的 'N/A'/'--'/None 等非法值"""
        if val is None:
            return default
        if isinstance(val, str):
            val = val.strip()
            if val in ("N/A", "--", "-", "") or val.lower() == "nan":
                return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    # ── 汇率缓存 (2026-06-10) ──
    _fx_cache: Dict[str, tuple] = {}  # {"HKD_USD": (rate, timestamp)}
    _FX_TTL = 3600  # 1 hour (HKD is pegged, low volatility)

    def _get_fx_rate(self, from_currency: str, to_currency: str = "USD") -> float:
        """获取汇率 (from_currency → to_currency).

        HKD/USD 联系汇率制 (7.75-7.85), 使用 Futu 实时汇率或固定值.
        """
        cache_key = f"{from_currency}_{to_currency}"
        import time as _time
        now = _time.time()

        # Check cache
        if cache_key in self._fx_cache:
            rate, ts = self._fx_cache[cache_key]
            if now - ts < self._FX_TTL:
                return rate

        rate = 0.0

        # Try Futu market data for real-time rate
        if from_currency == "HKD" and to_currency == "USD":
            try:
                from futu import OpenQuoteContext, RET_OK
                quote_ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
                try:
                    # USDHKD forex pair
                    ret, data = quote_ctx.get_market_snapshot(["FX.USDCNH"])
                    # Fallback: use fixed peg rate
                    rate = 1.0 / 7.80  # HKD→USD
                finally:
                    quote_ctx.close()
            except Exception:
                pass

        # Fallback: fixed rates for known pegs
        if rate <= 0:
            _FIXED_RATES = {
                "HKD_USD": 1.0 / 7.80,   # HKD pegged at ~7.80
                "CNY_USD": 1.0 / 7.25,   # approximate
                "JPY_USD": 1.0 / 150.0,  # approximate
            }
            rate = _FIXED_RATES.get(cache_key, 0.0)

        if rate > 0:
            self._fx_cache[cache_key] = (rate, now)

        return rate

    def get_account_info(self) -> Dict[str, Any]:
        """获取账户资金信息 (完整 accinfo 字段)"""
        if not self.is_connected():
            return {}

        try:
            from futu import TrdEnv, RET_OK
            sf = self._safe_float

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL
            ret, data = self.trade_ctx.accinfo_query(
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )
            if ret == RET_OK and len(data) > 0:
                row = data.iloc[0]
                # P0 修复 (2026-05-02): 提取完整 accinfo 字段供保证金风控使用
                # 2026-05-04: _safe_float 处理模拟盘 'N/A' 返回值
                result = {
                    # 基础字段
                    "total_assets": sf(row.get("total_assets")),
                    "cash": sf(row.get("cash")),
                    "market_val": sf(row.get("market_val")),
                    "buying_power": sf(row.get("power")),
                    # 新增: 保证金风控字段
                    "avl_withdrawal_cash": sf(row.get("avl_withdrawal_cash")),
                    "frozen_cash": sf(row.get("frozen_cash")),
                    "debt": sf(row.get("debt")),
                    "interest_debt": sf(row.get("interest_debt")),
                    "initial_margin": sf(row.get("initial_margin")),
                    "maintain_margin": sf(row.get("maintain_margin")),
                    "short_sell_power": sf(row.get("short_sell_power")),
                    "net_assets_power": sf(row.get("net_assets_power")),
                    # 币种信息
                    "currency": str(row.get("currency", "USD")),
                    # 元数据
                    "_trade_env": self.trade_env,
                    "_raw": {
                        k: row.get(k) for k in [
                            "total_assets", "cash", "market_val", "power",
                            "avl_withdrawal_cash", "frozen_cash", "debt", "interest_debt",
                            "initial_margin", "maintain_margin", "short_sell_power", "net_assets_power",
                        ]
                    },
                }

                # ── 币种转换 (2026-06-15 修复): HKD → USD ──
                # 优先使用账户内置汇率 (usd_assets/total_assets), 回退到固定 7.80
                acct_currency = result["currency"]
                if acct_currency != "USD" and acct_currency != "N/A":
                    # 计算实际汇率: HKD total_assets / USD usd_assets
                    _usd_assets = sf(row.get("usd_assets"))
                    _hkd_assets = sf(row.get("total_assets"))
                    if _usd_assets > 0 and _hkd_assets > 0:
                        fx_rate = 1.0 / (_hkd_assets / _usd_assets)  # HKD→USD = usd/hkd
                    else:
                        fx_rate = self._get_fx_rate(acct_currency, "USD")

                    if fx_rate and fx_rate > 0:
                        result["_fx_rate"] = fx_rate
                        result["_original_currency"] = acct_currency

                        # 核心字段: 优先使用 USD 子账户字段 (更精准)
                        _usd_cash = sf(row.get("us_cash"))
                        _usd_avl = sf(row.get("us_avl_withdrawal_cash"))
                        _usd_power = sf(row.get("usd_net_cash_power"))

                        if _usd_assets > 0:
                            result["total_assets"] = round(_usd_assets, 2)
                        else:
                            result["total_assets"] = round(result["total_assets"] * fx_rate, 2)

                        result["cash"] = round(_usd_cash * 1 if _usd_cash > 0 else result["cash"] * fx_rate, 2)
                        result["avl_withdrawal_cash"] = round(_usd_avl if _usd_avl > 0 else result["avl_withdrawal_cash"] * fx_rate, 2)

                        if _usd_power > 0:
                            result["buying_power"] = round(_usd_power, 2)
                        else:
                            result["buying_power"] = round(result["buying_power"] * fx_rate, 2)

                        # 其余字段按比例折算
                        for key in ["market_val", "frozen_cash",
                                    "initial_margin", "maintain_margin"]:
                            if key in result and isinstance(result[key], (int, float)):
                                result[f"_raw_{key}_{acct_currency}"] = result[key]
                                result[key] = round(result[key] * fx_rate, 2)

                        logger.info(
                            f"[富途] 币种转换 {acct_currency}→USD (rate={fx_rate:.4f}): "
                            f"total_assets=${result['total_assets']:,.0f} "
                            f"buying_power=${result['buying_power']:,.0f}"
                        )

                return result
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"[富途] 查询账户异常: {e}")

        return {}

    # ------------------------------------------------------------------
    # 直接卖空
    # ------------------------------------------------------------------

    def try_direct_short(self, symbol: str, qty: int, price: float) -> Dict[str, Any]:
        """
        尝试直接卖空（TrdSide.SELL）

        幻方增强 (2026-04-29):
        - 借券可用性预检 (避免无效下单)
        - 重试机制 (仅瞬态错误, 最多 2 次, 1s 退避)
        - 扩展错误关键词分类

        Args:
            symbol: 股票代码，如 "US.TSLA"
            qty:    卖空数量
            price:  限价（0 使用市价）

        Returns:
            {"success": bool, "order_id": str, "futu_order_id": str, "status": str, "error": str}
        """
        import time as _time

        if not self.is_connected():
            logger.warning(f"[卖空] {symbol} 未连接富途，降级到 Put 期权")
            return {"success": False, "error": "not_connected"}

        # ── 预检: 借券可用性 ──
        try:
            from app.risk.short_specific_checks import check_borrow_cost
            borrow_ok, borrow_reason = check_borrow_cost(symbol)
            if not borrow_ok:
                logger.warning(f"[卖空] {symbol} 借券预检失败: {borrow_reason}")
                return {"success": False, "error": f"borrow_check_failed: {borrow_reason}"}
        except ImportError:
            pass

        # ── 执行卖空 (含重试) ──
        max_attempts = 2
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK

                futu_order_type = FutuOrderType.NORMAL if price > 0 else FutuOrderType.MARKET
                trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

                # 兜底门禁 (2026-06-06): broker 前最后一道墙
                if not self._notional_sanity_check(symbol, qty, price if price > 0 else 1.0):
                    return {"success": False, "error": "broker_notional_cap", "order_id": ""}

                ret, data = self.trade_ctx.place_order(
                    price=price if price > 0 else 0,
                    qty=qty,
                    code=symbol,
                    trd_side=TrdSide.SELL,
                    order_type=futu_order_type,
                    trd_env=trd_env,
                    acc_id=int(self.account_id) if self.account_id else 0,
                )

                if ret == RET_OK:
                    futu_order_id = str(data["order_id"].values[0])
                    logger.info(
                        f"[卖空] {symbol} 直接卖空成功 qty={qty} price={price} "
                        f"futu_order_id={futu_order_id}"
                    )
                    return {
                        "success": True,
                        "order_id": futu_order_id,
                        "futu_order_id": futu_order_id,
                        "status": "submitted",
                    }
                else:
                    error_msg = str(data).lower() if data is not None else ""
                    last_error = str(data)

                    # 不支持卖空的错误特征 (扩展版)
                    short_fail_keywords = [
                        "not support", "short sell", "margin", "insufficient",
                        "short", "borrow", "locate", "restricted",
                        "not allowed", "cannot sell", "no position",
                        "hard to borrow", "locate", "restricted security",
                        "margin requirement",
                    ]
                    is_short_not_supported = any(
                        kw in error_msg for kw in short_fail_keywords
                    )
                    is_transient = "timeout" in error_msg or "network" in error_msg or "temporary" in error_msg

                    if is_short_not_supported:
                        logger.info(
                            f"[卖空] {symbol} 不支持直接卖空，将降级到 Put 期权"
                        )
                        return {"success": False, "error": last_error}

                    if is_transient and attempt < max_attempts:
                        logger.warning(
                            f"[卖空] {symbol} 瞬态错误 (attempt={attempt}/{max_attempts}), "
                            f"1s 后重试..."
                        )
                        _time.sleep(1)
                        continue

                    logger.warning(f"[卖空] {symbol} 直接卖空失败: {data}")
                    return {"success": False, "error": last_error}

            except ImportError:
                logger.warning("[卖空] futu-api 未安装，降级到 Put 期权")
                return {"success": False, "error": "futu_not_installed"}
            except Exception as e:
                last_error = str(e)
                is_transient = any(kw in last_error.lower() for kw in ["timeout", "network", "temporary", "connection"])

                if is_transient and attempt < max_attempts:
                    logger.warning(
                        f"[卖空] {symbol} 瞬态异常 (attempt={attempt}/{max_attempts}): {e}"
                    )
                    _time.sleep(1)
                    continue

                logger.error(f"[卖空] {symbol} 卖空异常: {e}")
                return {"success": False, "error": last_error}

        return {"success": False, "error": last_error or "max_retries_exceeded"}

    # ------------------------------------------------------------------
    # 期权下单 & 查询
    # ------------------------------------------------------------------

    def submit_option_order(
        self,
        option_code: str,
        qty: int,
        price: float,
        direction: str = "BUY",
    ) -> Dict[str, Any]:
        """
        提交期权订单（买入/卖出）。

        Args:
            option_code: 富途期权代码，如 "US.TSLA250425P380000"
            qty:         手数（1手=100股权益）
            price:       限价（0 使用市价）
            direction:   "BUY" 开仓买入 Put / "SELL" 平仓卖出 Put

        Returns:
            {"order_id", "futu_order_id", "status"}
        """
        # 模拟盘期权交易 — 通过 Futu 模拟 API 执行 (2026-04-30 修复)
        # 旧逻辑：trade_env==1 直接 simulated_fill，跳过 API 验证
        # 新逻辑：统一走 API，trd_env=TrdEnv.SIMULATE 自动路由到模拟盘
        # 优势：验证期权代码有效性、获取真实成交状态、避免虚假持仓
        if not self.is_connected():
            logger.info(
                f"[期权·模拟] {direction} {option_code} qty={qty} price={price}"
            )
            return {
                "order_id": f"OPT_{option_code}_{direction}",
                "futu_order_id": f"SIM_OPT_{option_code[:20]}",
                "status": "simulated_fill",
            }

        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK

            trd_side = TrdSide.BUY if direction.upper() == "BUY" else TrdSide.SELL
            futu_order_type = FutuOrderType.NORMAL if price > 0 else FutuOrderType.MARKET
            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL

            # 兜底门禁 (2026-06-06): 期权也过 broker 兜底 (notional = qty × 100 × premium)
            _opt_notional = float(qty) * 100 * (price if price > 0 else 1.0)
            if not self._notional_sanity_check(option_code, qty, _opt_notional / max(qty, 1)):
                return {"success": False, "error": "broker_notional_cap"}

            ret, data = self.trade_ctx.place_order(
                price=price if price > 0 else 0,
                qty=qty,
                code=option_code,
                trd_side=trd_side,
                order_type=futu_order_type,
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )

            if ret == RET_OK:
                futu_order_id = str(data["order_id"].values[0])
                logger.info(
                    f"[期权] {direction} {option_code} qty={qty} price={price} "
                    f"futu_order_id={futu_order_id}"
                )
                return {
                    "order_id": f"OPT_{option_code}_{direction}",
                    "futu_order_id": futu_order_id,
                    "status": "submitted",
                }
            else:
                logger.error(f"[期权] 下单失败 {option_code}: {data}")
                return {
                    "order_id": f"OPT_{option_code}_{direction}",
                    "futu_order_id": f"SIM_FAIL_{option_code[:16]}",
                    "status": "rejected",
                    "error": str(data),
                }

        except ImportError:
            return {
                "order_id": f"OPT_{option_code}_{direction}",
                "futu_order_id": f"SIM_OPT_{option_code[:20]}",
                "status": "simulated_fill",
            }
        except Exception as e:
            logger.error(f"[期权] 下单异常 {option_code}: {e}")
            raise

    def get_option_positions(self) -> List[Dict[str, Any]]:
        """
        查询账户中所有期权持仓（过滤 security_type=OPTION）。

        Returns:
            [{"option_code", "qty", "cost_price", "current_price",
              "unrealized_pnl", "unrealized_pnl_ratio"}, ...]
        """
        if not self.is_connected():
            logger.debug("[期权] 未连接，返回空期权持仓")
            return []

        try:
            from futu import TrdEnv, RET_OK

            trd_env = TrdEnv.SIMULATE if self.trade_env == 1 else TrdEnv.REAL
            ret, data = self.trade_ctx.position_list_query(
                trd_env=trd_env,
                acc_id=int(self.account_id) if self.account_id else 0,
            )

            if ret != RET_OK or data is None or len(data) == 0:
                return []

            positions = []
            for _, row in data.iterrows():
                sec_type = str(row.get("security_type", "")).upper()
                if sec_type != "OPTION":
                    continue
                qty = float(row.get("qty", 0))
                if qty <= 0:
                    continue
                cost  = float(row.get("cost_price", 0))
                mval  = float(row.get("market_val", 0))
                cur_p = mval / max(1, qty * 100)  # 期权市值 / 份数/100
                positions.append({
                    "option_code":         str(row.get("code", "")),
                    "qty":                 int(qty),
                    "cost_price":          cost,
                    "current_price":       round(cur_p, 4),
                    "unrealized_pnl":      float(row.get("pl_val", 0)),
                    "unrealized_pnl_ratio": float(row.get("pl_ratio", 0)),
                })
            return positions

        except ImportError:
            return []
        except Exception as e:
            logger.error(f"[期权] 查询期权持仓异常: {e}")
            return []

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _simulate_fill(self, order: Order) -> Dict[str, Any]:
        """模拟成交（无富途连接时）"""
        logger.info(
            f"[模拟成交] {order.symbol} {order.direction.value} "
            f"qty={order.quantity} price={order.price}"
        )
        return {
            "order_id": order.order_id,
            "futu_order_id": f"SIM_{order.order_id[:8]}",
            "status": "simulated_fill",
        }

    @staticmethod
    def _convert_order_status(futu_status: str) -> OrderState:
        """转换富途订单状态到内部状态"""
        mapping = {
            "NONE": OrderState.CREATED,
            "UNSUBMITTED": OrderState.CREATED,
            "WAITING_SUBMIT": OrderState.CREATED,
            "SUBMITTING": OrderState.SENT,
            "SUBMITTED": OrderState.ACKED,
            "FILLED_PART": OrderState.PARTIALLY_FILLED,
            "FILLED_ALL": OrderState.FILLED,
            "CANCELLING_PART": OrderState.CANCEL_REQUESTED,
            "CANCELLING_ALL": OrderState.CANCEL_REQUESTED,
            "CANCELLED_PART": OrderState.CANCELED,
            "CANCELLED_ALL": OrderState.CANCELED,
            "FAILED": OrderState.REJECTED,
            "DISABLED": OrderState.CANCELED,
            "DELETED": OrderState.CANCELED,
        }
        return mapping.get(futu_status.upper(), OrderState.UNKNOWN)
