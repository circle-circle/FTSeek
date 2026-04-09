"""
Risk-Evaluator (风险评估器)

负责风险评估、审批控制和熔断管理。
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import Agent, AgentConfig

__all__ = ["RiskOfficer", "RiskLevel", "RiskCheckResult"]


@dataclass
class RiskCheckResult:
    """风险检查结果"""

    check_name: str
    passed: bool
    risk_level: str  # LOW/MEDIUM/HIGH/CRITICAL
    message: str
    details: Optional[Dict[str, Any]] = None


class RiskOfficer(Agent):
    """
    风险评估器 Agent

    职责:
    - 评估交易风险
    - 审批交易信号
    - 监控风险指标
    - 触发熔断机制

    使用示例:
        risk_officer = RiskOfficer()
        result = await risk_officer.run({
            "task": "approve",
            "signal": {"stock": "HK.09992", "direction": "LONG", ...},
            "position": {...},
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """
        初始化风险评估器

        Args:
            config: Agent 配置
        """
        if config is None:
            config = AgentConfig(
                name="risk-evaluator",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "风险评估器",
                    "capabilities": ["risk_assessment", "approval", "circuit_breaker"],
                },
            )

        super().__init__(config)

        # 风险阈值配置
        self.risk_limits = {
            "max_single_position": 0.2,  # 单笔最大仓位 20%
            "max_total_position": 0.5,   # 总仓位最大 50%
            "max_daily_trades": 10,      # 每日最大交易次数
            "max_drawdown": 0.15,        # 最大回撤 15%
            "min_confidence": 0.3,       # 最低置信度 30%
        }

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行风险评估

        Args:
            input_data: 输入数据，包含:
                - task: 任务类型 (approve/assess/monitor)
                - signal: 交易信号
                - position: 持仓状态
                - portfolio: 组合状态

        Returns:
            风险评估结果
        """
        task = input_data.get("task", "assess")

        if task == "approve":
            return await self._approve_signal(input_data)
        elif task == "assess":
            return await self._assess_risk(input_data)
        elif task == "monitor":
            return await self._monitor_risk(input_data)
        else:
            raise ValueError(f"Unknown task: {task}")

    async def _approve_signal(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        审批交易信号

        Args:
            input_data: 输入数据

        Returns:
            审批结果
        """
        signal = input_data.get("signal", {})
        position = input_data.get("position", {})
        portfolio = input_data.get("portfolio", {})

        self.logger.info(f"Approving signal for {signal.get('stock', 'UNKNOWN')}")

        # 执行风险检查
        checks = await self._run_risk_checks(signal, position, portfolio)

        # 汇总结果
        passed = all(check.passed for check in checks)
        risk_level = self._calculate_overall_risk(checks)

        approval_result = {
            "task": "approve",
            "approved": passed,
            "risk_level": risk_level,
            "stock": signal.get("stock", "UNKNOWN"),
            "checks": [
                {
                    "name": c.check_name,
                    "passed": c.passed,
                    "message": c.message,
                }
                for c in checks
            ],
            "recommendations": self._generate_recommendations(checks),
        }

        if passed:
            self.logger.info(f"Signal approved (risk: {risk_level})")
        else:
            self.logger.warning(f"Signal rejected (risk: {risk_level})")

        return approval_result

    async def _assess_risk(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估整体风险

        Args:
            input_data: 输入数据

        Returns:
            风险评估报告
        """
        portfolio = input_data.get("portfolio", {})

        self.logger.info("Assessing portfolio risk")

        # 评估各维度风险
        market_risk = self._assess_market_risk(portfolio)
        position_risk = self._assess_position_risk(portfolio)
        liquidity_risk = self._assess_liquidity_risk(portfolio)

        overall_risk = self._calculate_overall_risk_level([
            market_risk,
            position_risk,
            liquidity_risk,
        ])

        return {
            "task": "assess",
            "overall_risk": overall_risk,
            "market_risk": market_risk,
            "position_risk": position_risk,
            "liquidity_risk": liquidity_risk,
            "recommendations": self._generate_risk_recommendations(
                market_risk, position_risk, liquidity_risk
            ),
        }

    async def _monitor_risk(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        监控实时风险

        Args:
            input_data: 输入数据

        Returns:
            风险监控状态
        """
        positions = input_data.get("positions", [])
        pnl = input_data.get("pnl", {})

        self.logger.info("Monitoring real-time risk")

        # 检查各项风险指标
        alerts = []

        # 检查回撤
        drawdown = pnl.get("drawdown", 0)
        if drawdown > self.risk_limits["max_drawdown"]:
            alerts.append({
                "level": "CRITICAL",
                "type": "drawdown",
                "message": f"回撤超限：{drawdown:.2%} > {self.risk_limits['max_drawdown']:.2%}",
            })

        # 检查持仓跌幅
        for pos in positions:
            drop = pos.get("drop", 0)
            if drop > 0.1:  # 跌幅超过 10%
                alerts.append({
                    "level": "WARNING",
                    "type": "position_drop",
                    "message": f"{pos.get('stock', 'UNKNOWN')} 跌幅 {drop:.2%}",
                })

        return {
            "task": "monitor",
            "status": "CRITICAL" if any(a["level"] == "CRITICAL" for a in alerts) else "NORMAL",
            "alerts": alerts,
            "alert_count": len(alerts),
        }

    async def _run_risk_checks(
        self, signal: Dict, position: Dict, portfolio: Dict
    ) -> List[RiskCheckResult]:
        """执行风险检查"""
        checks = []

        # 1. 置信度检查
        confidence = signal.get("confidence", 0)
        checks.append(RiskCheckResult(
            check_name="confidence_check",
            passed=confidence >= self.risk_limits["min_confidence"],
            risk_level="HIGH" if confidence < 0.2 else "MEDIUM" if confidence < 0.3 else "LOW",
            message=f"置信度：{confidence:.2%} (要求：≥{self.risk_limits['min_confidence']:.2%})",
        ))

        # 2. 仓位检查
        suggested_position = signal.get("suggested_position", 0)
        checks.append(RiskCheckResult(
            check_name="position_limit_check",
            passed=suggested_position <= self.risk_limits["max_single_position"],
            risk_level="HIGH" if suggested_position > 0.3 else "MEDIUM" if suggested_position > 0.2 else "LOW",
            message=f"建议仓位：{suggested_position:.2%} (限制：≤{self.risk_limits['max_single_position']:.2%})",
        ))

        # 3. 总仓位检查
        total_position = portfolio.get("total_position", 0)
        new_position = suggested_position
        checks.append(RiskCheckResult(
            check_name="total_position_check",
            passed=(total_position + new_position) <= self.risk_limits["max_total_position"],
            risk_level="HIGH" if (total_position + new_position) > 0.6 else "MEDIUM" if (total_position + new_position) > 0.5 else "LOW",
            message=f"总仓位：{total_position + new_position:.2%} (限制：≤{self.risk_limits['max_total_position']:.2%})",
        ))

        # 4. 交易频率检查
        daily_trades = portfolio.get("daily_trades", 0)
        checks.append(RiskCheckResult(
            check_name="trade_frequency_check",
            passed=daily_trades < self.risk_limits["max_daily_trades"],
            risk_level="MEDIUM" if daily_trades >= self.risk_limits["max_daily_trades"] else "LOW",
            message=f"今日交易：{daily_trades} 笔 (限制：≤{self.risk_limits['max_daily_trades']} 笔)",
        ))

        # 5. 回撤检查
        current_drawdown = portfolio.get("drawdown", 0)
        checks.append(RiskCheckResult(
            check_name="drawdown_check",
            passed=current_drawdown <= self.risk_limits["max_drawdown"],
            risk_level="CRITICAL" if current_drawdown > 0.2 else "HIGH" if current_drawdown > 0.15 else "LOW",
            message=f"当前回撤：{current_drawdown:.2%} (限制：≤{self.risk_limits['max_drawdown']:.2%})",
        ))

        return checks

    def _calculate_overall_risk(self, checks: List[RiskCheckResult]) -> str:
        """计算整体风险等级"""
        risk_scores = {
            "LOW": 1,
            "MEDIUM": 2,
            "HIGH": 3,
            "CRITICAL": 4,
        }

        max_risk = max(checks, key=lambda c: risk_scores.get(c.risk_level, 0))
        return max_risk.risk_level

    def _calculate_overall_risk_level(self, risks: List[str]) -> str:
        """计算整体风险等级 (字符串列表)"""
        risk_scores = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
        max_score = max(risk_scores.get(r, 0) for r in risks)
        return [k for k, v in risk_scores.items() if v == max_score][0]

    def _generate_recommendations(self, checks: List[RiskCheckResult]) -> List[str]:
        """生成建议"""
        recommendations = []

        for check in checks:
            if not check.passed:
                if check.check_name == "confidence_check":
                    recommendations.append("提高信号置信度阈值")
                elif check.check_name == "position_limit_check":
                    recommendations.append("降低单笔仓位")
                elif check.check_name == "total_position_check":
                    recommendations.append("降低总仓位")
                elif check.check_name == "trade_frequency_check":
                    recommendations.append("减少交易频率")
                elif check.check_name == "drawdown_check":
                    recommendations.append("暂停交易，控制回撤")

        return recommendations if recommendations else ["风险可控，可执行"]

    def _generate_risk_recommendations(
        self, market: str, position: str, liquidity: str
    ) -> List[str]:
        """生成风险建议"""
        recommendations = []

        if market == "HIGH":
            recommendations.append("市场风险较高，建议降低仓位")
        if position == "HIGH":
            recommendations.append("持仓集中度过高，建议分散")
        if liquidity == "HIGH":
            recommendations.append("流动性风险较高，注意仓位控制")

        return recommendations if recommendations else ["风险整体可控"]

    def _assess_market_risk(self, portfolio: Dict) -> str:
        """评估市场风险"""
        # 简单实现
        vix = portfolio.get("vix", 20)
        if vix > 30:
            return "HIGH"
        elif vix > 25:
            return "MEDIUM"
        return "LOW"

    def _assess_position_risk(self, portfolio: Dict) -> str:
        """评估持仓风险"""
        total_position = portfolio.get("total_position", 0)
        if total_position > 0.6:
            return "HIGH"
        elif total_position > 0.4:
            return "MEDIUM"
        return "LOW"

    def _assess_liquidity_risk(self, portfolio: Dict) -> str:
        """评估流动性风险"""
        cash_ratio = portfolio.get("cash_ratio", 0.5)
        if cash_ratio < 0.2:
            return "HIGH"
        elif cash_ratio < 0.3:
            return "MEDIUM"
        return "LOW"

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "signal_approval",
            "risk_assessment",
            "real_time_monitoring",
            "circuit_breaker",
        ])
        caps["risk_limits"] = self.risk_limits
        return caps
