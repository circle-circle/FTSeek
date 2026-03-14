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
from app.strategy.base import SignalAction


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
    
    def __init__(self, db_path: str, config: Dict[str, Any] = None, dingtalk_webhook: str = None):
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
        
        logger.info(f"DecisionEngine V3 初始化完成，滑点={slippage*100:.2f}%")
    
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
            
            # 6. 执行交易
            if mode in [TradingMode.PAPER, TradingMode.LIVE]:
                result = self._execute_paper_trade(signal)
            else:
                return DecisionResult(False, "UNKNOWN_MODE", mode, False, message=f"未知模式：{mode}")
            
            # 7. 检查自动退出
            if result.accepted and result.executed:
                exit_result = self._check_auto_exit(code)
                if exit_result:
                    result.exit_decision = exit_result
            
            return result
            
        except Exception as e:
            logger.exception(f"处理信号异常：{e}")
            return DecisionResult(False, f"ERROR: {e}", TradingMode.PAPER, False, message=str(e))
    
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
        
        try:
            if action == SignalAction.BUY:
                trade = self.paper_trader.buy(code, quantity, price, timestamp)
            elif action == SignalAction.SELL:
                trade = self.paper_trader.sell(code, quantity, price, timestamp)
            else:
                return DecisionResult(False, "UNKNOWN_ACTION", TradingMode.PAPER, False, message=f"未知动作：{action}")
            
            self.rate_limiter.record_signal(code, action)
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
    
    def _check_auto_exit(self, code: str) -> Optional[ExitDecision]:
        """检查自动退出"""
        position = self.get_position(code)
        if not position:
            return None
        
        if position.current_price > 0:
            self.exit_manager.update_peak_price(code, position.current_price)
        
        exit_decision = self.exit_manager.check_exit(position)
        
        if exit_decision and exit_decision.should_exit:
            logger.info(f"触发自动退出：{code}, 原因={exit_decision.reason}")
            try:
                current_price = position.current_price if position.current_price > 0 else position.avg_cost
                trade = self.paper_trader.sell(code, position.quantity, current_price)
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
            indicators = {'rsi': 50, 'macd_histogram': 0}  # 默认中性
            # TODO: 从信号引擎获取实际指标
            
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


def init_decision_engine(db_path: str, config: Dict[str, Any] = None, dingtalk_webhook: str = None) -> DecisionEngine:
    global _decision_engine
    _decision_engine = DecisionEngine(db_path, config, dingtalk_webhook)
    return _decision_engine


def get_decision_engine() -> Optional[DecisionEngine]:
    return _decision_engine
