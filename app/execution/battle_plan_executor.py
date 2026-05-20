"""盘前计划执行器 (Battle Plan Executor) - 智能竞价 + 整手对齐

职责:
1. 09:00-09:15 HKT: 提交竞价限价单 (At-Auction Limit Order)。
2. 09:30 HKT: 提交开盘市价单/限价单。

P1 修复 (2026-04-19):
- 接入 AuctionManager 智能定价 (替代固定 1% 溢价)
- 港股整手倍数对齐 (防止碎股下单失败)
"""
import json
import logging
import os
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

# 富途 API
try:
    from futu import (
        OpenSecTradeContext, TrdEnv, OrderType, TrdSide,
        TrdMarket, ModifyOrderOp, OrderStatus
    )
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False

# FTSeek 模块
from app.strategy.base import Signal, SignalAction, TradeTier
from app.trader.executor import TradeExecutor, get_executor
from app.trader.position_manager import PositionManager
from app.notifier import get_dingtalk_notifier
from app import config
from app.execution.lot_size_manager import get_lot_size_manager
from app.core.logging_config import get_trace_id, set_trace_id
import uuid

BATTLE_PLAN_PATH = "/opt/ftseek/FTSeek/data/daily_battle_plan.json"
logger = logging.getLogger("ftseek.battle_executor")


