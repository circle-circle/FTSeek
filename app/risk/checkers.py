"""
风控检查器模块
负责各种风险检查逻辑
"""
import logging
from typing import Dict, Optional, Any, List
from datetime import datetime, timedelta

from app.risk.metrics import RiskConfig, RiskLimit, RiskMetrics, CheckResult, RiskCheckResult

logger = logging.getLogger("ftseek.risk.checkers")
logger.propagate = False


class PositionChecker:
    """仓位检查器"""
    
    def __init__(self, limits: RiskLimit):
        """
        初始化仓位检查器
        
        Args:
            limits: 风险限制
        """
        self.limits = limits
    
    def check_position_limit(self, code: str, quantity: int, current_price: float,
                            total_capital: float, existing_positions: Dict) -> CheckResult:
        """
        检查仓位限制
        
        Args:
            code: 股票代码
            quantity: 交易数量
            current_price: 当前价格
            total_capital: 总资金
            existing_positions: 现有持仓
        
        Returns:
            检查结果
        """
        # 计算单笔仓位占比
        position_value = quantity * current_price
        position_ratio = position_value / total_capital if total_capital > 0 else 0
        
        if position_ratio > self.limits.max_single_position_pct:
            return CheckResult(
                passed=False,
                reason=f"单笔仓位超限：{position_ratio:.1%} > {self.limits.max_single_position_pct:.1%}",
                level="single_position",
                details={
                    'position_ratio': position_ratio,
                    'limit': self.limits.max_single_position_pct,
                }
            )
        
        # 计算总仓位占比
        total_position_value = sum(
            pos.get('quantity', 0) * pos.get('current_price', 0)
            for pos in existing_positions.values()
        )
        total_position_value += position_value
        total_position_ratio = total_position_value / total_capital if total_capital > 0 else 0
        
        if total_position_ratio > self.limits.max_total_position_pct:
            return CheckResult(
                passed=False,
                reason=f"总仓位超限：{total_position_ratio:.1%} > {self.limits.max_total_position_pct:.1%}",
                level="total_position",
                details={
                    'total_position_ratio': total_position_ratio,
                    'limit': self.limits.max_total_position_pct,
                }
            )
        
        return CheckResult(passed=True, reason="仓位检查通过")


class TradeLimitChecker:
    """交易限制检查器"""
    
    def __init__(self, limits: RiskLimit):
        """
        初始化交易限制检查器
        
        Args:
            limits: 风险限制
        """
        self.limits = limits
    
    def check_daily_trades(self, daily_trade_count: int) -> CheckResult:
        """
        检查日交易次数
        
        Args:
            daily_trade_count: 当日交易次数
        
        Returns:
            检查结果
        """
        if daily_trade_count >= self.limits.max_daily_trades:
            return CheckResult(
                passed=False,
                reason=f"日交易次数超限：{daily_trade_count} >= {self.limits.max_daily_trades}",
                level="daily_trades",
                details={
                    'daily_trade_count': daily_trade_count,
                    'limit': self.limits.max_daily_trades,
                }
            )
        
        return CheckResult(passed=True, reason="日交易次数检查通过")
    
    def check_daily_loss(self, daily_pnl: float, total_capital: float) -> CheckResult:
        """
        检查日亏损
        
        Args:
            daily_pnl: 当日盈亏
            total_capital: 总资金
        
        Returns:
            检查结果
        """
        if total_capital <= 0:
            return CheckResult(passed=True, reason="总资金为 0，跳过检查")
        
        daily_loss_pct = -daily_pnl / total_capital if daily_pnl < 0 else 0
        
        if daily_loss_pct > self.limits.max_daily_loss_pct:
            return CheckResult(
                passed=False,
                reason=f"日亏损超限：{daily_loss_pct:.1%} > {self.limits.max_daily_loss_pct:.1%}",
                level="daily_loss",
                details={
                    'daily_loss_pct': daily_loss_pct,
                    'limit': self.limits.max_daily_loss_pct,
                }
            )
        
        return CheckResult(passed=True, reason="日亏损检查通过")
    
    def check_weekly_loss(self, weekly_pnl: float, total_capital: float) -> CheckResult:
        """
        检查周亏损
        
        Args:
            weekly_pnl: 当周盈亏
            total_capital: 总资金
        
        Returns:
            检查结果
        """
        if total_capital <= 0:
            return CheckResult(passed=True, reason="总资金为 0，跳过检查")
        
        weekly_loss_pct = -weekly_pnl / total_capital if weekly_pnl < 0 else 0
        
        if weekly_loss_pct > self.limits.max_weekly_loss_pct:
            return CheckResult(
                passed=False,
                reason=f"周亏损超限：{weekly_loss_pct:.1%} > {self.limits.max_weekly_loss_pct:.1%}",
                level="weekly_loss",
                details={
                    'weekly_loss_pct': weekly_loss_pct,
                    'limit': self.limits.max_weekly_loss_pct,
                }
            )
        
        return CheckResult(passed=True, reason="周亏损检查通过")


