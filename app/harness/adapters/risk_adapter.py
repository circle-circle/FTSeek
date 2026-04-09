"""
Risk Adapter - 风险管理模块适配器

将现有 risk_manager 模块适配为 RiskOfficer Agent。
"""

import logging
from typing import Any, Dict, Optional

from ..base import AgentConfig
from .base_adapter import BaseAdapter

__all__ = ["RiskAdapter"]


class RiskAdapter(BaseAdapter):
    """
    风险适配器

    将 RiskManager 适配为 RiskOfficer Agent。

    使用示例:
        adapter = RiskAdapter()
        result = await adapter.run({
            "task": "assess",
            "portfolio": {...},
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """初始化风险适配器"""
        if config is None:
            config = AgentConfig(
                name="risk-adapter",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "风险适配器",
                    "adapts": "app.risk.risk_manager",
                },
            )

        super().__init__(config, module_name="risk")
        self._risk_manager = None

    async def _load_module(self, module_path: str) -> Any:
        """加载风险管理模块"""
        try:
            from app.risk.risk_manager import RiskManager
            self._risk_manager = RiskManager()
            self.logger.info("RiskManager loaded successfully")
            return self._risk_manager
        except ImportError as e:
            self.logger.warning(f"Failed to load RiskManager: {e}")
            self._risk_manager = None
            raise

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        适配执行

        Args:
            input_data: 输入数据

        Returns:
            执行结果
        """
        task = input_data.get("task", "assess")

        self.logger.info(f"RiskAdapter executing task: {task}")

        if not self._risk_manager:
            self.logger.warning("RiskManager not loaded, using fallback")
            return self._fallback_assessment(input_data)

        try:
            if task == "assess":
                return await self._assess(input_data)
            elif task == "check":
                return await self._check_limits(input_data)
            elif task == "monitor":
                return await self._monitor(input_data)
            else:
                raise ValueError(f"Unknown task: {task}")
        except Exception as e:
            self.logger.error(f"RiskAdapter execution failed: {e}", exc_info=True)
            return self._fallback_assessment(input_data)

    async def _assess(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """风险评估"""
        portfolio = input_data.get("portfolio", {})

        try:
            assessment = await self._risk_manager.assess_portfolio(portfolio)
            return {
                "task": "assess",
                "assessment": assessment,
                "adapter": "risk",
            }
        except Exception as e:
            self.logger.error(f"Assessment failed: {e}")
            return self._fallback_assessment(input_data)

    async def _check_limits(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """检查限制"""
        position = input_data.get("position", {})

        try:
            result = await self._risk_manager.check_position_limits(position)
            return {
                "task": "check",
                "result": result,
                "adapter": "risk",
            }
        except Exception as e:
            self.logger.error(f"Limits check failed: {e}")
            return {"error": str(e), "task": "check"}

    async def _monitor(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """风险监控"""
        positions = input_data.get("positions", [])

        try:
            monitoring = await self._risk_manager.monitor_positions(positions)
            return {
                "task": "monitor",
                "monitoring": monitoring,
                "adapter": "risk",
            }
        except Exception as e:
            self.logger.error(f"Monitoring failed: {e}")
            return {"error": str(e), "task": "monitor"}

    def _fallback_assessment(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """降级评估"""
        return {
            "task": input_data.get("task", "assess"),
            "assessment": {
                "overall_risk": "MEDIUM",
                "market_risk": "LOW",
                "position_risk": "LOW",
                "liquidity_risk": "LOW",
                "note": "Fallback assessment (risk manager not available)",
            },
            "adapter": "risk",
            "fallback": True,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "portfolio_assessment",
            "limits_check",
            "position_monitoring",
        ])
        caps["risk_manager_loaded"] = self._risk_manager is not None
        return caps