class BattlePlanExecutionService:
    """盘前计划执行服务"""

    def __init__(self):
        self.executor = get_executor()
        self.position_mgr = PositionManager(self.executor.position_manager.db_path) if self.executor else None
        self.notifier = get_dingtalk_notifier()
        self._last_execution_date = None
        self._auction_order_ids = {} # 记录竞价单 ID，用于状态追踪

    def run_daily_check(self):
        """
        每分钟调用一次。
        根据当前时间决定是执行竞价盘 (09:00-09:15) 还是开盘盘 (09:30+)。
        """
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        # 防止同一天重复执行
        if self._last_execution_date == today_str:
            return

        # ── 阶段 1: 竞价盘窗口 (09:00 - 09:12) ──
        # 留 3 分钟余量 (09:12)，因为 09:15 后不能撤单
        if now.hour == 9 and 0 <= now.minute <= 12:
            logger.info("🚀 [BattlePlan] 进入竞价盘窗口，开始执行...")
            self.execute_auction_orders(today_str)
            self._last_execution_date = today_str
            return

        # ── 阶段 2: 开盘盘窗口 (09:30 - 09:35) ──
        # 检查是否有竞价单未成交，如有则转为市价单
        if now.hour == 9 and 30 <= now.minute <= 35:
            logger.info("🚀 [BattlePlan] 进入开盘盘窗口，开始执行...")
            self.execute_open_market_orders(today_str)
            self._last_execution_date = today_str
            return

    def execute_auction_orders(self, date_str: str):
        """执行竞价限价单

        P1 修复 (2026-04-19): 接入 AuctionManager 智能定价，替代固定 1% 溢价。
        根据 ADR 表现动态计算竞价限价，并自动对齐整手倍数。
        """
        plan = self._load_and_validate_plan(date_str)
        if not plan: return

        targets = plan.get("target_positions", [])
        if not targets: return

        # 获取昨日收盘价 + ADR 数据 (用于 AuctionManager 智能定价)
        yesterday_close_map = self._get_yesterday_close_prices([t['symbol'] for t in targets])

        # 初始化 AuctionManager (P1 修复)
        try:
            from app.pipeline.auction_manager import AuctionManager
            auction_mgr = AuctionManager()
        except Exception as e:
            logger.warning(f"[BattlePlan] AuctionManager 初始化失败，使用备用定价: {e}")
            auction_mgr = None

        executed_count = 0
        for target in targets:
            symbol = target.get("symbol")
            direction = target.get("direction", "BUY")

            # 仅在做多时使用竞价限价单 (做空通常不建议竞价)
            if direction != "BUY":
                continue

            # 获取持仓，如果已有则跳过
            if self.position_mgr and self.position_mgr.get_position(symbol):
                logger.info(f"[BattlePlan] {symbol} 已有持仓，跳过竞价。")
                continue

            # P1 修复: 智能定价 (AuctionManager) vs 备用定价
            ref_price = yesterday_close_map.get(symbol)
            if not ref_price:
                logger.warning(f"[BattlePlan] 无法获取 {symbol} 昨日收盘价，跳过。")
                continue

            if auction_mgr:
                # 使用 ADR 传导 + 安全垫的智能定价
                adr_pct = target.get("adr_change_pct", 0.0)
                plan_dir = "LONG" if direction == "BUY" else "SHORT"
                result = auction_mgr.calculate_auction_price(
                    symbol, ref_price, adr_pct, plan_dir
                )
                if result.status == "INVALID":
                    logger.warning(f"[BattlePlan] {symbol} 竞价计划失效: {result.invalidation_reason}")
                    continue
                limit_price = result.limit_price
                if limit_price <= 0:
                    logger.warning(f"[BattlePlan] {symbol} AuctionManager 返回价格无效，使用备用定价")
                    limit_price = round(ref_price * 1.01, 3)
                else:
                    logger.info(f"[BattlePlan] {symbol} 智能竞价: ¥{limit_price} (昨收 ¥{ref_price}, ADR {adr_pct:+.1%})")
            else:
                # 备用: 固定 1% 溢价
                limit_price = round(ref_price * 1.01, 3)

            # P0-1 修复: 统一从 LotSizeManager 获取整手股数
            lot_mgr = get_lot_size_manager()
            qty = lot_mgr.enforce_lot_size(symbol, target.get("quantity", 100))

            # 提交竞价限价单
            order_id = self._place_futu_order(
                symbol=symbol,
                price=limit_price,
                qty=qty,
                side="BUY",
                order_type=OrderType.AT_AUCTION_LIMIT,
                remark="盘前竞价单(智能定价)"
            )

            if order_id:
                self._auction_order_ids[symbol] = {
                    "id": order_id,
                    "price": limit_price,
                    "qty": qty
                }
                executed_count += 1

        if self.notifier and executed_count > 0:
            self.notifier.send_text(
                f"[FTSeek] ✅ 盘前竞价单已提交\n"
                f"共 {executed_count} 笔 (智能定价 + 整手对齐)。\n"
                "注：竞价限价单将以开盘撮合价成交，不会因溢价而吃亏。"
            )

    def execute_open_market_orders(self, date_str: str):
        """
        开盘执行：
        1. 检查竞价单状态。如果未成交，撤单并改投市价单。
        2. 如果竞价单已成交，跳过。
        """
        plan = self._load_and_validate_plan(date_str)
        if not plan: return

        targets = plan.get("target_positions", [])
        
        for target in targets:
            symbol = target.get("symbol")
            
            # 1. 检查竞价单
            if symbol in self._auction_order_ids:
                info = self._auction_order_ids[symbol]
                status = self._get_order_status(info["id"])
                
                if status in [OrderStatus.SUBMITTED, OrderStatus.WAITING]:
                    # 竞价单未成交 -> 撤单
                    self._cancel_order(info["id"])
                    logger.info(f"[BattlePlan] 竞价单 {symbol} 未成交，已撤销，转市价单。")
                elif status == OrderStatus.FILLED:
                    logger.info(f"[BattlePlan] 竞价单 {symbol} 已成交，无需开盘操作。")
                    continue
                else:
                    logger.warning(f"[BattlePlan] 竞价单 {symbol} 状态异常: {status}")

            # 2. 执行市价单 (Fallback)
            # 如果竞价单没成功，或者原本就没走竞价单（如 SELL 方向）
            if not self.position_mgr.get_position(symbol):
                self._execute_target_via_signal(target, "市价单")

    def _place_futu_order(self, symbol, price, qty, side, order_type, remark=""):
        """直接调用富途 API 下单"""
        if not FUTU_AVAILABLE:
            logger.error("富途 API 不可用，无法下单")
            return None

        acc_id = config.FUTU_ACCOUNT_ID if hasattr(config, 'FUTU_ACCOUNT_ID') else None
        trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

        try:
            ctx = OpenSecTradeContext(host=config.OPEND_HOST, port=config.OPEND_PORT, security_firm=symbol.split('.')[0])
            
            ret, data = ctx.place_order(
                price=price,
                qty=qty,
                code=symbol,
                trd_side=TrdSide.BUY if side == "BUY" else TrdSide.SELL,
                order_type=order_type,
                trd_env=trd_env,
                acc_id=acc_id,
                remark=remark
            )
            ctx.close()

            if ret == 0:
                order_id = data['order_id'].iloc[0]
                logger.info(f"[BattlePlan] ✅ 竞价单提交成功: {symbol} ID={order_id}")
                return order_id
            else:
                logger.error(f"[BattlePlan] ❌ 竞价单提交失败: {symbol} - {data}")
                return None
        except Exception as e:
            logger.error(f"[BattlePlan] 下单异常: {e}")
            return None

    def _get_order_status(self, order_id):
        """查询订单状态"""
        if not FUTU_AVAILABLE: return None
        try:
            ctx = OpenSecTradeContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = ctx.open_order_list_query(trd_env=trd_env)
            ctx.close()
            
            if ret == 0:
                order_row = data[data['order_id'] == order_id]
                if not order_row.empty:
                    return order_row['order_status'].iloc[0]
        except Exception as e:
            logger.debug(f"[BattlePlan] 查询订单状态失败: {e}")
        return None

    def _cancel_order(self, order_id):
        """撤单"""
        if not FUTU_AVAILABLE: return
        try:
            ctx = OpenSecTradeContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = ctx.modify_order(ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=trd_env)
            ctx.close()
            if ret == 0:
                logger.info(f"[BattlePlan] 订单 {order_id} 已撤销")
            else:
                logger.warning(f"[BattlePlan] 撤单失败: {data}")
        except Exception as e:
            logger.error(f"[BattlePlan] 撤单异常: {e}")

    def _get_yesterday_close_prices(self, symbols):
        """获取昨日收盘价"""
        results = {}
        if not FUTU_AVAILABLE: return results
        
        try:
            from futu import OpenQuoteContext
            from app.utils.futu_limiter import get_global_limiter
            limiter = get_global_limiter()

            if not limiter.acquire(timeout=10):
                logger.warning("[限流] 获取昨日收盘价被拒绝")
                return results

            ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            try:
                ret, data = ctx.get_market_snapshot(symbols)
                limiter.record()
            finally:
                ctx.close()
            
            if ret == 0:
                for _, row in data.iterrows():
                    code = row['code']
                    # 尝试获取 last_price 或 close_price
                    price = row.get('last_price') or row.get('close_price')
                    if price and price > 0:
                        results[code] = price
        except Exception as e:
            logger.error(f"获取收盘价失败: {e}")
        return results

    def _execute_target_via_signal(self, target, order_name):
        """通过 Signal -> TradeExecutor 标准流程执行"""
        symbol = target.get("symbol")
        direction = target.get("direction", "BUY")
        qty = target.get("quantity", 100)

        # 获取当前价格用于 Signal 创建 (实际下单时 TradeExecutor 会重取)
        current_price = 0.0 # 实际下单时会通过 API 获取最新价
        if self._auction_order_ids.get(symbol):
            current_price = self._auction_order_ids[symbol]['price'] # fallback

        action = SignalAction.BUY if direction == "BUY" else SignalAction.SELL

        # P0 修复 (2026-05-17): 透传 battle_plan 的 signal_impact 到 Signal extra
        # 确保 executor 门禁可以读取 LLM 的定性判断
        _signal_impact = target.get("signal_impact", "neutral")
        _urgency = target.get("urgency", "Medium")

        # P2-5 (2026-05-20): trace_id 全链路追踪增强
        # 生成唯一 trace_id，格式：BP-{日期}-{uuid6}，便于日志追踪
        trace_id = f"BP-{datetime.now().strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:6].upper()}"
        set_trace_id(trace_id)
        logger.info(f"[BattlePlan] trace_id={trace_id} 提交 {order_name}: {symbol} {action.value} {qty}股")

        signal = Signal(
            code=symbol,
            action=action,
            price=current_price,
            quantity=qty,
            strategy="battle_plan",
            reason=f"盘前计划 - {order_name}",
            tier=TradeTier.TIER_TREND,
            trace_id=trace_id,
            extra={
                "signal_impact": _signal_impact,
                "urgency": _urgency,
                "source": "battle_plan",
                "order_name": order_name,
            },
        )

        logger.info(f"[BattlePlan] trace_id={trace_id} 提交 {order_name}: {symbol} {action.value} {qty}股")
        if self.executor:
            self.executor.execute(signal)

    def _load_and_validate_plan(self, date_str):
        """加载并校验计划"""
        if not os.path.exists(BATTLE_PLAN_PATH): return None
        with open(BATTLE_PLAN_PATH, 'r') as f:
            plan = json.load(f)
        if plan.get("date") != date_str: return None
        return plan
