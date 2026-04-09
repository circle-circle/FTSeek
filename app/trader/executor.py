"""
交易执行器
对接富途 API 下单，支持港股/美股双市场，自动识别标的市场
"""
import logging
import threading
import time
from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.strategy.base import Signal, SignalAction
from app.trader.position_manager import PositionManager
from app.risk.risk_manager import get_risk_manager
from app.risk.dynamic_exit_manager import dynamic_exit_manager
from app.logs.failure_tracker import failure_tracker
from app.risk.circuit_breaker import circuit_breaker
from app.execution.smart_router import smart_router
from app.execution.account_router import select_account_for_code, is_option_code
from app.execution.order_retry import order_retry_manager, RetryReason
from app.risk.trend_filter import get_trend_filter
from app.risk.dynamic_position_sizer import get_dynamic_position_sizer
# P1-3: 执行质量监控器 (2026-04-06 新增)
from app.execution.execution_monitor import get_execution_monitor
# 新闻增强模块 (2026-04-08 新增)
from app.trader.news_enhancer import get_news_enhancer, check_news_block
# 归因系统 (2026-04-09 新增)
from app.analytics.strategy_health import get_strategy_health_tracker, TradeRecord as HealthTradeRecord

# 富途常量 - 提前导入避免运行时错误
try:
    from futu import TrdSide, OrderType, TrdEnv
except ImportError:
    TrdSide = None
    OrderType = None
    TrdEnv = None

# 账户选择逻辑导入（用于港股多账户支持）
try:
    import sys
    import os
    sys.path.insert(0, '/root/.openclaw/skills/openapi/scripts')
    from common import select_account_for_code
    ACCOUNT_SELECTION_AVAILABLE = True
    print("[账户选择] 模块加载成功")
except Exception as e:
    ACCOUNT_SELECTION_AVAILABLE = False
    print(f"[账户选择] 模块加载失败，将使用默认账户：{e}")

logger = logging.getLogger("ftseek.trader.executor")

logger.propagate = True  # 允许日志传递到根日志器，以便写入文件


@dataclass
class TradeResult:
    """交易结果"""
    success: bool
    order_id: Optional[str] = None
    position_id: Optional[int] = None
    trade_type: Optional[str] = None  # OPEN/CLOSE
    pnl: Optional[float] = None
    message: str = ""
    close_ratio: Optional[float] = None  # 平仓比例 (部分平仓时使用)
    remaining_quantity: Optional[int] = None  # 剩余数量 (部分平仓时使用)


def _detect_market(code: str) -> str:
    """根据标的代码判断市场：HK / US"""
    if code.startswith("US."):
        return "US"
    return "HK"


def _is_option(code: str) -> bool:
    """
    判断是否为期权代码 (增强版，带详细日志)
    
    期权代码特征:
    - HK.POP260429C190000 (泡泡玛特 Call 190)
    - HK.TCH260429C510000 (腾讯 Call 510)
    - 格式：市场代码 + 底层资产 + 到期日 (6 位) + C/P + 行权价
    
    正股代码特征:
    - HK.09992 (5 位数字)
    - HK.00700 (5 位数字)
    """
    import re
    
    if not code or not isinstance(code, str):
        logger.warning(f"[期权判断] 无效代码：{code}")
        return False
    
    # 提取市场代码后的部分
    parts = code.split('.')
    if len(parts) < 2:
        logger.warning(f"[期权判断] 代码格式错误：{code}")
        return False
    
    symbol = parts[-1]
    logger.debug(f"[期权判断] 分析代码：{code}, symbol={symbol}")
    
    # 规则 1: 期权特征 - 包含 6 位日期 + C/P
    if re.search(r'\d{6}[CP]', symbol, re.IGNORECASE):
        logger.info(f"[期权判断] {code} -> 期权 (匹配日期+C/P)")
        return True
    
    # 规则 2: 正股特征 - 纯数字，通常 5 位
    if re.match(r'^\d{5}$', symbol):
        logger.info(f"[期权判断] {code} -> 正股 (5 位数字)")
        return False
    
    # 规则 3: 窝轮/牛熊证特征 - 5 位数字但包含字母
    if re.match(r'^\d{5}[A-Z]$', symbol, re.IGNORECASE):
        logger.info(f"[期权判断] {code} -> 窝轮/牛熊证")
        return False
    
    # 规则 4: 备用判断 - 包含 C 或 P 且长度>10
    if ('C' in symbol.upper() or 'P' in symbol.upper()) and len(symbol) > 10:
        logger.info(f"[期权判断] {code} -> 期权 (备用规则)")
        return True
    
    # 默认：记录日志并返回 False
    logger.warning(f"[期权判断] {code} 无法识别，默认视为正股")
    return False


def _get_account_for_code(code: str, trd_env) -> Optional[int]:
    """
    根据证券代码自动选择账户
    
    港股期权: 8246818 (MARGIN)
    港股正股: 18820895 (CASH)
    美股: 暂不支持
    
    Args:
        code: 证券代码
        trd_env: 交易环境
        
    Returns:
        账户ID，实盘返回None
    """
    from futu import TrdEnv
    
    # 实盘不指定账户
    if trd_env != TrdEnv.SIMULATE:
        return None
    
    market = _detect_market(code)
    
    if market == "HK":
        if _is_option(code):
            # 港股期权账户
            logger.info(f"[账户选择] {code} -> 期权账户 8246818")
            return 8246818
        else:
            # 港股正股账户
            logger.info(f"[账户选择] {code} -> 正股账户 18820895")
            return 18820895
    elif market == "US":
        # 美股暂不支持
        logger.warning(f"[账户选择] {code} -> 美股暂不支持")
        return None
    
    return None


