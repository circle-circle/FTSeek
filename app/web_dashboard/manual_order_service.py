"""
手动下单服务 — Web Dashboard 后端

功能:
  - 连接 Futu broker 执行手动订单
  - 风控校验 (金额上限、持仓限制)
  - 审计日志 (每笔手动下单写 DB)
  - 模拟/实盘模式感知

安全设计:
  - 单笔金额上限 (可配置)
  - 每日手动下单次数限制
  - 所有操作写 audit log
  - 实盘下单需二次确认
"""

import os
import json
import sqlite3
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict

logger = logging.getLogger("ftseek_us.manual_order_service")


@dataclass
class ManualOrder:
    """手动订单数据结构"""
    symbol: str
    direction: str          # "LONG" or "SHORT"
    action: str             # "BUY" or "SELL"
    quantity: int
    order_type: str         # "MARKET" or "LIMIT"
    price: Optional[float]  # None for MARKET orders
    reason: str             # 下单理由 (必填)
    mode: str               # "simulate" or "live"
    time_in_force: str = "DAY"      # "DAY" or "GTC"
    fill_outside_rth: bool = False  # 是否允许盘前盘后成交
    timestamp: str = ""
    order_id: str = ""
    status: str = "pending"  # pending, submitted, filled, rejected, error
    error_msg: str = ""


