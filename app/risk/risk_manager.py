"""
风险管理核心模块 (Risk Manager) - FTSeek v3.0

实现五层风控检查体系：
1. 信号级检查 - 置信度过滤
2. 订单级检查 - 仓位/止损限制
3. 仓位级检查 - 总仓位/交易次数
4. 账户级检查 - 亏损/回撤控制
5. 系统级检查 - 连续亏损/策略失效

支持钉钉预警推送和紧急清仓功能
"""
import logging
import os
import yaml
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek.risk.manager")
logger.propagate = False  # 防止日志重复


@dataclass
class CheckResult:
    """风控检查结果"""
    passed: bool
    reason: str
    level: str = ""
    details: Dict = field(default_factory=dict)


@dataclass
class RiskCheckResult:
    """交易前风控检查结果"""
    passed: bool
    details: Dict[str, bool] = field(default_factory=dict)
    timestamp: Optional[datetime] = None


@dataclass
class RiskConfig:
    """风控配置"""
    # 交易模式
    trading_mode: str = "paper"
    initial_cash: float = 1000000.0
    
    # 信号级
    min_confidence: float = 0.60
    
    # 订单级
    max_single_position: float = 0.20
    max_single_loss: float = 0.10
    
    # 仓位级
    max_total_position: float = 0.80
    max_daily_trades: int = 10
    max_daily_loss: float = 0.05
    max_weekly_loss: float = 0.10
    
    # 账户级
    max_drawdown: float = 0.15
    consecutive_loss_limit: int = 5
    
    # 策略级
    strategy_min_trades: int = 20
    strategy_min_winrate: float = 0.40
    
    # 预警配置
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    
    @classmethod
    def from_yaml(cls, config_path: str) -> "RiskConfig":
        """从 YAML 配置文件加载"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # 处理环境变量占位符
            def resolve_env(value):
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    env_var = value[2:-1]
                    return os.environ.get(env_var, "")
                return value
            
            def resolve_dict(d):
                if isinstance(d, dict):
                    return {k: resolve_dict(v) for k, v in d.items()}
                elif isinstance(d, list):
                    return [resolve_dict(item) for item in d]
                else:
                    return resolve_env(d)
            
            config = resolve_dict(config)
            
            risk_limits = config.get("risk_limits", {})
            alerts = config.get("alerts", {})
            trading = config.get("trading", {})
            
            return cls(
                trading_mode=trading.get("mode", "paper"),
                initial_cash=trading.get("paper_initial_cash", 1000000.0),
                min_confidence=risk_limits.get("min_confidence", 0.60),
                max_single_position=risk_limits.get("max_single_position", 0.20),
                max_single_loss=risk_limits.get("max_single_loss", 0.10),
                max_total_position=risk_limits.get("max_total_position", 0.80),
                max_daily_trades=risk_limits.get("max_daily_trades", 10),
                max_daily_loss=risk_limits.get("max_daily_loss", 0.05),
                max_weekly_loss=risk_limits.get("max_weekly_loss", 0.10),
                max_drawdown=risk_limits.get("max_drawdown", 0.15),
                consecutive_loss_limit=risk_limits.get("consecutive_loss_limit", 5),
                strategy_min_trades=risk_limits.get("strategy_min_trades", 20),
                strategy_min_winrate=risk_limits.get("strategy_min_winrate", 0.40),
                dingtalk_webhook=alerts.get("dingtalk_webhook", ""),
                dingtalk_secret=alerts.get("dingtalk_secret", ""),
            )
        except Exception as e:
            logger.warning(f"加载风控配置失败：{e}, 使用默认配置")
            return cls()


class RiskManager:
    """
    风控管理器 - FTSeek v3.0
    
    实现五层风控检查体系，确保交易安全
    """

    def __init__(self, config_path: Optional[str] = None, 
                 initial_capital: Optional[float] = None,
                 risk_per_trade: float = 0.02,
                 limits: Optional[RiskConfig] = None):
        """
        初始化风控管理器
        
        Args:
            config_path: 配置文件路径，默认使用 config/risk_config.yaml
            initial_capital: 初始资金 (可选，覆盖配置)
            risk_per_trade: 每笔交易风险 (可选)
            limits: 风险限制 (向后兼容，覆盖配置)
        """
        # 确定配置文件路径
        if config_path is None:
            # 尝试从常见路径加载
            possible_paths = [
                "/opt/ftseek/FTSeek/config/risk_config.yaml",
                "config/risk_config.yaml",
                os.path.join(os.path.dirname(__file__), "../../config/risk_config.yaml"),
            ]
            for path in possible_paths:
                if os.path.exists(path):
                    config_path = path
                    break
        
        # 加载配置
        if config_path and os.path.exists(config_path):
            self.config = RiskConfig.from_yaml(config_path)
            logger.info(f"风控配置已加载：{config_path}")
        else:
            self.config = RiskConfig()
            logger.warning("未找到风控配置文件，使用默认配置")
        
        # 向后兼容：limits 参数覆盖配置
        if limits is not None:
            self._apply_limits_override(limits)
        
        # 向后兼容：initial_capital 参数覆盖配置
        if initial_capital is not None:
            self.config.initial_cash = initial_capital
        
        # 账户状态
        self.account = {
            "total_capital": self.config.initial_cash,
            "available_cash": self.config.initial_cash,
            "positions": {},  # {code: {quantity, entry_price, current_price, ...}}
            "peak_capital": self.config.initial_cash,
        }
        
        # 交易统计
        self.today_trades = 0
        self.today_pnl = 0.0
        self.today_date = datetime.now().strftime("%Y-%m-%d")
        self.weekly_pnl = 0.0
        self.week_start = self._get_week_start()
        
        # 系统统计
        self.consecutive_losses = 0
        self.total_trades = 0
        self.winning_trades = 0
        
        # 策略统计 {strategy_name: {trades, wins, losses}}
        self.strategy_stats = {}

        # Phase 3: 波动率适配器 (延迟初始化)
        self._vol_adapter = None
        
        logger.info(f"风控管理器已初始化 | 模式={self.config.trading_mode} | 资金={self.config.initial_cash:,.0f}")

    def _apply_limits_override(self, limits: RiskConfig):
        """应用 limits 参数覆盖配置 (向后兼容)"""
        if hasattr(limits, 'max_position_percent'):
            self.config.max_single_position = limits.max_position_percent
        if hasattr(limits, 'max_total_exposure'):
            self.config.max_total_position = limits.max_total_exposure
        if hasattr(limits, 'max_daily_loss'):
            self.config.max_daily_loss = limits.max_daily_loss
        if hasattr(limits, 'max_drawdown'):
            self.config.max_drawdown = limits.max_drawdown
        if hasattr(limits, 'max_trades_per_day'):
            self.config.max_daily_trades = limits.max_trades_per_day
        if hasattr(limits, 'max_single_loss'):
            self.config.max_single_loss = limits.max_single_loss
        if hasattr(limits, 'max_weekly_loss'):
            self.config.max_weekly_loss = limits.max_weekly_loss
        if hasattr(limits, 'consecutive_loss_limit'):
            self.config.consecutive_loss_limit = limits.consecutive_loss_limit
        if hasattr(limits, 'strategy_min_winrate'):
            self.config.strategy_min_winrate = limits.strategy_min_winrate
        if hasattr(limits, 'min_confidence'):
            self.config.min_confidence = limits.min_confidence

    def _get_week_start(self) -> datetime:
        """获取本周开始日期 (周一)"""
        today = datetime.now()
        return today - timedelta(days=today.weekday())

    def _reset_daily_stats(self):
        """重置日统计"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.today_date:
            self.today_trades = 0
            self.today_pnl = 0.0
            self.today_date = today
            
            # 检查是否需要重置周统计
            if datetime.now() < self.week_start:
                self.weekly_pnl = 0.0
                self.week_start = self._get_week_start()

    # ==================== 交易前风控检查 ====================

    def check_before_trade(self, signal: Any) -> RiskCheckResult:
        """
        交易前风控检查
        :param signal: 交易信号
        :return: RiskCheckResult {passed: bool, details: dict, timestamp: datetime}
        """
        try:
            # 获取置信度 (从 signal 属性或 extra 字段)
            confidence = getattr(signal, 'confidence', None)
            if confidence is None and hasattr(signal, 'extra'):
                confidence = signal.extra.get('confidence', 0.0)
            if confidence is None:
                confidence = 0.0
            
            checks = {
                'position_limit': self._check_position_limit(signal.code, signal.quantity),
                'daily_trades': self._check_daily_trades(),
                'daily_loss': self._check_daily_loss(),
                'drawdown': self._check_drawdown(),
                'confidence': self._check_confidence(confidence)
            }

            # Phase 3: 波动率覆盖检查
            vol_override = self._check_volatility_override(signal, confidence)
            if vol_override:
                checks["volatility_override"] = vol_override
            
            passed = all(checks.values())
            
            return RiskCheckResult(
                passed=passed,
                details=checks,
                timestamp=datetime.now()
            )
        except Exception as e:
            logger.error(f"交易前风控检查异常：{e}", exc_info=True)
            # 异常时返回失败，确保安全
            return RiskCheckResult(
                passed=False,
                details={'error': False},
                timestamp=datetime.now()
            )
    
    def _check_position_limit(self, code: str, quantity: int) -> bool:
        """
        检查仓位限制 (单股<20%)
        :param code: 标的代码
        :param quantity: 交易数量
        :return: 是否通过检查
        """
        try:
            # 获取当前持仓
            positions = self.account.get("positions", {})
            total_capital = self.account.get("total_capital", self.config.initial_cash)
            
            # 2026-03-27 修复: 从富途获取实时价格
            current_price = 0.0
            
            # 方式1: 尝试从本地持仓获取
            if code in positions:
                current_price = positions[code].get('current_price', positions[code].get('entry_price', 0))
            
            # 方式2: 如果本地无价格，从富途获取
            if current_price <= 0:
                try:
                    from futu import OpenQuoteContext, RET_OK
                    from app import config as app_config
                    
                    quote_ctx = OpenQuoteContext(host=app_config.OPEND_HOST, port=app_config.OPEND_PORT)
                    ret, data = quote_ctx.get_market_snapshot([code])
                    quote_ctx.close()
                    
                    if ret == RET_OK and len(data) > 0:
                        current_price = float(data.iloc[0].get('last_price', 0))
                        logger.debug(f"从富途获取 {code} 实时价格: {current_price}")
                except Exception as e:
                    logger.warning(f"获取 {code} 实时价格失败: {e}")
            
            # 如果仍无法获取价格，保守估计 (假设通过)
            if current_price <= 0:
                logger.warning(f"无法获取 {code} 当前价格，跳过仓位检查")
                return True
            
            # 计算新仓位价值
            new_position_value = quantity * current_price
            
            # 计算现有该标的的仓位价值
            existing_position_value = 0.0
            if code in positions:
                existing_position_value = positions[code].get('quantity', 0) * current_price
            
            # 计算总仓位比例 (现有 + 新增)
            total_position_value = existing_position_value + new_position_value
            position_ratio = total_position_value / total_capital if total_capital > 0 else 1.0
            
            # 检查是否超过单标的限制
            if position_ratio > self.config.max_single_position:
                logger.warning(
                    f"仓位限制检查失败：{code} 仓位 {position_ratio:.1%} "
                    f"超过限制 {self.config.max_single_position:.1%}"
                )
                return False
            
            return True
        except Exception as e:
            logger.error(f"_check_position_limit 异常：{e}")
            return False
    
    def _check_daily_trades(self) -> bool:
        """
        检查日交易次数 (<10 次)
        :return: 是否通过检查
        """
        try:
            passed = self.today_trades < self.config.max_daily_trades
            if not passed:
                logger.warning(
                    f"日交易次数检查失败：{self.today_trades} 达到限制 {self.config.max_daily_trades}"
                )
            return passed
        except Exception as e:
            logger.error(f"_check_daily_trades 异常：{e}")
            return False
    
    def _check_daily_loss(self) -> bool:
        """
        检查日亏损 (<5%)
        :return: 是否通过检查
        """
        try:
            total_capital = self.account.get("total_capital", self.config.initial_cash)
            max_loss_amount = self.config.max_daily_loss * total_capital
            passed = abs(self.today_pnl) < max_loss_amount if self.today_pnl < 0 else True
            
            if not passed:
                logger.warning(
                    f"日亏损检查失败：{self.today_pnl:.2f} 超过限制 {max_loss_amount:.2f}"
                )
            return passed
        except Exception as e:
            logger.error(f"_check_daily_loss 异常：{e}")
            return False
    
    def _check_drawdown(self) -> bool:
        """
        检查回撤 (<15%)
        :return: 是否通过检查
        """
        try:
            peak_capital = self.account.get("peak_capital", self.config.initial_cash)
            total_capital = self.account.get("total_capital", self.config.initial_cash)
            
            if peak_capital <= 0:
                return True
            
            drawdown = (peak_capital - total_capital) / peak_capital
            passed = drawdown < self.config.max_drawdown
            
            if not passed:
                logger.warning(
                    f"回撤检查失败：{drawdown:.1%} 超过限制 {self.config.max_drawdown:.1%}"
                )
            return passed
        except Exception as e:
            logger.error(f"_check_drawdown 异常：{e}")
            return False
    
    def _check_confidence(self, confidence: float) -> bool:
        """
        检查置信度 (>60%)
        :param confidence: 置信度 (0-1)
        :return: 是否通过检查
        """
        try:
            passed = confidence >= self.config.min_confidence
            
            if not passed:
                logger.warning(
                    f"置信度检查失败：{confidence:.2f} 低于阈值 {self.config.min_confidence:.2f}"
                )
            return passed
        except Exception as e:
            logger.error(f"_check_confidence 异常：{e}")
            return False

    # ==================== 五层风控检查 ====================
    
    def _check_volatility_override(self, signal, confidence: float) -> Dict:
        """
        Phase 3: 波动率覆盖检查

        Args:
            signal: 交易信号
            confidence: 置信度

        Returns:
            覆盖详情
        """
        try:
            # 延迟加载波动率适配器
            if self._vol_adapter is None:
                from app.risk.volatility_risk_adapter import get_volatility_risk_adapter
                self._vol_adapter = get_volatility_risk_adapter()

            # 获取信号方向
            action = getattr(signal, 'action', 'BUY')
            if hasattr(action, 'value'):
                action = action.value

            # 执行覆盖检查
            passed, override = self._vol_adapter.check_volatility_override(
                signal_code=getattr(signal, 'code', 'unknown'),
                action=action,
                original_confidence=confidence,
            )

            # 记录结果
            if override.get('position_override'):
                logger.info(
                    f"[波动率覆盖] {signal.code} 仓位限制调整为 {override['position_override']:.0%}"
                )

            return override

        except Exception as e:
            logger.debug(f"波动率覆盖检查失败：{e}")
            return {}
    def check_signal(self, signal: Any) -> bool:
        """
        信号级检查 - 置信度过滤
        
        Args:
            signal: 信号对象，需有 confidence 属性
            
        Returns:
            bool: 是否通过检查
        """
        self._reset_daily_stats()
        
        confidence = getattr(signal, 'confidence', 0.0)
        
        if confidence < self.config.min_confidence:
            logger.warning(f"信号级检查拒绝：置信度 {confidence:.2f} < {self.config.min_confidence:.2f}")
            self.send_alert(
                f"⚠️ 信号被拒绝：置信度 {confidence:.2f} 低于阈值 {self.config.min_confidence:.2f}",
                "warning"
            )
            return False
        
        logger.debug(f"信号级检查通过：置信度 {confidence:.2f} >= {self.config.min_confidence:.2f}")
        return True

    def check_order(self, order: Any, account: Optional[Dict] = None) -> CheckResult:
        """
        订单级检查 - 仓位/止损限制
        
        Args:
            order: 订单对象，需有 code, quantity, price, stop_loss_price 等属性
            account: 账户信息 (可选，使用内部账户如果未提供)
            
        Returns:
            CheckResult: 检查结果
        """
        self._reset_daily_stats()
        acc = account or self.account
        
        code = getattr(order, 'code', getattr(order, 'symbol', ''))
        quantity = getattr(order, 'quantity', 0)
        price = getattr(order, 'price', getattr(order, 'entry_price', 0))
        stop_loss_price = getattr(order, 'stop_loss_price', None)
        
        order_value = quantity * price
        position_ratio = order_value / acc.get("total_capital", self.config.initial_cash)
        
        # 检查单标的仓位
        if position_ratio > self.config.max_single_position:
            reason = f"订单仓位 {position_ratio:.1%} 超过限制 {self.config.max_single_position:.1%}"
            logger.warning(f"订单级检查拒绝：{reason}")
            return CheckResult(passed=False, reason=reason, level="order")
        
        # 检查止损
        if stop_loss_price is not None and price > 0:
            loss_ratio = abs(price - stop_loss_price) / price
            if loss_ratio > self.config.max_single_loss:
                reason = f"止损幅度 {loss_ratio:.1%} 超过限制 {self.config.max_single_loss:.1%}"
                logger.warning(f"订单级检查拒绝：{reason}")
                return CheckResult(passed=False, reason=reason, level="order")
        
        logger.debug(f"订单级检查通过：{code} 仓位 {position_ratio:.1%}")
        return CheckResult(passed=True, reason="订单检查通过", level="order")

    def check_position(self, account: Optional[Dict] = None) -> CheckResult:
        """
        仓位级检查 - 总仓位/交易次数
        
        Args:
            account: 账户信息 (可选)
            
        Returns:
            CheckResult: 检查结果
        """
        self._reset_daily_stats()
        acc = account or self.account
        
        # 计算总仓位
        total_position_value = sum(
            pos.get('quantity', 0) * pos.get('current_price', pos.get('entry_price', 0))
            for pos in acc.get("positions", {}).values()
        )
        total_position_ratio = total_position_value / acc.get("total_capital", self.config.initial_cash)
        
        # 检查总仓位
        if total_position_ratio > self.config.max_total_position:
            reason = f"总仓位 {total_position_ratio:.1%} 超过限制 {self.config.max_total_position:.1%}"
            logger.warning(f"仓位级检查拒绝：{reason}")
            return CheckResult(passed=False, reason=reason, level="position")
        
        # 检查日交易次数
        if self.today_trades >= self.config.max_daily_trades:
            reason = f"日交易次数 {self.today_trades} 达到限制 {self.config.max_daily_trades}"
            logger.warning(f"仓位级检查拒绝：{reason}")
            return CheckResult(passed=False, reason=reason, level="position")
        
        logger.debug(f"仓位级检查通过：总仓位 {total_position_ratio:.1%}, 今日交易 {self.today_trades}/{self.config.max_daily_trades}")
        return CheckResult(passed=True, reason="仓位检查通过", level="position")

    def check_account(self, account: Optional[Dict] = None) -> CheckResult:
        """
        账户级检查 - 亏损/回撤控制
        
        Args:
            account: 账户信息 (可选)
            
        Returns:
            CheckResult: 检查结果
        """
        self._reset_daily_stats()
        acc = account or self.account
        
        total_capital = acc.get("total_capital", self.config.initial_cash)
        peak_capital = acc.get("peak_capital", self.config.initial_cash)
        
        # 计算回撤
        drawdown = (peak_capital - total_capital) / peak_capital if peak_capital > 0 else 0
        
        # 检查日亏损
        daily_loss_ratio = abs(self.today_pnl) / total_capital if self.today_pnl < 0 else 0
        if daily_loss_ratio > self.config.max_daily_loss:
            reason = f"日亏损 {daily_loss_ratio:.1%} 超过限制 {self.config.max_daily_loss:.1%}"
            logger.warning(f"账户级检查拒绝：{reason}")
            self.send_alert(f"🚨 日亏损超限：{daily_loss_ratio:.1%}", "critical")
            return CheckResult(passed=False, reason=reason, level="account")
        
        # 检查周亏损
        weekly_loss_ratio = abs(self.weekly_pnl) / total_capital if self.weekly_pnl < 0 else 0
        if weekly_loss_ratio > self.config.max_weekly_loss:
            reason = f"周亏损 {weekly_loss_ratio:.1%} 超过限制 {self.config.max_weekly_loss:.1%}"
            logger.warning(f"账户级检查拒绝：{reason}")
            self.send_alert(f"🚨 周亏损超限：{weekly_loss_ratio:.1%}", "critical")
            return CheckResult(passed=False, reason=reason, level="account")
        
        # 检查回撤
        if drawdown > self.config.max_drawdown:
            reason = f"回撤 {drawdown:.1%} 超过限制 {self.config.max_drawdown:.1%}"
            logger.warning(f"账户级检查拒绝：{reason}")
            self.send_alert(f"🚨 回撤超限：{drawdown:.1%}", "critical")
            return CheckResult(passed=False, reason=reason, level="account")
        
        logger.debug(f"账户级检查通过：日亏损 {daily_loss_ratio:.1%}, 周亏损 {weekly_loss_ratio:.1%}, 回撤 {drawdown:.1%}")
        return CheckResult(passed=True, reason="账户检查通过", level="account")

    def check_system(self, strategy_stats: Optional[Dict] = None) -> CheckResult:
        """
        系统级检查 - 连续亏损/策略失效
        
        Args:
            strategy_stats: 策略统计 (可选，使用内部统计如果未提供)
            
        Returns:
            CheckResult: 检查结果
        """
        stats = strategy_stats or self._get_system_stats()
        
        # 检查连续亏损
        consecutive_losses = stats.get("consecutive_losses", self.consecutive_losses)
        if consecutive_losses >= self.config.consecutive_loss_limit:
            reason = f"连续亏损 {consecutive_losses} 次达到限制 {self.config.consecutive_loss_limit}"
            logger.warning(f"系统级检查拒绝：{reason}")
            self.send_alert(f"🚨 连续亏损 {consecutive_losses} 次，暂停交易", "critical")
            return CheckResult(passed=False, reason=reason, level="system")
        
        # 检查策略胜率
        total_trades = stats.get("total_trades", self.total_trades)
        winning_trades = stats.get("winning_trades", self.winning_trades)
        
        if total_trades >= self.config.strategy_min_trades:
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            if win_rate < self.config.strategy_min_winrate:
                reason = f"策略胜率 {win_rate:.1%} 低于最低要求 {self.config.strategy_min_winrate:.1%}"
                logger.warning(f"系统级检查拒绝：{reason}")
                self.send_alert(f"⚠️ 策略胜率过低：{win_rate:.1%}", "warning")
                return CheckResult(passed=False, reason=reason, level="system")
        
        logger.debug(f"系统级检查通过：连续亏损 {consecutive_losses}, 胜率 {winning_trades}/{total_trades}")
        return CheckResult(passed=True, reason="系统检查通过", level="system")

    # ==================== 辅助方法 ====================
    
    def _get_system_stats(self) -> Dict:
        """获取系统统计"""
        return {
            "consecutive_losses": self.consecutive_losses,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
        }

    def record_trade_result(self, pnl: float, strategy: str = ""):
        """
        记录交易结果
        
        Args:
            pnl: 盈亏金额
            strategy: 策略名称
        """
        self.total_trades += 1
        self.today_trades += 1
        self.today_pnl += pnl
        self.weekly_pnl += pnl
        
        if pnl > 0:
            self.winning_trades += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
        
        # 更新策略统计
        if strategy:
            if strategy not in self.strategy_stats:
                self.strategy_stats[strategy] = {"trades": 0, "wins": 0, "losses": 0}
            self.strategy_stats[strategy]["trades"] += 1
            if pnl > 0:
                self.strategy_stats[strategy]["wins"] += 1
            else:
                self.strategy_stats[strategy]["losses"] += 1
        
        # 更新账户峰值
        if self.account["total_capital"] > self.account["peak_capital"]:
            self.account["peak_capital"] = self.account["total_capital"]
        
        logger.info(f"交易结果记录：盈亏={pnl:.2f}, 连续亏损={self.consecutive_losses}")

    def update_account(self, total_capital: float, positions: Dict = None):
        """
        更新账户状态
        
        Args:
            total_capital: 总资金
            positions: 持仓信息
        """
        self.account["total_capital"] = total_capital
        if total_capital > self.account["peak_capital"]:
            self.account["peak_capital"] = total_capital
        if positions:
            self.account["positions"] = positions

    # ==================== 预警推送 ====================
    
    def send_alert(self, message: str, level: str = "warning"):
        """
        发送预警到钉钉
        
        Args:
            message: 预警消息
            level: 预警级别 (info/warning/critical)
        """
        if not self.config.dingtalk_webhook:
            logger.info(f"[预警] {level.upper()}: {message}")
            return
        
        try:
            # 导入钉钉推送模块
            from ..notifier.dingtalk import DingTalkNotifier
            
            notifier = DingTalkNotifier(
                webhook=self.config.dingtalk_webhook,
                secret=self.config.dingtalk_secret
            )
            
            # 根据级别设置标题
            title_map = {
                "info": "📢 风控提示",
                "warning": "⚠️ 风控预警",
                "critical": "🚨 风控警报"
            }
            title = title_map.get(level, "📢 风控通知")
            
            notifier.send_markdown(title=title, text=message)
            logger.info(f"预警已推送：{level} - {message[:50]}...")
            
        except Exception as e:
            logger.error(f"发送预警失败：{e}")

    # ==================== 紧急清仓 ====================
    
    def liquidate_all(self, reason: str = "紧急风控"):
        """
        紧急清仓
        
        Args:
            reason: 清仓原因
        """
        positions = self.account.get("positions", {})
        if not positions:
            logger.info("无持仓，无需清仓")
            return
        
        logger.critical(f"执行紧急清仓：{reason}, 持仓数={len(positions)}")
        self.send_alert(f"🚨 紧急清仓执行\n原因：{reason}\n持仓数：{len(positions)}", "critical")
        
        # 清空持仓记录
        cleared_positions = list(positions.keys())
        self.account["positions"] = {}
        self.account["available_cash"] = self.account["total_capital"]
        
        logger.info(f"紧急清仓完成：{len(cleared_positions)} 个持仓已清空")
        return cleared_positions

    # ==================== 综合检查 ====================
    
    def check_all(self, signal: Any = None, order: Any = None, 
                  account: Dict = None, strategy_stats: Dict = None) -> Tuple[bool, List[str]]:
        """
        执行全部五层检查
        
        Args:
            signal: 信号对象
            order: 订单对象
            account: 账户信息
            strategy_stats: 策略统计
            
        Returns:
            (passed, reasons): 是否全部通过及原因列表
        """
        reasons = []
        all_passed = True
        
        # 1. 信号级检查
        if signal is not None:
            if not self.check_signal(signal):
                all_passed = False
                reasons.append("信号级检查失败")
        
        # 2. 订单级检查
        if order is not None:
            result = self.check_order(order, account)
            if not result.passed:
                all_passed = False
                reasons.append(f"订单级检查：{result.reason}")
        
        # 3. 仓位级检查
        result = self.check_position(account)
        if not result.passed:
            all_passed = False
            reasons.append(f"仓位级检查：{result.reason}")
        
        # 4. 账户级检查
        result = self.check_account(account)
        if not result.passed:
            all_passed = False
            reasons.append(f"账户级检查：{result.reason}")
        
        # 5. 系统级检查
        result = self.check_system(strategy_stats)
        if not result.passed:
            all_passed = False
            reasons.append(f"系统级检查：{result.reason}")
        
        if all_passed:
            logger.debug("全部风控检查通过")
        else:
            logger.warning(f"风控检查失败：{reasons}")
        
        return all_passed, reasons

    def get_risk_metrics(self) -> Dict:
        """获取风险指标 (供 realtime_monitor 使用)"""
        self._reset_daily_stats()
        
        acc = self.account
        total_position_value = sum(
            pos.get('quantity', 0) * pos.get('current_price', pos.get('entry_price', 0))
            for pos in acc.get("positions", {}).values()
        )
        total_capital = acc.get("total_capital", self.config.initial_cash)
        peak_capital = acc.get("peak_capital", self.config.initial_cash)
        current_drawdown = (peak_capital - total_capital) / peak_capital if peak_capital > 0 else 0
        total_exposure = total_position_value / total_capital if total_capital > 0 else 0
        
        return {
            "current_drawdown": current_drawdown,
            "total_exposure": total_exposure,
            "total_capital": total_capital,
            "peak_capital": peak_capital,
            "total_position_value": total_position_value,
            "positions": acc.get("positions", {}),
        }
    
    def get_status(self) -> Dict:
        """获取风控状态"""
        self._reset_daily_stats()
        
        acc = self.account
        total_position_value = sum(
            pos.get('quantity', 0) * pos.get('current_price', pos.get('entry_price', 0))
            for pos in acc.get("positions", {}).values()
        )
        total_capital = acc.get("total_capital", self.config.initial_cash)
        peak_capital = acc.get("peak_capital", self.config.initial_cash)
        
        return {
            "mode": self.config.trading_mode,
            "capital": {
                "total": total_capital,
                "peak": peak_capital,
                "drawdown": f"{(peak_capital - total_capital) / peak_capital:.1%}" if peak_capital > 0 else "0%"
            },
            "position": {
                "total_ratio": f"{total_position_value / total_capital:.1%}" if total_capital > 0 else "0%",
                "count": len(acc.get("positions", {})),
            },
            "today": {
                "trades": f"{self.today_trades}/{self.config.max_daily_trades}",
                "pnl": f"{self.today_pnl:+.2f}",
            },
            "weekly": {
                "pnl": f"{self.weekly_pnl:+.2f}",
            },
            "system": {
                "consecutive_losses": self.consecutive_losses,
                "total_trades": self.total_trades,
                "win_rate": f"{self.winning_trades / self.total_trades:.1%}" if self.total_trades > 0 else "N/A"
            },
            "limits": {
                "min_confidence": self.config.min_confidence,
                "max_single_position": f"{self.config.max_single_position:.1%}",
                "max_total_position": f"{self.config.max_total_position:.1%}",
                "max_daily_loss": f"{self.config.max_daily_loss:.1%}",
                "max_drawdown": f"{self.config.max_drawdown:.1%}",
            }
        }
    
    def get_closed_trades_today(self) -> List[Dict]:
        """
        获取今日已平仓交易列表
        2026-03-14 新增：用于收盘统计和周报复盘
        
        Returns:
            交易列表 [{code, pnl, pnl_percent, close_time, ...}, ...]
        """
        # 从内部交易历史中获取今日交易
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = []
        
        # 遍历交易历史 (如果有的话)
        if hasattr(self, 'trade_history') and self.trade_history:
            for trade in self.trade_history:
                try:
                    close_time = trade.get('close_time', '')
                    if close_time and close_time.startswith(today):
                        today_trades.append(trade)
                except Exception:
                    continue
        
        # 如果没有内部历史，尝试从 OpenD API 获取
        if not today_trades:
            try:
                from app.v4.trader.opend_executor import get_opend_executor
                executor = get_opend_executor(simulate=True)
                
                if executor.client:
                    ret_code, ret_data = executor.client.get_history_order_list(
                        filter_orders_by_status=[1, 2],  # 已成交
                    )
                    
                    if ret_code == 0 and ret_data is not None and len(ret_data) > 0:
                        for order in ret_data.itertuples():
                            try:
                                order_time = getattr(order, 'create_time', '')
                                if order_time and order_time.startswith(today):
                                    today_trades.append({
                                        'code': getattr(order, 'code', ''),
                                        'pnl': getattr(order, 'profit', 0),
                                        'pnl_percent': 0,
                                        'close_time': order_time,
                                        'quantity': getattr(order, 'qty', 0),
                                        'price': getattr(order, 'price', 0),
                                    })
                            except Exception:
                                continue
            except Exception as e:
                logger.debug(f"从 OpenD 获取今日交易失败：{e}")
        
        return today_trades


