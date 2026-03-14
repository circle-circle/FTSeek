"""
AutoExitManager - 自动退出管理器

功能:
- 动态止盈止损决策
- 支持多种退出策略
- 与 PaperTradingEngine 集成

退出策略:
1. 目标止盈：收益率≥5% → 自动卖出
2. 止损：亏损≥3% → 自动卖出
3. 时间退出：持仓超 24 小时 → 自动平仓
4. Trailing Stop：从最高点回撤≥2% → 卖出 (可选)

数据结构:
- ExitDecision: 退出决策结果

核心逻辑:
- 优先级：止盈 > 止损 > Trailing > 超时
- 支持动态调整阈值
- Trailing Stop 可选启用
"""
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass
import logging

from app.v4.trader.paper_trader import PaperPosition

logger = logging.getLogger("ftseek.v4.core.exit_manager")


@dataclass
class ExitDecision:
    """
    退出决策数据类

    Attributes:
        should_exit: 是否应该退出
        reason: 退出原因 (TARGET_PROFIT/STOP_LOSS/TIMEOUT/TRAILING/HOLD_STRONG)
        expected_pnl: 预期盈亏
        expected_return: 预期收益率
        current_price: 当前价格
        trigger_price: 触发价格
        message: 人类可读的消息
    """
    should_exit: bool
    reason: str  # TARGET_PROFIT/STOP_LOSS/TIMEOUT/TRAILING/HOLD_STRONG
    expected_pnl: float
    expected_return: float
    current_price: float
    trigger_price: float
    message: str = ""

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "should_exit": self.should_exit,
            "reason": self.reason,
            "expected_pnl": self.expected_pnl,
            "expected_return": self.expected_return,
            "current_price": self.current_price,
            "trigger_price": self.trigger_price,
        }


