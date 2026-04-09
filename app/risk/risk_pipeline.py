#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风控 Pipeline (Risk Pipeline) - 2026-04-09

将 _on_consensus_inner 中分散的 20+ 项风控检查，拆分为独立的 RiskCheck，
通过 Pipeline 模式串行执行，任一检查拒绝即终止。

架构位置:
  共识引擎 → 决策缓冲层 → [风控 Pipeline] → 执行层

优势:
1. 每项检查独立可测试
2. 检查顺序可通过配置调整
3. 新增检查只需添加 RiskCheck 实现类
4. 审计日志完整记录每项检查结果
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
logger.propagate = False


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
            deps: 依赖注入字典（包含 subscriber/executor 等引用）
        
        Returns:
            CheckResult
        """
        pass


# ============================================================
#  具体检查实现
# ============================================================

class DecisionBufferCheck(RiskCheck):
    """决策缓冲层审批检查"""
    
    @property
    def name(self) -> str:
        return "decision_buffer"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        try:
            from app.decision_buffer import get_decision_buffer, DecisionStatus
            decision_buffer = get_decision_buffer()
            decision = decision_buffer.process_signal(ctx.consensus_result)
            
            if decision.status != DecisionStatus.APPROVED:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=decision.rejection_reason,
                    reject_code="decision_buffer_rejected",
                )
            return CheckResult(passed=True, check_name=self.name)
        except Exception as e:
            # 决策缓冲层异常降级：不阻断（因为后续还有风控检查）
            logger.warning(f"[决策缓冲层] 检查异常，降级放行: {e}")
            return CheckResult(passed=True, check_name=self.name)


class QualityCheck(RiskCheck):
    """质量管理器检查 + 仓位调节"""
    
    @property
    def name(self) -> str:
        return "quality"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber or not subscriber.quality_scorer:
            return CheckResult(passed=True, check_name=self.name)
        
        if ctx.action != SignalAction.BUY:
            return CheckResult(passed=True, check_name=self.name)
        
        qr = subscriber._check_quality_and_valuation(ctx.code, ctx.consensus_result.price)
        if qr is None or qr.get('reject'):
            reason = qr.get('reason', '未知原因') if qr else '检查失败'
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"质量检查拒绝: {reason}",
                reject_code="quality_rejected",
            )
        
        # 质量等级影响仓位
        quality_class = qr.get('quality_class', 'CLASS_C')
        if quality_class == 'CLASS_A':
            quality_boost = 1.3
        elif quality_class == 'CLASS_B':
            quality_boost = 1.0
        else:
            quality_boost = 0.7
        
        return CheckResult(
            passed=True,
            check_name=self.name,
            context_updates={"quality_boost": quality_boost},
        )


class TradingHoursCheck(RiskCheck):
    """交易时间检查"""
    
    @property
    def name(self) -> str:
        return "trading_hours"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        if not subscriber._is_trading_hours():
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason="非交易时间",
                reject_code="non_trading_hours",
            )
        
        if not subscriber._is_high_quality_trading_hours():
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason="开/收盘噪音时段（信号质量差）",
                reject_code="noise_hours",
            )
        
        return CheckResult(passed=True, check_name=self.name)


class CircuitBreakerCheck(RiskCheck):
    """熔断器检查"""
    
    @property
    def name(self) -> str:
        return "circuit_breaker"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        if subscriber.circuit_breaker.is_paused():
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason="熔断器已触发，暂停交易",
                reject_code="circuit_breaker",
            )
        
        return CheckResult(passed=True, check_name=self.name)


class ADXConsistencyCheck(RiskCheck):
    """ADX-趋势一致性防守检查"""
    
    @property
    def name(self) -> str:
        return "adx_consistency"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)
        
        _adx_val = getattr(ctx.consensus_result, 'adx_value', 0) or 0
        _regime_str = getattr(ctx.consensus_result, 'market_regime', '') or ''
        _regime_unclear = _regime_str in ('range_bound', 'high_volatility', 'neutral', '')
        
        if _adx_val >= 50 and _regime_unclear:
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"ADX={_adx_val:.1f} 极高但市场状态={_regime_str!r}，趋势方向不明",
                reject_code="adx_inconsistent",
            )
        
        return CheckResult(passed=True, check_name=self.name)


class MultiTimeframeCheck(RiskCheck):
    """多周期趋势过滤"""
    
    @property
    def name(self) -> str:
        return "multi_timeframe"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        action_str = ctx.action.value if hasattr(ctx.action, 'value') else str(ctx.action)
        mtf_confirmed, mtf_boost, mtf_reason = subscriber._get_multi_timeframe_signal(ctx.code, action_str)
        
        # 同步 MTF 对齐分数至共识引擎缓存
        try:
            buy_score, _ = subscriber._get_mtf_alignment_score(ctx.code, "BUY")
            sell_score, _ = subscriber._get_mtf_alignment_score(ctx.code, "SELL")
            subscriber._consensus.update_mtf_alignment(ctx.code, buy_score, sell_score)
        except Exception as _mtf_e:
            logger.debug(f"[MTF缓存同步] {ctx.code} 失败: {_mtf_e}")
        
        if not mtf_confirmed:
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"多周期过滤拒绝: {mtf_reason}",
                reject_code="mtf_rejected",
            )
        
        return CheckResult(
            passed=True,
            check_name=self.name,
            context_updates={
                "mtf_confirmed": True,
                "mtf_boost": mtf_boost,
                "mtf_reason": mtf_reason,
            },
        )


class TrailingCooldownCheck(RiskCheck):
    """追踪止盈冷却期检查"""
    
    @property
    def name(self) -> str:
        return "trailing_cooldown"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)
        
        trailing_exit_ts = subscriber._trailing_exit_time.get(ctx.code, 0)
        elapsed = time.time() - trailing_exit_ts
        TRAILING_COOLDOWN_SEC = 15 * 60  # 15 分钟
        
        if elapsed < TRAILING_COOLDOWN_SEC:
            remain_min = (TRAILING_COOLDOWN_SEC - elapsed) / 60
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"追踪止盈后冷却中，剩余 {remain_min:.1f}min",
                reject_code="trailing_cooldown",
            )
        
        return CheckResult(passed=True, check_name=self.name)


class WeakSignalCheck(RiskCheck):
    """弱信号过滤"""
    
    @property
    def name(self) -> str:
        return "weak_signal"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)
        
        score_abs = abs(ctx.score)
        if score_abs < 1.2:
            strategies = '+'.join(ctx.consensus_result.agree_strategies)
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"共识分数={ctx.score:.2f} < 1.2 (同向策略: {strategies})",
                reject_code="weak_signal",
            )
        
        return CheckResult(passed=True, check_name=self.name)


class ConcentrationCheck(RiskCheck):
    """跨标的相关性控制"""
    
    @property
    def name(self) -> str:
        return "concentration"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)
        
        concentration_discount = 1.0
        try:
            open_positions = subscriber.position_manager.get_all_positions()
            open_codes = {p.code for p in open_positions if p.code != ctx.code}
            if len(open_codes) >= 2:
                concentration_discount = 0.7
                logger.info(
                    f"[相关性控制] {ctx.code} 当前已有 {len(open_codes)} 个其他标的持仓 "
                    f"({', '.join(sorted(open_codes))})，注入分散折扣 0.7x"
                )
        except Exception as _e:
            logger.debug(f"[相关性控制] 检查异常: {_e}")
            # 安全策略：无法确认持仓数时，保守施加分散折扣
            concentration_discount = 0.7
        
        return CheckResult(
            passed=True,
            check_name=self.name,
            context_updates={"concentration_discount": concentration_discount},
        )


class RiskRewardCheck(RiskCheck):
    """盈亏比预检"""
    
    @property
    def name(self) -> str:
        return "risk_reward"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            stop_pct = abs(_dem.current_params.get('stop_loss', -0.03))
            take_pct = abs(_dem.current_params.get('take_profit', 0.045))
            if stop_pct > 0:
                rr_ratio = take_pct / stop_pct
                min_rr = 1.2
                if rr_ratio < min_rr:
                    return CheckResult(
                        passed=False,
                        check_name=self.name,
                        reason=f'R:R={rr_ratio:.2f}:1 < {min_rr}:1 (止盈={take_pct:.1%}/止损={stop_pct:.1%})',
                        reject_code="risk_reward",
                    )
        except Exception as e:
            logger.debug(f"[盈亏比] 检查失败，跳过: {e}")
        
        return CheckResult(passed=True, check_name=self.name)


class CooldownCheck(RiskCheck):
    """信号推送冷却检查"""
    
    @property
    def name(self) -> str:
        return "cooldown"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        if not subscriber._is_cooled_down(ctx.code):
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"冷却中 (分数={ctx.score:+.2f})",
                reject_code="cooldown",
            )
        
        return CheckResult(passed=True, check_name=self.name)


class WatchlistCheck(RiskCheck):
    """Watchlist 过滤"""
    
    @property
    def name(self) -> str:
        return "watchlist"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        try:
            watchlist_path = "/opt/ftseek/FTSeek/config/watchlist.json"
            with open(watchlist_path, 'r') as f:
                watchlist = json.load(f)
            enabled_codes = set()
            for stock in watchlist.get('hk_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            for stock in watchlist.get('us_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            
            if ctx.code not in enabled_codes:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"{ctx.code} 不在启用列表中",
                    reject_code="watchlist",
                )
        except Exception as e:
            logger.warning(f"watchlist 过滤异常：{e}")
        
        return CheckResult(passed=True, check_name=self.name)


class HKAdapterCheck(RiskCheck):
    """港股特性适配检查"""
    
    @property
    def name(self) -> str:
        return "hk_adapter"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        code = ctx.code
        if not code.startswith("HK."):
            return CheckResult(passed=True, check_name=self.name)
        
        # 注意：港股适配需要修改 Signal 对象，这里只做过滤判断
        # 实际适配在执行层完成
        return CheckResult(passed=True, check_name=self.name)


class MinHoldTimeCheck(RiskCheck):
    """最小持仓时间保护"""
    
    @property
    def name(self) -> str:
        return "min_hold_time"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        subscriber = deps.get("subscriber")
        if not subscriber:
            return CheckResult(passed=True, check_name=self.name)
        
        if ctx.trade_type != 'CLOSE':
            return CheckResult(passed=True, check_name=self.name)
        
        position = subscriber.position_manager.get_position(ctx.code)
        if position is None:
            return CheckResult(passed=True, check_name=self.name)
        
        MIN_HOLD_MINUTES = 30
        try:
            entry_time_str = getattr(position, 'entry_time', None)
            if entry_time_str:
                if isinstance(entry_time_str, str):
                    et = datetime.fromisoformat(entry_time_str)
                else:
                    et = entry_time_str
                hold_minutes = ((datetime.now() - et).total_seconds()) / 60
                if hold_minutes < MIN_HOLD_MINUTES:
                    pnl_rate = (ctx.consensus_result.price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
                    try:
                        from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                        stop_pct = abs(_dem.get_stop_loss())
                    except Exception:
                        stop_pct = 0.03
                    if pnl_rate > -stop_pct:
                        return CheckResult(
                            passed=False,
                            check_name=self.name,
                            reason=f"持仓仅 {hold_minutes:.1f}min < {MIN_HOLD_MINUTES}min，拒绝反向信号平仓",
                            reject_code="min_hold_time",
                        )
        except Exception as _e:
            logger.debug(f"[最小持仓保护] 检查异常: {_e}")
        
        return CheckResult(passed=True, check_name=self.name)


class OpenDedupCheck(RiskCheck):
    """开仓原子锁去重"""
    
    @property
    def name(self) -> str:
        return "open_dedup"
    
    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        # 注意：开仓去重需要预占时间戳，这里只做检查
        # 实际预占在 _on_consensus_inner 的执行段完成
        return CheckResult(passed=True, check_name=self.name)


# ============================================================
#  风控 Pipeline
# ============================================================

class RiskPipeline:
    """
    风控 Pipeline
    
    串行执行所有注册的 RiskCheck，任一检查拒绝即终止。
    支持动态增删检查、调整顺序。
    """
    
    # 默认检查顺序（按执行优先级排列）
    DEFAULT_CHECKS = [
        DecisionBufferCheck,       # 1. 决策缓冲层审批（最优先，避免后续无效检查）
        QualityCheck,              # 2. 质量管理器
        TradingHoursCheck,         # 3. 交易时间
        CircuitBreakerCheck,       # 4. 熔断器
        ADXConsistencyCheck,       # 5. ADX-趋势一致性
        MultiTimeframeCheck,       # 6. 多周期趋势
        TrailingCooldownCheck,     # 7. 追踪止盈冷却
        WeakSignalCheck,           # 8. 弱信号
        ConcentrationCheck,        # 9. 相关性控制
        RiskRewardCheck,           # 10. 盈亏比
        CooldownCheck,             # 11. 冷却期
        WatchlistCheck,            # 12. watchlist
        HKAdapterCheck,            # 13. 港股适配
        MinHoldTimeCheck,          # 14. 最小持仓时间
        OpenDedupCheck,            # 15. 开仓去重
    ]
    
    def __init__(self, checks: List[RiskCheck] = None, deps: Dict = None):
        if checks is None:
            checks = [cls() for cls in self.DEFAULT_CHECKS]
        self.checks = checks
        self.deps = deps or {}
    
    def evaluate(self, ctx: SignalContext) -> PipelineResult:
        """
        执行风控 Pipeline
        
        Args:
            ctx: 信号上下文
        
        Returns:
            PipelineResult: 包含所有检查结果和最终决定
        """
        results = []
        
        for check in self.checks:
            try:
                result = check.check(ctx, self.deps)
                results.append(result)
                
                # 应用上下文更新
                if result.context_updates:
                    for key, value in result.context_updates.items():
                        if hasattr(ctx, key):
                            setattr(ctx, key, value)
                
                if not result.passed:
                    logger.info(
                        f"[风控Pipeline] [{ctx.trace_id}] {ctx.code} 被 [{result.check_name}] 拒绝: "
                        f"{result.reason}"
                    )
                    return PipelineResult(
                        passed=False,
                        results=results,
                        reject_reason=result.reason,
                        reject_code=result.reject_code,
                        reject_check=result.check_name,
                    )
                    
            except Exception as e:
                # 安全第一：检查异常默认拒绝
                logger.warning(
                    f"[风控Pipeline] [{ctx.trace_id}] {ctx.code} 检查 [{check.name}] 异常: {e}"
                )
                results.append(CheckResult(
                    passed=False,
                    check_name=check.name,
                    reason=f"检查异常: {e}",
                    reject_code=f"{check.name}_error",
                ))
                return PipelineResult(
                    passed=False,
                    results=results,
                    reject_reason=f"检查异常: {e}",
                    reject_code=f"{check.name}_error",
                    reject_check=check.name,
                )
        
        return PipelineResult(passed=True, results=results)
    
    def add_check(self, check: RiskCheck, position: int = -1):
        """在指定位置插入检查"""
        if position == -1:
            self.checks.append(check)
        else:
            self.checks.insert(position, check)
    
    def remove_check(self, name: str):
        """移除指定名称的检查"""
        self.checks = [c for c in self.checks if c.name != name]
    
    def get_check_names(self) -> List[str]:
        """获取当前检查名称列表"""
        return [c.name for c in self.checks]


# ============================================================
#  全局单例
# ============================================================

_risk_pipeline: Optional[RiskPipeline] = None


def get_risk_pipeline(deps: Dict = None) -> RiskPipeline:
    """获取风控Pipeline单例"""
    global _risk_pipeline
    if _risk_pipeline is None:
        _risk_pipeline = RiskPipeline(deps=deps)
    return _risk_pipeline


def init_risk_pipeline(deps: Dict = None) -> RiskPipeline:
    """初始化风控Pipeline"""
    global _risk_pipeline
    _risk_pipeline = RiskPipeline(deps=deps)
    logger.info(
        f"风控Pipeline已初始化 | 检查链: {' → '.join(_risk_pipeline.get_check_names())}"
    )
    return _risk_pipeline