# ==================== 向后兼容类 ====================

@dataclass
class RiskLimit:
    """风险限制 (向后兼容旧 API)"""
    max_position_percent: float = 0.20      # 单标的最大仓位
    max_total_exposure: float = 0.80        # 总仓位上限
    max_daily_loss: float = 0.05            # 日亏损限制
    max_drawdown: float = 0.15              # 最大回撤限制
    max_var_percent: float = 0.03           # 最大 VaR
    max_correlated_exposure: float = 0.40   # 高相关标的合计上限
    max_trades_per_day: int = 10            # 每日最大交易次数
    max_single_loss: float = 0.10           # 单笔最大止损
    max_weekly_loss: float = 0.10           # 周亏损限制
    consecutive_loss_limit: int = 5         # 连续亏损限制
    strategy_min_winrate: float = 0.40      # 策略最低胜率
    min_confidence: float = 0.60            # 最小置信度


@dataclass
class RiskMetrics:
    """风险指标 (向后兼容)"""
    total_capital: float = 0.0
    available_capital: float = 0.0
    total_exposure: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    n_positions: int = 0
    win_rate: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            "total_capital": self.total_capital,
            "available_capital": self.available_capital,
            "total_exposure": f"{self.total_exposure:.1%}",
            "unrealized_pnl": f"{self.unrealized_pnl:.2f}",
            "current_drawdown": f"{self.current_drawdown:.1%}",
            "n_positions": self.n_positions,
            "win_rate": f"{self.win_rate:.1%}",
        }


