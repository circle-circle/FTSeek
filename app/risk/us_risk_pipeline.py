"""
FTSeek-US 风控 Pipeline

将原有 RiskEngine 的单体 pre_trade_check 拆分为独立 RiskCheck 类的 Pipeline 模式。
任一检查拒绝即终止。

用法:
    from app.risk.us_risk_pipeline import USRiskPipeline
    
    pipeline = USRiskPipeline(risk_engine=risk_engine)
    result = pipeline.run(signal, context={...})
    
    if not result.passed:
        logger.warning("风控拒绝: %s", result.reason)
        return
"""

import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from app.risk.checks.base import RiskCheck, CheckResult
from app.core.config_defaults import CFG

logger = logging.getLogger("ftseek_us.risk.us_risk_pipeline")


@dataclass
class PipelineResult:
    """Pipeline 执行结果"""
    passed: bool
    checks_run: int = 0
    checks_passed: int = 0
    rejected_by: str = ""
    reason: str = ""
    check_details: List[Dict[str, Any]] = field(default_factory=list)
    symbol: str = ""
    direction: str = ""


class USRiskPipeline:
    """
    美股风控 Pipeline
    
    检查顺序 (从保守到宽松):
    1. MarketCircuit    — 大盘熔断 (最保守)
    2. CircuitBreaker   — 系统/策略/品种熔断
    3. Drawdown         — 日内回撤
    4. VIX              — 波动率
    5. Earnings         — 财报感知
    6. BattlePlan       — 盘前计划门禁
    7. Confidence       — 置信度门槛
    8. PositionLimit    — 仓位限制
    9. CoolDown         — 冷却期
    """
    
    def __init__(self, risk_engine=None, config: Dict[str, Any] = None):
        self.risk_engine = risk_engine
        self.config = config or {}
        self._checks: List[RiskCheck] = []
        self._build_default_checks()
    
    def _build_default_checks(self):
        """构建默认检查链"""
        from app.risk.checks.circuit_breaker_check import CircuitBreakerCheck
        from app.risk.checks.confidence_check import ConfidenceCheck
        from app.risk.checks.position_limit_check import PositionLimitCheck
        from app.risk.checks.drawdown_check import DrawdownCheck
        from app.risk.checks.cool_down_check import CoolDownCheck
        
        # 美股特有检查 (可选)
        from app.risk.checks.market_circuit_check import MarketCircuitCheck
        from app.risk.checks.vix_check import VIXCheck
        from app.risk.checks.earnings_check import EarningsCheck
        from app.risk.checks.battle_plan_check import BattlePlanCheck
        
        self._checks = [
            MarketCircuitCheck(),                           # 大盘熔断
            CircuitBreakerCheck(),                           # 系统/策略/品种熔断
            DrawdownCheck(),                                 # 日内回撤
            VIXCheck(),                                      # VIX 波动率
            EarningsCheck(),                                 # 财报感知
            BattlePlanCheck(),                               # 盘前计划门禁
            ConfidenceCheck(                                 # 置信度
                min_confidence=self.config.get("min_confidence", CFG.MIN_CONFIDENCE),
                min_score=self.config.get("min_score", 50),
            ),
            PositionLimitCheck(                              # 仓位限制
                max_per_symbol=self.config.get("max_per_symbol", 0.25),
                max_total=self.config.get("max_total", 0.80),
                max_per_direction=self.config.get("max_per_direction", 0.50),
            ),
            CoolDownCheck(                                   # 冷却期
                cooldown_seconds=self.config.get("cooldown_seconds", 120),
            ),
        ]
        
        logger.info("[USRiskPipeline] 构建 %d 个检查", len(self._checks))
    
    def add_check(self, check: RiskCheck, index: int = None):
        """添加自定义检查"""
        if index is not None:
            self._checks.insert(index, check)
        else:
            self._checks.append(check)
    
    def remove_check(self, check_name: str) -> bool:
        """移除检查"""
        for i, c in enumerate(self._checks):
            if c.name == check_name:
                self._checks.pop(i)
                return True
        return False
    
    def run(self, signal, context: Dict[str, Any] = None) -> PipelineResult:
        """
        执行风控 Pipeline
        
        Args:
            signal: 信号对象
            context: 上下文 {risk_engine, position_store, vix, spy_change_pct, ...}
        
        Returns:
            PipelineResult
        """
        if context is None:
            context = {}
        
        # 注入 risk_engine
        if self.risk_engine and "risk_engine" not in context:
            context["risk_engine"] = self.risk_engine
        
        result = PipelineResult(
            passed=True,
            symbol=getattr(signal, 'symbol', ''),
            direction=getattr(signal, 'direction', ''),
        )
        
        for check in self._checks:
            try:
                check_result = check.check(signal, context)
                result.checks_run += 1
                result.check_details.append({
                    "check": check_result.check_name,
                    "passed": check_result.passed,
                    "reason": check_result.reason,
                })
                
                if not check_result.passed:
                    result.passed = False
                    result.rejected_by = check_result.check_name
                    result.reason = check_result.reason
                    logger.warning(
                        "[USRiskPipeline] ❌ %s 拒绝 | %s %s | %s",
                        check_result.check_name, result.symbol, result.direction, check_result.reason
                    )
                    return result
                
                result.checks_passed += 1
                
            except Exception as e:
                logger.error("[USRiskPipeline] 检查异常 [%s]: %s", check.name, e, exc_info=True)
                # P0-1 修复 (2026-05-03): 风控检查崩溃 → 拒绝交易 (fail-closed)
                # 安全原则: 无法确认安全 = 不安全 → 必须拦截
                result.rejected_by = f"{check.name}:EXCEPTION"
                result.reason = f"风控检查崩溃: {check.name} ({e})"
                logger.critical(
                    "[USRiskPipeline] 🚨 %s 因检查崩溃被拒绝 (fail-closed) | %s %s",
                    check.name, result.symbol, result.direction
                )
                return result
        
        if result.passed:
            logger.debug(
                "[USRiskPipeline] ✅ %s %s 全部通过 (%d/%d)",
                result.symbol, result.direction, result.checks_passed, result.checks_run
            )
        
        return result
    
    @property
    def checks(self) -> List[str]:
        return [c.name for c in self._checks]
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_checks": len(self._checks),
            "checks": self.checks,
        }
