"""港股手动交易直通服务 (stateless pass-through).

设计原则 (2026-07-22, 与美股 manual_order_service 物理隔离):
  - 无状态直通: 下单/撤单/持仓/订单查询全部实时透传富途 API,
    不写 ftseek_us.db 任何表 (open_positions / trade_records / 审计表全部不碰),
    成交与持仓管理以富途 App 为准。
  - 物理隔离: 独立 OpenSecTradeContext(TrdMarket.HK) + 独立 OpenQuoteContext,
    与美股 ManualOrderService 不继承、不共享连接。
  - 能力跟随富途: 仅暴露 BUY/SELL (TrdSide 直传), 做空可行性由券商侧
    (保证金账户 + 可空名单) 裁决, 富途错误文案原样透传。
  - 审计不落库但留痕: data/audit/hk_manual_orders.jsonl (纯文件),
    兼作当日下单次数统计依据。

安全设计 (沿用美股手动交易语义, 独立实现):
  - 单笔金额上限 MAX_ORDER_VALUE_HKD
  - 数量必须为 lot_size 整数倍 (港股按手交易)
  - 限价偏离现价 >=5% 警告 / >=10% 硬拦截
  - 每日手动下单次数上限
  - 下单理由与 CONFIRM 确认已去除 (2026-07-22 用户要求)
  - 线程锁 + 幂等 key (防双击/网络重试重复下单)
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek_us.hk_order_service")

_ROOT = Path(__file__).parent.parent.parent
_AUDIT_PATH = _ROOT / "data" / "audit" / "hk_manual_orders.jsonl"


@dataclass
class HKOrder:
    """港股手动订单 (直通, 不落库)."""
    symbol: str                       # "HK.00700"
    action: str                       # "BUY" or "SELL"
    quantity: int                     # 股数 (须为 lot_size 整数倍)
    order_type: str                   # "MARKET" or "LIMIT"
    price: Optional[float]            # None for MARKET
    reason: str = ""
    acknowledge_price_warning: bool = False
    timestamp: str = ""
    order_id: str = ""
    status: str = "pending"           # pending, submitted, rejected, error
    error_msg: str = ""
    warnings: List[str] = field(default_factory=list)


class HKOrderService:
    """港股直通下单服务 (单例, 与美股服务平行)."""

    MAX_ORDER_VALUE_HKD = 400_000     # 单笔最大金额 (约等于美股 $50K)
    MAX_DAILY_MANUAL_ORDERS = 20      # 每日最大手动下单次数
    LIVE_CONFIRM_KEYWORD = "CONFIRM"  # 保留常量 (2026-07-22 路由层已不再校验)
    PRICE_DEVIATION_WARN_PCT = 5.0    # 限价偏离市价 >=5% 警告
    PRICE_DEVIATION_HARD_PCT = 10.0   # 限价偏离市价 >=10% 硬拦截

    def __init__(self):
        self._broker_ctx = None
        self._quote_ctx = None
        self._connected = False
        self._order_lock = threading.Lock()
        self._recent_keys: Dict[str, float] = {}  # idempotency_key -> ts
        self._idempotency_ttl = 60

    # ── 环境/账户 ────────────────────────────────────────────

    def _read_env(self, key: str, default: str = "") -> str:
        """从 .env.us 读取环境变量 (与美股服务同法, 独立实现)."""
        env_file = _ROOT / ".env.us"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(f"{key}="):
                        return stripped.split("=", 1)[1].strip()
        return os.getenv(key, default)

    def _acc_id(self) -> int:
        """港股账户 ID: HK_REAL_ACCOUNT_ID 优先 (港美分户场景), 回落综合账户."""
        raw = self._read_env("HK_REAL_ACCOUNT_ID", "") or self._read_env("REAL_ACCOUNT_ID", "0")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # ── Broker 连接 ──────────────────────────────────────────

    def connect(self) -> bool:
        if self._connected and self._broker_ctx:
            return True
        try:
            from futu import (
                OpenQuoteContext, OpenSecTradeContext, TrdMarket, RET_OK,
            )
            host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            port = int(os.getenv("FUTU_OPEND_PORT", "11111"))

            self._quote_ctx = OpenQuoteContext(host=host, port=port)
            self._broker_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.HK,
                host=host, port=port, is_encrypt=False,
            )

            # 手动交易固定实盘 (与美股手动交易现状一致), 需解锁
            pwd_md5 = self._read_env("FUTU_UNLOCK_PWD_MD5", "")
            if not pwd_md5:
                logger.error("[港股直通] FUTU_UNLOCK_PWD_MD5 未设置")
                self.disconnect()
                return False
            ret, data = self._broker_ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)
            if ret != RET_OK:
                logger.error(f"[港股直通] 实盘解锁失败: {data}")
                self.disconnect()
                return False

            self._connected = True
            logger.info("[港股直通] 连接成功 (实盘, TrdMarket.HK)")
            return True
        except Exception as e:
            logger.error(f"[港股直通] 连接失败: {e}")
            self.disconnect()
            return False

    def disconnect(self):
        for ctx in (self._quote_ctx, self._broker_ctx):
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass
        self._quote_ctx = None
        self._broker_ctx = None
        self._connected = False

    # ── 报价 (含 lot_size) ───────────────────────────────────

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """快照报价: 现价/买卖盘/涨跌 + lot_size (港股按手交易关键字段)."""
        if not self.connect():
            return {"symbol": symbol, "error": "broker_not_connected"}
        from app.utils.numeric import safe_float as _f
        try:
            from futu import RET_OK
            ret, snap = self._quote_ctx.get_market_snapshot([symbol])
            if ret != RET_OK or snap.empty:
                return {"symbol": symbol, "error": "no_data"}
            row = snap.iloc[0]
            cur = _f(row.get("cur_price")) or _f(row.get("last_price")) \
                or _f(row.get("prev_close_price"))
            prev_close = _f(row.get("prev_close_price"))
            return {
                "symbol": symbol,
                "name": str(row.get("name") or row.get("stock_name") or ""),
                "currency": "HKD",
                "cur_price": cur,
                "prev_close": prev_close,
                "change_pct": round((cur - prev_close) / prev_close * 100, 2)
                              if prev_close > 0 else None,
                "high_price": _f(row.get("high_price")),
                "low_price": _f(row.get("low_price")),
                "volume": int(_f(row.get("volume"))),
                "lot_size": int(_f(row.get("lot_size"))) or None,
                "bid_price": _f(row.get("bid_price")) or None,
                "ask_price": _f(row.get("ask_price")) or None,
                "bid_vol": int(_f(row.get("bid_vol"))) or None,
                "ask_vol": int(_f(row.get("ask_vol"))) or None,
                "update_time": str(row.get("update_time") or ""),
            }
        except Exception as e:
            logger.error(f"[港股直通] 报价失败 {symbol}: {e}")
            return {"symbol": symbol, "error": "quote_fetch_failed"}

    # ── 校验 (无 DB 依赖) ────────────────────────────────────

    @staticmethod
    def normalize_symbol(raw: str) -> str:
        """'700'/'0700'/'HK.00700' -> 'HK.00700' (5 位数字补零)."""
        s = str(raw or "").strip().upper()
        if s.startswith("HK."):
            s = s[3:]
        if s.isdigit() and 1 <= len(s) <= 5:
            return f"HK.{s.zfill(5)}"
        return f"HK.{s}"  # 交给后续校验拒绝

    def validate(self, order: HKOrder) -> List[str]:
        """返回错误列表 (空 = 通过); 软警告写入 order.warnings."""
        errors: List[str] = []
        sym = order.symbol
        if not (sym.startswith("HK.") and sym[3:].isdigit() and len(sym) == 8):
            errors.append(f"标的格式无效: {sym} (需 HK.+5位数字, 如 HK.00700)")
        if order.action not in ("BUY", "SELL"):
            errors.append(f"方向无效: {order.action}")
        if order.quantity <= 0:
            errors.append("数量必须大于 0")
        if order.order_type not in ("MARKET", "LIMIT"):
            errors.append(f"订单类型无效: {order.order_type}")
        if order.order_type == "LIMIT" and (order.price is None or order.price <= 0):
            errors.append("限价单必须指定有效价格")
        # 下单理由为可选 (2026-07-22 用户要求去掉必填)
        if errors:
            return errors

        # 当日次数上限 (jsonl 统计, 非 DB)
        if self._count_today_orders() >= self.MAX_DAILY_MANUAL_ORDERS:
            errors.append(f"今日手动下单已达上限 {self.MAX_DAILY_MANUAL_ORDERS} 次")
            return errors

        quote = self.get_quote(sym)
        if quote.get("error"):
            # 报价降级不阻塞限价单 (直通原则: 限价可盲下, 富途端最终裁决)
            if order.order_type == "MARKET":
                errors.append(f"无法获取报价 ({quote['error']}), 市价单已拦截")
                return errors
            order.warnings.append(f"报价不可用 ({quote['error']}), 跳过整手/金额/偏离校验")
            return errors

        # 整手校验
        lot = quote.get("lot_size") or 0
        if lot > 0 and order.quantity % lot != 0:
            errors.append(f"数量 {order.quantity} 不是每手股数 {lot} 的整数倍")

        # 金额上限
        cur = quote.get("cur_price") or 0
        ref_price = order.price if (order.order_type == "LIMIT" and order.price) else cur
        if ref_price > 0:
            value = ref_price * order.quantity
            if value > self.MAX_ORDER_VALUE_HKD:
                errors.append(
                    f"订单金额 HK${value:,.0f} 超过上限 HK${self.MAX_ORDER_VALUE_HKD:,.0f}"
                )

        # 限价偏离
        if order.order_type == "LIMIT" and order.price and cur > 0:
            dev_pct = abs(order.price - cur) / cur * 100
            if dev_pct >= self.PRICE_DEVIATION_HARD_PCT:
                errors.append(
                    f"限价 {order.price} 偏离现价 {cur} 达 {dev_pct:.1f}% (>=10% 硬拦截)"
                )
            elif dev_pct >= self.PRICE_DEVIATION_WARN_PCT and not order.acknowledge_price_warning:
                errors.append(
                    f"PRICE_WARNING:限价偏离现价 {dev_pct:.1f}% (>=5%), "
                    f"确认无误请勾选偏离确认后重新提交"
                )
        return errors

    # ── 下单 (直通) ──────────────────────────────────────────

    def submit_order(self, order: HKOrder,
                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        with self._order_lock:
            # 幂等: 60s 内相同 key 拒绝 (防双击/重试)
            now = time.time()
            self._recent_keys = {k: t for k, t in self._recent_keys.items()
                                 if now - t < self._idempotency_ttl}
            if idempotency_key:
                if idempotency_key in self._recent_keys:
                    return {"success": False, "error": "重复提交 (幂等拦截, 60秒内相同请求)"}
                self._recent_keys[idempotency_key] = now

            errors = self.validate(order)
            if errors:
                order.status = "rejected"
                order.error_msg = "; ".join(errors)
                self._append_audit("submit", order)
                return {"success": False, "error": order.error_msg,
                        "warnings": order.warnings}

            if not self.connect():
                return {"success": False, "error": "broker 未连接"}

            try:
                from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK
                trd_side = TrdSide.BUY if order.action == "BUY" else TrdSide.SELL
                is_market = (order.order_type == "MARKET")
                futu_type = FutuOrderType.MARKET if is_market else FutuOrderType.NORMAL
                # 市价单 price 传 0 占位 (富途 API 要求), 限价单传用户价
                px = 0.0 if is_market else float(order.price)

                ret, data = self._broker_ctx.place_order(
                    price=px,
                    qty=order.quantity,
                    code=order.symbol,
                    trd_side=trd_side,
                    order_type=futu_type,
                    trd_env=TrdEnv.REAL,
                    acc_id=self._acc_id(),
                )
                order.timestamp = datetime.now().isoformat()
                if ret == RET_OK:
                    order.order_id = str(data["order_id"][0])
                    order.status = "submitted"
                    self._append_audit("submit", order)
                    logger.info(f"[港股直通] 已提交 {order.action} {order.symbol} "
                                f"x{order.quantity} @{order.price or 'MKT'} "
                                f"order_id={order.order_id}")
                    return {"success": True, "order_id": order.order_id,
                            "status": order.status, "warnings": order.warnings,
                            "note": "直通下单已提交, 成交与持仓请以富途 App 为准"}
                # 富途拒单: 错误文案原样透传
                order.status = "error"
                order.error_msg = str(data)
                self._append_audit("submit", order)
                return {"success": False, "error": f"富途拒单: {data}"}
            except Exception as e:
                self._connected = False  # 连接可能已断, 下次重连
                order.status = "error"
                order.error_msg = str(e)
                self._append_audit("submit", order)
                logger.error(f"[港股直通] 下单异常: {e}")
                return {"success": False, "error": f"下单异常: {e}"}

    # ── 撤单 ─────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        if not self.connect():
            return {"success": False, "error": "broker 未连接"}
        try:
            from futu import TrdEnv, ModifyOrderOp, RET_OK
            ret, data = self._broker_ctx.modify_order(
                ModifyOrderOp.CANCEL, order_id, 0, 0,
                trd_env=TrdEnv.REAL, acc_id=self._acc_id(),
            )
            rec = HKOrder(symbol="", action="CANCEL", quantity=0,
                          order_type="", price=None, order_id=order_id,
                          timestamp=datetime.now().isoformat())
            if ret == RET_OK:
                rec.status = "cancelled"
                self._append_audit("cancel", rec)
                return {"success": True, "order_id": order_id}
            rec.status = "error"
            rec.error_msg = str(data)
            self._append_audit("cancel", rec)
            return {"success": False, "error": f"撤单失败: {data}"}
        except Exception as e:
            self._connected = False
            return {"success": False, "error": f"撤单异常: {e}"}

    # ── 查询 (实时透传, 不缓存) ──────────────────────────────

    def get_positions(self) -> List[Dict[str, Any]]:
        """港股持仓 (只读展示, 数据源=富途实时)."""
        if not self.connect():
            return []
        from app.utils.numeric import safe_float as _f
        try:
            from futu import TrdEnv, RET_OK
            ret, data = self._broker_ctx.position_list_query(
                trd_env=TrdEnv.REAL, acc_id=self._acc_id(),
            )
            if ret != RET_OK:
                logger.warning(f"[港股直通] 持仓查询失败: {data}")
                return []
            out = []
            for _, row in data.iterrows():
                code = str(row.get("code") or "")
                # 综合账户返回全市场持仓, 此页只展示港股
                if not code.startswith("HK."):
                    continue
                qty = _f(row.get("qty"))
                if qty == 0:
                    continue
                out.append({
                    "symbol": code,
                    "name": str(row.get("stock_name") or ""),
                    "qty": qty,
                    "can_sell_qty": _f(row.get("can_sell_qty")),
                    "cost_price": _f(row.get("cost_price")),
                    "cur_price": _f(row.get("nominal_price")),
                    "market_val": _f(row.get("market_val")),
                    "pl_val": _f(row.get("pl_val")),
                    # 富途 pl_ratio 已是百分比值 (如 -4.9 表示 -4.9%)
                    "pl_ratio": round(_f(row.get("pl_ratio")), 2),
                    "position_side": str(row.get("position_side") or ""),
                })
            return out
        except Exception as e:
            self._connected = False
            logger.error(f"[港股直通] 持仓查询异常: {e}")
            return []

    def get_today_orders(self) -> List[Dict[str, Any]]:
        """今日订单 (实时查 broker, 按时间倒序)."""
        if not self.connect():
            return []
        from app.utils.numeric import safe_float as _f
        try:
            from futu import TrdEnv, RET_OK
            ret, data = self._broker_ctx.order_list_query(
                trd_env=TrdEnv.REAL, acc_id=self._acc_id(),
            )
            if ret != RET_OK:
                logger.warning(f"[港股直通] 订单查询失败: {data}")
                return []
            out = []
            for _, row in data.iterrows():
                code = str(row.get("code") or "")
                if not code.startswith("HK."):
                    continue  # 只展示港股订单
                out.append({
                    "order_id": str(row.get("order_id") or ""),
                    "symbol": code,
                    "name": str(row.get("stock_name") or ""),
                    "trd_side": str(row.get("trd_side") or ""),
                    "order_type": str(row.get("order_type") or ""),
                    "status": str(row.get("order_status") or ""),
                    "qty": _f(row.get("qty")),
                    "dealt_qty": _f(row.get("dealt_qty")),
                    "price": _f(row.get("price")),
                    "dealt_avg_price": _f(row.get("dealt_avg_price")),
                    "create_time": str(row.get("create_time") or ""),
                })
            out.sort(key=lambda x: x["create_time"], reverse=True)
            return out
        except Exception as e:
            self._connected = False
            logger.error(f"[港股直通] 订单查询异常: {e}")
            return []

    # ── 审计 (jsonl, 不落 DB) ────────────────────────────────

    def _append_audit(self, op: str, order: HKOrder) -> None:
        try:
            _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": order.timestamp or datetime.now().isoformat(),
                "op": op,
                "symbol": order.symbol,
                "action": order.action,
                "quantity": order.quantity,
                "order_type": order.order_type,
                "price": order.price,
                "reason": order.reason,
                "status": order.status,
                "order_id": order.order_id,
                "error": order.error_msg or None,
            }
            with open(_AUDIT_PATH, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[港股直通] 审计写入失败 (不影响下单): {e}")

    def _count_today_orders(self) -> int:
        """当日已提交订单数 (从 jsonl 统计成功提交的 submit 记录)."""
        if not _AUDIT_PATH.exists():
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        n = 0
        try:
            with open(_AUDIT_PATH) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if (rec.get("op") == "submit"
                            and rec.get("status") == "submitted"
                            and str(rec.get("ts", "")).startswith(today)):
                        n += 1
        except Exception:
            return 0
        return n


# ── 单例 ─────────────────────────────────────────────────────

_instance: Optional[HKOrderService] = None


def get_hk_order_service() -> HKOrderService:
    global _instance
    if _instance is None:
        _instance = HKOrderService()
    return _instance