# 全局单例
risk_manager: Optional[RiskManager] = None


def get_risk_manager(config_path: Optional[str] = None) -> RiskManager:
    """获取风控管理器单例"""
    global risk_manager
    if risk_manager is None:
        risk_manager = RiskManager(config_path)
    return risk_manager


# ============================================================
# 止损类型枚举 (2026-03-27 新增 - 强化止损)
# ============================================================

from enum import Enum

class StopLossType(Enum):
    """止损类型"""
    FIXED_PERCENT = "fixed_percent"      # 固定百分比止损
    SUPERTREND = "supertrend"            # SuperTrend 止损
    TRAILING = "trailing"                # 移动止损
    TIME_BASED = "time_based"            # 时间止损
    ATR_BASED = "atr_based"              # ATR 止损


# ============================================================
# 止损检查函数 (2026-03-27 新增 - 强化止损执行)
# ============================================================

def check_stop_loss_enhanced(position, current_price, config):
    """
    强化止损检查
    
    Args:
        position: 持仓信息 {code, entry_price, quantity, stop_loss_level, ...}
        current_price: 当前价格
        config: 配置字典 {max_single_loss, trailing_percent, ...}
    
    Returns:
        (是否触发止损，触发原因)
    """
    if not position:
        return False, "无持仓"
    
    stop_loss_level = position.get('stop_loss_level')
    entry_price = position.get('entry_price', 0)
    
    if not stop_loss_level or not entry_price:
        # 如果没有设置止损位，使用默认固定百分比止损
        default_stop_price = entry_price * (1 - config.get('max_single_loss', 0.05))
        if current_price <= default_stop_price:
            return True, f"价格触及默认止损位 {default_stop_price:.2f} (-{config.get('max_single_loss', 0.05)*100:.1f}%)"
        return False, ""
    
    stop_type = stop_loss_level.get('type', StopLossType.FIXED_PERCENT.value)
    
    # 1. 固定百分比止损
    if stop_type == StopLossType.FIXED_PERCENT.value:
        stop_price = entry_price * (1 - config.get('max_single_loss', 0.05))
        if current_price <= stop_price:
            return True, f"价格触及固定止损位 {stop_price:.2f} (-{config.get('max_single_loss', 0.05)*100:.1f}%)"
    
    # 2. SuperTrend 止损
    elif stop_type == StopLossType.SUPERTREND.value:
        super_trend = stop_loss_level.get('value', 0)
        if super_trend > 0 and current_price <= super_trend:
            return True, f"价格触及 SuperTrend 止损位 {super_trend:.2f}"
    
    # 3. 移动止损
    elif stop_type == StopLossType.TRAILING.value:
        trailing_percent = config.get('trailing_percent', 0.03)
        highest_price = position.get('highest_price', entry_price)
        trailing_stop = highest_price * (1 - trailing_percent)
        if current_price <= trailing_stop:
            return True, f"价格触及移动止损位 {trailing_stop:.2f} (从最高点回撤{trailing_percent*100:.1f}%)"
    
    # 4. 时间止损
    elif stop_type == StopLossType.TIME_BASED.value:
        entry_time = position.get('entry_time')
        max_holding_hours = config.get('max_holding_hours', 72)
        if entry_time:
            from datetime import datetime
            entry_dt = datetime.fromisoformat(entry_time) if isinstance(entry_time, str) else entry_time
            holding_hours = (datetime.now() - entry_dt).total_seconds() / 3600
            if holding_hours > max_holding_hours:
                return True, f"持仓时间超过{max_holding_hours}小时 ({holding_hours:.1f}小时)"
    
    # 5. ATR 止损
    elif stop_type == StopLossType.ATR_BASED.value:
        atr_value = stop_loss_level.get('value', 0)
        atr_multiplier = config.get('atr_multiplier', 2.0)
        atr_stop = entry_price - (atr_multiplier * atr_value)
        if current_price <= atr_stop:
            return True, f"价格触及 ATR 止损位 {atr_stop:.2f} (ATR×{atr_multiplier})"
    
    return False, ""