class ManualOrderService:
    """手动下单服务"""

    # 安全限制
    MAX_ORDER_VALUE_USD = 50_000       # 单笔最大金额 $50K
    MAX_DAILY_MANUAL_ORDERS = 20       # 每日最大手动下单次数
    LIVE_CONFIRM_KEYWORD = "CONFIRM"   # 实盘确认关键词

    def __init__(self):
        self._broker_ctx = None
        self._quote_ctx = None
        self._connected = False
        self._db_path = None

    def _get_db_path(self) -> str:
        """获取当前模式对应的 DB 路径"""
        if self._db_path is None:
            from app.core.db_utils import get_main_db
            self._db_path = str(get_main_db())
        return self._db_path

    def _is_live(self) -> bool:
        """从 .env.us 实时读取 (无需重启进程)"""
        env_file = Path(__file__).parent.parent.parent / ".env.us"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    if line.strip().startswith("TRADE_ENV="):
                        return line.strip().split("=", 1)[1].strip() == "0"
        return os.getenv("TRADE_ENV", "1").strip() == "0"

    def _read_env(self, key: str, default: str = "") -> str:
        """从 .env.us 读取环境变量 (无需重启进程)"""
        env_file = Path(__file__).parent.parent.parent / ".env.us"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(f"{key}="):
                        return stripped.split("=", 1)[1].strip()
        return os.getenv(key, default)

    def _get_mode_label(self) -> str:
        if self._is_live():
            conservative = self._read_env("LIVE_CONSERVATIVE", "1").strip() == "1"
            return "live_conservative" if conservative else "live_normal"
        return "simulate"

    # ─────────────────────────────────────────────────────────────
    # Broker 连接管理
    # ─────────────────────────────────────────────────────────────

    def _trd_env(self):
        """获取当前交易环境 (避免重复代码)"""
        from futu import TrdEnv
        return TrdEnv.REAL if self._is_live() else TrdEnv.SIMULATE

    def _acc_id(self) -> int:
        """获取当前账户 ID"""
        return int(self._read_env(
            "REAL_ACCOUNT_ID" if self._is_live() else "SIMULATE_ACCOUNT_ID", "0"
        ))

    def connect(self) -> bool:
        """连接到 Futu broker
        
        Auto-reconnect (2026-06-11): 如果连接断开，重置状态后重试。
        之前 _connected 永远不重置，断连后所有 RPC 永久失败直到重启进程。
        """
        if self._connected and self._broker_ctx:
            # 快速健康检查：用一个轻量调用验证连接存活
            try:
                from futu import RET_OK
                ret, _ = self._broker_ctx.accinfo_query(trd_env=self._trd_env(), acc_id=self._acc_id())
                if ret == RET_OK:
                    return True
                # 连接存活但查询失败 — 可能是临时问题，保持连接
                logger.warning("[手动下单] 健康检查失败，保持连接")
                return True
            except Exception:
                # 连接已死 — 重置并重连
                logger.warning("[手动下单] 连接已断开，正在重连...")
                self.disconnect()

        try:
            from futu import (
                OpenQuoteContext, OpenSecTradeContext,
                TrdMarket, TrdEnv, RET_OK,
            )

            host = os.getenv("OPEND_HOST", "127.0.0.1")
            port = int(os.getenv("OPEND_PORT", "11111"))

            self._quote_ctx = OpenQuoteContext(host=host, port=port)
            self._broker_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=host, port=port, is_encrypt=False,
            )

            # 实盘解锁
            if self._is_live():
                pwd_md5 = self._read_env("FUTU_UNLOCK_PWD_MD5", "")
                if not pwd_md5:
                    logger.error("[手动下单] 实盘模式但 FUTU_UNLOCK_PWD_MD5 未设置")
                    return False
                ret, data = self._broker_ctx.unlock_trade(
                    password_md5=pwd_md5, is_unlock=True,
                )
                if ret != RET_OK:
                    logger.error(f"[手动下单] 实盘解锁失败: {data}")
                    return False
                logger.info("[手动下单] ✅ 实盘已解锁")

            self._connected = True
            logger.info(f"[手动下单] 连接成功 ({'实盘' if self._is_live() else '模拟盘'})")
            return True

        except Exception as e:
            logger.error(f"[手动下单] 连接失败: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self._quote_ctx:
            try:
                self._quote_ctx.close()
            except Exception:
                pass
        if self._broker_ctx:
            try:
                self._broker_ctx.close()
            except Exception:
                pass
        self._connected = False

    # ─────────────────────────────────────────────────────────────
    # 报价查询
    # ─────────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """获取报价 (snapshot 稳定可靠, 盘中盘后均可用)"""
        if not self.connect():
            return {"symbol": symbol, "error": "broker_not_connected"}

        from app.utils.numeric import safe_float as _f

        try:
            from futu import RET_OK

            ret, snap = self._quote_ctx.get_market_snapshot([symbol])
            if ret == RET_OK and not snap.empty:
                row = snap.iloc[0]
                # 优先 cur_price → last_price → prev_close_price
                cur = _f(row.get("cur_price"))
                if cur <= 0:
                    cur = _f(row.get("last_price"))
                if cur <= 0:
                    cur = _f(row.get("prev_close_price"))
                return {
                    "symbol": symbol,
                    "cur_price": cur,
                    "high_price": _f(row.get("high_price")),
                    "low_price": _f(row.get("low_price")),
                    "prev_close": _f(row.get("prev_close_price")),
                    "volume": int(_f(row.get("volume"))),
                    "update_time": str(row.get("update_time") or ""),
                }

            return {"symbol": symbol, "error": "no_data"}
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}

    def _batch_quotes(self, symbols: list) -> Dict[str, float]:
        """批量获取报价，返回 {symbol: price}"""
        if not symbols or not self.connect():
            return {}
        try:
            from futu import RET_OK
            # Futu 每次最多 400 个
            ret, snap = self._quote_ctx.get_market_snapshot(symbols[:400])
            if ret != RET_OK or snap.empty:
                return {}

            def _f(v):
                try:
                    return float(v) if v is not None and v != "N/A" else 0.0
                except (ValueError, TypeError):
                    return 0.0

            prices = {}
            for _, row in snap.iterrows():
                code = row.get("code", "")
                cur = _f(row.get("cur_price"))
                if cur <= 0:
                    cur = _f(row.get("last_price"))
                if cur <= 0:
                    cur = _f(row.get("prev_close_price"))
                if cur > 0:
                    prices[code] = cur
            return prices
        except Exception as e:
            logger.error(f"[批量报价] 失败: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────
    # 风控校验
    # ─────────────────────────────────────────────────────────────

    def validate_order(self, order: ManualOrder) -> List[str]:
        """校验订单，返回错误列表 (空列表 = 通过)"""
        errors = []

        # 1. 基础参数校验
        if not order.symbol:
            errors.append("标的代码不能为空")
        if order.direction not in ("LONG", "SHORT"):
            errors.append(f"方向无效: {order.direction} (必须 LONG 或 SHORT)")
        if order.action not in ("BUY", "SELL"):
            errors.append(f"操作无效: {order.action} (必须 BUY 或 SELL)")
        if order.quantity <= 0:
            errors.append(f"数量必须大于 0: {order.quantity}")
        if order.quantity > 10000:
            errors.append(f"数量过大: {order.quantity} (上限 10000)")
        if order.order_type == "LIMIT" and (not order.price or order.price <= 0):
            errors.append("限价单必须指定价格")

        # 2. 金额上限校验
        quote = self.get_quote(order.symbol)
        cur_price = quote.get("cur_price", 0)
        if cur_price > 0:
            order_value = order.quantity * cur_price
            if order_value > self.MAX_ORDER_VALUE_USD:
                errors.append(
                    f"订单金额 ${order_value:,.0f} 超过上限 ${self.MAX_ORDER_VALUE_USD:,.0f}"
                )

        # 3. 每日手动下单次数限制
        daily_count = self._count_today_manual_orders()
        if daily_count >= self.MAX_DAILY_MANUAL_ORDERS:
            errors.append(
                f"今日手动下单已达 {daily_count} 次 (上限 {self.MAX_DAILY_MANUAL_ORDERS})"
            )

        # 4. 理由可选 (不再强制)

        # 5. 实盘额外校验
        if self._is_live():
            allow = self._read_env("ALLOW_REAL_TRADING", "false").lower()
            if allow != "true":
                errors.append("ALLOW_REAL_TRADING 未设为 true，实盘下单被拦截")

        return errors

    def _count_today_manual_orders(self) -> int:
        """统计今日手动下单次数"""
        try:
            conn = sqlite3.connect(self._get_db_path())
            today = datetime.now().strftime("%Y-%m-%d")
            count = conn.execute(
                "SELECT COUNT(*) FROM manual_order_audit WHERE timestamp LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    # ─────────────────────────────────────────────────────────────
    # 订单执行
    # ─────────────────────────────────────────────────────────────

    def submit_order(self, order: ManualOrder) -> Dict[str, Any]:
        """提交订单到 Futu broker"""

        # 1. 风控校验
        errors = self.validate_order(order)
        if errors:
            order.status = "rejected"
            order.error_msg = "; ".join(errors)
            self._write_audit(order)
            return {"success": False, "errors": errors, "order": asdict(order)}

        # 2. 连接 broker
        if not self.connect():
            order.status = "error"
            order.error_msg = "Broker 连接失败"
            self._write_audit(order)
            return {"success": False, "error": "broker_connection_failed"}

        # 3. 执行下单
        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, TimeInForce, RET_OK

            trd_env = TrdEnv.REAL if self._is_live() else TrdEnv.SIMULATE

            # 方向映射
            if order.action == "BUY":
                trd_side = TrdSide.BUY
            elif order.action == "SELL":
                trd_side = TrdSide.SELL
            else:
                # 从 direction 推断
                trd_side = TrdSide.BUY if order.direction == "LONG" else TrdSide.SELL

            # 订单类型
            if order.order_type == "LIMIT" and order.price:
                futu_order_type = FutuOrderType.NORMAL
                price = order.price
            else:
                futu_order_type = FutuOrderType.MARKET
                price = 0  # MARKET 单价格为 0

            # 获取账户 ID
            if self._is_live():
                acc_id = int(self._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(self._read_env("SIMULATE_ACCOUNT_ID", "0"))

            order.timestamp = datetime.now().isoformat()
            order.mode = self._get_mode_label()

            # 有效期映射
            tif = TimeInForce.GTC if order.time_in_force == "GTC" else TimeInForce.DAY

            logger.info(
                f"[手动下单] 提交: {order.symbol} {order.action} {order.quantity}股 "
                f"{'限价$'+str(price) if price else '市价'} "
                f"TIF={order.time_in_force} RTH外={'是' if order.fill_outside_rth else '否'} "
                f"环境={'实盘' if self._is_live() else '模拟'}"
            )

            ret, data = self._broker_ctx.place_order(
                price=price,
                qty=order.quantity,
                code=order.symbol,
                trd_side=trd_side,
                order_type=futu_order_type,
                trd_env=trd_env,
                acc_id=acc_id,
                time_in_force=tif,
                fill_outside_rth=order.fill_outside_rth,
            )

            if ret == RET_OK:
                order.order_id = str(data.iloc[0]["order_id"]) if not data.empty else ""
                order.status = "submitted"
                logger.info(f"[手动下单] ✅ 订单提交成功: order_id={order.order_id}")
                # 标记为手动持仓, 避免被系统退出监控误平
                self._mark_position_manual(order.symbol, order.direction)
                # PDT 记录移至平仓时 — 开仓不计入 day trade (P0 2026-06-11)
            else:
                order.status = "error"
                order.error_msg = str(data)
                logger.error(f"[手动下单] ❌ 订单提交失败: {data}")

        except Exception as e:
            order.status = "error"
            order.error_msg = str(e)
            logger.error(f"[手动下单] 下单异常: {e}")

        # 4. 写审计日志
        self._write_audit(order)

        return {
            "success": order.status == "submitted",
            "order": asdict(order),
        }

    def close_position(self, symbol: str, reason: str, close_qty: int = 0) -> Dict[str, Any]:
        """
        平仓指定持仓

        Args:
            symbol: 标的代码
            reason: 平仓理由
            close_qty: 平仓数量 (0=全平, >0=部分平仓)
        """
        if not self.connect():
            return {"success": False, "error": "broker_connection_failed"}

        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK

            trd_env = TrdEnv.REAL if self._is_live() else TrdEnv.SIMULATE
            if self._is_live():
                acc_id = int(self._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(self._read_env("SIMULATE_ACCOUNT_ID", "0"))

            # 查询当前持仓
            ret, positions = self._broker_ctx.position_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret != RET_OK:
                return {"success": False, "error": f"持仓查询失败: {positions}"}

            pos = positions[positions["code"] == symbol]
            if pos.empty:
                return {"success": False, "error": f"未找到 {symbol} 的持仓"}

            row = pos.iloc[0]
            raw_qty = float(row.get("qty", 0))
            if raw_qty == 0:
                return {"success": False, "error": f"{symbol} 持仓数量为 0"}

            # 根据持仓方向决定平仓方向
            side = str(row.get("position_side", "")).upper()
            is_long = not ((raw_qty < 0) or (side == "SHORT"))
            position_dir = "LONG" if is_long else "SHORT"
            total_qty = int(abs(raw_qty))
            close_side = TrdSide.SELL if is_long else TrdSide.BUY  # 平多卖, 平空买

            # 部分平仓 vs 全平
            if close_qty <= 0 or close_qty >= total_qty:
                qty = total_qty
                close_type = "全平"
            else:
                qty = close_qty
                close_type = f"部分({qty}/{total_qty})"

            logger.info(
                f"[手动平仓] {symbol}: {close_type} {position_dir}, "
                f"平仓动作={'SELL' if is_long else 'BUY'}, 数量={qty}"
            )

            # 构造审计订单
            order = ManualOrder(
                symbol=symbol,
                direction=position_dir,
                action="SELL" if is_long else "BUY",
                quantity=qty,
                order_type="MARKET",
                price=None,
                reason=f"[{close_type}] {reason}",
                mode=self._get_mode_label(),
            )

            ret, data = self._broker_ctx.place_order(
                price=0,
                qty=qty,
                code=symbol,
                trd_side=close_side,
                order_type=FutuOrderType.MARKET,
                trd_env=trd_env,
                acc_id=acc_id,
            )

            if ret == RET_OK:
                order.order_id = str(data.iloc[0]["order_id"]) if not data.empty else ""
                order.status = "submitted"
                logger.info(f"[手动平仓] ✅ {symbol} 平仓成功: {int(qty)}股")
                self._mark_position_manual(symbol, position_dir)
                # PDT 日内交易记录: 平仓时判断是否 round trip (P0 2026-06-11)
                try:
                    from app.risk.pdt_guard import get_pdt_guard
                    get_pdt_guard().record_close(symbol, row.get("open_date", row.get("entry_time")))
                except Exception as _pdt_err:
                    logger.debug(f"[手动平仓] PDT 记录失败 (不影响平仓): {_pdt_err}")

                # 2026-06-10: 写入 trade_records 形成手动交易闭环
                try:
                    self._record_manual_close(
                        symbol=symbol,
                        direction=position_dir,
                        entry_price=float(row.get("cost_price", 0)),
                        exit_price=self._calc_exit_price(row, raw_qty),  # TODO: 精确值需从 Futu 成交回报获取
                        quantity=int(qty),
                        reason=f"[{close_type}] {reason}",
                        order_id=order.order_id,
                    )
                except Exception as _rec_err:
                    logger.warning(f"[手动平仓] trade_records 写入失败 (不影响平仓): {_rec_err}")
            else:
                order.status = "error"
                order.error_msg = str(data)

            self._write_audit(order)
            return {
                "success": order.status == "submitted",
                "order": asdict(order),
            }

        except Exception as e:
            logger.error(f"[手动平仓] 异常: {e}")
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # 订单状态查询和撤单
    # ─────────────────────────────────────────────────────────────

    def get_today_orders(self) -> List[Dict[str, Any]]:
        """查询今日所有订单状态"""
        if not self.connect():
            return []

        try:
            from futu import TrdEnv, RET_OK, OrderStatus
            trd_env = TrdEnv.REAL if self._is_live() else TrdEnv.SIMULATE
            if self._is_live():
                acc_id = int(self._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(self._read_env("SIMULATE_ACCOUNT_ID", "0"))

            ret, data = self._broker_ctx.order_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret != RET_OK or data.empty:
                return []

            orders = []
            for _, row in data.iterrows():
                orders.append({
                    "order_id": str(row.get("order_id", "")),
                    "code": str(row.get("code", "")),
                    "qty": int(row.get("qty", 0)),
                    "price": float(row.get("price", 0)),
                    "order_type": str(row.get("order_type", "")),
                    "order_status": str(row.get("order_status", "")),
                    "trd_side": str(row.get("trd_side", "")),
                    "create_time": str(row.get("create_time", "")),
                    "update_time": str(row.get("update_time", "")),
                    "filled_qty": int(row.get("qty", 0)) if str(row.get("order_status", "")) in ("FILLED_ALL", "FILLED_PARTIAL") else 0,
                })
            return orders
        except Exception as e:
            logger.error(f"[订单查询] 异常: {e}")
            return []

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """撤销指定订单"""
        if not self.connect():
            return {"success": False, "error": "broker_connection_failed"}

        try:
            from futu import TrdEnv, RET_OK, ModifyOrderOp
            trd_env = TrdEnv.REAL if self._is_live() else TrdEnv.SIMULATE
            if self._is_live():
                acc_id = int(self._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(self._read_env("SIMULATE_ACCOUNT_ID", "0"))

            ret, data = self._broker_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=trd_env,
                acc_id=acc_id,
            )

            if ret == RET_OK:
                logger.info(f"[撤单] ✅ {order_id} 撤销成功")
                return {"success": True, "message": f"订单 {order_id} 已撤销"}
            else:
                logger.error(f"[撤单] ❌ {order_id} 撤销失败: {data}")
                return {"success": False, "error": str(data)}

        except Exception as e:
            logger.error(f"[撤单] 异常: {e}")
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # 审计日志
    # ─────────────────────────────────────────────────────────────

    def _ensure_audit_table(self):
        """确保审计表存在"""
        try:
            conn = sqlite3.connect(self._get_db_path())
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manual_order_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT,
                    action TEXT,
                    quantity INTEGER,
                    order_type TEXT,
                    price REAL,
                    reason TEXT,
                    mode TEXT,
                    order_id TEXT,
                    status TEXT,
                    error_msg TEXT,
                    time_in_force TEXT DEFAULT 'DAY',
                    fill_outside_rth INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manual_audit_time ON manual_order_audit(timestamp)"
            )
            # 兼容旧表: 尝试添加新列
            for col_sql in ["ALTER TABLE manual_order_audit ADD COLUMN time_in_force TEXT DEFAULT 'DAY'",
                            "ALTER TABLE manual_order_audit ADD COLUMN fill_outside_rth INTEGER DEFAULT 0"]:
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass  # 列已存在
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manual_audit_symbol ON manual_order_audit(symbol)"
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[审计] 创建审计表失败: {e}")

    @staticmethod
    def _calc_exit_price(row: dict, raw_qty: float) -> float:
        """从持仓行估算平仓价格 (近似值, 精确值需从 Futu 成交回报获取)"""
        if raw_qty == 0:
            return 0.0
        cur = float(row.get("cur_price", 0) or 0)
        if cur > 0:
            return cur
        val = float(row.get("val_price", 0) or 0)
        if val > 0:
            return val / abs(raw_qty)
        return 0.0

    def _record_manual_close(self, symbol: str, direction: str, entry_price: float,
                             exit_price: float, quantity: int, reason: str, order_id: str):
        """手动平仓后写入 trade_records, 形成交易闭环 (P&L 追踪)"""
        from datetime import datetime
        try:
            # 计算 P&L
            if direction == "LONG":
                pnl = (exit_price - entry_price) * quantity
            else:
                pnl = (entry_price - exit_price) * quantity
            pnl_pct = (pnl / (entry_price * quantity)) if entry_price > 0 and quantity > 0 else 0

            conn = sqlite3.connect(self._get_db_path())
            now = datetime.now()
            trade_date = now.strftime("%Y-%m-%d")

            conn.execute("""
                INSERT INTO trade_records (
                    trade_date, symbol, direction, entry_time, exit_time,
                    entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                    exit_reason, strategy_id, source_type, mode, trade_mode,
                    data_quality, created_at, close_futu_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_date, symbol, direction, now.isoformat(), now.isoformat(),
                entry_price, exit_price, quantity, round(pnl, 2), round(pnl_pct, 4),
                reason, "manual", "MANUAL",
                self._get_mode_label(),
                "live" if self._is_live() else "simulate",
                "manual", now.isoformat(), order_id,
            ))
            conn.commit()
            conn.close()
            logger.info(
                f"[手动交易闭环] {symbol} {direction} {quantity}股 "
                f"P&L=${pnl:.2f} ({pnl_pct*100:.2f}%) → trade_records"
            )
        except Exception as e:
            logger.warning(f"[手动交易闭环] 写入失败: {e}")

    def _mark_position_manual(self, symbol: str, direction: str = "LONG"):
        """标记持仓为手动管理，防止 ExitMonitor 误平
        
        Race condition fix (2026-06-11): web 下单时 open_positions 可能尚未创建
        (position_sync 30s 后才写入)。纯 UPDATE 会 0 rows affected → 标记丢失。
        改为 UPSERT: UPDATE + INSERT IF NOT EXISTS。
        """
        try:
            conn = sqlite3.connect(self._get_db_path())
            cursor = conn.execute(
                "UPDATE open_positions SET owner='manual' WHERE symbol=?",
                (symbol,),
            )
            if cursor.rowcount == 0:
                # Row doesn't exist yet — position_sync hasn't run.
                # Insert stub row; position_sync will merge/update on next cycle.
                from datetime import datetime
                conn.execute(
                    """INSERT INTO open_positions (symbol, direction, size, avg_entry_price, owner, sync_status, updated_at)
                       VALUES (?, ?, 0, 0, 'manual', 'pending_sync', ?)""",
                    (symbol, direction, datetime.now().isoformat()),
                )
                logger.info(f"[手动下单] {symbol} 新建 stub 行 owner=manual dir={direction} (等 position_sync 补全)")
            else:
                logger.info(f"[手动下单] {symbol} 已标记为 owner=manual (跳过系统退出监控)")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[手动下单] 标记 owner=manual 失败 ({symbol}): {e}")

    def _write_audit(self, order: ManualOrder):
        """写审计日志到 DB"""
        try:
            self._ensure_audit_table()
            conn = sqlite3.connect(self._get_db_path())
            conn.execute(
                """INSERT INTO manual_order_audit
                   (timestamp, symbol, direction, action, quantity, order_type,
                    price, reason, mode, order_id, status, error_msg,
                    time_in_force, fill_outside_rth)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.timestamp or datetime.now().isoformat(),
                    order.symbol, order.direction, order.action,
                    order.quantity, order.order_type, order.price,
                    order.reason, order.mode, order.order_id,
                    order.status, order.error_msg,
                    order.time_in_force, 1 if order.fill_outside_rth else 0,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[审计] 写入失败: {e}")

    def get_audit_history(self, limit: int = 50) -> List[Dict]:
        """获取审计历史"""
        try:
            self._ensure_audit_table()
            conn = sqlite3.connect(self._get_db_path())
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM manual_order_audit ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[审计] 查询失败: {e}")
            return []


# ─────────────────────────────────────────────────────────────────
# 单例
# ─────────────────────────────────────────────────────────────────

_instance: Optional[ManualOrderService] = None


def get_manual_order_service() -> ManualOrderService:
    global _instance
    if _instance is None:
        _instance = ManualOrderService()
    return _instance