class DrawdownChecker:
    """回撤检查器"""
    
    def __init__(self, limits: RiskLimit):
        """
        初始化回撤检查器
        
        Args:
            limits: 风险限制
        """
        self.limits = limits
    
    def check_drawdown(self, current_capital: float, peak_capital: float) -> CheckResult:
        """
        检查回撤
        
        Args:
            current_capital: 当前资金
            peak_capital: 峰值资金
        
        Returns:
            检查结果
        """
        if peak_capital <= 0:
            return CheckResult(passed=True, reason="峰值资金为 0，跳过检查")
        
        drawdown = (peak_capital - current_capital) / peak_capital
        
        if drawdown > self.limits.max_drawdown_pct:
            return CheckResult(
                passed=False,
                reason=f"回撤超限：{drawdown:.1%} > {self.limits.max_drawdown_pct:.1%}",
                level="drawdown",
                details={
                    'drawdown': drawdown,
                    'limit': self.limits.max_drawdown_pct,
                    'current_capital': current_capital,
                    'peak_capital': peak_capital,
                }
            )
        
        return CheckResult(passed=True, reason="回撤检查通过")


class ConsecutiveLossChecker:
    """连亏检查器"""
    
    def __init__(self, limits: RiskLimit):
        """
        初始化连亏检查器
        
        Args:
            limits: 风险限制
        """
        self.limits = limits
    
    def check_consecutive_losses(self, consecutive_losses: int) -> CheckResult:
        """
        检查连续亏损次数
        
        Args:
            consecutive_losses: 连续亏损次数
        
        Returns:
            检查结果
        """
        if consecutive_losses >= self.limits.consecutive_loss_limit:
            return CheckResult(
                passed=False,
                reason=f"连续亏损超限：{consecutive_losses} >= {self.limits.consecutive_loss_limit}",
                level="consecutive_loss",
                details={
                    'consecutive_losses': consecutive_losses,
                    'limit': self.limits.consecutive_loss_limit,
                }
            )
        
        return CheckResult(passed=True, reason="连续亏损检查通过")


class ConfidenceChecker:
    """置信度检查器"""

    def __init__(self, limits: RiskLimit):
        """
        初始化置信度检查器

        Args:
            limits: 风险限制
        """
        self.limits = limits

    def check_confidence(self, confidence: float) -> CheckResult:
        """
        检查置信度

        Args:
            confidence: 置信度

        Returns:
            检查结果
        """
        if confidence < self.limits.min_confidence:
            return CheckResult(
                passed=False,
                reason=f"置信度不足：{confidence:.2f} < {self.limits.min_confidence:.2f}",
                level="confidence",
                details={
                    'confidence': confidence,
                    'limit': self.limits.min_confidence,
                }
            )

        return CheckResult(passed=True, reason="置信度检查通过")


