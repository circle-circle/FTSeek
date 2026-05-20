"""
交易质量门控系统 (TradeQualityGate)

核心理念:
将交易决策从“信号驱动”升级为“漏斗筛选”，确保每笔交易都经过环境、形态、资金、龙头和执行质量的综合评估。
符合 Livermore "等待关键点 -> 确认资金 -> 只做领头羊 -> 控制滑点" 的交易哲学。

配置来源: unified_config.yaml -> strategy_enhancements.quality_gate
"""

import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """门控检测结果"""
    name: str                   # 门控名称
    passed: bool                # 是否通过硬性拦截
    score: float                # 0.0 - 1.0 质量分
    weight: float               # 该门控权重
    reason: str                 # 结果描述
    metadata: Dict = field(default_factory=dict)  # 详细归因数据


class TradeQualityGate:
    """交易质量门控管理器"""

    def __init__(self, config_path: str = None):
        self.config = get_config(config_path).get('strategy_enhancements.quality_gate', {})
        self.enabled = self.config.get('enabled', True)
        self.min_score = self.config.get('min_score_to_trade', 0.60)
        self.weights = self.config.get('weights', {
            'regime': 0.15,
            'pivotal': 0.30,
            'capital_flow': 0.20,
            'leader': 0.15,
            'execution': 0.20
        })

        # 延迟加载模块
        self._modules = {}
        self._initialized = False

    def _init_modules(self):
        if self._initialized:
            return
        try:
            from app.factors.regime_filter import RegimeFilter
            from app.factors.pivotal_detector import PivotalDetector
            from app.factors.capital_flow import CapitalFlowMonitor
            from app.factors.leader_pool import LeaderPoolEvaluator
            from app.factors.execution_assessor import ExecutionAssessor

            self._modules = {
                'regime': RegimeFilter(),
                'pivotal': PivotalDetector(),
                'capital_flow': CapitalFlowMonitor(),
                'leader': LeaderPoolEvaluator(),
                'execution': ExecutionAssessor()
            }
            self._initialized = True
            logger.info("[交易质量门] 5大模块初始化完成")
        except Exception as e:
            logger.error(f"[交易质量门] 模块加载失败: {e}")
            self.enabled = False

    def evaluate(self, symbol: str, signal: Any, history: Dict, market_ctx: Dict) -> Dict:
        """
        执行全流程门控评估

        Args:
            symbol: 标的代码
            signal: 交易信号对象
            history: 历史数据 (OHLCV)
            market_ctx: 市场上下文 (VIX, SPY, Sector info)

        Returns:
            评估结果字典
        """
        if not self.enabled:
            return {"opportunity_score": 0.5, "passed_gate": True, "gate_details": {}}

        self._init_modules()

        results = {}
        total_score = 0.0
        passed_gate = True

        # 1. 市场环境门控 (Regime Filter)
        res = self._modules['regime'].check(symbol, history, market_ctx)
        results['regime'] = res.__dict__
        total_score += res.score * self.weights['regime']
        if not res.passed:
            passed_gate = False
            return self._finalize(results, total_score, passed_gate, "Regime Rejection")

        # 2. 关键点门控 (Pivotal Points) - 权重最高
        res = self._modules['pivotal'].check(symbol, history)
        results['pivotal'] = res.__dict__
        total_score += res.score * self.weights['pivotal']
        if not res.passed:
            passed_gate = False
            return self._finalize(results, total_score, passed_gate, "Pivot Rejection")

        # 3. 资金流门控 (Capital Flow)
        res = self._modules['capital_flow'].check(symbol, history)
        results['capital_flow'] = res.__dict__
        total_score += res.score * self.weights['capital_flow']
        if not res.passed:
            passed_gate = False
            return self._finalize(results, total_score, passed_gate, "Flow Rejection")

        # 4. 龙头地位门控 (Leader Pool)
        res = self._modules['leader'].check(symbol, history, market_ctx)
        results['leader'] = res.__dict__
        total_score += res.score * self.weights['leader']
        if not res.passed:
            passed_gate = False
            return self._finalize(results, total_score, passed_gate, "Leader Rejection")

        # 5. 执行质量门控 (Execution Assessor)
        res = self._modules['execution'].check(symbol, history, market_ctx)
        results['execution'] = res.__dict__
        total_score += res.score * self.weights['execution']
        if not res.passed:
            passed_gate = False
            return self._finalize(results, total_score, passed_gate, "Execution Rejection")

        return self._finalize(results, total_score, passed_gate)

    def _finalize(self, results, total_score, passed_gate, reject_reason=""):
        """归一化输出结果

        P1 修复 (2026-04-17): 只记录已存在的模块结果，避免 KeyError
        """
        # 归一化分数 (0~1)
        final_score = min(1.0, max(0.0, total_score))

        # 只记录已存在的模块结果
        detail_parts = []
        for key in ['regime', 'pivotal', 'capital_flow', 'leader', 'execution']:
            if key in results:
                score_val = results[key].get('score', 0)
                detail_parts.append(f"{key}={score_val:.2f}")

        log_msg = (
            f"[交易质量门] 评估完成 | 总分={final_score:.2f} | "
            f"通过={passed_gate} | 详情: {', '.join(detail_parts)}"
        )
        if reject_reason:
            log_msg += f" | 拒绝原因: {reject_reason}"

        logger.info(log_msg)

        return {
            "opportunity_score": final_score,
            "passed_gate": passed_gate,
            "reject_reason": reject_reason,
            "gate_details": results
        }