def update_stop_loss_level(position, current_price, config):
    """
    动态更新止损位
    
    Args:
        position: 持仓信息
        current_price: 当前价格
        config: 配置字典
    
    Returns:
        更新后的止损位信息
    """
    stop_loss_level = position.get('stop_loss_level', {})
    stop_type = stop_loss_level.get('type', StopLossType.FIXED_PERCENT.value)
    
    # 移动止损需要动态更新
    if stop_type == StopLossType.TRAILING.value:
        highest_price = position.get('highest_price', current_price)
        if current_price > highest_price:
            position['highest_price'] = current_price
            trailing_percent = config.get('trailing_percent', 0.03)
            stop_loss_level['value'] = current_price * (1 - trailing_percent)
    
    return stop_loss_level



# ============================================================
# 动态止盈策略 (2026-03-27 新增 - 专家团决策)
# ============================================================

class TrailingTakeProfit:
    """
    移动止盈计算器
    
    功能:
    - 跟踪价格峰值
    - 动态调整止盈位
    - 支持多种止盈类型
    """
    
    def __init__(self, trailing_percent=0.03, time_limit_hours=72):
        """
        初始化移动止盈
        
        Args:
            trailing_percent: 移动止盈百分比 (默认 3%)
            time_limit_hours: 时间止盈限制 (默认 72 小时)
        """
        self.trailing_percent = trailing_percent
        self.time_limit_hours = time_limit_hours
        self.peak_price = 0.0
        self.entry_time = None
    
    def update_peak(self, current_price: float):
        """
        更新价格峰值
        
        Args:
            current_price: 当前价格
        """
        if current_price > self.peak_price:
            self.peak_price = current_price
    
    def calculate_trailing_stop(self, entry_price: float) -> float:
        """
        计算移动止盈位
        
        Args:
            entry_price: 入场价格
        
        Returns:
            移动止盈价格
        """
        if self.peak_price == 0.0:
            # 没有峰值数据，使用入场价
            return entry_price * (1 + self.trailing_percent)
        
        # 从峰值回撤 trailing_percent
        return self.peak_price * (1 - self.trailing_percent)
    
    def check_time_exit(self, entry_time_str: str) -> bool:
        """
        检查时间止盈
        
        Args:
            entry_time_str: 入场时间字符串 (ISO 格式)
        
        Returns:
            是否触发时间止盈
        """
        if not entry_time_str:
            return False
        
        try:
            from datetime import datetime
            entry_time = datetime.fromisoformat(entry_time_str)
            holding_hours = (datetime.now() - entry_time).total_seconds() / 3600
            
            if holding_hours >= self.time_limit_hours:
                return True
        except Exception as e:
            logger.debug(f"时间止盈检查失败：{e}")
        
        return False
    
    def check_condition_exit(self, rsi: float = None, macd_signal: str = None) -> bool:
        """
        检查条件止盈
        
        Args:
            rsi: RSI 指标值
            macd_signal: MACD 信号 ('buy', 'sell', 'neutral')
        
        Returns:
            是否触发条件止盈
        """
        # RSI 超买 (>70) 触发止盈
        if rsi and rsi > 70:
            return True
        
        # MACD 死叉触发止盈
        if macd_signal == 'sell':
            return True
        
        return False
    
    def reset(self):
        """重置止盈计算器"""
        self.peak_price = 0.0
        self.entry_time = None