class ConcentrationChecker:
    """集中度风险检查器 - 检查标的间的相关性"""

    def __init__(self, limits: RiskLimit):
        """
        初始化集中度检查器

        Args:
            limits: 风险限制
        """
        self.limits = limits

        # 股票相关性分组 (简化版，实际可以用历史收益率计算相关系数)
        # 港股科技股集群
        self.correlation_clusters = {
            "tech_hk": ["HK.00700", "HK.03690", "HK.09988", "HK.09992", "HK.01810", "HK.09618"],
            "finance_hk": ["HK.00005", "HK.00012", "HK.00388", "HK.02318", "HK.03968"],
            "property_hk": ["HK.00001", "HK.00002", "HK.00006", "HK.00011", "HK.00016"],
            "us_tech": ["US.AAPL", "US.MSFT", "US.GOOG", "US.AMZN", "US.META", "US.NVDA", "US.TSLA"],
            "us_finance": ["US.JPM", "US.BAC", "US.WFC", "US.GS", "US.MS"],
        }

        # 集群仓位上限 (默认 35%)
        self.cluster_limit = 0.35

    def _get_cluster_name(self, code: str) -> Optional[str]:
        """获取股票所属的集群名称"""
        for cluster_name, stocks in self.correlation_clusters.items():
            if code.upper() in [s.upper() for s in stocks]:
                return cluster_name
        return None

    def _calculate_correlation(self, code1: str, code2: str) -> float:
        """
        计算两只股票的相关系数 (简化版)

        实际部署时可以从数据库获取历史价格计算真实相关系数
        这里使用简化规则:
        - 同一集群：0.7-0.9
        - 不同集群但同市场：0.3-0.5
        - 不同市场：0.1-0.3
        """
        cluster1 = self._get_cluster_name(code1)
        cluster2 = self._get_cluster_name(code2)

        if cluster1 and cluster2 and cluster1 == cluster2:
            # 同一集群，高相关
            return 0.8

        if code1.startswith("HK.") and code2.startswith("HK."):
            # 同市场，中等相关
            return 0.4

        if code1.startswith("US.") and code2.startswith("US."):
            # 同市场，中等相关
            return 0.4

        # 不同市场，低相关
        return 0.2

    def check_concentration_risk(self, code: str, position_value: float,
                                 existing_positions: Dict,
                                 total_capital: float) -> CheckResult:
        """
        检查集中度风险

        Args:
            code: 新股票代码
            position_value: 新仓位价值
            existing_positions: 现有持仓 {code: {quantity, current_price, ...}}
            total_capital: 总资金

        Returns:
            检查结果
        """
        if total_capital <= 0:
            return CheckResult(passed=True, reason="总资金为 0，跳过检查")

        # 1. 获取新股票的集群
        new_cluster = self._get_cluster_name(code)

        # 2. 计算现有持仓中与之高度相关的股票
        high_corr_positions = []
        for pos_code, pos_data in existing_positions.items():
            corr = self._calculate_correlation(code, pos_code)
            if corr > 0.7:  # 高度相关阈值
                pos_value = pos_data.get('quantity', 0) * pos_data.get('current_price', 0)
                high_corr_positions.append({
                    'code': pos_code,
                    'value': pos_value,
                    'correlation': corr,
                })

        # 3. 计算集群总暴露
        cluster_exposure = sum(p['value'] for p in high_corr_positions)
        cluster_exposure += position_value  # 加上新仓位
        cluster_ratio = cluster_exposure / total_capital

        # 4. 检查是否超过集群上限
        if cluster_ratio > self.cluster_limit and len(high_corr_positions) > 0:
            details = {
                'cluster': new_cluster or '未分类',
                'cluster_exposure': cluster_exposure,
                'cluster_ratio': cluster_ratio,
                'limit': self.cluster_limit,
                'correlated_stocks': [p['code'] for p in high_corr_positions],
            }
            return CheckResult(
                passed=False,
                reason=f"集中度风险：{code} 与 {len(high_corr_positions)} 只持仓股票高度相关，集群暴露 {cluster_ratio:.1%} > {self.cluster_limit:.1%}",
                level="concentration",
                details=details
            )

        # 5. 如果没有明显相关股票，但属于已知集群，检查该集群总暴露
        if new_cluster:
            cluster_stocks = self.correlation_clusters.get(new_cluster, [])
            cluster_value = sum(
                pos_data.get('quantity', 0) * pos_data.get('current_price', 0)
                for pos_code, pos_data in existing_positions.items()
                if any(pos_code.upper() == s.upper() for s in cluster_stocks)
            )
            cluster_value += position_value
            cluster_ratio = cluster_value / total_capital

            if cluster_ratio > self.cluster_limit:
                return CheckResult(
                    passed=False,
                    reason=f"集群 {new_cluster} 总暴露超限：{cluster_ratio:.1%} > {self.cluster_limit:.1%}",
                    level="concentration",
                    details={
                        'cluster': new_cluster,
                        'cluster_exposure': cluster_value,
                        'cluster_ratio': cluster_ratio,
                        'limit': self.cluster_limit,
                    }
                )

        return CheckResult(passed=True, reason="集中度风险检查通过")


