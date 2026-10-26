"""手动下单服务 — Web Dashboard 后端 (薄门面 / Facade).

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

架构 (Task #26 重构, 2026-06-15):
  本文件是 Facade — 公共方法签名保持不变, 内部逻辑委派到 ``order/`` 子模块:
    - app.web_dashboard.order.validator : 风控/参数校验
    - app.web_dashboard.order.executor  : Futu API 调用 (下单/平仓/撤单/止损)
    - app.web_dashboard.order.audit     : DB 写入 / WebSocket 推送
    - app.web_dashboard.order.thesis    : auto-thesis 入场/出场上下文
"""

import os
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger("ftseek_us.manual_order_service")


@dataclass
class ManualOrder:
    """手动订单数据结构."""
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
    stop_loss: float = 0.0          # 止损价 (成交后自动挂 STOP 单)
    take_profit: float = 0.0        # 止盈价 (成交后自动挂 LIMIT 单)
    acknowledge_price_warning: bool = False  # 前端确认价格偏离警告
    timestamp: str = ""
    order_id: str = ""
    status: str = "pending"  # pending, submitted, filled, rejected, error
    error_msg: str = ""


class ManualOrderService:
    """手动下单服务 (Facade).

    所有公共方法签名与原实现保持一致, 业务逻辑委派到 ``app.web_dashboard.order``
    下的子模块。共享状态 (broker 连接 / 锁 / 幂等表) 仍由本类持有, 子模块通过
    ``service`` 参数访问。
    """

    # ── 安全限制 ──────────────────────────────────────────────
    MAX_ORDER_VALUE_USD = 50_000       # 单笔最大金额 $50K
    # P0-2 修复 (2026-06-12): 平仓金额上限 (单笔), 防止误操作一次性平掉超大仓位
    MAX_CLOSE_VALUE_USD = 200_000      # 平仓单笔金额上限 $200K
    MAX_DAILY_MANUAL_ORDERS = 20       # 每日最大手动下单次数
    LIVE_CONFIRM_KEYWORD = "CONFIRM"   # 实盘确认关键词
    PRICE_DEVIATION_WARN_PCT = 5.0     # 限价偏离市价 ≥5% 警告
    PRICE_DEVIATION_HARD_PCT = 10.0    # 限价偏离市价 ≥10% 硬拦截

    def __init__(self):
        self._broker_ctx = None
        self._quote_ctx = None
        self._connected = False
        self._db_path = None
        self._last_health_check = 0  # 健康检查时间戳
        self._health_check_ttl = 10  # 健康检查间隔 (秒)
        # P0 2026-06-15: 并发保护 — 防止双击/网络重试导致重复下单
        self._order_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._recent_keys: Dict[str, float] = {}  # idempotency_key → timestamp
        self._idempotency_ttl = 60  # 幂等 key 有效期 (秒)

    # ── 基础工具 (DB / 环境 / 模式) ───────────────────────────

    def _db(self) -> sqlite3.Connection:
        """P0-4 修复 (2026-06-12): 统一安全 SQLite 连接 (timeout + WAL + busy_timeout)."""
        conn = sqlite3.connect(self._get_db_path(), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _get_db_path(self) -> str:
        """获取看板(手动实盘台)对应的 DB 路径 (RFC-078: 恒 live 库, 与 TRADE_ENV 解耦)."""
        if self._db_path is None:
            from app.core.db_utils import get_dashboard_db
            self._db_path = str(get_dashboard_db())
        return self._db_path

    def _is_live(self) -> bool:
        """手动交易固定走实盘 (三态共存 Phase 0, 2026-06-24).

        手动交易永远是实盘, 不再受 TRADE_ENV 控制 (TRADE_ENV 现仅控制 DB 路由).
        实盘账户 REAL_ACCOUNT_ID + 解锁密码.
        """
        return True

    def _read_env(self, key: str, default: str = "") -> str:
        """从 .env.us 读取环境变量 (无需重启进程)."""
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

    def _trd_env(self):
        """获取当前交易环境 (避免重复代码)."""
        from futu import TrdEnv
        return TrdEnv.REAL if self._is_live() else TrdEnv.SIMULATE

    def _acc_id(self) -> int:
        """获取当前账户 ID."""
        return int(self._read_env(
            "REAL_ACCOUNT_ID" if self._is_live() else "SIMULATE_ACCOUNT_ID", "0"
        ))

    # ── Broker 连接管理 ──────────────────────────────────────

    def connect(self) -> bool:
        """连接到 Futu broker.

        简化版: 仅检查 _connected 标志，不做 accinfo_query 健康探测。
        连接断了由调用方 catch 异常后 disconnect+reconnect。
        """
        if self._connected and self._broker_ctx:
            return True

        try:
            from futu import (
                OpenQuoteContext, OpenSecTradeContext,
                TrdMarket, RET_OK,
            )

            host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            port = int(os.getenv("FUTU_OPEND_PORT", "11111"))

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

            # 注册 OCO handler（事件驱动止损止盈配对取消）
            from app.web_dashboard.order.oco_handler import OCOHandler
            self._oco_handler = OCOHandler(self)
            self._broker_ctx.set_handler(self._oco_handler)

            self._connected = True
            self._last_health_check = time.time()
            self._ensure_lifecycle_columns()  # 幂等确保 lifecycle 列存在
            logger.info(f"[手动下单] 连接成功 ({'实盘' if self._is_live() else '模拟盘'})")
            return True

        except Exception as e:
            logger.error(f"[手动下单] 连接失败: {e}")
            return False

    def disconnect(self):
        """断开连接."""
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

    # ── 报价查询 ─────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """获取报价 (snapshot 稳定可靠, 盘中盘后均可用).

        P2 (2026-06-21): 扩展返回基本面+资金流字段, 一次snapshot调用替代多次API
        """
        if not self.connect():
            return {"symbol": symbol, "error": "broker_not_connected"}

        from app.utils.numeric import safe_float as _f

        try:
            from futu import RET_OK

            ret, snap = self._quote_ctx.get_market_snapshot([symbol])
            if ret == RET_OK and not snap.empty:
                row = snap.iloc[0]
                # 按时段选最合适的实时价:
                # cur_price → 盘中实时价 (休市时为 None)
                # last_price → 最后成交价 (含盘中/盘前盘后)
                # pre_price → 盘前最新价
                # after_price → 盘后最新价
                # overnight_price → 夜盘价 (富途24h盘)
                cur = _f(row.get("cur_price"))
                pre = _f(row.get("pre_price"))
                after = _f(row.get("after_price"))
                overnight = _f(row.get("overnight_price"))
                last = _f(row.get("last_price"))

                # cur_price 为空时, 优先用 last_price (盘中最新成交价)
                # 注意: overnight/after/pre 是各时段价格, 盘中时它们是旧值不应优先
                if cur <= 0:
                    if last > 0:
                        cur = last
                    elif overnight > 0:
                        cur = overnight
                    elif after > 0:
                        cur = after
                    elif pre > 0:
                        cur = pre
                    else:
                        cur = _f(row.get("prev_close_price"))

                return {
                    "symbol": symbol,
                    "cur_price": cur,
                    "high_price": _f(row.get("high_price")),
                    "low_price": _f(row.get("low_price")),
                    "prev_close": _f(row.get("prev_close_price")),
                    "volume": int(_f(row.get("volume"))),
                    "update_time": str(row.get("update_time") or ""),
                    # 盘前/盘后/夜盘实时价
                    "pre_price": pre or None,
                    "after_price": after or None,
                    "overnight_price": overnight or None,
                    "pre_volume": int(_f(row.get("pre_volume"))) or None,
                    "after_volume": int(_f(row.get("after_volume"))) or None,
                    "pre_high": _f(row.get("pre_high_price")) or None,
                    "pre_low": _f(row.get("pre_low_price")) or None,
                    "after_high": _f(row.get("after_high_price")) or None,
                    "after_low": _f(row.get("after_low_price")) or None,
                    # 买卖盘
                    "bid_price": _f(row.get("bid_price")) or None,
                    "ask_price": _f(row.get("ask_price")) or None,
                    "bid_vol": int(_f(row.get("bid_vol"))) or None,
                    "ask_vol": int(_f(row.get("ask_vol"))) or None,
                    # 基本面+资金流
                    "pe": _f(row.get("pe_ttm_ratio")) or _f(row.get("pe_ratio")) or None,
                    "pb": _f(row.get("pb_ratio")) or None,
                    "eps": _f(row.get("earning_per_share")) or None,
                    "market_cap": _f(row.get("total_market_val")) or None,
                    "bid_ask_ratio": _f(row.get("bid_ask_ratio")) or None,
                    "volume_ratio": _f(row.get("volume_ratio")) or None,
                    "high_52w": _f(row.get("highest52weeks_price")) or None,
                    "low_52w": _f(row.get("lowest52weeks_price")) or None,
                    "turnover_rate": _f(row.get("turnover_rate")) or None,
                }

            return {"symbol": symbol, "error": "no_data"}
        except Exception:
            return {"symbol": symbol, "error": "quote_fetch_failed"}

    def _batch_quotes(self, symbols: list) -> Dict[str, float]:
        """批量获取报价，返回 {symbol: price}."""
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

    # ─────────────────────────────────────────────────────────
    # 委派到 validator
    # ─────────────────────────────────────────────────────────

    def validate_order(self, order: ManualOrder) -> List[str]:
        """校验订单, 返回错误列表 (空列表 = 通过)."""
        from app.web_dashboard.order.validator import validate_order
        return validate_order(self, order)

    def _count_today_manual_orders(self) -> int:
        from app.web_dashboard.order.validator import count_today_manual_orders
        return count_today_manual_orders(self)

    # ─────────────────────────────────────────────────────────
    # 委派到 executor
    # ─────────────────────────────────────────────────────────

    def submit_order(self, order: ManualOrder, idempotency_key: str = None) -> Dict[str, Any]:
        """提交订单到 Futu broker (带锁 + 幂等保护)."""
        from app.web_dashboard.order.executor import submit_order
        return submit_order(self, order, idempotency_key)

    def close_position(self, symbol: str, reason: str,
                       close_qty: int = 0, price: float = None,
                       fill_outside_rth: bool = False) -> Dict[str, Any]:
        """平仓指定持仓 (close_qty=0 全平, 否则部分平仓)."""
        from app.web_dashboard.order.executor import close_position
        return close_position(self, symbol, reason, close_qty, price, fill_outside_rth)

    def get_today_orders(self) -> List[Dict[str, Any]]:
        """查询今日所有订单状态 (按时间倒序)."""
        from app.web_dashboard.order.executor import get_today_orders
        return get_today_orders(self)

    def get_today_orders_with_status(self) -> Dict[str, Any]:
        """查询今日订单 + 来源/券商查询状态 (runtime 契约)."""
        from app.web_dashboard.order.executor import get_today_orders_with_status
        return get_today_orders_with_status(self)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """撤销指定订单."""
        from app.web_dashboard.order.executor import cancel_order
        return cancel_order(self, order_id)

    def takeover_position(self, symbol, owner: str = "manual"):
        """P1-2 (2026-06-12): import broker LEGACY position into open_positions."""
        from app.web_dashboard.order.executor import takeover_position
        return takeover_position(self, symbol, owner)

    def modify_stop_loss(self, symbol: str, new_sl: float) -> Dict[str, Any]:
        """修改持仓止损 — 撤销旧 STOP 单 + 下新 STOP 单 + 更新 DB."""
        from app.web_dashboard.order.executor import modify_stop_loss
        return modify_stop_loss(self, symbol, new_sl)

    def _check_idempotency(self, key: Optional[str]) -> bool:
        from app.web_dashboard.order.executor import check_idempotency
        return check_idempotency(self, key)

    def _cancel_broker_order(self, order_id: str) -> bool:
        from app.web_dashboard.order.executor import cancel_broker_order
        return cancel_broker_order(self, order_id)

    def _fetch_dealt_avg_price(self, order_id: str, trd_env, acc_id: int,
                               retries: int = 2, delay: float = 0.4):
        from app.web_dashboard.order.executor import fetch_dealt_avg_price
        return fetch_dealt_avg_price(self, order_id, trd_env, acc_id, retries, delay)

    @staticmethod
    def _calc_exit_price(row: dict, raw_qty: float) -> float:
        from app.web_dashboard.order.executor import calc_exit_price
        return calc_exit_price(row, raw_qty)

    def _place_protective_stop(self, order: ManualOrder):
        from app.web_dashboard.order.executor import place_protective_stop
        return place_protective_stop(self, order)

    # ─────────────────────────────────────────────────────────
    # 委派到 audit
    # ─────────────────────────────────────────────────────────

    def get_audit_history(self, limit: int = 50) -> List[Dict]:
        """获取审计历史."""
        from app.web_dashboard.order.audit import get_audit_history
        return get_audit_history(self, limit)

    def _ensure_audit_table(self):
        from app.web_dashboard.order.audit import ensure_audit_table
        return ensure_audit_table(self)

    def _ensure_sl_column(self):
        from app.web_dashboard.order.audit import ensure_sl_column
        return ensure_sl_column(self)

    def _ensure_lifecycle_columns(self):
        from app.web_dashboard.order.audit import ensure_lifecycle_columns
        return ensure_lifecycle_columns(self)

    def _write_audit(self, order: ManualOrder):
        from app.web_dashboard.order.audit import write_audit
        return write_audit(self, order)

    def _record_manual_open(self, order: "ManualOrder"):
        from app.web_dashboard.order.audit import record_manual_open
        return record_manual_open(self, order)

    def _record_manual_close(self, symbol: str, direction: str, entry_price: float,
                             exit_price: float, quantity: int, reason: str, order_id: str,
                             entry_reason: str = ""):
        """手动平仓 → 写 trade_records 形成交易闭环。

        2026-09-08 (REVIEW-20260908 §H) 修复**委派签名漂移**: `order/executor.py:711` 自
        2026-06-19 (768e443f) 起就传 `entry_reason=`, 而本委派一直没这个形参 ⇒ 每次手动平仓
        都在此处抛 TypeError, 被 executor.py:784 的 `except Exception` 降级成一行 warning
        ("trade_records 写入失败 (不影响平仓)") ⇒ **券商侧已平仓、账本侧零记录**的静默断链
        (实证: trade_records 中 `exit_reason LIKE '[%'` 的手动平仓行 = 0, 最后一笔手动平仓
        留痕停在 2026-06-09 = 该 commit 之前)。下游 `record_manual_close` 早就收这个参数,
        只是委派层没跟着改 —— 典型的"重构改了实现忘了改门面"。
        """
        from app.web_dashboard.order.audit import record_manual_close
        return record_manual_close(
            self, symbol, direction, entry_price, exit_price,
            quantity, reason, order_id, entry_reason=entry_reason,
        )

    def _mark_position_manual(self, symbol: str, direction: str = "LONG"):
        from app.web_dashboard.order.audit import mark_position_manual
        return mark_position_manual(self, symbol, direction)

    def _push_attribution_to_feishu(self, attr) -> bool:
        from app.web_dashboard.order.audit import push_attribution_to_feishu
        return push_attribution_to_feishu(self, attr)

    # ─────────────────────────────────────────────────────────
    # 委派到 thesis
    # ─────────────────────────────────────────────────────────

    def _auto_generate_thesis(self, order: ManualOrder) -> dict:
        from app.web_dashboard.order.thesis import auto_generate_thesis
        return auto_generate_thesis(self, order)

    def _save_thesis_to_db(self, order: ManualOrder, thesis_ctx: dict):
        from app.web_dashboard.order.thesis import save_thesis_to_db
        return save_thesis_to_db(self, order, thesis_ctx)

    def _build_exit_context(self, symbol: str, entry_time_str: str, thesis_ctx: dict) -> dict:
        from app.web_dashboard.order.thesis import build_exit_context
        return build_exit_context(self, symbol, entry_time_str, thesis_ctx)


# ─────────────────────────────────────────────────────────────────
# 单例
# ─────────────────────────────────────────────────────────────────

_instance: Optional[ManualOrderService] = None
# 2026-10-18 空跑/安全修复: FundamentalProvider 在 5 线程筛选池里并发调用本单例,
# 原裸 if 判断可同时建出多个实例, 每个实例 connect() 都会解锁实盘 (实测盘前生成
# 一次出现 5 次 "[手动下单] 实盘已解锁")。双检锁保证全进程只建一个连接。
_instance_lock = threading.Lock()


def get_manual_order_service() -> ManualOrderService:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ManualOrderService()
    return _instance
