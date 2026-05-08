"""
每日复盘工作流

实现收盘后每日自动化复盘流程。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..base import Agent, AgentConfig
from ..coordinator import Coordinator, TaskDefinition, WorkflowContext

__all__ = ["DailyReviewWorkflow"]


class DailyReviewWorkflow:
    """
    每日复盘工作流

    流程:
    1. trade-monitor 收集当日交易数据
    2. strategist 分析交易表现
    3. risk-evaluator 评估风险
    4. context-engineer 生成复盘报告
    5. 汇总并推送

    使用示例:
        workflow = DailyReviewWorkflow()
        result = await workflow.execute("2026-03-29")
    """

    def __init__(self, coordinator: Optional[Coordinator] = None):
        """
        初始化工作流

        Args:
            coordinator: 协调器实例
        """
        self.coordinator = coordinator or Coordinator()
        self.logger = logging.getLogger("harness.workflow.daily_review")

    async def execute(self, date: str, market: str = "HK") -> WorkflowContext:
        """
        执行每日复盘工作流

        Args:
            date: 日期 (YYYY-MM-DD)
            market: 市场 (HK/US)

        Returns:
            WorkflowContext 工作流上下文
        """
        self.logger.info(f"Starting daily review workflow for {date} ({market})")

        # 定义任务
        tasks = self._define_tasks(date, market)

        # 执行工作流
        context = await self.coordinator.execute_workflow(
            workflow_id=f"daily_review_{date}_{market}",
            tasks=tasks,
            shared_data={"date": date, "market": market},
        )

        self.logger.info(f"Daily review workflow completed: {context.status}")

        return context

    def _define_tasks(self, date: str, market: str) -> List[TaskDefinition]:
        """定义任务列表"""
        return [
            TaskDefinition(
                name="collect_trades",
                agent_name="trade-monitor",
                input_data={
                    "task": "collect",
                    "market": market,
                    "date": date,
                },
                timeout_seconds=120,
                dependencies=[],
            ),
            TaskDefinition(
                name="analyze_trades",
                agent_name="hk-eod-review",
                input_data={
                    "task": "review",
                    "date": date,
                    "market": market,
                },
                timeout_seconds=600,
                dependencies=["collect_trades"],
            ),
            TaskDefinition(
                name="assess_risk",
                agent_name="risk-evaluator",
                input_data={
                    "task": "assess",
                    "date": date,
                },
                timeout_seconds=120,
                dependencies=["analyze_trades"],
            ),
            TaskDefinition(
                name="generate_report",
                agent_name="context-engineer",
                input_data={
                    "task": "summarize",
                    "date": date,
                    "period_type": "daily",
                },
                timeout_seconds=120,
                dependencies=["assess_risk"],
            ),
        ]

    async def execute_with_agents(
        self,
        date: str,
        agents: Dict[str, Agent],
        market: str = "HK",
    ) -> WorkflowContext:
        """
        使用指定 Agent 执行工作流

        Args:
            date: 日期
            agents: Agent 字典
            market: 市场

        Returns:
            WorkflowContext 工作流上下文
        """
        # 注册 Agent
        for name, agent in agents.items():
            self.coordinator.register_agent(name, agent)

        # 执行工作流
        return await self.execute(date, market)

    def get_workflow_result(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        获取工作流结果

        Args:
            context: 工作流上下文

        Returns:
            结果字典
        """
        task_results = context.task_results

        return {
            "workflow_id": context.workflow_id,
            "status": context.status,
            "date": context.shared_data.get("date"),
            "market": context.shared_data.get("market"),
            "trade_collection": task_results.get("collect_trades"),
            "analysis": task_results.get("analyze_trades"),
            "risk_assessment": task_results.get("assess_risk"),
            "report": task_results.get("generate_report"),
            "total_time": (
                (context.completed_at - context.started_at).total_seconds()
                if context.completed_at and context.started_at
                else 0
            ),
        }

    def generate_summary(self, result: Dict[str, Any]) -> str:
        """
        生成复盘摘要

        Args:
            result: 工作流结果

        Returns:
            摘要文本
        """
        lines = [
            f"📊 每日复盘报告 ({result.get('date', 'N/A')})",
            "",
            f"状态：{'✅ 完成' if result.get('status') == 'completed' else '❌ 失败'}",
            "",
        ]

        # 交易收集
        trade_result = result.get("trade_collection")
        if trade_result and trade_result.success:
            data = trade_result.data or {}
            lines.append(f"📈 交易收集：{data.get('trade_count', 0)} 笔")
        else:
            lines.append("📈 交易收集：失败")

        # 分析结果
        analysis = result.get("analysis")
        if analysis and analysis.success:
            data = analysis.data or {}
            analysis_data = data.get("analysis", {})
            lines.append(f"📉 胜率：{analysis_data.get('win_rate', 0):.1%}")
            lines.append(f"💰 总盈亏：{analysis_data.get('total_pnl', 0):.2f}")
        else:
            lines.append("📉 分析：失败")

        # 风险评估
        risk = result.get("risk_assessment")
        if risk and risk.success:
            data = risk.data or {}
            assessment = data.get("assessment", {})
            lines.append(f"🛡️ 整体风险：{assessment.get('overall_risk', 'N/A')}")
        else:
            lines.append("🛡️ 风险评估：失败")

        # 报告生成
        report = result.get("report")
        if report and report.success:
            lines.append("📝 报告生成：成功")
        else:
            lines.append("📝 报告生成：失败")

        return "\n".join(lines)