class TradeExecutor:
    """富途交易执行器 (港股 + 美股双市场)"""

    def __init__(self):
        self._trd_ctx_hk = None
        self._trd_ctx_us = None
        self.position_manager = PositionManager()
        self.risk_manager = get_risk_manager()
        # 幂等锁：防止网络抖动/重复回调导致重复下单
        self._order_lock = threading.Lock()
        self._pending_signals: Dict[str, float] = {}  # signal_hash → timestamp, TTL 30s
        # 滑点记录：(signal_price, dealt_avg_price, code)，最多保留 200 条，供健康度看板读取
        self._slippage_records: List[tuple] = []
        
        # S3: 水位管理单例注入 (2026-04-06)
        # 资金来源: app.main.py 中初始化，initial_capital=1000000 (100万)
        try:
            from app.factors.water_level_manager import get_water_level_manager, WaterLevel
            self._water_level_manager = get_water_level_manager()
            logger.info("[水位管理] 单例注入成功，当前水位等级可通过 get_current_level() 查询")
        except Exception as e:
            logger.warning(f"[水位管理] 单例注入失败，水位限制功能禁用: {e}")
            self._water_level_manager = None
        
        # 断点2修复: 止盈止损管理器单例注入 (2026-04-06)
        try:
            from app.factors.profit_loss_manager import get_profit_loss_manager
            self._profit_loss_manager = get_profit_loss_manager()
            logger.info("[止盈止损管理] 单例注入成功，三层止盈架构已激活")
        except Exception as e:
            logger.warning(f"[止盈止损管理] 单例注入失败，止盈止损增强功能禁用: {e}")
            self._profit_loss_manager = None
        
        # 断点5修复: 持仓周期管理器单例注入 (2026-04-06)
        try:
            from app.factors.holding_period_manager import get_holding_period_manager
            self._holding_period_manager = get_holding_period_manager()
            logger.info("[持仓周期管理] 单例注入成功，质量分级持仓期限已激活")
        except Exception as e:
            logger.warning(f"[持仓周期管理] 单例注入失败，持仓周期管理功能禁用: {e}")
            self._holding_period_manager = None

    def _get_trd_ctx(self, market: str):
        """获取交易上下文 (按市场懒加载)"""
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket

        if market == "US":
            if self._trd_ctx_us is None:
                self._trd_ctx_us = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.US,
                    security_firm=None,
                )
                logger.info(f"美股交易上下文已创建，环境：{'模拟' if config.TRADE_ENV == 1 else '真实'}")
            return self._trd_ctx_us
        else:
            if self._trd_ctx_hk is None:
                self._trd_ctx_hk = OpenSecTradeContext(
                    host=config.OPEND_HOST,
                    port=config.OPEND_PORT,
                    filter_trdmarket=TrdMarket.HK,
                    security_firm=None,
                )
                logger.info(f"港股交易上下文已创建，环境：{'模拟' if config.TRADE_ENV == 1 else '真实'}")
            return self._trd_ctx_hk

    def _get_qty(self, signal: Signal) -> int:
        """根据市场、股票和动态仓位计算下单数量 (避免碎股)"""
        market = _detect_market(signal.code)

        # ----------------------------------------------------------------
        # 动态仓位计算（基于置信度、市场状态、AI分数、波动率）
        # ----------------------------------------------------------------
        try:
            sizer = get_dynamic_position_sizer()
            extra = signal.extra or {}
            confidence = extra.get('confidence', 0.6)
            raw_regime = extra.get('market_regime', 'sideways')

            # 将共识引擎 market_regime 映射到 DynamicPositionSizer 期望的格式
            regime_map = {
                'trending': 'uptrend',
                'ranging': 'sideways',
                'neutral': 'sideways',
                'strong_trend_up': 'uptrend',
                'weak_trend_up': 'uptrend',
                'range_bound': 'sideways',
                'weak_trend_down': 'downtrend',
                'strong_trend_down': 'downtrend',
                'high_volatility': 'sideways',
                'low_volatility': 'sideways',
            }
            market_regime = regime_map.get(str(raw_regime), 'sideways')

            ai_score = extra.get('ai_score', 0.0)

            # 计算动态仓位比例（0.05~0.30）
            position_ratio = sizer.calculate_position(
                signal_confidence=confidence,
                market_regime=market_regime,
                ai_score=ai_score,
                volatility=0.0,
            )
        except Exception as e:
            logger.warning(f"[动态仓位] 计算失败，使用默认配置: {e}")
            position_ratio = None

        # ----------------------------------------------------------------
        # S3: 水位管理限制 (2026-04-06 新增)
        # 水位低时自动降低仓位或禁止交易
        # ----------------------------------------------------------------
        water_level_max_ratio = 1.0  # 默认不限制
        water_level_action = "NORMAL"
        
        if self._water_level_manager is not None:
            try:
                # 获取当前水位状态
                state = self._water_level_manager.get_state()
                current_level = state.current_level
                water_level_action = state.suggested_action
                
                # 记录水位等级变化日志
                if state.previous_level and state.previous_level != current_level:
                    logger.warning(
                        f"[水位变化] {state.previous_level.value} → {current_level.value} | "
                        f"建议操作: {water_level_action} | 当前水位: {state.current_ratio:.1%}"
                    )
                
                # 检查是否允许交易
                can_trade, reason = self._water_level_manager.can_trade()
                if not can_trade:
                    logger.warning(f"[水位限制] {signal.code} 交易被禁止: {reason}")
                    return 0  # 返回 0 表示禁止交易
                
                # 获取最大仓位比例
                water_level_max_ratio = self._water_level_manager.get_max_position()
                
                # 如果水位限制比动态仓位更严格，应用水位限制
                if position_ratio is not None and position_ratio > water_level_max_ratio:
                    original_ratio = position_ratio
                    position_ratio = water_level_max_ratio
                    logger.info(
                        f"[水位限制] {signal.code} 动态仓位 {original_ratio:.1%} → "
                        f"水位限制 {water_level_max_ratio:.1%} (水位等级: {current_level.value})"
                    )
                
            except Exception as e:
                logger.warning(f"[水位管理] 获取水位限制失败，跳过: {e}")
        
        # 港股：根据股票配置每手股数
        if market == "HK":
            from app.config import DEFAULT_ORDER_QTY_BY_CODE
            # 基础每手股数
            base_qty = DEFAULT_ORDER_QTY_BY_CODE.get(signal.code) or config.DEFAULT_ORDER_QTY_HK

            # 如果动态仓位计算成功，按比例调整倍数（以 0.10 基准比例对应 1 手）
            if position_ratio is not None:
                base_ratio = 0.10  # 基础仓位对应 1 手
                multiplier = max(1, round(position_ratio / base_ratio))
                qty = base_qty * multiplier
                
                # 断点4修复: 质量仓位系数 (2026-04-06)
                # Class A: 1.3x, Class B: 1.0x, Class C: 0.7x
                quality_boost = extra.get('quality_boost', 1.0)
                if quality_boost != 1.0:
                    qty = max(base_qty, int(qty * quality_boost))
                    qty = (qty // base_qty) * base_qty or base_qty
                    logger.info(
                        f"[质量仓位] {signal.code} 质量加成={quality_boost:.1f}x → 调整后={qty}"
                    )
                
                # 应用新闻情绪倍率（由 data_subscriber 注入 extra['news_boost']）
                news_boost = extra.get('news_boost', 1.0)
                if news_boost != 1.0:
                    raw_qty = qty
                    qty = max(base_qty, int(qty * news_boost))
                    # 对齐整手
                    qty = (qty // base_qty) * base_qty or base_qty
                    logger.info(
                        f"[动态仓位] {signal.code} 置信度={confidence:.2f} "
                        f"市场={market_regime} 仓位={position_ratio:.1%} "
                        f"基础={base_qty} 倍数={multiplier}x 新闻={news_boost:.1f}x "
                        f"原={raw_qty} 最终={qty}"
                    )
                else:
                    logger.info(
                        f"[动态仓位] {signal.code} 置信度={confidence:.2f} "
                        f"市场={market_regime} 仓位={position_ratio:.1%} "
                        f"基础={base_qty} 倍数={multiplier}x 最终={qty}"
                    )
                return qty

            # 优先使用信号中的数量
            if signal.quantity and signal.quantity > 0:
                return signal.quantity

            return base_qty

        # 美股
        if position_ratio is not None:
            base_qty = config.DEFAULT_ORDER_QTY_US
            base_ratio = 0.10
            multiplier = max(1, round(position_ratio / base_ratio))
            qty = base_qty * multiplier
            logger.info(
                f"[动态仓位] {signal.code}(US) 置信度={confidence:.2f} "
                f"仓位={position_ratio:.1%} 最终={qty}"
            )
            return qty

        if signal.quantity and signal.quantity > 0:
            return signal.quantity

        return config.DEFAULT_ORDER_QTY_US
    
    def _execute_order_with_retry(self, trd_ctx, code: str, price: float, qty: int,
                                  trd_side, order_type, trd_env, signal) -> tuple:
        """
        执行订单并支持重试 (2026-03-17 新增)
        
        Args:
            trd_ctx: 交易上下文
            code: 股票代码
            price: 价格
            qty: 数量
            trd_side: 买卖方向
            order_type: 订单类型
            trd_env: 交易环境
            signal: 交易信号
        
        Returns:
            (ret, data) 元组
        """
        # 生成临时订单 ID 用于重试追踪
        import uuid
        temp_order_id = str(uuid.uuid4())[:8]
        
        # 根据证券类型自动选择账户 (2026-03-31: 期权/正股双账户支持)
        acc_id = _get_account_for_code(code, trd_env)
        ret, data = trd_ctx.place_order(
            price=price,
            qty=qty,
            code=code,
            trd_side=trd_side,
            order_type=order_type,
            trd_env=trd_env,
            acc_id=acc_id,
        )
        
        # 如果失败且可以重试
        if ret != 0 and order_retry_manager.should_retry(temp_order_id):
            retry_count = order_retry_manager.get_retry_count(temp_order_id)
            logger.warning(f"订单失败，准备重试 (第{retry_count + 1}次): {data}")
            
            # 计算新价格
            action = 'BUY' if trd_side == TrdSide.BUY else 'SELL'
            new_price = order_retry_manager.get_next_price(
                temp_order_id, code, action, price, 
                'MARKET' if order_type == OrderType.MARKET else 'LIMIT'
            )
            
            # 记录重试
            order_retry_manager.record_retry(
                temp_order_id, code, action,
                RetryReason.MARKET_FAILED if order_type == OrderType.MARKET else RetryReason.LIMIT_NOT_FILLED,
                price, new_price, False, str(data)
            )
            
            # 重试下单
            logger.info(f"重试下单：价格={new_price:.2f}")
            ret, data = trd_ctx.place_order(
                price=new_price,
                qty=qty,
                code=code,
                trd_side=trd_side,
                order_type=OrderType.NORMAL,  # 重试时使用限价单
                trd_env=trd_env,
                acc_id=acc_id,
            )
            
            # 记录重试结果
            order_retry_manager.record_retry(
                temp_order_id, code, action,
                RetryReason.MARKET_FAILED,
                price, new_price, ret == 0, str(data) if ret != 0 else "成功"
            )
        
        return ret, data

    def _signal_hash(self, code: str, action, price: float) -> str:
        """生成信号哈希，用于幂等去重。价格对齐到最近tick(0.02)避免微小浮动造成误判"""
        action_str = action.value if hasattr(action, 'value') else str(action)
        price_band = round(price / 0.02) * 0.02
        return f"{code}:{action_str}:{price_band:.3f}"

    def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
        """
        执行交易信号 (集成熔断器 + 失败追踪 + 自动识别开仓/平仓)
        :param signal: 交易信号
        :param trade_type: AUTO(自动识别) / OPEN(开仓) / CLOSE(平仓)
        :return: TradeResult {success, order_id, position_id, trade_type, pnl}
        """
        if signal.action == SignalAction.ALERT:
            return TradeResult(success=False, message="ALERT 类型信号不执行交易")

        # 熔断器检查 (2026-03-17 新增)
        if not circuit_breaker.can_trade():
            failure_tracker.record_failure(signal.code, 'circuit_breaker', '熔断暂停中')
            return TradeResult(success=False, message="熔断器触发，暂停交易")

        # 新闻增强检查 (2026-04-08 新增)
        # 仅对开仓信号执行新闻增强（平仓/风控信号直接执行）
        if trade_type != 'CLOSE':
            # 构造信号字典
            signal_dict = {
                'stock_code': signal.code,
                'action': 'buy' if signal.action == SignalAction.BUY else 'sell',
                'confidence': signal.confidence if hasattr(signal, 'confidence') else 0.5,
                'strategy': signal.strategy if hasattr(signal, 'strategy') else 'unknown'
            }
            
            # 检查是否应该阻止交易
            block_reason = check_news_block(signal_dict)
            if block_reason:
                logger.warning(f"[新闻阻止] {signal.code} 阻止交易: {block_reason}")
                failure_tracker.record_failure(signal.code, 'news_block', block_reason)
                return TradeResult(success=False, message=f"新闻阻止: {block_reason}")
            
            # 获取新闻增强器并增强信号
            try:
                news_enhancer = get_news_enhancer()
                enhanced_signal = news_enhancer.enhance_signal(signal_dict)
                
                if enhanced_signal.get('news_enhanced'):
                    # 更新信号置信度
                    original_conf = signal.confidence if hasattr(signal, 'confidence') else 0.5
                    new_conf = enhanced_signal.get('confidence', original_conf)
                    
                    logger.info(f"[新闻增强] {signal.code} 置信度调整: {original_conf:.2f} → {new_conf:.2f}")
                    
                    # 如果置信度被大幅降低，记录但继续执行
                    if new_conf < 0.3:
                        logger.warning(f"[新闻增强] {signal.code} 置信度过低 ({new_conf:.2f})，建议人工审核")
            except Exception as e:
                logger.warning(f"[新闻增强] 检查失败，继续执行: {e}")

        # 幂等检查：防止同一信号（code+方向+价格档位）在30s内重复下单
        # 2026-03-27 修复: 平仓/止损/止盈信号无条件跳过幂等检查，风控信号必须立即执行
        # 优先检查 trade_type 参数：CLOSE 类型直接跳过（最根本的修复，防止11000+次duplicate_signal）
        if trade_type == 'CLOSE':
            logger.info(f"[平仓信号] {signal.code} {signal.strategy} 跳过幂等检查，立即执行")
        else:
            # 仅对开仓信号(OPEN/AUTO)执行幂等去重
            is_risk_signal = (
                signal.strategy in ("止损", "止盈", "移动止损", "保本止损", "时间止损", "硬止损") or
                signal.extra.get("stop_loss") or
                signal.extra.get("take_profit") or
                signal.extra.get("trailing_stop")
            )
            if not is_risk_signal:
                sig_hash = self._signal_hash(signal.code, signal.action, signal.price)
                with self._order_lock:
                    now = time.time()
                    if sig_hash in self._pending_signals:
                        if now - self._pending_signals[sig_hash] < 30:
                            logger.warning(f"[幂等拦截] {sig_hash} 30s内重复信号，跳过")
                            return TradeResult(success=False, message="duplicate_signal")
                    self._pending_signals[sig_hash] = now
                    # 清理超过60s的过期记录
                    self._pending_signals = {k: v for k, v in self._pending_signals.items() if now - v < 60}
            else:
                logger.info(f"[风控信号] {signal.code} {signal.strategy} 跳过幂等检查，立即执行")

        # 自动识别开仓/平仓 (2026-03-20 新增)
        # 如果有多头持仓且信号是 SELL，应该是平仓
        # 如果有空头持仓且信号是 BUY，应该是平仓
        # 2026-03-27 修复: 无持仓时不允许做空开仓 (模拟账户不支持)
        # 2026-04-09 优化: 已有持仓时的 BUY 信号处理
        if trade_type == 'AUTO':
            position = self.position_manager.get_position(signal.code)
            if position and position.status == 'OPEN':
                if position.action == 'BUY' and signal.action == SignalAction.SELL:
                    trade_type = 'CLOSE'
                    logger.info(f"[自动识别] {signal.code} 多头持仓存在，SELL 信号转为平仓")
                elif position.action == 'SELL' and signal.action == SignalAction.BUY:
                    trade_type = 'CLOSE'
                    logger.info(f"[自动识别] {signal.code} 空头持仓存在，BUY 信号转为平仓")
                elif position.action == 'BUY' and signal.action == SignalAction.BUY:
                    # 2026-04-09 新增: 已有多头持仓时的 BUY 信号处理
                    # 检查是否应该加仓或止盈
                    current_price = signal.price
                    entry_price = position.entry_price
                    pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                    
                    # 如果持仓盈利，优先考虑止盈而非加仓
                    if pnl_pct >= 0.03:  # 盈利 3% 以上
                        logger.info(
                            f"[持仓优化] {signal.code} 已有盈利 ({pnl_pct:+.1%})，"
                            f"BUY信号转为部分止盈检查"
                        )
                        # 不自动转为止盈，让止盈引擎处理
                        # 但拒绝加仓信号
                        return TradeResult(
                            success=False, 
                            message=f"existing_position_profitable ({pnl_pct:+.1%})"
                        )
                    elif pnl_pct <= -0.02:  # 亏损 2% 以上
                        # 亏损时不加仓，让止损引擎处理
                        logger.info(
                            f"[持仓优化] {signal.code} 已有亏损 ({pnl_pct:+.1%})，"
                            f"拒绝加仓，由止损引擎处理"
                        )
                        return TradeResult(
                            success=False,
                            message=f"existing_position_loss ({pnl_pct:+.1%})"
                        )
                    else:
                        # 盈亏不大，允许加仓逻辑（但需要持仓数检查）
                        trade_type = 'OPEN'
                        logger.info(
                            f"[持仓优化] {signal.code} 盈亏不大 ({pnl_pct:+.1%})，允许加仓检查"
                        )
                else:
                    trade_type = 'OPEN'
            else:
                # 无持仓：FTSeek 为纯多头策略，忽略 SELL 信号
                if signal.action == SignalAction.SELL:
                    logger.info(f"[纯多头策略] {signal.code} 无持仓时忽略 SELL 信号 (策略设计为纯多头)")
                    return TradeResult(success=False, message="pure_long_strategy_no_position")
                trade_type = 'OPEN'

        # 风控检查 (仅开仓需要)
        if trade_type == 'OPEN':
            # 2026-03-25 新增: 最大持仓数检查
            max_open_positions = getattr(config, 'MAX_OPEN_POSITIONS', 3)  # 默认3个
            current_open_positions = self.position_manager.get_open_positions_count()
            if current_open_positions >= max_open_positions:
                msg = f"已达最大持仓数 ({current_open_positions}/{max_open_positions})，拒绝开仓"
                logger.warning(f"[持仓限制] {signal.code} {msg}")
                failure_tracker.record_failure(signal.code, 'position_limit', msg)
                return TradeResult(success=False, message=msg)
            
            # 趋势过滤检查 V2 (2026-03-21 增强: 多周期确认)
            try:
                from futu import OpenQuoteContext, KLType, SubType, RET_OK
                quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                
                # 2026-03-27 修复: 先订阅 K 线数据才能获取
                # 订阅日K和60分钟K
                sub_types = [SubType.K_DAY, SubType.K_60M]
                ret_sub, msg_sub = quote_ctx.subscribe(signal.code, sub_types)
                if ret_sub != RET_OK:
                    logger.warning(f"[趋势过滤] 订阅K线失败: {msg_sub}，尝试直接获取")
                
                # 获取日K数据
                ret_daily, data_daily = quote_ctx.get_cur_kline(signal.code, 30, KLType.K_DAY)
                # 获取60分钟K线数据 (用于多周期确认)
                ret_60min, data_60min = quote_ctx.get_cur_kline(signal.code, 50, KLType.K_60M)
                
                if ret_daily == 0:
                    closes = data_daily['close'].values.tolist()
                    intraday_closes = None
                    
                    if ret_60min == 0:
                        intraday_closes = data_60min['close'].values.tolist()
                        logger.debug(f"[趋势过滤] 获取日K {len(closes)}条, 60分钟K {len(intraday_closes)}条")
                    else:
                        logger.warning(f"[趋势过滤] 60分钟K获取失败，仅使用日K")
                    
                    # 重置趋势过滤器以加载新配置
                    from app.risk.trend_filter import reset_trend_filter, get_trend_filter
                    reset_trend_filter()
                    trend_filter = get_trend_filter()
                    
                    # 从信号extra中获取ADX值 (2026-03-25 新增)
                    adx_value = None
                    if hasattr(signal, 'extra') and signal.extra:
                        adx_value = signal.extra.get('adx')
                    
                    # 多周期趋势分析 (增强版：支持ADX + 多周期确认)
                    trend_info = trend_filter.analyze_trend(
                        closes, 
                        intraday_closes=intraday_closes,
                        adx=adx_value,
                        code=signal.code
                    )
                    
                    action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
                    should_trade, reason = trend_filter.should_trade(action_str, trend_info)
                    
                    if not should_trade:
                        quote_ctx.close()
                        failure_tracker.record_failure(signal.code, 'trend_filter', reason)
                        logger.warning(f"[趋势过滤] {signal.code} 交易被阻止: {reason}")
                        return TradeResult(success=False, message=reason)
                    
                    logger.info(f"[趋势过滤] {signal.code} {trend_info.trend.value} | {reason}")
                    
                    # 2026-03-25 V3 新增: 多周期趋势确认
                    if not trend_info.multi_timeframe_confirmed:
                        logger.warning(f"[多周期趋势] {signal.code} 日线与60分钟线趋势冲突，建议观望")
                        # 不强制禁止，但记录警告
                    
                    # 2026-03-25 新增: 入场价格检查
                    try:
                        from app.risk.entry_price_checker import get_entry_price_checker
                        price_checker = get_entry_price_checker()
                        
                        # 获取日内数据
                        from futu import KLType
                        ret_snap, snap_data = quote_ctx.get_market_snapshot([signal.code])
                        if ret_snap == 0 and len(snap_data) > 0:
                            day_high = float(snap_data.iloc[0].get('high_price', 0))
                            day_low = float(snap_data.iloc[0].get('low_price', 0))
                            day_open = float(snap_data.iloc[0].get('open_price', 0))
                            
                            if day_high > 0 and day_low > 0:
                                price_result = price_checker.check_entry_price(
                                    code=signal.code,
                                    action=signal.action.value if hasattr(signal.action, 'value') else str(signal.action),
                                    current_price=signal.price,
                                    day_high=day_high,
                                    day_low=day_low,
                                    day_open=day_open
                                )
                                
                                if not price_result.allowed:
                                    quote_ctx.close()
                                    failure_tracker.record_failure(signal.code, 'price_check', price_result.reason)
                                    logger.warning(f"[价格检查] {signal.code} {price_result.reason}")
                                    return TradeResult(success=False, message=price_result.reason)
                                
                                if price_result.warning:
                                    logger.warning(f"[价格检查] {signal.code} {price_result.warning}")
                                else:
                                    logger.info(f"[价格检查] {signal.code} {price_result.reason}")
                    except Exception as e:
                        logger.warning(f"[价格检查] 检查失败，跳过: {e}")
                    
                else:
                    # 2026-03-27 修复: 日K获取失败时应拒绝交易，而非跳过检查
                    logger.warning(f"[趋势过滤] {signal.code} 日K获取失败，拒绝交易以确保安全")
                    quote_ctx.close()
                    failure_tracker.record_failure(signal.code, 'trend_filter', '日K数据获取失败')
                    return TradeResult(success=False, message="趋势过滤数据获取失败，拒绝交易")
                quote_ctx.close()
            except Exception as e:
                # 2026-03-27 修复: 异常时拒绝交易，而非跳过
                logger.warning(f"[趋势过滤] {signal.code} 检查失败: {e}，拒绝交易")
                failure_tracker.record_failure(signal.code, 'trend_filter', str(e))
                return TradeResult(success=False, message=f"趋势过滤检查失败: {e}")
            
            risk_result = self.risk_manager.check_before_trade(signal)
            
            if not risk_result.passed:
                failed_checks = [k for k, v in risk_result.details.items() if not v]
                logger.warning(f"风控检查失败：{', '.join(failed_checks)}")
                # 记录失败 (2026-03-17 新增)
                failure_tracker.record_failure(signal.code, 'risk', f"风控检查失败：{', '.join(failed_checks)}")
                circuit_breaker.record_reject(f"风控检查失败：{', '.join(failed_checks)}")
                return TradeResult(
                    success=False,
                    message=f"风控检查失败：{', '.join(failed_checks)}"
                )
            
            logger.debug(f"风控检查通过：{risk_result.details}")

        if trade_type == 'OPEN':
            return self._execute_open(signal)
        else:
            return self._execute_close(signal)

    def _execute_open(self, signal: Signal) -> TradeResult:
        """
        执行开仓交易
        1. 富途下单
        2. 下单成功后创建持仓记录
        3. 返回 position_id
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # P1-3: 记录执行质量监控开始
        exec_monitor = get_execution_monitor()
        order_type_str = "MARKET" if config.DEFAULT_ORDER_TYPE == "MARKET" else "LIMIT"
        record_id = exec_monitor.record_start(
            code=signal.code,
            signal_price=signal.price,
            quantity=signal.quantity,
            order_type=order_type_str,
        )

        try:
            # TrdSide, OrderType, TrdEnv 已在文件顶部导入

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            trd_side = TrdSide.BUY if signal.action == SignalAction.BUY else TrdSide.SELL

            qty = self._get_qty(signal)

            # 2026-03-17 集成智能订单路由
            # 修复: 兼容 action 可能是 SignalAction 枚举或字符串的情况
            action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
            order = smart_router.route_order(
                code=signal.code,
                action=action_str,
                price=signal.price,
                quantity=qty,
                market_data={'volatility': 0.0}  # 可传入实时波动率数据
            )
            
            # 根据智能路由结果设置订单类型和价格
            # 2026-03-20 修复: 做空开仓不能使用市价单(价格=0)，需获取实时价格
            if order.order_type.value == 'market':
                if signal.action == SignalAction.SELL:
                    # 做空开仓：获取当前买一价作为限价
                    from futu import OpenQuoteContext
                    quote_ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
                    ret, snapshot = quote_ctx.get_market_snapshot([signal.code])
                    if ret == 0 and len(snapshot) > 0:
                        price = snapshot.iloc[0]['bid_price']  # 买一价
                        order_type = OrderType.NORMAL  # 改用限价单
                        logger.info(f"[做空开仓] 使用买一价 {price:.2f} 作为限价")
                    else:
                        price = signal.price  # 回退到信号价格
                        order_type = OrderType.NORMAL
                    quote_ctx.close()
                else:
                    order_type = OrderType.MARKET
                    price = 0
            else:
                order_type = OrderType.NORMAL
                price = order.price
            
            logger.info(
                f"智能路由：[{market}] {signal.code} 类型={order.order_type.value} "
                f"价格={price:.2f} 偏移={order.offset_pct:.1%} 原因={order.reason}"
            )

            # 安全检查：金额限制
            estimated_amount = signal.price * qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝下单"
                logger.warning(msg)
                # 记录失败 (2026-03-17 新增)
                failure_tracker.record_failure(signal.code, 'limit', msg)
                circuit_breaker.record_reject('订单金额超限')
                return TradeResult(success=False, message=msg)

            logger.info(
                f"准备下单：[{market}] {signal.code} {trd_side} "
                f"qty={qty} type={order_type} price={price}"
            )

            # 2026-03-17 集成订单重试机制
            ret, data = self._execute_order_with_retry(
                trd_ctx=trd_ctx,
                code=signal.code,
                price=price,
                qty=qty,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
                signal=signal,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # ----------------------------------------------------------------
                # 获取真实成交价：市价单下单后查询实际成交均价
                # 避免使用 signal.price（多个K线的均值）作为 entry_price，
                # 否则止损止盈价基准不准确，导致开仓后立刻亏损平仓
                # ----------------------------------------------------------------
                actual_entry_price = signal.price  # 默认值
                try:
                    import time as _time
                    _time.sleep(0.3)  # 等待成交数据落盘
                    ret_order, order_data = trd_ctx.order_list_query(order_id=order_id, trd_env=trd_env)
                    if ret_order == 0 and len(order_data) > 0:
                        dealt_avg = float(order_data.iloc[0].get('dealt_avg_price', 0))
                        if dealt_avg > 0:
                            actual_entry_price = dealt_avg
                            logger.info(f"[真实成交价] {signal.code} dealt_avg_price={dealt_avg:.4f} (原信号均价={signal.price:.4f})")
                            # 记录滑点数据供健康度看板使用（最多保留200条）
                            if signal.price > 0:
                                self._slippage_records.append((signal.price, dealt_avg, signal.code))
                                if len(self._slippage_records) > 200:
                                    self._slippage_records = self._slippage_records[-200:]
                        else:
                            # 市价单尚未完全成交时，用快照最新价兜底
                            try:
                                ret_s, snap = trd_ctx.get_market_snapshot([signal.code])
                                if ret_s == 0 and len(snap) > 0:
                                    last_price = float(snap.iloc[0].get('last_price', 0))
                                    if last_price > 0:
                                        actual_entry_price = last_price
                                        logger.info(f"[成交价兜底] {signal.code} last_price={last_price:.4f}")
                            except Exception:
                                pass
                except Exception as ep:
                    logger.warning(f"[成交价查询失败] {signal.code}: {ep}，使用信号价格 {signal.price:.4f}")
                
                # 获取动态止盈止损参数 (2026-03-17 新增)
                stop_loss = dynamic_exit_manager.get_stop_loss()
                take_profit = dynamic_exit_manager.get_take_profit()
                
                # 以真实成交价为基础计算止盈止损价 (考虑交易费用)
                # 2026-03-24: 优化止盈止损计算，加入交易费用考量
                from app.risk.trading_fees import DEFAULT_FEES
                
                # 使用考虑费用的止盈止损计算
                stop_price = dynamic_exit_manager.get_stop_loss_price_with_fees(
                    actual_entry_price, qty, DEFAULT_FEES
                )
                take_profit_price = dynamic_exit_manager.get_take_profit_price_with_fees(
                    actual_entry_price, qty, DEFAULT_FEES
                )
                
                # 如果是做空，反转止盈止损方向
                if signal.action == SignalAction.SELL:
                    # 做空: 止损在上方，止盈在下方
                    stop_price = actual_entry_price * (1 + abs(stop_loss) + DEFAULT_FEES.total_with_slippage)
                    take_profit_price = actual_entry_price * (1 - take_profit - DEFAULT_FEES.total_with_slippage)
                
                logger.info(
                    f"[止盈止损] {signal.code} 入场价={actual_entry_price:.4f} "
                    f"止损={stop_price:.4f} 止盈={take_profit_price:.4f} "
                    f"(含交易费用 {DEFAULT_FEES.total_with_slippage:.2%})"
                )
                
                # 创建持仓记录 (带止盈止损 + 信号关联)
                # 注意：加仓信号（is_add_position=True）不创建新持仓，
                # 由 data_subscriber._try_add_position 统一调用 add_to_position 更新均价
                is_add = signal.extra.get('is_add_position', False) if hasattr(signal, 'extra') and signal.extra else False
                if not is_add:
                    entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    action_str = "BUY" if signal.action == SignalAction.BUY else "SELL"
                    position_id = self.position_manager.create_position(
                        code=signal.code,
                        action=action_str,
                        quantity=qty,
                        entry_price=actual_entry_price,  # 用真实成交价，不用信号均价
                        entry_time=entry_time,
                        stop_loss=stop_price,
                        take_profit=take_profit_price,
                        signal_id=getattr(signal, 'id', None),
                        strategy=getattr(signal, 'strategy', None),
                    )
                    trade_type_label = 'OPEN'
                else:
                    # 加仓：position_id 指向已有持仓（get_position 取第一条 OPEN）
                    existing = self.position_manager.get_position(signal.code)
                    position_id = existing.id if existing else None
                    trade_type_label = 'ADD'
                    logger.info(f"[加仓下单] {signal.code} +{qty}股 @ {actual_entry_price:.4f}，跳过新建持仓记录")
                
                msg = f"[{market}] {trade_type_label}下单成功，order_id={order_id}, position_id={position_id}, 成交价={actual_entry_price:.4f}, 止损={stop_price:.4f}, 止盈={take_profit_price:.4f}"
                logger.info(msg)
                
                # P1-3: 记录执行质量监控 - 成交
                exec_monitor.record_fill(
                    record_id=record_id,
                    fill_price=actual_entry_price,
                    retry_count=0,
                )

                # P0 修复：记录成交到数据库 (同时更新 trades 和 positions 表)
                try:
                    self.record_trade_to_db(order_id, signal, actual_entry_price, pnl=0, status='OPEN')
                    logger.info(f"✅ [数据库] 开仓成交已记录：trade_id=trade_{order_id}")
                except Exception as e:
                    logger.error(f"❌ [数据库] 记录开仓成交失败：{e}", exc_info=True)

                # 记录成功交易 (重置熔断器计数)
                circuit_breaker.record_success()
                
                # ----------------------------------------------------------------
                # 断点2+5修复: 止盈止损管理器 + 持仓周期管理器集成 (2026-04-06)
                # 开仓成功后将持仓信息同步到两个管理器
                # ----------------------------------------------------------------
                # 获取质量等级 (从signal.extra中获取，或默认为C级)
                quality_class_str = "CLASS_C"  # 默认值
                if hasattr(signal, 'extra') and signal.extra:
                    quality_class_str = signal.extra.get('quality_class', 'CLASS_C')
                
                # 导入质量等级枚举
                try:
                    from app.factors.quality_scorer import QualityClass
                    quality_class = getattr(QualityClass, quality_class_str.upper(), QualityClass.CLASS_C)
                except Exception:
                    quality_class = None
                
                # 断点2: 止盈止损管理器 - 添加持仓记录
                # 参数: code, entry_price, quantity, quality_class, entry_time
                if self._profit_loss_manager is not None and not is_add and quality_class is not None:
                    try:
                        self._profit_loss_manager.add_position(
                            code=signal.code,
                            entry_price=actual_entry_price,
                            quantity=qty,
                            quality_class=quality_class,
                            entry_time=datetime.now(),
                        )
                        logger.info(f"[止盈止损管理] {signal.code} 持仓已添加，三层止盈架构生效")
                    except Exception as plm_e:
                        logger.warning(f"[止盈止损管理] {signal.code} 添加持仓失败: {plm_e}")
                
                # 断点5: 持仓周期管理器 - 添加持仓记录
                # 参数: code, quality_class, entry_price, entry_time
                if self._holding_period_manager is not None and not is_add and quality_class is not None:
                    try:
                        self._holding_period_manager.add_position(
                            code=signal.code,
                            quality_class=quality_class,
                            entry_price=actual_entry_price,
                            entry_time=datetime.now(),
                        )
                        logger.info(f"[持仓周期管理] {signal.code} 持仓已添加，质量等级={quality_class_str}")
                    except Exception as hpm_e:
                        logger.warning(f"[持仓周期管理] {signal.code} 添加持仓失败: {hpm_e}")
                
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position_id,
                    trade_type=trade_type_label,
                    message=msg
                )
            else:
                msg = f"[{market}] 开仓下单失败：{data}"
                logger.error(msg)

                # P1-3: 记录执行质量监控 - 拒绝
                exec_monitor.record_reject(
                    record_id=record_id,
                    reason=str(data),
                )

                # 记录失败 (2026-03-17 新增)
                failure_tracker.record_failure(signal.code, 'other', f"下单失败：{data}")
                circuit_breaker.record_reject('下单失败')
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 开仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            # 记录失败 (2026-03-17 新增)
            failure_tracker.record_failure(signal.code, 'other', f"执行异常：{e}")
            circuit_breaker.record_reject('执行异常')
            return TradeResult(success=False, message=msg)

    def _execute_close(self, signal: Signal) -> TradeResult:
        """
        执行平仓交易
        1. 获取持仓
        2. 富途下单平仓
        3. 更新持仓状态
        4. 计算已实现盈亏
        5. 返回 pnl
        
        2026-03-25 修复: 处理碎股问题，确保平仓数量是整手倍数
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        try:
            # 1. 获取持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                msg = f"[{market}] 平仓失败：未找到 {signal.code} 的持仓记录"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            # TrdSide, OrderType, TrdEnv 已在文件顶部导入

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 平仓方向与开仓相反
            if position.action == 'BUY':
                trd_side = TrdSide.SELL
            else:
                trd_side = TrdSide.BUY

            # 使用持仓的数量
            qty = position.quantity
            
            # 2026-03-25 修复碎股问题: 确保平仓数量是整手倍数
            if market == "HK":
                from app.config import DEFAULT_ORDER_QTY_BY_CODE
                lot_size = DEFAULT_ORDER_QTY_BY_CODE.get(signal.code) or config.DEFAULT_ORDER_QTY_HK
                original_qty = qty
                # 向下取整到最近的整手倍数
                qty = (qty // lot_size) * lot_size
                if qty <= 0:
                    msg = f"[{market}] 平仓失败：持仓 {original_qty} 股不足 1 手 ({lot_size} 股/手)，无法平仓"
                    logger.error(msg)
                    return TradeResult(success=False, message=msg)
                if qty < original_qty:
                    logger.warning(f"[碎股处理] {signal.code} 持仓 {original_qty} 股，调整为整手 {qty} 股 (每手 {lot_size} 股)，剩余 {original_qty - qty} 股碎股暂无法平仓")

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 平仓不做金额限制 - 平仓是减少风险的操作，不应被金额限制
            # 2026-03-24 修复: 移除平仓金额检查，解决止盈被拦截的问题
            # estimated_amount = signal.price * qty
            # if estimated_amount > config.MAX_ORDER_AMOUNT:
            #     msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝平仓"
            #     logger.warning(msg)
            #     return TradeResult(success=False, message=msg)

            logger.info(
                f"准备平仓：[{market}] {signal.code} {trd_side} "
                f"qty={qty} type={order_type} price={price} position_id={position.id}"
            )

            # 根据证券类型自动选择账户 (2026-03-31: 期权/正股双账户支持)
            acc_id = _get_account_for_code(signal.code, trd_env)

            # 诊断日志：记录平仓下单关键参数
            logger.info(f"[平仓下单] {signal.code} trd_side={trd_side} acc_id={acc_id} qty={qty} price={price}")

            ret, data = trd_ctx.place_order(
                price=price,
                qty=qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
                acc_id=acc_id,
            )

            # 平仓重试与退避机制：区分可重试/不可重试错误
            RETRYABLE_ERRORS = ["频率太高", "rate limit", "网络", "timeout", "系统繁忙"]
            NON_RETRYABLE_ERRORS = ["余额不足", "权限不足"]
            MAX_CLOSE_RETRIES = 2
            RETRY_DELAYS = [2, 5]  # 第1次重试等2秒，第2次等5秒

            for retry_i in range(MAX_CLOSE_RETRIES):
                if ret == 0:
                    break  # 成功，退出重试

                error_msg = str(data) if data is not None else ""

                # 证券类型错误特殊处理：尝试切换账户
                if any(kw in error_msg for kw in ["证券类型", "security type", "不支持此证券"]):
                    logger.error(f"[证券类型错误] {signal.code} 账户 {acc_id} 不支持此证券类型")
                    # 尝试切换到另一个账户
                    alternative_acc_ids = [18820895, 8246818]
                    switched = False
                    for alt_acc_id in alternative_acc_ids:
                        if alt_acc_id == acc_id:
                            continue
                        logger.info(f"[平仓] {signal.code} 尝试切换账户 {alt_acc_id}")
                        ret, data = trd_ctx.place_order(
                            price=price,
                            qty=qty,
                            code=signal.code,
                            trd_side=trd_side,
                            order_type=order_type,
                            trd_env=trd_env,
                            acc_id=alt_acc_id,
                        )
                        if ret == 0:
                            logger.info(f"[平仓] {signal.code} 账户 {alt_acc_id} 平仓成功")
                            acc_id = alt_acc_id
                            switched = True
                            break
                    if switched:
                        break  # 切换成功，退出重试
                    break  # 证券类型错误不再重试

                # 卖空错误特殊处理：尝试切换CASH账户
                if any(kw in error_msg for kw in ["卖空", "short selling", "short"]):
                    # P0 修复：使用备选账户列表
                    alternative_acc_ids = [18820895, 8246818]
                    try:
                        alt_acc = int(config.SIMULATE_ACCOUNT_ID)
                        if alt_acc not in alternative_acc_ids:
                            alternative_acc_ids.insert(0, alt_acc)
                    except: pass
                    
                    switched = False
                    for alt_acc_id in alternative_acc_ids:
                        if alt_acc_id == acc_id:
                            continue
                        logger.info(f"[平仓] {signal.code} 尝试账户 {alt_acc_id}")
                        ret, data = trd_ctx.place_order(
                            price=price,
                            qty=qty,
                            code=signal.code,
                            trd_side=trd_side,
                            order_type=order_type,
                            trd_env=trd_env,
                            acc_id=alt_acc_id,
                        )
                        if ret == 0:
                            logger.info(f"[平仓] {signal.code} 账户 {alt_acc_id} 平仓成功")
                            acc_id = alt_acc_id
                            switched = True
                            break
                    
                    if switched:
                        break  # 切换成功，退出重试

                # 不可重试错误直接退出
                if any(kw in error_msg for kw in NON_RETRYABLE_ERRORS):
                    logger.warning(f"[平仓] {signal.code} 不可重试错误: {error_msg}")
                    break

                # 可重试错误进行退避重试
                if any(kw in error_msg for kw in RETRYABLE_ERRORS):
                    delay = RETRY_DELAYS[retry_i]
                    logger.info(
                        f"[平仓重试] {signal.code} 第{retry_i+1}次重试，"
                        f"等待{delay}秒: {error_msg}"
                    )
                    time.sleep(delay)
                    ret, data = trd_ctx.place_order(
                        price=price,
                        qty=qty,
                        code=signal.code,
                        trd_side=trd_side,
                        order_type=order_type,
                        trd_env=trd_env,
                        acc_id=acc_id,
                    )
                else:
                    break  # 未知错误不重试

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 查询真实平仓成交价（同开仓逻辑）
                actual_exit_price = signal.price  # 默认值（当前价）
                try:
                    import time as _time
                    _time.sleep(0.3)
                    ret_order, order_data = trd_ctx.order_list_query(order_id=order_id, trd_env=trd_env)
                    if ret_order == 0 and len(order_data) > 0:
                        dealt_avg = float(order_data.iloc[0].get('dealt_avg_price', 0))
                        if dealt_avg > 0:
                            actual_exit_price = dealt_avg
                            logger.info(f"[真实平仓价] {signal.code} dealt_avg_price={dealt_avg:.4f}")
                except Exception as ep:
                    logger.debug(f"[平仓价查询失败] {signal.code}: {ep}")
                
                # 更新持仓状态并计算盈亏
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                pnl = self.position_manager.close_position(
                    code=signal.code,
                    exit_price=actual_exit_price,
                    exit_time=exit_time
                )
                
                # P0 修复：同步更新 trades 表 (确保与 positions 表一致)
                try:
                    from app.storage.database import get_database
                    db = get_database()
                    open_trades = db.get_open_trades()
                    updated = False
                    for trade in open_trades:
                        if trade['code'] == signal.code:
                            pnl_percent = pnl / (trade['entry_price'] * trade['quantity']) if trade['entry_price'] > 0 else 0
                            db.update_trade_exit(
                                trade_id=trade['id'],
                                exit_price=actual_exit_price,
                                pnl=pnl,
                                pnl_percent=pnl_percent,
                                exit_reason='normal_close'
                            )
                            logger.info(f"✅ [数据库] 交易记录已更新：trade_id={trade['id']}, pnl={pnl:.2f}, pnl%={pnl_percent:.2%}")
                            updated = True
                            break
                    
                    if not updated:
                        logger.warning(f"⚠️ [数据库] 未找到 {signal.code} 的 OPEN 交易记录，positions 表已平仓但 trades 表未同步")
                except Exception as e:
                    logger.error(f"❌ [数据库] 更新交易记录失败：{e}", exc_info=True)
                
                msg = f"[{market}] 平仓下单成功，order_id={order_id}, pnl={pnl:.2f}"
                logger.info(msg)

                # P0 修复：记录平仓成交到数据库
                try:
                    self.record_trade_to_db(order_id, signal, actual_exit_price, pnl)
                    logger.info(f"✅ 平仓成交已记录到数据库")
                except Exception as e:
                    logger.error(f"❌ 记录平仓成交失败：{e}")

                # ================================================================
                # 归因系统集成 (2026-04-09 新增): 记录交易结果到健康度追踪器
                # ================================================================
                try:
                    tracker = get_strategy_health_tracker()

                    # 从信号获取策略名称和市场状态
                    strategy = getattr(signal, 'strategy', 'unknown')
                    market_regime = 'neutral'
                    if hasattr(signal, 'extra') and signal.extra:
                        market_regime = signal.extra.get('market_regime', 'neutral')

                    # 获取持仓信息
                    position_obj = self.position_manager.get_position(signal.code)
                    entry_time = position_obj.entry_time if position_obj else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    entry_price = position_obj.entry_price if position_obj else actual_exit_price

                    # 创建健康度交易记录
                    health_trade = HealthTradeRecord(
                        trade_id=order_id,
                        strategy=strategy,
                        code=signal.code,
                        action=position_obj.action if position_obj else signal.action.value,
                        entry_price=entry_price,
                        exit_price=actual_exit_price,
                        pnl=pnl,
                        pnl_percent=pnl / (entry_price * position_obj.quantity) if position_obj and entry_price > 0 else 0,
                        entry_time=entry_time,
                        exit_time=datetime.now().isoformat(),
                        market_regime=market_regime,
                    )

                    # 记录到健康度追踪器
                    tracker.record_trade(health_trade)
                    logger.info(
                        f"[归因系统] {strategy} 交易已记录：{signal.code} "
                        f"盈亏={pnl:.2f} ({pnl/entry_price*100:.2f}%) 市场状态={market_regime}"
                    )
                except Exception as health_e:
                    logger.warning(f"[归因系统] 记录交易健康度失败：{health_e}")
                # ================================================================

                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position.id,
                    trade_type='CLOSE',
                    pnl=pnl,
                    message=msg
                )
            else:
                msg = f"[{market}] 平仓下单失败：{data}"
                logger.error(msg)
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 平仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            return TradeResult(success=False, message=msg)

    def execute_partial_close(self, signal: Signal, close_ratio: float = 0.5) -> TradeResult:
        """
        执行部分平仓
        :param signal: 交易信号
        :param close_ratio: 平仓比例 (0.5 = 50%)
        :return: TradeResult {success, order_id, pnl, close_ratio, remaining_quantity}
        """
        market = _detect_market(signal.code)
        currency = "US$" if market == "US" else "HK$"

        # 验证平仓比例
        if not 0 < close_ratio <= 1:
            msg = f"无效的平仓比例：{close_ratio}，必须在 (0, 1] 范围内"
            logger.error(msg)
            return TradeResult(success=False, message=msg)

        try:
            # 1. 获取持仓
            position = self.position_manager.get_position(signal.code)
            if position is None:
                msg = f"[{market}] 部分平仓失败：未找到 {signal.code} 的持仓记录"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            # TrdSide, OrderType, TrdEnv 已在文件顶部导入

            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL

            # 平仓方向与开仓相反
            if position.action == 'BUY':
                trd_side = TrdSide.SELL
            else:
                trd_side = TrdSide.BUY

            # 2. 计算平仓数量 = quantity * close_ratio
            close_qty = int(position.quantity * close_ratio)
            if close_qty < 1:
                close_qty = 1  # 至少平仓 1 股
            
            # 剩余数量
            remaining_qty = position.quantity - close_qty

            if config.DEFAULT_ORDER_TYPE == "MARKET":
                order_type = OrderType.MARKET
                price = 0
            else:
                order_type = OrderType.NORMAL
                price = signal.price

            # 安全检查：金额限制
            estimated_amount = signal.price * close_qty
            if estimated_amount > config.MAX_ORDER_AMOUNT:
                msg = f"预估金额 {currency}{estimated_amount:.2f} 超过限额 {currency}{config.MAX_ORDER_AMOUNT:.2f}, 拒绝部分平仓"
                logger.warning(msg)
                return TradeResult(success=False, message=msg)

            logger.info(
                f"准备部分平仓：[{market}] {signal.code} {trd_side} "
                f"close_qty={close_qty} (ratio={close_ratio}) remaining_qty={remaining_qty} "
                f"type={order_type} price={price} position_id={position.id}"
            )

            # 根据证券类型自动选择账户 (2026-03-31: 期权/正股双账户支持)
            acc_id = _get_account_for_code(signal.code, trd_env)
            ret, data = trd_ctx.place_order(
                price=price,
                qty=close_qty,
                code=signal.code,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=trd_env,
                acc_id=acc_id,
            )

            if ret == 0:
                order_id = str(data.iloc[0]["order_id"]) if len(data) > 0 else "unknown"
                
                # 4. 更新持仓 (减少数量，不改变状态)
                exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                pnl = self.position_manager.partial_close_position(
                    code=signal.code,
                    close_qty=close_qty,
                    exit_price=signal.price,
                    exit_time=exit_time
                )
                
                msg = f"[{market}] 部分平仓下单成功，order_id={order_id}, close_qty={close_qty}, remaining_qty={remaining_qty}, pnl={pnl:.2f}"
                logger.info(msg)
                
                # 5. 返回结果 (包含剩余数量)
                return TradeResult(
                    success=True,
                    order_id=order_id,
                    position_id=position.id,
                    trade_type='PARTIAL_CLOSE',
                    pnl=pnl,
                    message=msg,
                    close_ratio=close_ratio,
                    remaining_quantity=remaining_qty
                )
            else:
                msg = f"[{market}] 部分平仓下单失败：{data}"
                logger.error(msg)
                return TradeResult(success=False, message=msg)

        except Exception as e:
            msg = f"[{market}] 部分平仓交易执行异常：{e}"
            logger.error(msg, exc_info=True)
            return TradeResult(success=False, message=msg)

    def get_account_info(self, market: str = "HK") -> Optional[dict]:
        """获取账户信息"""
        try:
            from futu import TrdEnv
            trd_ctx = self._get_trd_ctx(market)
            trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = trd_ctx.accinfo_query(trd_env=trd_env)
            if ret == 0:
                return data.to_dict("records")[0] if len(data) > 0 else {}
            return None
        except Exception as e:
            logger.error(f"获取{market}账户信息失败：{e}")
            return None

    def close(self):
        if self._trd_ctx_hk:
            self._trd_ctx_hk.close()
            self._trd_ctx_hk = None
        if self._trd_ctx_us:
            self._trd_ctx_us.close()
            self._trd_ctx_us = None


# ============================================================================
# P0 修复：添加成交记录方法
# ============================================================================

# ============================================================================
# P0 修复：添加 record_trade_to_db 方法 (2026-03-30 13:19)
# ============================================================================

    def record_trade_to_db(self, order_id: str, signal, price: float = None, pnl: float = 0, status: str = 'OPEN'):
        """
        记录成交到数据库 (P0 修复)
        
        Args:
            order_id: 订单 ID
            signal: 交易信号
            price: 成交价 (可选)
            pnl: 盈亏 (平仓时使用)
        """
        import sqlite3
        from datetime import datetime
        from app import config
        
        DB_PATH = str(config.DATA_DIR / "ftseek.db")
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # 生成唯一 ID
            trade_id = f"trade_{order_id}"
            
            # 检查是否已存在
            cursor.execute("SELECT id FROM trades WHERE id = ?", (trade_id,))
            if cursor.fetchone():
                logger.info(f"⏭️  成交已存在：{trade_id}")
                conn.close()
                return
            
            # 准备数据
            code = signal.code
            # 修复：signal.action 是 SignalAction 枚举，不是字符串
            action = 'BUY' if signal.action == SignalAction.BUY else 'SELL'
            quantity = getattr(signal, 'quantity', 0)
            entry_price = price if price else getattr(signal, 'price', 0)
            strategy = getattr(signal, 'strategy', 'manual')
            entry_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 插入数据库
            cursor.execute("""
                INSERT INTO trades (
                    id, signal_id, code, name, action,
                    entry_price, exit_price, quantity,
                    pnl, pnl_percent, fees,
                    strategy, entry_time, exit_time,
                    exit_reason, order_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                f"signal_{trade_id}",
                code,
                '',
                action,
                entry_price,
                None,
                quantity,
                pnl,
                0,
                0,
                strategy,
                entry_time,
                None,
                '',
                order_id,
                status if status else ('OPEN' if action == 'BUY' else 'CLOSED')
            ))
            
            conn.commit()
            conn.close()
            
            logger.info(f"✅ [数据库] 成交记录成功：{code} {action} {quantity}股 @ {entry_price}")
            
        except Exception as e:
            logger.error(f"❌ [数据库] 记录成交失败：{e}", exc_info=True)
            raise


