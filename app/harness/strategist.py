"""
Strategy-Gen (策略生成器)

负责策略分析、信号生成和参数优化。
"""

import logging
from typing import Any, Dict, List, Optional

from .base import Agent, AgentConfig, AgentResult

__all__ = ["Strategist"]


class Strategist(Agent):
    """
    策略生成器 Agent

    职责:
    - 分析市场数据和交易信号
    - 生成交易策略建议
    - 优化策略参数
    - 评估策略表现

    使用示例:
        strategist = Strategist()
        result = await strategist.run({
            "stock": "HK.09992",
            "market_data": {...},
            "signals": [...],
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """
        初始化策略生成器

        Args:
            config: Agent 配置
        """
        if config is None:
            config = AgentConfig(
                name="strategy-gen",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "策略生成器",
                    "capabilities": ["strategy_analysis", "signal_generation", "parameter_optimization"],
                },
            )

        super().__init__(config)
        self._batch_processor = None

    async def _load_batch_processor(self) -> None:
        """加载批量处理器模块"""
        try:
            from app.review.batch_processor_hk import BatchReviewProcessor
            self._batch_processor = BatchReviewProcessor()
            self.logger.info("BatchReviewProcessor loaded")
        except ImportError as e:
            self.logger.warning(f"Failed to load BatchReviewProcessor: {e}")
            self._batch_processor = None

    async def initialize(self) -> None:
        """初始化"""
        await super().initialize()
        await self._load_batch_processor()

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行策略分析

        Args:
            input_data: 输入数据，包含:
                - stock: 股票代码
                - market_data: 市场数据
                - signals: 交易信号
                - task: 任务类型 (analyze/generate/optimize)

        Returns:
            策略分析结果
        """
        task = input_data.get("task", "analyze")

        if task == "analyze":
            return await self._analyze_strategy(input_data)
        elif task == "generate":
            return await self._generate_signal(input_data)
        elif task == "optimize":
            return await self._optimize_parameters(input_data)
        else:
            raise ValueError(f"Unknown task: {task}")

    async def _analyze_strategy(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析策略表现

        Args:
            input_data: 输入数据

        Returns:
            策略分析报告
        """
        stock = input_data.get("stock", "UNKNOWN")
        date = input_data.get("date", None)

        self.logger.info(f"Analyzing strategy for {stock} (date: {date})")

        # 如果有批量处理器，使用现有分析逻辑
        if self._batch_processor and date:
            try:
                analysis = await self._batch_processor.analyze_trades(date, stock)
                return {
                    "task": "analyze",
                    "stock": stock,
                    "date": date,
                    "analysis": analysis,
                    "recommendation": self._generate_recommendation(analysis),
                }
            except Exception as e:
                self.logger.error(f"Analysis failed: {e}")
                # 降级处理
                return self._simple_analysis(stock, date)
        else:
            return self._simple_analysis(stock, date)

    async def _generate_signal(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成交易信号

        Args:
            input_data: 输入数据

        Returns:
            交易信号
        """
        stock = input_data.get("stock", "UNKNOWN")
        market_data = input_data.get("market_data", {})
        signals = input_data.get("signals", [])

        self.logger.info(f"Generating signal for {stock}")

        # 简单信号生成逻辑 (示例)
        signal_strength = self._calculate_signal_strength(market_data, signals)

        return {
            "task": "generate",
            "stock": stock,
            "signal": {
                "direction": "LONG" if signal_strength > 0.5 else "SHORT" if signal_strength < -0.5 else "HOLD",
                "strength": signal_strength,
                "confidence": min(abs(signal_strength) * 100, 100),
                "suggested_position": self._calculate_position(signal_strength),
            },
            "factors": self._identify_factors(market_data, signals),
        }

    async def _optimize_parameters(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        优化策略参数

        Args:
            input_data: 输入数据

        Returns:
            优化后的参数
        """
        current_params = input_data.get("current_params", {})
        performance_data = input_data.get("performance", {})

        self.logger.info("Optimizing strategy parameters")

        # 简单参数优化逻辑 (示例)
        optimized = self._optimize_params(current_params, performance_data)

        return {
            "task": "optimize",
            "original_params": current_params,
            "optimized_params": optimized,
            "improvement": self._estimate_improvement(current_params, optimized, performance_data),
        }

    def _simple_analysis(self, stock: str, date: Optional[str]) -> Dict[str, Any]:
        """简单分析 (降级处理)"""
        return {
            "task": "analyze",
            "stock": stock,
            "date": date,
            "analysis": {
                "win_rate": 0.0,
                "total_trades": 0,
                "total_pnl": 0.0,
            },
            "recommendation": "No data available for analysis",
        }

    def _generate_recommendation(self, analysis: Dict[str, Any]) -> str:
        """生成建议"""
        if not analysis:
            return "No data available"

        win_rate = analysis.get("win_rate", 0)
        if win_rate >= 0.6:
            return "策略表现优秀，继续保持"
        elif win_rate >= 0.5:
            return "策略表现良好，可微调参数"
        elif win_rate >= 0.4:
            return "策略表现一般，建议优化"
        else:
            return "策略表现较差，需要重新评估"

    def _calculate_signal_strength(self, market_data: Dict, signals: List) -> float:
        """计算信号强度"""
        # 简单实现：根据信号数量计算
        if not signals:
            return 0.0

        bullish = sum(1 for s in signals if s.get("direction") == "LONG")
        bearish = sum(1 for s in signals if s.get("direction") == "SHORT")
        total = len(signals)

        return (bullish - bearish) / total if total > 0 else 0.0

    def _calculate_position(self, signal_strength: float) -> float:
        """计算建议仓位"""
        # 信号越强，仓位越高
        return min(abs(signal_strength) * 0.2, 0.2)  # 最大 20%

    def _identify_factors(self, market_data: Dict, signals: List) -> List[str]:
        """识别影响因素"""
        factors = []

        if market_data.get("trend") == "up":
            factors.append("上涨趋势")
        elif market_data.get("trend") == "down":
            factors.append("下跌趋势")

        if market_data.get("volume", 0) > market_data.get("avg_volume", 0) * 1.5:
            factors.append("放量")

        if len(signals) > 5:
            factors.append("多信号共振")

        return factors if factors else ["无明显因素"]

    def _optimize_params(
        self, current: Dict[str, Any], performance: Dict[str, Any]
    ) -> Dict[str, Any]:
        """优化参数"""
        # 简单实现：根据表现调整
        optimized = current.copy()

        win_rate = performance.get("win_rate", 0.5)
        if win_rate < 0.5:
            # 胜率低，调整止损止盈
            optimized["stop_loss"] = current.get("stop_loss", 0.05) * 0.9
            optimized["take_profit"] = current.get("take_profit", 0.1) * 1.1

        return optimized

    def _estimate_improvement(
        self, original: Dict, optimized: Dict, performance: Dict
    ) -> Dict[str, Any]:
        """估计改进效果"""
        # 简单估计
        return {
            "expected_win_rate_improvement": "+2-5%",
            "expected_risk_reduction": "-5-10%",
            "confidence": "medium",
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "strategy_analysis",
            "signal_generation",
            "parameter_optimization",
            "recommendation_generation",
        ])
        return caps
