"""
周度复盘工作流

实现周末自动化复盘流程。
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..coordinator import Coordinator, TaskDefinition, WorkflowContext

__all__ = ["WeeklyReviewWorkflow"]


class WeeklyReviewWorkflow:
    """
    周度复盘工作流

    流程:
    1. 收集本周交易数据
    2. 分析周度表现
    3. 评估周度风险
    4. 生成周度报告
    5. 趋势分析
    6. 汇总并推送

    使用示例:
        workflow = WeeklyReviewWorkflow()
        result = await workflow.execute("2026-W13")
    """

    def __init__(self, coordinator: Optional[Coordinator] = None):
        """
        初始化周度复盘工作流

        Args:
            coordinator: 协调器实例
        """
        self.coordinator = coordinator or Coordinator()
        self.logger = logging.getLogger("harness.workflow.weekly_review")

    async def execute(
        self,
        week: str,
        market: str = "HK",
        year: Optional[int] = None,
    ) -> WorkflowContext:
        """
        执行周度复盘工作流

        Args:
            week: 周数 (YYYY-Www 格式，如 "2026-W13")
            market: 市场 (HK/US)
            year: 年份 (可选，从 week 解析)

        Returns:
            WorkflowContext 工作流上下文
        """
        self.logger.info(f"Starting weekly review workflow for {week} ({market})")

        # 解析周数
        if not year:
            year = int(week.split("-W")[0])

        # 定义任务
        tasks = self._define_tasks(week, market)

        # 执行工作流
        context = await self.coordinator.execute_workflow(
            workflow_id=f"weekly_review_{week}_{market}",
            tasks=tasks,
            shared_data={"week": week, "market": market, "year": year},
        )

        self.logger.info(f"Weekly review workflow completed: {context.status}")

        return context

    def _define_tasks(self, week: str, market: str) -> List[TaskDefinition]:
        """定义任务列表"""
        return [
            TaskDefinition(
                name="collect_weekly_trades",
                agent_name="trade-monitor",
                input_data={
                    "task": "collect",
                    "market": market,
                    "week": week,
                },
                timeout_seconds=180,
                dependencies=[],
            ),
            TaskDefinition(
                name="analyze_weekly_performance",
                agent_name="strategy-gen",
                input_data={
                    "task": "analyze",
                    "week": week,
                    "stock": "ALL",
                },
                timeout_seconds=240,
                dependencies=["collect_weekly_trades"],
            ),
            TaskDefinition(
                name="assess_weekly_risk",
                agent_name="risk-evaluator",
                input_data={
                    "task": "assess",
                    "week": week,
                },
                timeout_seconds=180,
                dependencies=["analyze_weekly_performance"],
            ),
            TaskDefinition(
                name="analyze_trend",
                agent_name="strategy-gen",
                input_data={
                    "task": "analyze",
                    "week": week,
                    "analysis_type": "trend",
                },
                timeout_seconds=180,
                dependencies=["assess_weekly_risk"],
            ),
            TaskDefinition(
                name="generate_weekly_report",
                agent_name="context-engineer",
                input_data={
                    "task": "summarize",
                    "week": week,
                    "period_type": "weekly",
                },
                timeout_seconds=180,
                dependencies=["analyze_trend"],
            ),
        ]

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
            "week": context.shared_data.get("week"),
            "market": context.shared_data.get("market"),
            "trade_collection": task_results.get("collect_weekly_trades"),
            "performance_analysis": task_results.get("analyze_weekly_performance"),
            "risk_assessment": task_results.get("assess_weekly_risk"),
            "trend_analysis": task_results.get("analyze_trend"),
            "weekly_report": task_results.get("generate_weekly_report"),
            "total_time": (
                (context.completed_at - context.started_at).total_seconds()
                if context.completed_at and context.started_at
                else 0
            ),
        }

    def generate_summary(self, result: Dict[str, Any]) -> str:
        """
        生成周度复盘摘要

        Args:
            result: 工作流结果

        Returns:
            摘要文本
        """
        lines = [
            f"📊 周度复盘报告 ({result.get('week', 'N/A')})",
            "",
            f"状态：{'✅ 完成' if result.get('status') == 'completed' else '❌ 失败'}",
            "",
        ]

        # 交易收集
        trade_result = result.get("trade_collection")
        if trade_result and trade_result.success:
            data = trade_result.data or {}
            lines.append(f"📈 本周交易：{data.get('trade_count', 0)} 笔")
        else:
            lines.append("📈 交易收集：失败")

        # 表现分析
        performance = result.get("performance_analysis")
        if performance and performance.success:
            data = performance.data or {}
            analysis = data.get("analysis", {})
            lines.append(f"📉 周胜率：{analysis.get('win_rate', 0):.1%}")
            lines.append(f"💰 周盈亏：{analysis.get('total_pnl', 0):.2f}")
        else:
            lines.append("📉 表现分析：失败")

        # 风险评估
        risk = result.get("risk_assessment")
        if risk and risk.success:
            data = risk.data or {}
            assessment = data.get("assessment", {})
            lines.append(f"🛡️ 周度风险：{assessment.get('overall_risk', 'N/A')}")
        else:
            lines.append("🛡️ 风险评估：失败")

        # 趋势分析
        trend = result.get("trend_analysis")
        if trend and trend.success:
            data = trend.data or {}
            lines.append(f"📈 趋势：{data.get('trend', 'N/A')}")
        else:
            lines.append("📈 趋势分析：失败")

        # 报告生成
        report = result.get("weekly_report")
        if report and report.success:
            lines.append("📝 周度报告：成功")
        else:
            lines.append("📝 周度报告：失败")

        return "\n".join(lines)

    @staticmethod
    def get_current_week() -> str:
        """获取当前周数 (YYYY-Www 格式)"""
        today = datetime.now()
        iso_calendar = today.isocalendar()
        return f"{iso_calendar.year}-W{iso_calendar.week:02d}"

    @staticmethod
    def get_week_date_range(week: str) -> Dict[str, str]:
        """
        获取周日期范围

        Args:
            week: 周数 (YYYY-Www)

        Returns:
            日期范围字典
        """
        # 解析周数
        year = int(week.split("-W")[0])
        week_num = int(week.split("-W")[1])

        # 计算周一和周日
        jan_4 = datetime(year, 1, 4)
        start_of_week1 = jan_4 - timedelta(days=jan_4.weekday())
        start_of_target_week = start_of_week1 + timedelta(weeks=week_num - 1)
        end_of_target_week = start_of_target_week + timedelta(days=6)

        return {
            "start_date": start_of_target_week.strftime("%Y-%m-%d"),
            "end_date": end_of_target_week.strftime("%Y-%m-%d"),
        }
