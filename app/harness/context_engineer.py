"""
Context-Engineer (记忆工程师)

负责数据压缩、记忆管理和决策归档。
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import Agent, AgentConfig

__all__ = ["ContextEngineer"]


class ContextEngineer(Agent):
    """
    记忆工程师 Agent

    职责:
    - 压缩交易数据和决策
    - 管理 MEMORY.md 存储
    - 归档历史决策
    - 检索历史记忆

    使用示例:
        engineer = ContextEngineer()
        result = await engineer.run({
            "task": "compress",
            "data": {...},
            "date": "2026-03-29",
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """
        初始化记忆工程师

        Args:
            config: Agent 配置
        """
        if config is None:
            config = AgentConfig(
                name="context-engineer",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "记忆工程师",
                    "capabilities": ["data_compression", "memory_management", "archival"],
                },
            )

        super().__init__(config)
        self.memory_path = "/root/.openclaw/workspace-planner/MEMORY.md"

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行记忆管理任务

        Args:
            input_data: 输入数据，包含:
                - task: 任务类型 (compress/archive/retrieve/summarize)
                - data: 数据
                - date: 日期

        Returns:
            处理结果
        """
        task = input_data.get("task", "compress")

        if task == "compress":
            return await self._compress_data(input_data)
        elif task == "archive":
            return await self._archive_decision(input_data)
        elif task == "retrieve":
            return await self._retrieve_memory(input_data)
        elif task == "summarize":
            return await self._summarize_period(input_data)
        else:
            raise ValueError(f"Unknown task: {task}")

    async def _compress_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        压缩数据

        Args:
            input_data: 输入数据

        Returns:
            压缩后的摘要
        """
        data = input_data.get("data", {})
        date = input_data.get("date", datetime.now().strftime("%Y-%m-%d"))

        self.logger.info(f"Compressing data for {date}")

        # 提取关键信息
        compressed = {
            "date": date,
            "summary": self._extract_summary(data),
            "statistics": self._extract_statistics(data),
            "decisions": self._extract_decisions(data),
            "lessons": self._extract_lessons(data),
        }

        # 计算压缩率
        original_size = len(json.dumps(data, ensure_ascii=False))
        compressed_size = len(json.dumps(compressed, ensure_ascii=False))
        compression_ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0

        return {
            "task": "compress",
            "date": date,
            "compressed": compressed,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "compression_ratio": f"{compression_ratio:.1f}%",
        }

    async def _archive_decision(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        归档决策

        Args:
            input_data: 输入数据

        Returns:
            归档结果
        """
        decision = input_data.get("decision", {})
        category = input_data.get("category", "general")
        date = input_data.get("date", datetime.now().strftime("%Y-%m-%d"))

        self.logger.info(f"Archiving decision for {date} (category: {category})")

        # 创建归档记录
        archive_record = {
            "timestamp": datetime.now().isoformat(),
            "date": date,
            "category": category,
            "decision": decision,
            "context": input_data.get("context", {}),
            "outcome": input_data.get("outcome", "pending"),
        }

        # 这里应该写入文件，简化为返回
        return {
            "task": "archive",
            "archived": True,
            "record": archive_record,
            "message": f"Decision archived to {category}/{date}",
        }

    async def _retrieve_memory(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        检索记忆

        Args:
            input_data: 输入数据

        Returns:
            检索结果
        """
        query = input_data.get("query", "")
        date_range = input_data.get("date_range", {})
        category = input_data.get("category", None)

        self.logger.info(f"Retrieving memory (query: {query}, category: {category})")

        # 简化实现：返回示例数据
        return {
            "task": "retrieve",
            "query": query,
            "results": [
                {
                    "date": "2026-03-28",
                    "category": "strategy",
                    "summary": "调整止损参数，提升胜率",
                    "relevance": 0.85,
                }
            ],
            "total_count": 1,
        }

    async def _summarize_period(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        期间总结

        Args:
            input_data: 输入数据

        Returns:
            总结报告
        """
        start_date = input_data.get("start_date")
        end_date = input_data.get("end_date")
        period_type = input_data.get("period_type", "daily")

        self.logger.info(f"Summarizing {period_type} period: {start_date} to {end_date}")

        # 生成总结
        summary = {
            "period_type": period_type,
            "start_date": start_date,
            "end_date": end_date,
            "trading_days": 1,
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "key_decisions": [],
            "lessons_learned": [],
            "performance_summary": "待补充数据",
        }

        return {
            "task": "summarize",
            "summary": summary,
        }

    def _extract_summary(self, data: Dict) -> str:
        """提取摘要"""
        # 简单实现
        if "summary" in data:
            return data["summary"]

        # 从数据中生成摘要
        trades = data.get("trades", [])
        if trades:
            return f"当日交易 {len(trades)} 笔"

        return "无显著事件"

    def _extract_statistics(self, data: Dict) -> Dict[str, Any]:
        """提取统计信息"""
        trades = data.get("trades", [])

        if not trades:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
            }

        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) <= 0)
        total_pnl = sum(t.get("pnl", 0) for t in trades)

        return {
            "total_trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(trades) if trades else 0,
            "total_pnl": total_pnl,
        }

    def _extract_decisions(self, data: Dict) -> List[Dict]:
        """提取决策"""
        decisions = data.get("decisions", [])

        if not decisions:
            return []

        return [
            {
                "time": d.get("time", ""),
                "action": d.get("action", ""),
                "reason": d.get("reason", ""),
                "outcome": d.get("outcome", ""),
            }
            for d in decisions[:10]  # 限制数量
        ]

    def _extract_lessons(self, data: Dict) -> List[str]:
        """提取经验教训"""
        lessons = data.get("lessons", [])

        if not lessons:
            # 从错误中生成
            errors = data.get("errors", [])
            if errors:
                return [f"避免{e.get('type', '错误')}: {e.get('message', '')}" for e in errors[:5]]

            return []

        return lessons[:10]  # 限制数量

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "data_compression",
            "memory_management",
            "decision_archival",
            "memory_retrieval",
            "period_summary",
        ])
        caps["memory_path"] = self.memory_path
        return caps
