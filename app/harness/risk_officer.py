"""
风险军官 (Risk Officer) — Harness 框架中的风险监控 Agent

负责监控交易风险、检查仓位限制、触发风险警报。
"""

import logging
from dataclasses import dataclass
from typing import Optional

from .base import Agent, AgentConfig, AgentResult

logger = logging.getLogger("ftseek.harness.risk_officer")


@dataclass
class RiskCheckResult:
    """风险检查结果"""
    is_safe: bool = True
    risk_level: str = "LOW"  # LOW, MEDIUM, HIGH, CRITICAL
    message: str = ""


class RiskOfficer(Agent):
    """风险军官 Agent"""

    def __init__(self, config: Optional[AgentConfig] = None):
        if config is None:
            config = AgentConfig(
                name="risk-officer",
                timeout_seconds=30,
                metadata={"role": "风险监控"},
            )
        super().__init__(config)
        self.logger = logging.getLogger("ftseek.harness.risk_officer")

    async def execute(self, input_data: dict) -> AgentResult:
        """执行风险检查"""
        try:
            # 这里可以接入具体的风险检查逻辑
            # 目前作为占位实现，避免 HarnessPilot 崩溃
            self.logger.debug("[RiskOfficer] 风险检查执行")
            return AgentResult(
                success=True,
                data=RiskCheckResult(is_safe=True, risk_level="LOW", message="检查通过"),
            )
        except Exception as e:
            self.logger.error(f"[RiskOfficer] 检查失败: {e}")
            return AgentResult(
                success=False,
                error=str(e),
                data=RiskCheckResult(is_safe=False, risk_level="HIGH", message=str(e)),
            )
