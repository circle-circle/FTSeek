#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风控 Pipeline (Risk Pipeline) - 2026-04-09

将 _on_consensus_inner 中分散的 20+ 项风控检查，拆分为独立的 RiskCheck，
通过 Pipeline 模式串行执行，任一检查拒绝即终止。

架构位置:
  共识引擎 → 决策缓冲层 → [风控 Pipeline] → 执行层

风控层级优先级 (从高到低):
  1. SmartStopGuard  (账户级) — 日内回撤/连败熔断，全局暂停交易
  2. CircuitBreaker  (系统级) — 智能熔断，可返回 position_multiplier 缩减仓位
  3. RiskPipeline    (信号级) — 17 项信号风控检查 (本模块)
  4. BattlePlan      (计划级) — 盘前/午间作战计划白名单/方向/推荐
  5. DEM            (退出级) — DynamicExitManager 10 级优先级退出决策

优势:
1. 每项检查独立可测试
2. 检查顺序可通过配置调整
3. 新增检查只需添加 RiskCheck 实现类
4. 审计日志完整记录每项检查结果

重构历史:
  2026-05-19: Phase 8A - 17 个检查类移至 app/risk/checks/ 独立文件
  2026-05-19: Phase 8B - 移除 monolithic check 实现，仅保留基类和 Pipeline
"""

import logging
import time
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.strategy.base import SignalAction
from app.strategy.consensus_engine import ConsensusResult

logger = logging.getLogger("ftseek.risk_pipeline")


# ============================================================
#  数据结构
# ============================================================

@dataclass
class SignalContext:
    """
    信号上下文 - 在 Pipeline 各检查之间传递的共享数据

    包含原始共识结果以及各检查可能注入的附加信息
    """
    # 原始输入
    consensus_result: ConsensusResult
    trace_id: str = "N/A"

    # 检查过程中注入的信息
    quality_boost: float = 1.0          # 质量仓位系数 (A:1.3 / B:1.0 / C:0.7)
    concentration_discount: float = 1.0  # 相关性折扣
    news_boost: float = 1.0             # 新闻情绪倍率
    mtf_confirmed: bool = True          # 多周期确认
    mtf_boost: float = 0.0             # 多周期置信度调整
    mtf_reason: str = ""               # 多周期过滤原因
    trade_type: str = "OPEN"           # OPEN/CLOSE/HOLD

    # 2026-05-11: 智能熔断仓位倍率 (CircuitBreakerCheck 注入 -> executor 读取)
    position_multiplier: float = 1.0

    # 信号跟踪
    signal_id: str = ""                 # 由 record_signal 后填入

    @property
    def code(self) -> str:
        return self.consensus_result.code

    @property
    def action(self) -> SignalAction:
        return self.consensus_result.action

    @property
    def score(self) -> float:
        return self.consensus_result.score

    @property
    def confidence(self) -> float:
        return self.consensus_result.confidence


@dataclass
class CheckResult:
    """单项检查结果"""
    passed: bool
    check_name: str
    reason: str = ""                    # 拒绝原因（passed=False 时必填）
    reject_code: str = ""               # 拒绝代码（用于 signal_tracker）
    context_updates: Dict[str, Any] = field(default_factory=dict)  # 需要更新到 SignalContext 的字段


@dataclass
class PipelineResult:
    """Pipeline 执行结果"""
    passed: bool
    results: List[CheckResult] = field(default_factory=list)
    reject_reason: str = ""
    reject_code: str = ""
    reject_check: str = ""              # 拒绝的检查名称

    @property
    def audit_trail(self) -> List[Dict]:
        """获取审计日志"""
        return [
            {
                "check": r.check_name,
                "passed": r.passed,
                "reason": r.reason,
            }
            for r in self.results
        ]


# ============================================================
#  风控检查基类
# ============================================================

class RiskCheck(ABC):
    """风控检查基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """检查名称"""
        pass

    @abstractmethod
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        """
        执行检查

        Args:
            ctx: 信号上下文
            deps: 依赖注入字典（包含 Provider 接口引用）

        Returns:
            CheckResult
        """
        pass


# ============================================================
#  风控 Pipeline
# ============================================================

class RiskPipeline:
    """
    风控 Pipeline

    串行执行所有注册的 RiskCheck，任一检查拒绝即终止。
    支持动态增删检查、调整顺序。
    """

    def __init__(self, checks: Optional[List[RiskCheck]] = None):
        self._checks: List[RiskCheck] = checks or []

    def add_check(self, check: RiskCheck) -> None:
        """添加检查"""
        self._checks.append(check)

    def remove_check(self, name: str) -> None:
        """移除检查"""
        self._checks = [c for c in self._checks if c.name != name]

    def evaluate(self, ctx: SignalContext, deps: Dict) -> PipelineResult:
        """
        执行所有检查

        Args:
            ctx: 信号上下文
            deps: 依赖注入字典

        Returns:
            PipelineResult: 所有检查结果
        """
        results: List[CheckResult] = []
        start_time = time.time()

        for check in self._checks:
            try:
                result = check.check(ctx, deps)
                results.append(result)

                # 注入 context_updates 到 SignalContext
                if result.context_updates:
                    for key, value in result.context_updates.items():
                        if hasattr(ctx, key):
                            setattr(ctx, key, value)

                # 任一检查拒绝即终止
                if not result.passed:
                    logger.info(
                        f"[RiskPipeline] {ctx.code} 被 {check.name} 拒绝: {result.reason}"
                    )
                    return PipelineResult(
                        passed=False,
                        results=results,
                        reject_reason=result.reason,
                        reject_code=result.reject_code,
                        reject_check=check.name,
                    )

            except Exception as e:
                logger.error(
                    f"[RiskPipeline] {ctx.code} 执行 {check.name} 时异常: {e}",
                    exc_info=True
                )
                # 检查执行异常 → 拒绝（fail-close）
                return PipelineResult(
                    passed=False,
                    results=results,
                    reject_reason=f"检查 {check.name} 执行异常: {e}",
                    reject_code="risk_check_error",
                    reject_check=check.name,
                )

        elapsed_ms = (time.time() - start_time) * 1000
        logger.debug(
            f"[RiskPipeline] {ctx.code} 通过 {len(results)} 项检查 ({elapsed_ms:.1f}ms)"
        )

        return PipelineResult(passed=True, results=results)


# ============================================================
#  DEFAULT_CHECKS 从 checks 包导入
# ============================================================

from app.risk.checks import DEFAULT_CHECKS, discover_risk_checks

__all__ = [
    "SignalContext",
    "CheckResult",
    "PipelineResult",
    "RiskCheck",
    "RiskPipeline",
    "DEFAULT_CHECKS",
    "discover_risk_checks",
]
