"""
PreTradeRisk - 交易前风控系统

在交易执行前进行多维度风险检查，确保交易符合风控要求。

检查维度:
- 资金检查：确认可用资金是否充足
- 日交易次数限制：防止过度交易
- 仓位限制：控制单股和总仓位风险
- 黑名单检查：禁止交易限制股票
- 催化事件验证：确认催化事件存在性

Example:
    >>> risk = PreTradeRisk()
    >>> signal = {
    ...     'symbol': 'AAPL',
    ...     'quantity': 100,
    ...     'price': 150.0,
    ...     'side': 'buy'
    ... }
    >>> account_info = {
    ...     'available_cash': 100000.0,
    ...     'positions': {},
    ...     'total_capital': 200000.0,
    ...     'daily_trades': 0
    ... }
    >>> report = risk.full_check(signal, account_info)
    >>> if report.can_trade:
    ...     print("交易通过风控检查")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import time


@dataclass
class RiskCheckResult:
    """
    风控检查结果数据类

    Attributes:
        passed: 是否通过检查
        check_type: 检查类型 (capital/daily_limit/blacklist/catalyst/position_limit)
        message: 检查结果描述信息
        details: 检查详情数据
    """
    passed: bool
    check_type: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PreTradeRiskReport:
    """
    交易前风控报告数据类

    Attributes:
        all_passed: 所有检查是否全部通过
        checks: 各项检查结果列表
        timestamp: 检查时间戳
        can_trade: 是否可以进行交易（等同于 all_passed）
    """
    all_passed: bool
    checks: List[RiskCheckResult]
    timestamp: float
    can_trade: bool


class PreTradeRisk:
    """
    交易前风控系统

    在交易执行前进行多维度风险检查，确保交易符合预设的风控规则。

    支持的风控检查:
    1. 资金检查 - 验证可用资金是否满足交易需求
    2. 日交易次数限制 - 防止过度频繁交易
    3. 仓位限制 - 控制单股和总仓位风险敞口
    4. 黑名单检查 - 禁止交易限制股票
    5. 催化事件验证 - 确认交易标的是否有催化事件支持

    Attributes:
        config: 风控配置字典
        daily_trades: 当日已交易次数
        blacklist: 黑名单股票集合

    Example:
        >>> config = {
        ...     'max_daily_trades': 10,
        ...     'max_single_position': 0.20,
        ...     'max_total_position': 0.60,
        ...     'blacklist': ['XYZ', 'ABC']
        ... }
        >>> risk = PreTradeRisk(config=config)
    """

    # 默认风控参数配置
    DEFAULT_CONFIG = {
        'max_daily_trades': 10,           # 每日最大交易次数
        'max_single_position': 0.20,      # 单股仓位≤20%
        'max_total_position': 0.60,       # 总仓位≤60%
        'max_daily_loss': 0.03,           # 日亏损≤3%
        'blacklist': [],                  # 黑名单股票
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化交易前风控系统

        Args:
            config: 风控配置字典，可选。如果不提供则使用默认配置。
                    支持的配置项:
                    - max_daily_trades: 每日最大交易次数 (默认 10)
                    - max_single_position: 单股最大仓位比例 (默认 0.20)
                    - max_total_position: 总仓位比例 (默认 0.60)
                    - blacklist: 黑名单股票列表 (默认空列表)
        """
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.daily_trades: int = 0
        self.blacklist: set = set(self.config.get('blacklist', []))

    def check_capital(
        self,
        required: float,
        available: float
    ) -> RiskCheckResult:
        """
        检查可用资金是否充足

        Args:
            required: 交易需要的资金金额
            available: 账户可用资金金额

        Returns:
            RiskCheckResult: 资金检查结果
                - passed=True: 资金充足
                - passed=False: 资金不足

        Example:
            >>> risk = PreTradeRisk()
            >>> result = risk.check_capital(required=10000, available=50000)
            >>> result.passed
            True
        """
        passed = available >= required

        if passed:
            message = f"资金检查通过：可用资金 {available:.2f} >= 需要资金 {required:.2f}"
        else:
            shortfall = required - available
            message = (
                f"资金检查失败：可用资金 {available:.2f} < 需要资金 {required:.2f}, "
                f"资金缺口 {shortfall:.2f}"
            )

        return RiskCheckResult(
            passed=passed,
            check_type="capital",
            message=message,
            details={
                'required': required,
                'available': available,
                'shortfall': shortfall if not passed else 0.0
            }
        )

    def check_daily_limit(
        self,
        current_trades: int,
        max_limit: Optional[int] = None
    ) -> RiskCheckResult:
        """
        检查每日交易次数是否超过限制

        Args:
            current_trades: 当前已交易次数
            max_limit: 每日最大交易次数限制，如果不提供则使用配置值

        Returns:
            RiskCheckResult: 日交易次数检查结果
                - passed=True: 未超过限制
                - passed=False: 已达到或超过限制

        Example:
            >>> risk = PreTradeRisk()
            >>> result = risk.check_daily_limit(current_trades=5, max_limit=10)
            >>> result.passed
            True
        """
        limit = max_limit if max_limit is not None else self.config['max_daily_trades']
        passed = current_trades < limit

        if passed:
            remaining = limit - current_trades
            message = (
                f"日交易次数检查通过：当前 {current_trades} 次，"
                f"限制 {limit} 次，剩余 {remaining} 次"
            )
        else:
            message = (
                f"日交易次数检查失败：当前 {current_trades} 次，"
                f"已达到限制 {limit} 次"
            )

        return RiskCheckResult(
            passed=passed,
            check_type="daily_limit",
            message=message,
            details={
                'current_trades': current_trades,
                'max_limit': limit,
                'remaining': max(0, limit - current_trades)
            }
        )

    def check_position_limit(
        self,
        symbol: str,
        quantity: int,
        current_position: Dict[str, Dict[str, Any]],
        total_capital: float,
        price: Optional[float] = None
    ) -> RiskCheckResult:
        """
        检查仓位限制

        检查两项:
        1. 单股仓位不超过配置比例 (默认 20%)
        2. 总仓位不超过配置比例 (默认 60%)

        Args:
            symbol: 股票代码
            quantity: 拟买入数量
            current_position: 当前持仓字典，格式：{symbol: {'quantity': int, 'price': float}}
            total_capital: 总资金金额
            price: 股票价格，对于新股票必须提供，对于已有持仓可从持仓信息获取

        Returns:
            RiskCheckResult: 仓位限制检查结果
                - passed=True: 仓位在限制范围内
                - passed=False: 超出仓位限制

        Example:
            >>> risk = PreTradeRisk()
            >>> position = {'AAPL': {'quantity': 100, 'price': 150.0}}
            >>> result = risk.check_position_limit(
            ...     symbol='AAPL',
            ...     quantity=50,
            ...     current_position=position,
            ...     total_capital=100000.0
            ... )
        """
        max_single = self.config['max_single_position']
        max_total = self.config['max_total_position']

        # 计算当前持仓市值
        current_total_value = sum(
            pos['quantity'] * pos['price']
            for pos in current_position.values()
        )

        # 获取股票价格
        if symbol in current_position:
            price = current_position[symbol]['price']
            current_symbol_value = current_position[symbol]['quantity'] * price
        else:
            # 新股票，使用传入的价格
            current_symbol_value = 0
            if price is None:
                # 如果没有价格，无法计算，假设价格为 0
                price = 0

        # 计算新仓位价值
        trade_value = quantity * price
        new_symbol_value = current_symbol_value + trade_value
        new_total_value = current_total_value + trade_value

        # 检查单股仓位限制
        single_ratio = new_symbol_value / total_capital if total_capital > 0 else 1.0
        single_passed = single_ratio <= max_single

        # 检查总仓位限制
        total_ratio = new_total_value / total_capital if total_capital > 0 else 1.0
        total_passed = total_ratio <= max_total

        passed = single_passed and total_passed

        details = {
            'symbol': symbol,
            'current_symbol_value': current_symbol_value,
            'new_symbol_value': new_symbol_value,
            'single_ratio': single_ratio,
            'max_single_ratio': max_single,
            'current_total_value': current_total_value,
            'new_total_value': new_total_value,
            'total_ratio': total_ratio,
            'max_total_ratio': max_total,
        }

        if passed:
            message = (
                f"仓位检查通过：单股 {single_ratio:.1%} <= {max_single:.1%}, "
                f"总仓位 {total_ratio:.1%} <= {max_total:.1%}"
            )
        else:
            reasons = []
            if not single_passed:
                reasons.append(f"单股仓位 {single_ratio:.1%} > {max_single:.1%}")
            if not total_passed:
                reasons.append(f"总仓位 {total_ratio:.1%} > {max_total:.1%}")
            message = f"仓位检查失败：{', '.join(reasons)}"

        return RiskCheckResult(
            passed=passed,
            check_type="position_limit",
            message=message,
            details=details
        )

    def check_blacklist(self, symbol: str) -> RiskCheckResult:
        """
        检查股票是否在黑名单中

        Args:
            symbol: 股票代码

        Returns:
            RiskCheckResult: 黑名单检查结果
                - passed=True: 股票不在黑名单
                - passed=False: 股票在黑名单中

        Example:
            >>> risk = PreTradeRisk()
            >>> risk.blacklist.add('XYZ')
            >>> result = risk.check_blacklist('XYZ')
            >>> result.passed
            False
        """
        passed = symbol not in self.blacklist

        if passed:
            message = f"黑名单检查通过：{symbol} 不在黑名单中"
        else:
            message = f"黑名单检查失败：{symbol} 在黑名单中，禁止交易"

        return RiskCheckResult(
            passed=passed,
            check_type="blacklist",
            message=message,
            details={
                'symbol': symbol,
                'in_blacklist': not passed,
                'blacklist_size': len(self.blacklist)
            }
        )

    def validate_catalyst(self, symbol: str) -> RiskCheckResult:
        """
        验证催化事件是否存在

        使用 CatalystTracker 确认股票是否有即将发生的催化事件。
        注意：这是一个简化实现，实际应集成 CatalystTracker。

        Args:
            symbol: 股票代码

        Returns:
            RiskCheckResult: 催化事件验证结果
                - passed=True: 存在催化事件或无需验证
                - passed=False: 无催化事件

        Example:
            >>> risk = PreTradeRisk()
            >>> result = risk.validate_catalyst('AAPL')
            >>> result.check_type
            'catalyst'
        """
        # 简化实现：默认通过
        # 实际实现应集成 CatalystTracker 进行验证
        # 这里仅做基本结构验证
        passed = True
        message = f"催化事件验证通过：{symbol} 无需强制催化事件要求"

        return RiskCheckResult(
            passed=passed,
            check_type="catalyst",
            message=message,
            details={
                'symbol': symbol,
                'catalyst_required': False,
                'note': '简化实现，默认通过'
            }
        )

    def full_check(
        self,
        signal: Dict[str, Any],
        account_info: Dict[str, Any]
    ) -> PreTradeRiskReport:
        """
        执行完整的交易前风控检查

        按顺序执行以下检查:
        1. 资金检查 - 验证可用资金是否充足
        2. 日交易次数检查 - 验证是否超过日交易限制
        3. 仓位限制检查 - 验证单股和总仓位是否在限制内
        4. 黑名单检查 - 验证股票是否在黑名单
        5. 催化事件验证 - 验证是否有催化事件支持

        Args:
            signal: 交易信号字典，包含:
                    - symbol: 股票代码
                    - quantity: 交易数量
                    - price: 交易价格
                    - side: 交易方向 (buy/sell)
            account_info: 账户信息字典，包含:
                    - available_cash: 可用资金
                    - positions: 当前持仓
                    - total_capital: 总资金
                    - daily_trades: 当日已交易次数

        Returns:
            PreTradeRiskReport: 完整的风控检查报告
                - all_passed: 所有检查是否通过
                - checks: 各项检查结果列表
                - timestamp: 检查时间戳
                - can_trade: 是否可以进行交易

        Example:
            >>> risk = PreTradeRisk()
            >>> signal = {
            ...     'symbol': 'AAPL',
            ...     'quantity': 100,
            ...     'price': 150.0,
            ...     'side': 'buy'
            ... }
            >>> account_info = {
            ...     'available_cash': 100000.0,
            ...     'positions': {},
            ...     'total_capital': 200000.0,
            ...     'daily_trades': 0
            ... }
            >>> report = risk.full_check(signal, account_info)
            >>> report.can_trade
            True
        """
        checks: List[RiskCheckResult] = []

        symbol = signal.get('symbol', '')
        quantity = signal.get('quantity', 0)
        price = signal.get('price', 0.0)
        required_capital = quantity * price

        available_cash = account_info.get('available_cash', 0.0)
        positions = account_info.get('positions', {})
        total_capital = account_info.get('total_capital', 1.0)
        daily_trades = account_info.get('daily_trades', 0)

        # 1. 资金检查
        capital_result = self.check_capital(
            required=required_capital,
            available=available_cash
        )
        checks.append(capital_result)

        # 2. 日交易次数检查
        daily_limit_result = self.check_daily_limit(
            current_trades=daily_trades
        )
        checks.append(daily_limit_result)

        # 3. 仓位限制检查
        position_result = self.check_position_limit(
            symbol=symbol,
            quantity=quantity,
            current_position=positions,
            total_capital=total_capital,
            price=price
        )
        checks.append(position_result)

        # 4. 黑名单检查
        blacklist_result = self.check_blacklist(symbol)
        checks.append(blacklist_result)

        # 5. 催化事件验证
        catalyst_result = self.validate_catalyst(symbol)
        checks.append(catalyst_result)

        # 汇总结果
        all_passed = all(check.passed for check in checks)

        return PreTradeRiskReport(
            all_passed=all_passed,
            checks=checks,
            timestamp=time.time(),
            can_trade=all_passed
        )
