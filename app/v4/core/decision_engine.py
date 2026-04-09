"""
DecisionEngine V3 - 统一决策引擎 (自动模拟交易)

核心逻辑：信号通过限流和时段检查后 → **直接执行模拟交易** → 推送钉钉通知

**重要**: 无需确认，自动执行！纯通知模式。
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, List
from dataclasses import dataclass
from enum import Enum

from app.v4.models.trading_models import TradingDatabase, TradingMode as DBTradingMode
from app.v4.core.rate_limiter import SignalRateLimiter
from app.v4.core.session_manager import MarketSessionManager
from app.v4.trader.paper_trader import PaperTradingEngine, PaperPosition, PaperTrade
from app.v4.core.exit_manager import AutoExitManager, ExitDecision
from app.v4.notifier.dingtalk_v4 import DingTalkNotifierV4
from app.v4.risk.pre_trade import PreTradeRisk, PreTradeRiskReport
from app.strategy.base import SignalAction
from app.storage.trade_recorder import get_trade_chain_recorder
from app.strategy.indicators import rsi, macd


logger = logging.getLogger("ftseek.v4.core.decision_engine")


class TradingMode(str, Enum):
    """交易模式枚举"""
    PAPER = "PAPER"
    HYBRID = "HYBRID"
    LIVE = "LIVE"
    DISABLED = "DISABLED"
    
    @classmethod
    def from_string(cls, value: str) -> Optional['TradingMode']:
        try:
            return cls(value.upper())
        except (ValueError, AttributeError):
            return None


@dataclass
class DecisionResult:
    """决策结果"""
    accepted: bool
    reason: str
    mode: TradingMode
    executed: bool
    trade: Optional[PaperTrade] = None
    exit_decision: Optional[ExitDecision] = None
    message: str = ""
    notification_sent: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "mode": self.mode.value,
            "executed": self.executed,
            "trade": self.trade.to_dict() if self.trade else None,
            "exit_decision": self.exit_decision.to_dict() if self.exit_decision else None,
            "message": self.message,
            "notification_sent": self.notification_sent,
        }


class DecisionEngine:
    """统一决策引擎 V3 - 自动模拟交易"""

    def __init__(self, db_path: str, config: Dict[str, Any] = None, dingtalk_webhook: str = None,
                 market_data_provider: callable = None):
        """
        初始化 DecisionEngine

        Args:
            db_path: 数据库路径
            config: 配置字典
            dingtalk_webhook: 钉钉 webhook URL
            market_data_provider: 市场数据提供者回调函数 (code, period) -> List[Dict]
                                  返回 K 线数据列表，每个元素包含 {open, high, low, close, volume}
        """
        self.config = config or {}
        self.db_path = db_path
        self.db = TradingDatabase(db_path)

        self.rate_limiter = SignalRateLimiter(
            max_signals_per_hour=self.config.get('max_signals_per_hour', 2),
            cooldown_minutes=self.config.get('cooldown_minutes', 15)
        )
        self.session_manager = MarketSessionManager()

        slippage = self.config.get('slippage', 0.005)
        self.paper_trader = PaperTradingEngine(db_path=db_path, slippage=slippage)

        self.exit_manager = AutoExitManager(
            target_profit=self.config.get('target_profit', 0.05),
            stop_loss=self.config.get('stop_loss', 0.03),
            timeout_hours=self.config.get('timeout_hours', 24)
        )

        self.dingtalk_notifier = DingTalkNotifierV4(
            webhook_url=dingtalk_webhook or self.config.get('dingtalk_webhook'),
            enable_push=dingtalk_webhook is not None or self.config.get('dingtalk_webhook') is not None
        )

        # 交易前风控系统 (2026-04-09 新增)
        self.pre_trade_risk = PreTradeRisk(config={
            'max_daily_trades': self.config.get('max_daily_trades', 10),
            'max_single_position': self.config.get('max_single_position', 0.20),
            'max_total_position': self.config.get('max_total_position', 0.60),
            'blacklist': self.config.get('blacklist', []),
        })

        # TradeChain 映射表：code -> trade_chain_id (用于退出时查找)
        self._trade_chain_map: Dict[str, str] = {}

        # 账户信息缓存 (用于风控检查)
        self._account_info_cache: Dict[str, Any] = {}

        # 市场数据提供者 (用于获取 RSI/MACD 指标)
        self.market_data_provider = market_data_provider

        logger.info(f"DecisionEngine V3 初始化完成，滑点={slippage*100:.2f}%")
        logger.info(f"交易前风控已启用：日交易限制={self.pre_trade_risk.config['max_daily_trades']}次, "
                   f"单股仓位≤{self.pre_trade_risk.config['max_single_position']*100:.0f}%, "
                   f"总仓位≤{self.pre_trade_risk.config['max_total_position']*100:.0f}%")
    
    def process_signal(self, signal: Dict[str, Any]) -> DecisionResult:
        """处理交易信号 (自动执行，无需确认)"""
        try:
            code = signal.get('code')
            action = signal.get('action')
            name = signal.get('name', '')
            price = signal.get('price', 0.0)
            quantity = signal.get('quantity', 0)
            strategy = signal.get('strategy', 'unknown')
            reason = signal.get('reason', '')
            timestamp = signal.get('timestamp', datetime.now())

            logger.info(f"收到信号：{code} {action.value} @ {price}")

            # 1. 全局开关
            if not self.rate_limiter.is_global_enabled():
                return DecisionResult(False, "GLOBAL_DISABLED", self.get_mode(code), False, message="全局禁用")

            # 2. 限流检查
            if not self.rate_limiter.should_allow(code, action):
                status = self.rate_limiter.get_status(code)
                return DecisionResult(False, "RATE_LIMITED", self.get_mode(code), False, message=status.get('reason', '限流'))

            # 3. 时段检查
            if not self.session_manager.is_trading_session(code, timestamp):
                return DecisionResult(False, "NOT_IN_SESSION", self.get_mode(code), False, message="非交易时段")

            # 4. 获取模式
            mode = self.get_mode(code)

            # 5. 检查禁用
            if mode == TradingMode.DISABLED:
                return DecisionResult(False, "MODE_DISABLED", mode, False, message="模式禁用")

            # 6. 交易前风控检查 (2026-04-09 新增)
            risk_report = self._perform_pre_trade_risk_check(signal)
            if not risk_report.all_passed:
                failed_checks = [c for c in risk_report.checks if not c.passed]
                logger.warning(f"风控检查失败：{failed_checks[0].message}")
                return DecisionResult(False, "RISK_CHECK_FAILED", mode, False,
                                     message=f"风控检查失败：{failed_checks[0].message}")

            # 7. 执行交易
            if mode in [TradingMode.PAPER, TradingMode.LIVE]:
                result = self._execute_paper_trade(signal)
            else:
                return DecisionResult(False, "UNKNOWN_MODE", mode, False, message=f"未知模式：{mode}")

            # 8. 检查自动退出
            if result.accepted and result.executed:
                exit_result = self._check_auto_exit(code)
                if exit_result:
                    result.exit_decision = exit_result

            return result

        except Exception as e:
            logger.exception(f"处理信号异常：{e}")
            return DecisionResult(False, f"ERROR: {e}", TradingMode.PAPER, False, message=str(e))

    def _perform_pre_trade_risk_check(self, signal: Dict[str, Any]) -> PreTradeRiskReport:
        """
        执行交易前风控检查 (2026-04-09 新增)

        Args:
            signal: 交易信号字典

        Returns:
            PreTradeRiskReport: 风控检查报告
        """
        account_info = self._get_account_info(signal.get('code', ''))

        # 构建风控检查所需的信号格式
        risk_signal = {
            'symbol': signal.get('code', ''),
            'quantity': signal.get('quantity', 0),
            'price': signal.get('price', 0.0),
            'side': 'buy' if signal.get('action') == SignalAction.BUY else 'sell',
        }

        return self.pre_trade_risk.full_check(signal=risk_signal, account_info=account_info)

    def _get_account_info(self, code: str) -> Dict[str, Any]:
        """
        获取账户信息 (用于风控检查)

        Args:
            code: 股票代码

        Returns:
            账户信息字典：
            - available_cash: 可用资金
            - positions: 当前持仓
            - total_capital: 总资金
            - daily_trades: 当日已交易次数
        """
        # 尝试从缓存获取 (简化实现，实际应从数据库获取)
        if code in self._account_info_cache:
            return self._account_info_cache[code]

        # 获取所有持仓 (带异常处理)
        positions_dict = {}
        total_position_value = 0.0

        try:
            all_positions = self.get_all_positions()
            for pos in all_positions:
                if hasattr(pos, 'code') and hasattr(pos, 'quantity') and hasattr(pos, 'avg_cost'):
                    positions_dict[pos.code] = {
                        'quantity': pos.quantity,
                        'price': pos.avg_cost,
                    }
                    total_position_value += pos.quantity * pos.avg_cost
        except Exception as e:
            # 数据库未初始化或无数据时使用默认值
            logger.debug(f"获取持仓失败 (可能数据库未初始化): {e}")
            all_positions = []

        # 获取账户总资金 (简化：假设总资金 = 持仓市值 + 现金)
        # 实际应从账户 API 获取
        total_capital = self.config.get('initial_capital', 1000000.0)
        available_cash = total_capital - total_position_value

        # 获取当日交易次数 (从 rate_limiter 获取)
        daily_trades = len(self.rate_limiter._signal_history) if hasattr(self.rate_limiter, '_signal_history') else 0

        account_info = {
            'available_cash': max(0, available_cash),
            'positions': positions_dict,
            'total_capital': total_capital,
            'daily_trades': daily_trades,
        }

        # 缓存账户信息
        self._account_info_cache[code] = account_info

        return account_info

    def _get_technical_indicators(self, code: str) -> Dict[str, float]:
        """
        获取技术指标 (RSI, MACD)

        通过 market_data_provider 回调获取历史 K 线数据，并计算技术指标。
        如果无法获取数据，返回中性默认值。

        Args:
            code: 股票代码

        Returns:
            Dict[str, float]: 指标字典 {rsi, macd_histogram}
        """
        # 默认中性值
        default_indicators = {'rsi': 50, 'macd_histogram': 0}

        # 如果没有提供数据源，返回默认值
        if not self.market_data_provider:
            logger.debug(f"未配置 market_data_provider，使用默认指标：{code}")
            return default_indicators

        try:
            # 获取历史 K 线数据 (至少 35 根，用于 MACD 计算：26+9)
            klines = self.market_data_provider(code, period=50)
            if not klines or len(klines) < 35:
                logger.debug(f"K 线数据不足 ({len(klines) if klines else 0}根)，使用默认指标：{code}")
                return default_indicators

            # 提取收盘价
            closes = [k.get('close', 0) for k in klines]
            if not closes or min(closes) <= 0:
                return default_indicators

            # 计算 RSI(14)
            rsi_val = rsi(closes, period=14)
            if rsi_val is None:
                rsi_val = 50.0

            # 计算 MACD(12,26,9)
            macd_result = macd(closes, fast=12, slow=26, signal=9)
            if macd_result is None:
                macd_histogram = 0.0
            else:
                macd_histogram = macd_result[2]  # histogram = macd_line - signal_line

            return {'rsi': rsi_val, 'macd_histogram': macd_histogram}

        except Exception as e:
            logger.exception(f"计算技术指标失败：{code}, error={e}")
            return default_indicators

    def _execute_paper_trade(self, signal: Dict[str, Any]) -> DecisionResult:
        """执行 PAPER 模式交易"""
        code = signal.get('code')
        name = signal.get('name', '')
        action = signal.get('action')
        price = signal.get('price', 0.0)
        quantity = signal.get('quantity', 0)
        strategy = signal.get('strategy', 'unknown')
        reason = signal.get('reason', '')
        timestamp = signal.get('timestamp', datetime.now())

        # 提取 TradeChain ID (从信号层传递过来)
        trade_chain_id = signal.get('_trade_chain_id')

        try:
            if action == SignalAction.BUY:
                trade = self.paper_trader.buy(code, quantity, price, timestamp)
            elif action == SignalAction.SELL:
                trade = self.paper_trader.sell(code, quantity, price, timestamp)
            else:
                return DecisionResult(False, "UNKNOWN_ACTION", TradingMode.PAPER, False, message=f"未知动作：{action}")

            self.rate_limiter.record_signal(code, action)

            # 记录执行层数据并保存 trade_chain_id 映射
            if trade_chain_id:
                self._record_execution_chain(trade_chain_id, price, trade.price, trade)
                self._trade_chain_map[code] = trade_chain_id
                logger.debug(f"已保存 TradeChain 映射：{code} -> {trade_chain_id}")

            position = self.get_position(code)

            notification_sent = self._send_trade_notification(
                code, name, action, price, trade.price, quantity,
                strategy, reason, position, timestamp
            )

            return DecisionResult(
                True, "ACCEPTED", TradingMode.PAPER, True, trade=trade,
                message=f"已执行：{code} {action.value}", notification_sent=notification_sent
            )

        except ValueError as e:
            return DecisionResult(False, f"ERROR: {e}", TradingMode.PAPER, False, message=str(e))
        except Exception as e:
            logger.exception(f"执行失败：{e}")
            return DecisionResult(False, f"ERROR: {e}", TradingMode.PAPER, False, message=str(e))
    
    def _send_trade_notification(self, code, name, action, market_price, exec_price,
                                  quantity, strategy, reason, position, timestamp) -> bool:
        """推送钉钉交易通知"""
        try:
            slippage_pct = ((exec_price - market_price) / market_price) * 100 if market_price > 0 else 0
            target_price = exec_price * 1.05
            stop_loss_price = exec_price * 0.97
            
            action_text = "买入" if action == SignalAction.BUY else "卖出"
            action_icon = "🟢" if action == SignalAction.BUY else "🔴"
            
            position_info = ""
            market_value = ""
            if position:
                position_info = f"**当前持仓**: {position.quantity} 股\n**平均成本**: HK${position.avg_cost:.2f}\n"
                curr_price = position.current_price if position.current_price > 0 else exec_price
                market_value = f"\n💰 持仓市值：HK${curr_price * position.quantity:,.0f}"
            
            markdown = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n{action_icon} **{code} {action_text}成交**\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**标的**: {code} {name}\n**方向**: {action_text}\n**市价**: HK${market_price:.2f}\n"
                f"**成交价**: HK${exec_price:.2f} (含{slippage_pct:+.2f}%滑点)\n**数量**: {quantity} 股\n"
                f"**策略**: {strategy}\n**原因**: {reason}\n\n{position_info}"
                f"**止盈目标**: HK${target_price:.2f} (+5%)\n**止损价格**: HK${stop_loss_price:.2f} (-3%)\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n🕐 成交时间：{timestamp.strftime('%H:%M:%S')}{market_value}\n━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            result = self.dingtalk_notifier.send(markdown, msg_type="markdown")
            return result.get('success', False)
        except Exception as e:
            logger.exception(f"发送通知失败：{e}")
            return False

    def _record_execution_chain(self, trade_chain_id: str, expected_price: float,
                                 actual_price: float, trade) -> None:
        """记录执行层数据到 TradeChain"""
        try:
            recorder = get_trade_chain_recorder()

            # 计算滑点 (基点)
            slippage_bps = ((actual_price - expected_price) / expected_price) * 10000

            # 记录执行层数据
            recorder.record_execution(trade_chain_id, {
                'expected_price': expected_price,
                'actual_fill_price': actual_price,
                'slippage_bps': round(slippage_bps, 2),
                'execution_latency_ms': 100,  # 模拟延迟
                'fill_reason': 'market',
                'commission': 5.0,  # 最低收费
                'stamp_duty': 0.0,
                'transaction_fee': 0.5,
                'total_cost': 5.5,
            })

            logger.debug(f"已记录执行层数据：trade_chain_id={trade_chain_id}, slippage={slippage_bps:.2f}bps")
        except Exception as e:
            logger.exception(f"记录执行层失败：{e}")

    def _check_auto_exit(self, code: str) -> Optional[ExitDecision]:
        """检查自动退出"""
        position = self.get_position(code)
        if not position:
            return None

        if position.current_price > 0:
            self.exit_manager.update_peak_price(code, position.current_price)

        # 获取技术指标 (用于动态持仓判断)
        indicators = self._get_technical_indicators(code)

        exit_decision = self.exit_manager.check_exit(position, indicators=indicators)

        if exit_decision and exit_decision.should_exit:
            logger.info(f"触发自动退出：{code}, 原因={exit_decision.reason}")
            try:
                current_price = position.current_price if position.current_price > 0 else position.avg_cost
                trade = self.paper_trader.sell(code, position.quantity, current_price)

                # 记录退出数据 (完成 TradeChain)
                self._record_exit_chain(code, position, exit_decision, trade)

                self._send_exit_notification(code, position, exit_decision, trade)
                self.exit_manager.clear_peak_price(code)
            except Exception as e:
                logger.exception(f"自动退出失败：{e}")

        return exit_decision
    
    def _send_exit_notification(self, code, position, exit_decision, trade) -> bool:
        """推送退出通知"""
        try:
            reason_map = {
                "TARGET_PROFIT": ("✅ 止盈退出", "🟢"),
                "STOP_LOSS": ("🛑 止损退出", "🔴"),
                "TIMEOUT": ("⏰ 超时退出", "🟡"),
            }
            exit_text, icon = reason_map.get(exit_decision.reason, ("退出", "⚪"))
            
            markdown = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n{icon} **{code} {exit_text}**\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**退出原因**: {exit_decision.reason}\n**成交价**: HK${trade.price:.2f}\n"
                f"**数量**: {trade.quantity} 股\n**平均成本**: HK${position.avg_cost:.2f}\n\n"
                f"**实现盈亏**: HK${exit_decision.expected_pnl:+.2f} ({exit_decision.expected_return*100:+.2f}%)\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n🕐 退出时间：{datetime.now().strftime('%H:%M:%S')}\n━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            result = self.dingtalk_notifier.send(markdown, msg_type="markdown")
            return result.get('success', False)
        except Exception as e:
            logger.exception(f"发送退出通知失败：{e}")
            return False

    def _record_exit_chain(self, code: str, position, exit_decision, trade) -> None:
        """记录退出数据 (完成 TradeChain)"""
        try:
            recorder = get_trade_chain_recorder()

            # 从映射表查找 trade_chain_id
            trade_chain_id = self._trade_chain_map.get(code)
            if not trade_chain_id:
                logger.warning(f"未找到 code={code} 的 TradeChain，跳过记录")
                return

            # 计算持仓时间 (从 position 入仓时间到当前)
            from datetime import datetime
            holding_minutes = 0
            if hasattr(position, 'entry_time'):
                try:
                    entry = datetime.fromisoformat(str(position.entry_time))
                    holding_minutes = int((datetime.now() - entry).total_seconds() / 60)
                except Exception:
                    holding_minutes = 0

            # 计算最大浮盈/浮亏 (简化版本，实际应从持仓监控获取)
            max_profit = max(0, exit_decision.expected_pnl)
            max_loss = min(0, exit_decision.expected_pnl)

            # 完成 TradeChain
            recorder.complete_chain(trade_id=trade_chain_id, result_data={
                'exit_price': trade.price,
                'exit_reason': exit_decision.reason,
                'pnl': exit_decision.expected_pnl,
                'pnl_percent': exit_decision.expected_return,
                'holding_minutes': holding_minutes,
                'max_profit': max_profit,
                'max_loss': max_loss,
                'total_cost': 5.5,  # 手续费
            })

            # 清除映射 (交易已完成)
            del self._trade_chain_map[code]

            logger.info(f"已完成 TradeChain: {trade_chain_id}, pnl={exit_decision.expected_pnl:.2f}")
        except Exception as e:
            logger.exception(f"记录退出数据失败：{e}")

    def set_mode(self, code: str, mode: TradingMode) -> None:
        """设置交易模式"""
        existing = self.db.get_trading_mode(code)
        if existing:
            self.db.update_trading_mode(code, DBTradingMode.from_string(mode.value))
        else:
            self.db.create_trading_mode(code, DBTradingMode.from_string(mode.value))
        logger.info(f"设置模式：{code} → {mode.value}")
    
    def get_mode(self, code: str) -> TradingMode:
        """获取交易模式"""
        mode_record = self.db.get_trading_mode(code)
        if mode_record:
            return TradingMode.from_string(mode_record.mode.value)
        return TradingMode.PAPER
    
    def get_position(self, code: str) -> Optional[PaperPosition]:
        """获取持仓"""
        return self.paper_trader.get_position(code)
    
    def get_all_positions(self) -> List[PaperPosition]:
        """获取所有持仓"""
        return self.paper_trader.get_all_positions()
    
    def manual_exit(self, code: str, quantity: int = None) -> DecisionResult:
        """手动退出"""
        try:
            position = self.get_position(code)
            if not position:
                return DecisionResult(False, "NO_POSITION", self.get_mode(code), False, message=f"无持仓：{code}")
            
            if quantity is None:
                quantity = position.quantity
            if quantity > position.quantity:
                return DecisionResult(False, "INSUFFICIENT_POSITION", self.get_mode(code), False, message="数量超限")
            
            current_price = position.current_price if position.current_price > 0 else position.avg_cost
            trade = self.paper_trader.sell(code, quantity, current_price)
            self.exit_manager.clear_peak_price(code)
            
            return DecisionResult(True, "MANUAL_EXIT", self.get_mode(code), True, trade=trade, message=f"手动退出：{code}")
        except Exception as e:
            return DecisionResult(False, f"ERROR: {e}", self.get_mode(code), False, message=str(e))
    
    def check_all_exits(self) -> List[ExitDecision]:
        """检查所有持仓退出并执行"""
        positions = self.get_all_positions()
        exit_decisions = []
        for position in positions:
            if position.current_price > 0:
                self.exit_manager.update_peak_price(position.code, position.current_price)

            # 获取技术指标 (用于动态持仓判断)
            indicators = self._get_technical_indicators(position.code)

            exit_decision = self.exit_manager.check_exit(position, indicators=indicators)
            
            if exit_decision and exit_decision.should_exit:
                logger.info(f"触发自动退出：{position.code}, 原因={exit_decision.reason}")
                try:
                    current_price = position.current_price if position.current_price > 0 else position.avg_cost
                    trade = self.paper_trader.sell(position.code, position.quantity, current_price)
                    self._send_exit_notification(position.code, position, exit_decision, trade)
                    self.exit_manager.clear_peak_price(position.code)
                    exit_decisions.append(exit_decision)
                except Exception as e:
                    logger.exception(f"自动退出失败：{e}")
            elif exit_decision and exit_decision.reason == "HOLD_STRONG":
                logger.info(f"继续持仓：{position.code}, {exit_decision.message}")
        return exit_decisions
    
    def check_expired_confirmations(self) -> List[Dict[str, Any]]:
        """V3 无需确认"""
        return []
    
    def get_engine_status(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            "pending_confirmations": 0,
            "rate_limiter_status": "enabled" if self.rate_limiter.is_global_enabled() else "disabled",
            "exit_config": self.exit_manager.get_exit_config(),
            "positions_count": len(self.get_all_positions()),
        }
    
    def close(self):
        """关闭"""
        self.paper_trader.close()


_decision_engine: Optional[DecisionEngine] = None


def init_decision_engine(
    db_path: str,
    config: Dict[str, Any] = None,
    dingtalk_webhook: str = None,
    market_data_provider: callable = None
) -> DecisionEngine:
    """
    初始化 DecisionEngine 单例

    Args:
        db_path: 数据库路径
        config: 配置字典
        dingtalk_webhook: 钉钉 webhook URL
        market_data_provider: 市场数据提供者回调函数 (code, period) -> List[Dict]
                              用于获取 RSI/MACD 指标数据

    Returns:
        DecisionEngine: 初始化后的实例
    """
    global _decision_engine
    _decision_engine = DecisionEngine(db_path, config, dingtalk_webhook, market_data_provider)
    return _decision_engine


def get_decision_engine() -> Optional[DecisionEngine]:
    """获取 DecisionEngine 单例实例"""
    return _decision_engine