class DynamicTakeProfitManager:
    """
    动态止盈管理器
    
    功能:
    - 管理多个持仓的止盈
    - 支持多种止盈类型
    - 自动更新止盈位
    """
    
    def __init__(self):
        self.calculators = {}  # code -> TrailingTakeProfit
    
    def get_calculator(self, code: str) -> TrailingTakeProfit:
        """获取或创建止盈计算器"""
        if code not in self.calculators:
            self.calculators[code] = TrailingTakeProfit()
        return self.calculators[code]
    
    def update_price(self, code: str, price: float):
        """更新价格"""
        calculator = self.get_calculator(code)
        calculator.update_peak(price)
    
    def check_exit(self, code: str, current_price: float, entry_price: float, 
                   entry_time: str = None, rsi: float = None, 
                   macd_signal: str = None) -> tuple:
        """
        检查是否应该止盈
        
        Args:
            code: 股票代码
            current_price: 当前价格
            entry_price: 入场价格
            entry_time: 入场时间
            rsi: RSI 指标
            macd_signal: MACD 信号
        
        Returns:
            (是否止盈，止盈原因，止盈类型)
        """
        calculator = self.get_calculator(code)
        
        # 1. 移动止盈检查
        trailing_stop = calculator.calculate_trailing_stop(entry_price)
        if current_price <= trailing_stop:
            return (True, f"移动止盈触发 (止盈位:{trailing_stop:.2f})", "trailing")
        
        # 2. 时间止盈检查
        if calculator.check_time_exit(entry_time):
            return (True, f"时间止盈触发 (持仓>{calculator.time_limit_hours}小时)", "time")
        
        # 3. 条件止盈检查
        if calculator.check_condition_exit(rsi, macd_signal):
            reason = []
            if rsi and rsi > 70:
                reason.append(f"RSI 超买 ({rsi:.1f})")
            if macd_signal == 'sell':
                reason.append("MACD 死叉")
            return (True, f"条件止盈触发 ({', '.join(reason)})", "condition")
        
        return (False, "", "none")
    
    def remove(self, code: str):
        """移除持仓止盈计算器"""
        if code in self.calculators:
            del self.calculators[code]