class AutoExitManager:
    """
    自动退出管理器

    功能:
    - 检查持仓是否满足退出条件
    - 支持多种退出策略 (止盈/止损/超时/Trailing)
    - 追踪最高价 (用于 Trailing Stop)
    - 支持动态配置
    - 动态超时退出：涨势强烈 + 指标正向时允许持仓超过 24 小时

    Args:
        target_profit: 止盈阈值 (默认 0.05 = 5%)
        stop_loss: 止损阈值 (默认 0.03 = 3%)
        timeout_hours: 超时时间 (默认 24 小时)
        trailing_stop: 是否启用 Trailing Stop (默认 False)
        trailing_pct: Trailing Stop 回撤阈值 (默认 0.02 = 2%)
        strong_uptrend_threshold: 涨势阈值 (默认 0.03 = 3%)
        rsi_overbought: RSI 超买线 (默认 70)
    """

    def __init__(
        self,
        target_profit: float = 0.05,
        stop_loss: float = 0.03,
        timeout_hours: float = 24,
        trailing_stop: bool = False,
        trailing_pct: float = 0.02,
        strong_uptrend_threshold: float = 0.03,
        rsi_overbought: float = 70
    ):
        """
        初始化退出管理器

        Args:
            target_profit: 止盈阈值 (默认 5%)
            stop_loss: 止损阈值 (默认 3%)
            timeout_hours: 超时时间 (默认 24 小时)
            trailing_stop: 是否启用 Trailing Stop (默认 False)
            trailing_pct: Trailing Stop 回撤阈值 (默认 2%)
            strong_uptrend_threshold: 涨势阈值 (默认 3%)
            rsi_overbought: RSI 超买线 (默认 70)
        """
        self.target_profit = target_profit
        self.stop_loss = stop_loss
        self.timeout_hours = timeout_hours
        self.trailing_stop = trailing_stop
        self.trailing_pct = trailing_pct
        self.strong_uptrend_threshold = strong_uptrend_threshold
        self.rsi_overbought = rsi_overbought

        # 存储每只股票的最高价 (用于 Trailing Stop)
        self._peak_prices: Dict[str, float] = {}

        logger.info(
            f"AutoExitManager 初始化：止盈={target_profit*100:.1f}%, "
            f"止损={stop_loss*100:.1f}%, 超时={timeout_hours}h, "
            f"Trailing={trailing_stop} ({trailing_pct*100:.1f}%), "
            f"涨势阈值={strong_uptrend_threshold*100:.1f}%, RSI 超买={rsi_overbought}"
        )

    def check_exit(self, position: PaperPosition, indicators: Dict = None) -> Optional[ExitDecision]:
        """
        检查持仓是否应该退出

        检查顺序 (优先级从高到低):
        1. 目标止盈 (收益率 ≥ target_profit)
        2. 止损 (收益率 ≤ -stop_loss)
        3. Trailing Stop (从最高点回撤 ≥ trailing_pct)
        4. 时间退出 (持仓时间 ≥ timeout_hours，动态决策)

        Args:
            position: 持仓信息
            indicators: 技术指标字典 (可选), 包含:
                - rsi: RSI 指标值
                - macd_histogram: MACD 柱状图值

        Returns:
            ExitDecision: 退出决策 (如果不退出返回 None)
        """
        # 验证持仓有效性
        if not self._is_valid_position(position):
            return None

        # 计算收益率
        current_return = self._calculate_return(position)
        current_pnl = self._calculate_pnl(position)

        # 1. 检查止盈 (最高优先级)
        if self._check_target_profit(current_return):
            logger.info(
                f"触发止盈：{position.code} 收益率={current_return*100:.2f}% "
                f"≥ {self.target_profit*100:.1f}%"
            )
            return ExitDecision(
                should_exit=True,
                reason="TARGET_PROFIT",
                expected_pnl=current_pnl,
                expected_return=current_return,
                current_price=position.current_price,
                trigger_price=position.avg_cost * (1 + self.target_profit)
            )

        # 2. 检查止损
        if self._check_stop_loss(current_return):
            logger.info(
                f"触发止损：{position.code} 收益率={current_return*100:.2f}% "
                f"≤ {-self.stop_loss*100:.1f}%"
            )
            return ExitDecision(
                should_exit=True,
                reason="STOP_LOSS",
                expected_pnl=current_pnl,
                expected_return=current_return,
                current_price=position.current_price,
                trigger_price=position.avg_cost * (1 - self.stop_loss)
            )

        # 3. 检查 Trailing Stop (如果启用)
        if self.trailing_stop:
            trailing_decision = self._check_trailing_stop(position)
            if trailing_decision:
                logger.info(
                    f"触发 Trailing Stop: {position.code} 回撤≥{self.trailing_pct*100:.1f}%"
                )
                return trailing_decision

        # 4. 检查超时退出 (动态决策)
        timeout_decision = self._check_timeout(position, current_return, indicators)
        if timeout_decision:
            return timeout_decision

        # 不退出
        return None

    def update_peak_price(self, code: str, price: float):
        """
        更新股票最高价 (用于 Trailing Stop)

        Args:
            code: 股票代码
            price: 价格
        """
        # 只更新更高的价格
        if code not in self._peak_prices or price > self._peak_prices[code]:
            old_peak = self._peak_prices.get(code)
            self._peak_prices[code] = price
            logger.debug(
                f"更新 {code} 最高价：{old_peak} → {price}"
            )

    def get_exit_config(self) -> dict:
        """
        获取当前退出配置

        Returns:
            dict: 配置字典
        """
        return {
            "target_profit": self.target_profit,
            "stop_loss": self.stop_loss,
            "timeout_hours": self.timeout_hours,
            "trailing_stop": self.trailing_stop,
            "trailing_pct": self.trailing_pct,
            "strong_uptrend_threshold": self.strong_uptrend_threshold,
            "rsi_overbought": self.rsi_overbought,
        }

    def _is_valid_position(self, position: PaperPosition) -> bool:
        """
        验证持仓是否有效

        Args:
            position: 持仓信息

        Returns:
            bool: 是否有效
        """
        if position.quantity <= 0:
            logger.debug(f"持仓无效：{position.code} 数量={position.quantity}")
            return False

        if position.current_price <= 0:
            logger.debug(f"持仓无效：{position.code} 当前价={position.current_price}")
            return False

        if position.avg_cost <= 0:
            logger.debug(f"持仓无效：{position.code} 成本价={position.avg_cost}")
            return False

        return True

    def _calculate_return(self, position: PaperPosition) -> float:
        """
        计算持仓收益率

        Args:
            position: 持仓信息

        Returns:
            float: 收益率
        """
        if position.avg_cost == 0:
            return 0.0

        return (position.current_price - position.avg_cost) / position.avg_cost

    def _calculate_pnl(self, position: PaperPosition) -> float:
        """
        计算持仓盈亏

        Args:
            position: 持仓信息

        Returns:
            float: 盈亏金额
        """
        return (position.current_price - position.avg_cost) * position.quantity

    def _check_target_profit(self, current_return: float) -> bool:
        """
        检查是否触达止盈

        Args:
            current_return: 当前收益率

        Returns:
            bool: 是否触发
        """
        return current_return >= self.target_profit

    def _check_stop_loss(self, current_return: float) -> bool:
        """
        检查是否触达止损

        Args:
            current_return: 当前收益率

        Returns:
            bool: 是否触发
        """
        return current_return <= -self.stop_loss

    def _check_trailing_stop(self, position: PaperPosition) -> Optional[ExitDecision]:
        """
        检查是否触发 Trailing Stop

        Args:
            position: 持仓信息

        Returns:
            ExitDecision: 退出决策 (如果不触发返回 None)
        """
        # 检查是否有最高价记录
        if position.code not in self._peak_prices:
            logger.debug(f"Trailing Stop: {position.code} 无最高价记录")
            return None

        peak_price = self._peak_prices[position.code]
        current_price = position.current_price

        # 计算回撤
        if peak_price <= 0:
            return None

        drawdown = (peak_price - current_price) / peak_price

        if drawdown >= self.trailing_pct:
            current_return = self._calculate_return(position)
            current_pnl = self._calculate_pnl(position)

            logger.info(
                f"Trailing Stop: {position.code} 回撤={drawdown*100:.2f}% "
                f"≥ {self.trailing_pct*100:.1f}% (最高价={peak_price}, 现价={current_price})"
            )

            return ExitDecision(
                should_exit=True,
                reason="TRAILING",
                expected_pnl=current_pnl,
                expected_return=current_return,
                current_price=current_price,
                trigger_price=peak_price * (1 - self.trailing_pct)
            )

        return None

    def _check_timeout(self, position: PaperPosition, current_return: float, indicators: Dict = None) -> Optional[ExitDecision]:
        """
        检查是否超时 (动态决策)

        新逻辑:
        - 持仓超 24 小时时，检查涨势和技术指标
        - 涨势强烈 (+>3%) + 指标正向 (RSI<70, MACD 金叉) → 继续持仓
        - 否则 → 平仓

        Args:
            position: 持仓信息
            current_return: 当前收益率
            indicators: 技术指标字典 (可选)

        Returns:
            ExitDecision: 退出决策 (如果不超时返回 None)
        """
        if not position.entry_time:
            return None

        now = datetime.now()
        holding_time = now - position.entry_time
        holding_hours = holding_time.total_seconds() / 3600

        # 未超时，不退出
        if holding_hours < self.timeout_hours:
            return None

        # 已超时，进行动态决策
        # 检查涨势是否强烈
        unrealized_return = current_return
        is_strong_uptrend = (unrealized_return > self.strong_uptrend_threshold) and (position.current_price > position.avg_cost)

        # 检查技术指标
        indicators = indicators or {}
        rsi = indicators.get('rsi', 50)
        macd_histogram = indicators.get('macd_histogram', 0)
        indicators_favorable = (rsi < self.rsi_overbought) and (macd_histogram > 0)

        if is_strong_uptrend and indicators_favorable:
            # 涨势强烈 + 指标正向 → 继续持仓
            msg = f"涨势强烈 (+{unrealized_return*100:.1f}%), 指标正向 (RSI={rsi}, MACD={macd_histogram}), 继续持仓"
            logger.info(
                f"持仓超时但涨势强烈：{position.code} 持仓{holding_hours:.1f}小时，"
                f"收益={unrealized_return*100:.1f}%, RSI={rsi}, MACD 柱={macd_histogram} → 继续持仓"
            )
            return ExitDecision(
                should_exit=False,
                reason="HOLD_STRONG",
                expected_pnl=(position.current_price - position.avg_cost) * position.quantity,
                expected_return=unrealized_return,
                current_price=position.current_price,
                trigger_price=position.current_price,
                message=msg
            )
        else:
            # 涨势弱或指标不利 → 平仓
            reason_detail = []
            if not is_strong_uptrend:
                reason_detail.append(f"涨势不足 ({unrealized_return*100:.1f}%)")
            if rsi >= self.rsi_overbought:
                reason_detail.append(f"RSI 超买 ({rsi})")
            if macd_histogram <= 0:
                reason_detail.append(f"MACD 死叉 ({macd_histogram})")

            msg = f"持仓{holding_hours:.1f}小时，{'，'.join(reason_detail)}, 平仓"
            logger.info(
                f"触发超时退出：{position.code} 持仓{holding_hours:.1f}小时，"
                f"{'，'.join(reason_detail)} → 平仓"
            )
            return ExitDecision(
                should_exit=True,
                reason="TIMEOUT",
                expected_pnl=(position.current_price - position.avg_cost) * position.quantity,
                expected_return=unrealized_return,
                current_price=position.current_price,
                trigger_price=position.current_price,
                message=msg
            )

    def clear_peak_price(self, code: str):
        """
        清除股票最高价记录 (平仓后调用)

        Args:
            code: 股票代码
        """
        if code in self._peak_prices:
            del self._peak_prices[code]
            logger.debug(f"清除 {code} 最高价记录")

    def clear_all_peak_prices(self):
        """清除所有最高价记录"""
        self._peak_prices.clear()
        logger.debug("清除所有最高价记录")
