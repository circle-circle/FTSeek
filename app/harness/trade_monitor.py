"""
Trade-Monitor (交易监控器)

负责实时交易监控、数据收集和告警。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import Agent, AgentConfig

__all__ = ["TradeMonitor"]


class TradeMonitor(Agent):
    """
    交易监控器 Agent

    职责:
    - 实时监控交易执行
    - 定期收集交易数据
    - 检测异常情况
    - 触发告警

    使用示例:
        monitor = TradeMonitor()
        result = await monitor.run({
            "task": "collect",
            "market": "HK",
            "period": "30m",
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """
        初始化交易监控器

        Args:
            config: Agent 配置
        """
        if config is None:
            config = AgentConfig(
                name="trade-monitor",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "交易监控器",
                    "capabilities": ["real_time_monitoring", "data_collection", "alerting"],
                },
            )

        super().__init__(config)

        # 监控配置
        self.monitoring_config = {
            "collection_interval": 1800,  # 30 分钟
            "alert_thresholds": {
                "position_drop": 0.1,  # 持仓跌幅 10%
                "daily_loss": 0.05,    # 日亏损 5%
                "trade_frequency": 10,  # 交易频率
            },
        }

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行监控任务

        Args:
            input_data: 输入数据，包含:
                - task: 任务类型 (collect/monitor/alert/status)
                - market: 市场 (HK/US)
                - period: 周期

        Returns:
            监控结果
        """
        task = input_data.get("task", "monitor")

        if task == "collect":
            return await self._collect_data(input_data)
        elif task == "monitor":
            return await self._monitor_realtime(input_data)
        elif task == "alert":
            return await self._check_alerts(input_data)
        elif task == "status":
            return await self._get_status(input_data)
        else:
            raise ValueError(f"Unknown task: {task}")

    async def _collect_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        收集交易数据

        Args:
            input_data: 输入数据

        Returns:
            收集结果
        """
        market = input_data.get("market", "HK")
        start_time = input_data.get("start_time")
        end_time = input_data.get("end_time", datetime.now().isoformat())

        self.logger.info(f"Collecting data for {market} market")

        # 简化实现：返回示例数据
        trades = self._fetch_trades(market, start_time, end_time)

        return {
            "task": "collect",
            "market": market,
            "start_time": start_time,
            "end_time": end_time,
            "trades": trades,
            "trade_count": len(trades),
            "collection_time": datetime.now().isoformat(),
        }

    async def _monitor_realtime(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        实时监控

        Args:
            input_data: 输入数据

        Returns:
            监控状态
        """
        positions = input_data.get("positions", [])
        pnl = input_data.get("pnl", {})

        self.logger.info("Monitoring real-time trading")

        # 检查各项指标
        alerts = []

        # 检查持仓跌幅
        for pos in positions:
            drop = pos.get("drop", 0)
            if drop > self.monitoring_config["alert_thresholds"]["position_drop"]:
                alerts.append({
                    "type": "position_drop",
                    "level": "WARNING",
                    "stock": pos.get("stock", "UNKNOWN"),
                    "message": f"跌幅 {drop:.2%} 超过阈值",
                })

        # 检查日亏损
        daily_pnl = pnl.get("daily_pnl", 0)
        daily_loss_limit = self.monitoring_config["alert_thresholds"]["daily_loss"]
        if daily_pnl < -daily_loss_limit:
            alerts.append({
                "type": "daily_loss",
                "level": "CRITICAL",
                "message": f"日亏损 {daily_pnl:.2%} 超过阈值",
            })

        return {
            "task": "monitor",
            "status": "CRITICAL" if any(a["level"] == "CRITICAL" for a in alerts) else "WARNING" if alerts else "NORMAL",
            "positions": positions,
            "pnl": pnl,
            "alerts": alerts,
            "alert_count": len(alerts),
        }

    async def _check_alerts(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        检查告警

        Args:
            input_data: 输入数据

        Returns:
            告警结果
        """
        metrics = input_data.get("metrics", {})

        self.logger.info("Checking alert conditions")

        alerts = []

        # 检查各种告警条件
        if metrics.get("drawdown", 0) > 0.15:
            alerts.append({
                "type": "drawdown",
                "level": "CRITICAL",
                "message": f"回撤 {metrics['drawdown']:.2%} 超过 15%",
            })

        if metrics.get("trade_count", 0) > self.monitoring_config["alert_thresholds"]["trade_frequency"]:
            alerts.append({
                "type": "over_trading",
                "level": "WARNING",
                "message": f"交易频率过高：{metrics['trade_count']} 笔",
            })

        return {
            "task": "alert",
            "alerts": alerts,
            "alert_count": len(alerts),
            "highest_level": self._get_highest_alert_level(alerts),
        }

    async def _get_status(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取监控状态

        Args:
            input_data: 输入数据

        Returns:
            状态信息
        """
        return {
            "task": "status",
            "monitoring_active": True,
            "last_collection": datetime.now().isoformat(),
            "collection_interval": self.monitoring_config["collection_interval"],
            "alert_thresholds": self.monitoring_config["alert_thresholds"],
        }

    def _fetch_trades(self, market: str, start_time: Optional[str], end_time: str) -> List[Dict]:
        """获取交易数据 (简化实现)"""
        # 实际应该从数据库或 API 获取
        return [
            {
                "stock": "HK.09992",
                "direction": "LONG",
                "entry_price": 15.5,
                "current_price": 15.8,
                "pnl": 0.019,
                "entry_time": "2026-03-29T09:35:00",
            }
        ]

    def _get_highest_alert_level(self, alerts: List[Dict]) -> str:
        """获取最高告警级别"""
        if not alerts:
            return "NONE"

        levels = {"NONE": 0, "INFO": 1, "WARNING": 2, "CRITICAL": 3}
        max_level = max(alerts, key=lambda a: levels.get(a.get("level", "NONE"), 0))
        return max_level.get("level", "NONE")

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "data_collection",
            "real_time_monitoring",
            "alert_detection",
            "status_reporting",
        ])
        caps["monitoring_config"] = self.monitoring_config
        return caps
