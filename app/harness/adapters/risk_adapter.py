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
        """风险评估 — 映射到 RiskManager.check_system() + get_risk_metrics()"""
        portfolio = input_data.get("portfolio", {})

        try:
            # RiskManager 是同步的，不是 async
            check_result = self._risk_manager.check_system()
            metrics = self._risk_manager.get_risk_metrics()

            # 映射 RiskManager 的 CheckResult 到 adapter 格式
            overall_risk = "LOW"
            if not check_result.passed:
                if any(v > 0.8 for v in [
                    metrics.get("daily_loss_ratio", 0),
                    metrics.get("drawdown_ratio", 0),
                ]):
                    overall_risk = "CRITICAL"
                else:
                    overall_risk = "HIGH"

            return {
                "task": "assess",
                "assessment": {
                    "overall_risk": overall_risk,
                    "passed": check_result.passed,
                    "checks": {
                        "system_check": {
                            "passed": check_result.passed,
                            "reason": check_result.reason,
                            "level": check_result.level,
                            "details": check_result.details,
                        },
                    },
                    "metrics": metrics,
                },
                "adapter": "risk",
            }
        except Exception as e:
            self.logger.error(f"Assessment failed: {e}")
            return self._fallback_assessment(input_data)

    async def _check_limits(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """检查限制 — 映射到 RiskManager.check_position() + check_account()"""
        position = input_data.get("position", {})

        try:
            # RiskManager 是同步的，不是 async
            pos_result = self._risk_manager.check_position()
            acct_result = self._risk_manager.check_account()

            return {
                "task": "check",
                "result": {
                    "position_check": {
                        "passed": pos_result.passed,
                        "reason": pos_result.reason,
                        "level": pos_result.level,
                        "details": pos_result.details,
                    },
                    "account_check": {
                        "passed": acct_result.passed,
                        "reason": acct_result.reason,
                        "level": acct_result.level,
                        "details": acct_result.details,
                    },
                },
                "adapter": "risk",
            }
        except Exception as e:
            self.logger.error(f"Limits check failed: {e}")
            return {"error": str(e), "task": "check"}

    async def _monitor(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """风险监控 — 映射到 RiskManager.check_system() + get_closed_trades_today()"""
        positions = input_data.get("positions", [])

        try:
            # RiskManager 是同步的，不是 async
            sys_result = self._risk_manager.check_system()
            metrics = self._risk_manager.get_risk_metrics()
            today_trades = self._risk_manager.get_closed_trades_today()

            return {
                "task": "monitor",
                "monitoring": {
                    "system_status": "PASS" if sys_result.passed else "FAIL",
                    "today_trade_count": len(today_trades),
                    "today_trades": today_trades[:10],  # 限制返回数量
                    "metrics": metrics,
                },
                "adapter": "risk",
            }
        except Exception as e:
            self.logger.error(f"Monitoring failed: {e}")
            return {"error": str(e), "task": "monitor"}

    def _fallback_assessment(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """降级评估 — fail-closed: RiskManager 不可用时返回 CRITICAL 而非 MEDIUM"""
        return {
            "task": input_data.get("task", "assess"),
            "assessment": {
                "overall_risk": "CRITICAL",
                "market_risk": "HIGH",
                "position_risk": "HIGH",
                "liquidity_risk": "HIGH",
                "note": "FAIL-CLOSED: RiskManager unavailable — rejecting all trades until restored",
                "fail_closed": True,
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
