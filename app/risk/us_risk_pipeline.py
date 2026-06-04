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

超时控制 (Task #9, 2026-06-02):
    每个 RiskCheck 通过 ThreadPoolExecutor 包装并附加超时:
    - per_check_timeout: 默认 5 秒
    - total_timeout:     默认 30 秒
    - 关键检查(critical_checks_fail_close)超时 → fail-close (拒绝)
    - 非关键检查超时 → fail-open (默认通过) + WARNING 告警
"""

import logging
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from app.risk.checks.base import RiskCheck, CheckResult
from app.core.config_defaults import CFG

logger = logging.getLogger("ftseek_us.risk.us_risk_pipeline")


# 关键风控检查名单 — 这些检查超时时必须 fail-close (拒绝交易)
# 因为它们防护资金/仓位安全, 无法确认安全 = 不安全
_DEFAULT_CRITICAL_CHECKS = (
    "DrawdownCheck",
    "PositionLimitCheck",
    "BuyingPowerCheck",
    "MarketCircuitCheck",
    "CircuitBreakerCheck",
)


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
    total_elapsed_ms: float = 0.0
    timed_out: bool = False  # 是否触发了 pipeline 总超时


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

        # ── 超时控制配置 (Task #9, 2026-06-02) ──
        self.per_check_timeout: float = float(
            self.config.get("per_check_timeout", 5.0)
        )
        self.total_timeout: float = float(
            self.config.get("total_timeout", 30.0)
        )
        # 关键检查超时 fail-close, 其余 fail-open
        critical_cfg = self.config.get(
            "critical_checks_fail_close", list(_DEFAULT_CRITICAL_CHECKS)
        )
        # 标准化 (容忍大小写、下划线/驼峰), 同时保留原值
        self._critical_checks = {
            self._norm_check_name(c) for c in critical_cfg
        }

        # 慢检查统计 (>= per_check_timeout * 0.6 视为慢)
        self._slow_threshold = self.per_check_timeout * 0.6
        self._slow_check_counts: Dict[str, int] = {}
        self._timeout_counts: Dict[str, int] = {}

        self._checks: List[RiskCheck] = []
        self._build_default_checks()

    @staticmethod
    def _norm_check_name(name: str) -> str:
        """标准化检查名 (去下划线/小写) 用于 critical 匹配"""
        return (name or "").replace("_", "").replace("-", "").lower()

    def _is_critical(self, check_name: str) -> bool:
        """判断给定检查是否为关键检查 (超时需 fail-close)"""
        n = self._norm_check_name(check_name)
        # 同时支持精确匹配和子串匹配 (max_drawdown 匹配 DrawdownCheck)
        if n in self._critical_checks:
            return True
        for crit in self._critical_checks:
            if crit and (crit in n or n in crit):
                return True
        return False
    
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
        
        logger.info(
            "[USRiskPipeline] 构建 %d 个检查 | per_check_timeout=%.1fs total=%.1fs critical=%s",
            len(self._checks), self.per_check_timeout, self.total_timeout,
            sorted(self._critical_checks),
        )
    
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

    def _run_check_with_timeout(
        self,
        check: RiskCheck,
        signal,
        context: Dict[str, Any],
        timeout: float,
    ) -> (CheckResult, float, bool):
        """
        带超时的单个风控检查执行

        Returns:
            (CheckResult, elapsed_seconds, timed_out_flag)

        超时时:
            - 关键检查 (critical) → fail-close, 返回 passed=False
            - 非关键检查 → fail-open, 返回 passed=True (附带告警 reason)
        线程池仅在该检查内创建并自动关闭, 避免资源泄露。
        超时后无法强行 kill 线程, 仅停止等待; 但 cancel future 让 Python 解释器在
        GIL 释放时尽快回收, 同时 with 语句会等待线程退出 (shutdown wait=True),
        避免线程池累积。
        """
        critical = self._is_critical(check.name)
        t0 = time.monotonic()
        # 每个 check 独立线程池, 用完即关
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"risk-{check.name}"
        )
        try:
            future = executor.submit(check.check, signal, context)
            try:
                result = future.result(timeout=timeout)
                elapsed = time.monotonic() - t0
                # 兜底: check 返回非 CheckResult, 视为失败
                if not isinstance(result, CheckResult):
                    logger.error(
                        "[USRiskPipeline] %s 返回非 CheckResult 类型 (%s), 视为拒绝",
                        check.name, type(result).__name__,
                    )
                    return (
                        CheckResult(
                            passed=False,
                            check_name=check.name,
                            reason=f"invalid return type: {type(result).__name__}",
                        ),
                        elapsed,
                        False,
                    )
                return result, elapsed, False
            except FuturesTimeout:
                elapsed = time.monotonic() - t0
                future.cancel()  # 尝试取消 (CPU 密集任务不一定能 cancel)
                self._timeout_counts[check.name] = self._timeout_counts.get(check.name, 0) + 1
                if critical:
                    logger.critical(
                        "[USRiskPipeline] 🚨 关键检查超时 [%s] > %.1fs, fail-CLOSE (拒绝交易) | %s %s",
                        check.name, timeout,
                        getattr(signal, 'symbol', ''),
                        getattr(signal, 'direction', ''),
                    )
                    return (
                        CheckResult(
                            passed=False,
                            check_name=check.name,
                            reason=f"TIMEOUT after {timeout:.1f}s - fail-close (critical)",
                        ),
                        elapsed,
                        True,
                    )
                else:
                    logger.warning(
                        "[USRiskPipeline] ⏱️ 风控检查超时 [%s] > %.1fs, fail-OPEN (默认通过) | %s %s",
                        check.name, timeout,
                        getattr(signal, 'symbol', ''),
                        getattr(signal, 'direction', ''),
                    )
                    return (
                        CheckResult(
                            passed=True,
                            check_name=check.name,
                            reason=f"TIMEOUT after {timeout:.1f}s - fail-open",
                        ),
                        elapsed,
                        True,
                    )
        finally:
            # wait=False: 不阻塞主流程; 线程池在残留任务结束后自然回收
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Python < 3.9 不支持 cancel_futures
                executor.shutdown(wait=False)
    
    def run(self, signal, context: Dict[str, Any] = None) -> PipelineResult:
        """
        执行风控 Pipeline (含单步超时与总超时)
        
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

        pipeline_start = time.monotonic()

        for check in self._checks:
            # ── 总超时检查 ──
            elapsed_total = time.monotonic() - pipeline_start
            remaining = self.total_timeout - elapsed_total
            if remaining <= 0:
                result.timed_out = True
                result.passed = False  # 总超时按 fail-close 处理 (后续检查可能含关键)
                result.rejected_by = "PIPELINE_TOTAL_TIMEOUT"
                result.reason = (
                    f"Pipeline total timeout {self.total_timeout:.1f}s exceeded "
                    f"after {result.checks_run} checks"
                )
                logger.critical(
                    "[USRiskPipeline] 🚨 Pipeline 总超时 %.1fs (已执行 %d/%d), fail-close | %s %s",
                    self.total_timeout, result.checks_run, len(self._checks),
                    result.symbol, result.direction,
                )
                result.total_elapsed_ms = elapsed_total * 1000.0
                return result

            # 单步超时取较小值 (避免最后一项超总超时)
            step_timeout = min(self.per_check_timeout, max(remaining, 0.1))

            try:
                check_result, elapsed, timed_out = self._run_check_with_timeout(
                    check, signal, context, step_timeout,
                )
                result.checks_run += 1

                # 慢检查统计
                if elapsed >= self._slow_threshold and not timed_out:
                    self._slow_check_counts[check.name] = (
                        self._slow_check_counts.get(check.name, 0) + 1
                    )
                    logger.info(
                        "[USRiskPipeline] 🐢 慢检查 [%s] %.2fs (>= %.2fs)",
                        check.name, elapsed, self._slow_threshold,
                    )
                else:
                    logger.debug(
                        "[USRiskPipeline] · %s passed=%s elapsed=%.3fs",
                        check.name, check_result.passed, elapsed,
                    )

                result.check_details.append({
                    "check": check_result.check_name,
                    "passed": check_result.passed,
                    "reason": check_result.reason,
                    "elapsed_ms": round(elapsed * 1000.0, 2),
                    "timed_out": timed_out,
                })

                if not check_result.passed:
                    result.passed = False
                    result.rejected_by = check_result.check_name
                    result.reason = check_result.reason
                    if timed_out:
                        result.timed_out = True
                    logger.warning(
                        "[USRiskPipeline] ❌ %s 拒绝 | %s %s | %s",
                        check_result.check_name, result.symbol, result.direction, check_result.reason
                    )
                    result.total_elapsed_ms = (time.monotonic() - pipeline_start) * 1000.0
                    return result

                result.checks_passed += 1

            except Exception as e:
                logger.error("[USRiskPipeline] 检查异常 [%s]: %s", check.name, e, exc_info=True)
                # P0-1 修复 (2026-05-03): 风控检查崩溃 → 拒绝交易 (fail-closed)
                # 安全原则: 无法确认安全 = 不安全 → 必须拦截
                result.passed = False
                result.rejected_by = f"{check.name}:EXCEPTION"
                result.reason = f"风控检查崩溃: {check.name} ({e})"
                logger.critical(
                    "[USRiskPipeline] 🚨 %s 因检查崩溃被拒绝 (fail-closed) | %s %s",
                    check.name, result.symbol, result.direction
                )
                result.total_elapsed_ms = (time.monotonic() - pipeline_start) * 1000.0
                return result

        result.total_elapsed_ms = (time.monotonic() - pipeline_start) * 1000.0
        if result.passed:
            logger.debug(
                "[USRiskPipeline] ✅ %s %s 全部通过 (%d/%d) %.2fms",
                result.symbol, result.direction,
                result.checks_passed, result.checks_run, result.total_elapsed_ms,
            )

        return result
    
    @property
    def checks(self) -> List[str]:
        return [c.name for c in self._checks]
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_checks": len(self._checks),
            "checks": self.checks,
            "per_check_timeout": self.per_check_timeout,
            "total_timeout": self.total_timeout,
            "critical_checks": sorted(self._critical_checks),
            "slow_check_counts": dict(self._slow_check_counts),
            "timeout_counts": dict(self._timeout_counts),
        }
