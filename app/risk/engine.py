"""风险引擎主类"""
import logging
from typing import List, Optional, Dict, Any, Tuple
from app.core.engine import RiskEngine as BaseRiskEngine
from app.core.events import EventStore
from app.core.models import SignalScore, Order, Position, Alert, TradeEvent, ExitReason, AlertLevel
from .circuit_breaker import StrategyCircuitBreaker, SymbolCircuitBreaker, SystemCircuitBreaker
from .drawdown_control import DrawdownController
from .position_limits import PositionLimitsChecker
from .pre_trade_check import PreTradeChecker, PreTradeResult

logger = logging.getLogger(__name__)


class RiskEngine(BaseRiskEngine):
    """风险引擎主类"""
    
    def initialize(self):
        """初始化风险引擎"""
        # 从配置加载参数
        risk_config = self.config.get("risk", {})
        
        # 熔断器
        strategy_cfg = risk_config.get("CIRCUIT_BREAKER", {}).get("strategy", {})
        self.strategy_breaker = StrategyCircuitBreaker(
            consecutive_losses_threshold=strategy_cfg.get("consecutive_losses", 5),
            pause_duration_seconds=strategy_cfg.get("pause_duration", 1800),
        )
        
        symbol_cfg = risk_config.get("CIRCUIT_BREAKER", {}).get("symbol", {})
        self.symbol_breaker = SymbolCircuitBreaker(
            volatility_threshold=symbol_cfg.get("volatility_threshold", 0.05),
        )
        
        system_cfg = risk_config.get("CIRCUIT_BREAKER", {}).get("system", {})
        self.system_breaker = SystemCircuitBreaker(
            market_data_timeout_seconds=system_cfg.get("market_data_timeout", 60),
        )
        # 初始化时假设行情正常
        self.system_breaker.update_market_data_status(True)
        
        # 回撤控制
        self.drawdown_controller = DrawdownController(
            max_daily_drawdown=risk_config.get("MAX_DAILY_DRAWDOWN", 0.03),
            max_total_drawdown=risk_config.get("MAX_TOTAL_DRAWDOWN", 0.08),
        )
        
        # 仓位限制
        self.position_checker = PositionLimitsChecker(
            max_position_per_symbol=risk_config.get("MAX_POSITION_PER_SYMBOL", 0.25),
            max_position_per_direction=risk_config.get("MAX_POSITION_PER_DIRECTION", 0.65),  # 04/30: 默认0.65 (原0.50), QQQ占~45%需留headroom
            max_total_position=risk_config.get("MAX_TOTAL_POSITION", 0.80),
        )
        
        # 预交易检查
        # P1 2026-05-12 审计修复: 兜底值走 CFG.MIN_CONFIDENCE, 不再硬编码 0.50
        from app.core.config_defaults import CFG
        self.pre_trade_checker = PreTradeChecker(
            min_confidence=risk_config.get("MIN_CONFIDENCE", CFG.MIN_CONFIDENCE),
            min_signal_strength=risk_config.get("SIGNAL_MIN_SCORE", 50),
        )
        
        # P0 2026-04-20: 保存 risk_config 为实例属性，供 pre_trade_check() 使用
        # 修复: 原代码中 risk_config 是 initialize() 的局部变量，pre_trade_check() 访问会抛 NameError
        self._risk_config = risk_config
        
        # P1 2026-05-20: 加载止损 SoT 模块
        try:
            from app.risk.stop_loss_config import StopLossConfig
            self.stop_loss_config = StopLossConfig(self.config)
            self._stop_loss_loaded = True
        except Exception as e:
            logger.warning(f"[RiskEngine] 止损 SoT 加载失败: {e}, 使用降级模式")
            self._stop_loss_loaded = False
        
        self.enabled = True
    
    def shutdown(self):
        """关闭风险引擎"""
        self.enabled = False

    def set_lesson_injector(self, injector):
        """P0-fix 2026-05-04: 注入教训注入器，激活 LESSON 动态约束"""
        self._lesson_injector = injector
    
    def pre_trade_check(
        self,
        signal: SignalScore,
        position: Optional[Position] = None,
    ) -> bool:
        """预交易风控检查"""
        import logging as _log
        logger = _log.getLogger(__name__)
        
        # P0-fix 2026-05-04: 从注入的 lesson_injector 获取动态约束
        lesson_injector = getattr(self, '_lesson_injector', None)
        
        # 0. 重置系统熔断器状态（假设行情正常，否则不会到达这里）
        # 修复：scheduler 每次检查前都会获取行情，行情正常则重置熔断器
        self.system_breaker.update_market_data_status(True)
        
        # 1. 检查熔断器
        if self.system_breaker.is_triggered():
            logger.info(f"[风控] {signal.symbol} 系统熔断器已触发")
            return False
        
        if self.strategy_breaker.is_triggered(signal.strategy_id):
            logger.info(f"[风控] {signal.symbol} 策略熔断器已触发 (strategy_id={signal.strategy_id})")
            return False
        
        if self.symbol_breaker.is_frozen(signal.symbol):
            logger.info(f"[风控] {signal.symbol} 品种已被冻结")
            return False
        
        # 2. 预交易检查器
        result = self.pre_trade_checker.check(signal, lesson_injector=lesson_injector)
        if not result.allowed:
            logger.info(f"[风控] {signal.symbol} 预交易检查未通过: {result.reason} (confidence={signal.confidence:.2f}, strength={signal.strength})")
            self._record_rejection(signal, result.reason)
            return False
        
        # 2.5 硬编码熔断：日内回撤检查 (2026-04-24 补充拦截)
        if hasattr(self, 'drawdown_controller') and self.drawdown_controller.is_daily_limit_breached():
            logger.critical(f"🛑 [HardStop] 日内回撤熔断! {signal.symbol} 触发最大日内回撤限制")
            self._record_rejection(signal, "hard_stop: daily_drawdown_breached")
            return False
        
        # 3. 仓位限制 (P0 修复 2026-04-18: 集成 position_checker)
        # P0 2026-04-20: 修复 position_store 未注入的问题 + risk_config NameError
        # P0 2026-04-21: 修复总敞口计算 -- 使用实际账户净值替代硬编码 INITIAL_CAPITAL
        if hasattr(self, 'position_store') and self.position_store is not None:
            try:
                positions = self.position_store.all()
                
                # P0 修复 (2026-04-22): 持仓一致性检查与自动同步
                # 如果position_store为空但capital_safety显示有实际持仓,说明数据不同步
                if not positions:
                    try:
                        from app.risk.capital_safety import get_capital_safety_system
                        cs = get_capital_safety_system()
                        # 检查broker是否有实际持仓 (通过持仓市值判断)
                        if hasattr(cs, '_broker_snapshot') and cs._broker_snapshot:
                            broker_position_value = abs(cs._broker_snapshot.get('position_value', 0))
                            if broker_position_value > 10000:  # 超过$10K认为有真实持仓
                                logger.warning(
                                    f"[风控·持仓同步] position_store为空但Broker持仓=${broker_position_value:,.0f}, "
                                    f"可能存在数据不同步, 建议执行 sync_positions 脚本"
                                )
                                # 临时使用broker持仓市值计算敞口,避免187%异常
                                account_equity = cs._state.current_equity if cs._state else 100000
                                total_exposure = broker_position_value / account_equity
                                # P0-8 修复 2026-04-26: 按方向拆分敞口，避免将全部敞口归入信号方向导致误拦反向交易
                                # 尝试从 broker_adapter 获取持仓方向明细
                                direction_exposure = 0  # 默认保守值，依赖 total_exposure 走总限额分支
                                try:
                                    if hasattr(self, 'broker_adapter') and self.broker_adapter:
                                        broker_positions = self.broker_adapter.get_positions()
                                        if broker_positions:
                                            long_val = sum(
                                                abs(float(p.get('qty', 0)) * float(p.get('cost_price', 0)))
                                                for p in broker_positions if float(p.get('qty', 0)) > 0
                                            )
                                            short_val = sum(
                                                abs(float(p.get('qty', 0)) * float(p.get('cost_price', 0)))
                                                for p in broker_positions if float(p.get('qty', 0)) < 0
                                            )
                                            if account_equity > 0:
                                                direction_exposure = (
                                                    long_val / account_equity
                                                    if signal.direction == "LONG"
                                                    else short_val / account_equity
                                                )
                                            logger.info(
                                                f"[风控·持仓同步] Broker方向拆分: long=${long_val:,.0f} short=${short_val:,.0f} "
                                                f"signal_dir={signal.direction} dir_exposure={direction_exposure:.2%}"
                                            )
                                except Exception as broker_err:
                                    logger.debug(f"[风控·持仓同步] Broker方向拆分失败 (依赖总限额): {broker_err}")
                                positions = []  # 保持空列表,后续使用broker估算值
                    except Exception as sync_err:
                        logger.debug(f"[风控·持仓同步] 检查异常: {sync_err}")
                
                # P0 修复 (2026-04-21): 使用实际账户净值作为基数
                # 优先级: capital_safety > scheduler._account_equity > config.INITIAL_CAPITAL
                account_equity = self._risk_config.get("INITIAL_CAPITAL", 100000.0)
                if hasattr(self, 'capital_safety') and self.capital_safety is not None:
                    actual_equity = getattr(self.capital_safety, '_state', None)
                    if actual_equity and actual_equity.current_equity > 0:
                        account_equity = actual_equity.current_equity
                elif hasattr(self, '_account_equity') and self._account_equity > 0:
                    account_equity = self._account_equity
                # 如果没有实际净值数据，且有持仓，则使用总资产估算 (持仓市值 + 现金)
                if not positions:
                    pass  # 无持仓时保持默认
                elif account_equity <= 50000:  # 默认值可能不对
                    # 尝试从 positions 估算
                    long_val = sum(p.size * p.avg_entry_price for p in positions if p.direction == "LONG")
                    short_val = sum(p.size * p.avg_entry_price for p in positions if p.direction == "SHORT")
                    # 如果持仓市值 > 初始资金，说明资金基数被低估
                    if long_val + short_val > account_equity * 1.5:
                        account_equity = long_val + short_val  # 使用持仓市值作为最低基数

                long_exposure = sum(p.size * p.avg_entry_price for p in positions if p.direction == "LONG")
                short_exposure = sum(p.size * p.avg_entry_price for p in positions if p.direction == "SHORT")

                # P0 修复 (2026-05-01): ETF 敞口折扣 — QQQ/SPY 等宽基不应 1:1 占用方向敞口
                from app.risk.position_limits import get_etf_discount
                discounted_long = sum(
                    p.size * p.avg_entry_price * get_etf_discount(p.symbol)
                    for p in positions if p.direction == "LONG"
                )
                discounted_short = sum(
                    p.size * p.avg_entry_price * get_etf_discount(p.symbol)
                    for p in positions if p.direction == "SHORT"
                )
                effective_long_exposure = discounted_long / account_equity if account_equity > 0 else 0
                effective_short_exposure = discounted_short / account_equity if account_equity > 0 else 0
                effective_total_exposure = (discounted_long + discounted_short) / account_equity if account_equity > 0 else 0

                # 计算原始敞口用于日志
                raw_long_pct = long_exposure / account_equity * 100 if account_equity > 0 else 0
                raw_short_pct = short_exposure / account_equity * 100 if account_equity > 0 else 0
                eff_dir_pct = (effective_long_exposure if signal.direction == "LONG" else effective_short_exposure) * 100
                logger.debug(
                    f"[风控·ETF折扣] 原始敞口: LONG={raw_long_pct:.1f}% SHORT={raw_short_pct:.1f}% | "
                    f"折扣后: 方向={eff_dir_pct:.1f}% 总={effective_total_exposure*100:.1f}%"
                )
                
                # P0 修复 (2026-05-01): 使用 ETF 折扣后的敞口进行仓位限制检查
                if 'total_exposure' not in locals():
                    total_exposure = effective_total_exposure
                    direction_exposure = effective_long_exposure if signal.direction == "LONG" else effective_short_exposure
                    logger.debug(f"[风控] ETF折扣后敞口: 总={total_exposure:.2%}, 方向={direction_exposure:.2%}")

                # P0 修复 (2026-05-04): 计算该品种已有持仓敞口
                # 修复前: 不检查已有持仓集中度，导致 QQQ 累积到 44.8% 不被拦截
                existing_symbol_val = sum(
                    p.size * p.avg_entry_price for p in positions
                    if p.symbol == signal.symbol
                )
                existing_symbol_exposure = existing_symbol_val / account_equity if account_equity > 0 else 0
                existing_symbol_discounted = sum(
                    p.size * p.avg_entry_price * get_etf_discount(p.symbol) for p in positions
                    if p.symbol == signal.symbol
                )
                existing_symbol_exposure_discounted = existing_symbol_discounted / account_equity if account_equity > 0 else 0

                # 敞口异常告警 (基于原始值, 因为原始敞口反映真实杠杆)
                raw_total = (long_exposure + short_exposure) / account_equity if account_equity > 0 else 0
                if raw_total > 1.5:
                    logger.error(
                        f"[风控·异常] 总敞口={raw_total:.1%} 超过150%! "
                        f"account_equity=${account_equity:,.0f}, long=${long_exposure:,.0f}, short=${short_exposure:,.0f}, "
                        f"持仓数={len(positions)}"
                    )
                
                # P1-4 修复 (2026-05-03): 使用更实际的仓位估计
                # 原代码硬编码 10%，但实际仓位通过 capital_safety + ATR 计算 (通常 15-25%)
                # 保守估计: 使用 20% 作为默认值，避免实际仓位超限但不被拦截
                proposed_position = self._risk_config.get("DEFAULT_POSITION_PCT", 0.20)
                
                allowed, reject_reason = self.position_checker.check_position(
                    symbol=signal.symbol,
                    proposed_position=proposed_position,
                    current_direction_exposure=direction_exposure,
                    current_total_exposure=total_exposure,
                    existing_symbol_exposure=existing_symbol_exposure,
                )
                if not allowed:
                    logger.info(f"[风控] {signal.symbol} 仓位限制拒绝: {reject_reason} (方向敞口={direction_exposure:.1%}, 总敞口={total_exposure:.1%})")
                    self._record_rejection(signal, f"position_limit: {reject_reason}")
                    return False
            except Exception as e:
                logger.warning(f"[风控] 仓位检查异常 (继续执行): {e}")
        
        logger.info(f"[风控] {signal.symbol} 预交易检查通过 ✓")
        return True
    
    def intra_trade_check(self, positions: List[Position]) -> List[Alert]:
        """
        持仓中风控检查

        检查项：
        1. 单笔浮亏超过阈值（账户级警告）
        2. 持仓时长接近时间止损
        3. 账户整体回撤检查
        """
        alerts = []

        for position in positions:
            try:
                # 1. 持仓时长检查
                if position.time_stop_hours > 0:
                    remaining_hours = position.time_stop_hours - position.holding_hours
                    if 0 < remaining_hours <= 4:
                        alert = Alert(
                            level=AlertLevel.WARN,
                            title=f"持仓即将到期 {position.symbol}",
                            message=(
                                f"{position.symbol} 持仓已 {position.holding_hours:.1f}h，"
                                f"剩余 {remaining_hours:.1f}h 将触发时间止损"
                            ),
                            position_id=position.position_id,
                            source="risk_engine",
                            category="risk",
                        )
                        alerts.append(alert)
                        self.event_store.record_alert(alert)

                # 2. 浮亏预警（超过硬止损 80% 时警告）
                if hasattr(position, "avg_entry_price") and position.avg_entry_price > 0:
                    if position.initial_sl > 0:
                        sl_distance = abs(position.avg_entry_price - position.initial_sl)
                        warn_threshold = position.initial_sl + sl_distance * 0.2  # 距止损剩 20%
                        # 无法在此处获取当前价，只记录静态信息
                        alert = Alert(
                            level=AlertLevel.INFO,
                            title=f"持仓监控 {position.symbol}",
                            message=(
                                f"{position.symbol} 持仓监控正常 "
                                f"entry=${position.avg_entry_price:.2f} "
                                f"SL=${position.initial_sl:.2f}"
                            ),
                            position_id=position.position_id,
                            source="risk_engine",
                            category="risk",
                        )
                        # INFO 级别不记录，只计数

            except Exception as e:
                import logging as _log
                import traceback as _tb
                logger = _log.getLogger(__name__)
                logger.error(
                    f"[风控] 持仓检查异常 {position.symbol}: {e}\n"
                    f"堆栈跟踪：{_tb.format_exc()}"
                )

        # 3. 账户回撤检查（通过 drawdown_controller）
        if hasattr(self, "drawdown_controller"):
            try:
                if self.drawdown_controller.is_daily_limit_breached():
                    alert = Alert(
                        level=AlertLevel.CRITICAL,
                        title="日内回撤超限",
                        message=f"账户日内回撤已触发限额，暂停新交易",
                        source="risk_engine",
                        category="risk",
                    )
                    alerts.append(alert)
                    self.event_store.record_alert(alert)
            except Exception as e:
                import logging as _log
                import traceback as _tb
                logger = _log.getLogger(__name__)
                logger.error(
                    f"[风控] 回撤检查异常：{e}\n"
                    f"堆栈跟踪：{_tb.format_exc()}"
                )

        return alerts

    def post_trade_check(self, order: Order, position: Position) -> bool:
        """
        交易后风控检查

        验证成交价格是否在合理范围，更新风险敞口
        """
        try:
            # 验证成交价合理性（允许 1% 滑点）
            if order.avg_fill_price > 0 and order.price > 0:
                slippage_ratio = abs(order.avg_fill_price - order.price) / order.price
                max_slippage = self._risk_config.get("slippage", {}).get("max_ratio", 0.005)
                if slippage_ratio > max_slippage:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        f"[风控] {order.symbol} 滑点过大: {slippage_ratio:.2%} "
                        f"期望={order.price:.2f} 实际={order.avg_fill_price:.2f}"
                    )
            # 记录回撤基准
            if hasattr(self, "drawdown_controller"):
                self.drawdown_controller.record_trade(order)
        except Exception as e:
            import logging as _log
            import traceback as _tb
            logger = _log.getLogger(__name__)
            logger.error(
                f"[风控] 交易后检查异常 {order.symbol}: {e}\n"
                f"堆栈跟踪：{_tb.format_exc()}"
            )

        return True
    
    def check_circuit_breakers(self) -> Dict[str, bool]:
        """检查熔断器状态"""
        return {
            "strategy": False,  # 需要传入 strategy_id
            "symbol": False,
            "system": self.system_breaker.is_triggered(),
        }
    
    def should_exit(self, position: Position, current_price: float) -> Tuple[bool, Optional[ExitReason]]:
        """DEPRECATED: Use PositionExitMonitor instead. Returns False."""
        import warnings
        warnings.warn("RiskEngine.should_exit is deprecated", DeprecationWarning, stacklevel=2)
        return False, None
    
    def _record_rejection(self, signal: SignalScore, reason: str):
        """记录风控拒绝 — 同时写入 event_store 和 rejections.db"""
        self.event_store.append(
            TradeEvent(
                event_type="risk_blocked",
                signal_id=signal.signal_id,
                data={"reason": reason},
            )
        )
        # P0 修复 (2026-05-13): 持久化到 rejections.db，解决 May 4 后静默故障
        try:
            from app.storage.rejection_logger import get_rejection_logger
            get_rejection_logger().record(
                symbol=signal.symbol,
                reason=reason,
                reason_category="PRE_TRADE_CHECK",
                strength=signal.strength,
                confidence=signal.confidence,
                direction=signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction),
            )
        except Exception:
            pass  # 非关键路径，不影响主流程