class RiskCheckEngine:
    """风控检查引擎 - 组合所有检查器"""

    def __init__(self, config: Optional[RiskConfig] = None):
        """
        初始化风控检查引擎

        Args:
            config: 风控配置
        """
        self.config = config or RiskConfig()
        self.limits = RiskLimit(
            max_single_position_pct=self.config.max_single_position,
            max_single_loss_pct=self.config.max_single_loss,
            max_total_position_pct=self.config.max_total_position,
            max_daily_trades=self.config.max_daily_trades,
            max_daily_loss_pct=self.config.max_daily_loss,
            max_weekly_loss_pct=self.config.max_weekly_loss,
            max_drawdown_pct=self.config.max_drawdown,
            consecutive_loss_limit=self.config.consecutive_loss_limit,
            min_confidence=self.config.min_confidence,
        )

        # 初始化检查器
        self.position_checker = PositionChecker(self.limits)
        self.trade_limit_checker = TradeLimitChecker(self.limits)
        self.drawdown_checker = DrawdownChecker(self.limits)
        self.consecutive_loss_checker = ConsecutiveLossChecker(self.limits)
        self.confidence_checker = ConfidenceChecker(self.limits)
        self.concentration_checker = ConcentrationChecker(self.limits)

        logger.info("风控检查引擎已初始化 (含集中度风险检查)")
    
    def check_before_trade(self, signal: Any, metrics: RiskMetrics,
                          existing_positions: Dict) -> RiskCheckResult:
        """
        交易前风控检查
        
        Args:
            signal: 交易信号
            metrics: 风险指标
            existing_positions: 现有持仓
        
        Returns:
            检查结果
        """
        details = {}
        all_passed = True
        
        # 1. 置信度检查
        confidence = getattr(signal, 'extra', {}).get('confidence', 0.5)
        confidence_result = self.confidence_checker.check_confidence(confidence)
        details['confidence'] = confidence_result.passed
        if not confidence_result.passed:
            all_passed = False
            logger.warning(f"[风控] {confidence_result.reason}")
        
        # 2. 仓位检查
        code = getattr(signal, 'code', '')
        quantity = getattr(signal, 'quantity', 0)
        price = getattr(signal, 'price', 0)
        position_result = self.position_checker.check_position_limit(
            code, quantity, price, metrics.total_capital, existing_positions
        )
        details['position'] = position_result.passed
        if not position_result.passed:
            all_passed = False
            logger.warning(f"[风控] {position_result.reason}")
        
        # 3. 日交易次数检查
        trades_result = self.trade_limit_checker.check_daily_trades(metrics.daily_trades)
        details['daily_trades'] = trades_result.passed
        if not trades_result.passed:
            all_passed = False
            logger.warning(f"[风控] {trades_result.reason}")
        
        # 4. 日亏损检查
        daily_loss_result = self.trade_limit_checker.check_daily_loss(metrics.daily_pnl, metrics.total_capital)
        details['daily_loss'] = daily_loss_result.passed
        if not daily_loss_result.passed:
            all_passed = False
            logger.warning(f"[风控] {daily_loss_result.reason}")
        
        # 5. 回撤检查
        drawdown_result = self.drawdown_checker.check_drawdown(
            metrics.total_capital + metrics.total_pnl,
            metrics.total_capital
        )
        details['drawdown'] = drawdown_result.passed
        if not drawdown_result.passed:
            all_passed = False
            logger.warning(f"[风控] {drawdown_result.reason}")
        
        # 6. 连亏检查
        consecutive_result = self.consecutive_loss_checker.check_consecutive_losses(metrics.consecutive_losses)
        details['consecutive_loss'] = consecutive_result.passed
        if not consecutive_result.passed:
            all_passed = False
            logger.warning(f"[风控] {consecutive_result.reason}")

        # 7. 集中度风险检查 (新增)
        position_value = quantity * price
        concentration_result = self.concentration_checker.check_concentration_risk(
            code, position_value, existing_positions, metrics.total_capital
        )
        details['concentration'] = concentration_result.passed
        if not concentration_result.passed:
            all_passed = False
            logger.warning(f"[风控] {concentration_result.reason}")

        return RiskCheckResult(
            passed=all_passed,
            details=details,
            timestamp=datetime.now(),
        )


# 工厂函数
def create_check_engine(config: Optional[RiskConfig] = None) -> RiskCheckEngine:
    """
    创建风控检查引擎
    
    Args:
        config: 风控配置
    
    Returns:
        RiskCheckEngine 实例
    """
    return RiskCheckEngine(config)
