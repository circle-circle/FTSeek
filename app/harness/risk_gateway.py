"""
统一风控网关 (RiskGateway) — 新旧风控的统一入口

职责:
1. 合并旧系统风控 (RiskEngine) + 新框架风控 (DynamicRiskExit + MarketContext)
2. 提供统一的 check_entry / check_exit 接口
3. 紧急拦截 (从旧系统 EmergencyInterceptor 移植接口)
4. 风控决策审计日志

设计:
- 旧系统的 RiskEngine 是 "入场前宏观风控" (VIX/熔断/回撤/持仓限制)
- 新框架的 DynamicRiskExit 是 "入场后微观风控" (个股限额/连续亏损/ATR止损)
- MarketContext 是 "市场上下文门禁" (VIX regime + 资本水位 + 趋势)
- RiskGateway 将三者合并为一个统一决策点

调用链:
  signal_pipeline → RiskGateway.check_entry(symbol, signal)
    ├─ MarketContext (VIX + 资本水位)
    ├─ RiskEngine (旧: 熔断/回撤/持仓限制)
    └─ DynamicRiskExit (新: 个股限额/连续亏损)

  position_exit_monitor → RiskGateway.check_exit(position, market_data)
    ├─ EmergencyInterceptor (闪崩检测)
    └─ DynamicRiskExit.evaluate (ATR/TP/TS)

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """风控决策 — 统一格式"""
    allowed: bool
    source: str                     # "market_context" | "risk_engine" | "dynamic_risk" | "emergency"
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class RiskGateway:
    """
    统一风控网关

    使用方式:
        gateway = RiskGateway()
        
        # 入场检查
        decision = gateway.check_entry("AAPL", signal_data)
        if not decision.allowed:
            logger.info(f"AAPL 入场被拒: {decision.reason}")
            return
        
        # 退出检查
        exit_decision = gateway.check_exit(position, market_data)
    """

    def __init__(self, scheduler=None):
        self._scheduler = scheduler
        self._market_context = None
        self._dynamic_risk = None
        self._risk_engine = None
        self._audit_log: List[Dict] = []

    def _get_market_context(self):
        if self._market_context is None:
            from app.harness.market_context import MarketContextProvider
            self._market_context = MarketContextProvider()
        return self._market_context

    def _get_dynamic_risk(self):
        if self._dynamic_risk is None:
            try:
                from app.exit.dynamic_risk import DynamicRiskExit
                self._dynamic_risk = DynamicRiskExit()
            except Exception as e:
                logger.warning(f"DynamicRiskExit 加载失败: {e}")
        return self._dynamic_risk

    def _get_risk_engine(self):
        """获取旧系统 RiskEngine 实例"""
        if self._risk_engine is None and self._scheduler:
            self._risk_engine = getattr(self._scheduler, 'risk_engine', None)
        return self._risk_engine

    def check_entry(
        self,
        symbol: str,
        signal_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> RiskDecision:
        """
        入场风控综合检查 (三层门禁)

        Layer 1: MarketContext (VIX regime + 资本水位)
        Layer 2: RiskEngine (旧: 熔断/回撤/持仓限制/财报保护)
        Layer 3: DynamicRiskExit (新: 个股限额/连续亏损)

        任一层拒绝 → 整体拒绝

        Returns:
            RiskDecision
        """
        signal_data = signal_data or {}

        # Layer 1: MarketContext
        try:
            mc = self._get_market_context()
            allowed, reason = mc.check_entry_allowed(symbol)
            if not allowed:
                decision = RiskDecision(
                    allowed=False,
                    source="market_context",
                    reason=reason,
                    details=mc.get_context().to_dict(),
                )
                self._log(decision, symbol, "entry")
                return decision
        except Exception as e:
            logger.warning(f"MarketContext 检查异常 (降级通过): {e}")

        # Layer 2: 旧系统 RiskEngine (如果有)
        risk_engine = self._get_risk_engine()
        if risk_engine:
            try:
                # 旧系统 pre_trade_check 接口
                from app.risk.pre_trade_check import PreTradeChecker
                checker = PreTradeChecker(self._scheduler)
                result = checker.check(symbol, signal_data)
                if hasattr(result, 'allowed') and not result.allowed:
                    decision = RiskDecision(
                        allowed=False,
                        source="risk_engine",
                        reason=getattr(result, 'reason', '旧系统风控拒绝'),
                        details={"pre_trade_result": str(result)},
                    )
                    self._log(decision, symbol, "entry")
                    return decision
            except Exception as e:
                logger.debug(f"旧系统 RiskEngine 检查跳过: {e}")

        # Layer 3: DynamicRiskExit
        dr = self._get_dynamic_risk()
        if dr:
            try:
                allowed, reason = dr.check_entry_allowed(symbol)
                if not allowed:
                    decision = RiskDecision(
                        allowed=False,
                        source="dynamic_risk",
                        reason=reason,
                    )
                    self._log(decision, symbol, "entry")
                    return decision
            except Exception as e:
                logger.debug(f"DynamicRiskExit 检查跳过: {e}")

        # 全部通过
        decision = RiskDecision(
            allowed=True,
            source="all_passed",
            reason="三层风控全部通过",
        )
        self._log(decision, symbol, "entry")
        return decision

    def check_exit(
        self,
        position: Dict[str, Any],
        market_data: Dict[str, Any],
    ) -> Optional[RiskDecision]:
        """
        退出检查 — 紧急拦截 + 动态风控退出

        Returns:
            RiskDecision if exit triggered, None if no exit needed
        """
        symbol = position.get("symbol", "")

        # Emergency check (闪崩检测)
        # 从旧系统 emergency_interceptor 移植: 价格闪崩 > 3×ATR 在 60s 内
        try:
            price = market_data.get("price", 0)
            prev_price = position.get("last_price", price)
            atr = market_data.get("atr", 0)

            if atr > 0 and prev_price > 0:
                flash_threshold = atr * 3.0
                price_drop = abs(price - prev_price)

                direction = position.get("direction", "LONG")
                is_adverse = (
                    (direction == "LONG" and price < prev_price) or
                    (direction == "SHORT" and price > prev_price)
                )

                if is_adverse and price_drop >= flash_threshold:
                    decision = RiskDecision(
                        allowed=True,  # exit is allowed (encouraged)
                        source="emergency",
                        reason=f"闪崩检测: 价格变动 ${price_drop:.2f} >= 3×ATR ${flash_threshold:.2f}",
                        details={
                            "price": price,
                            "prev_price": prev_price,
                            "atr": atr,
                            "direction": direction,
                        },
                    )
                    self._log(decision, symbol, "exit_emergency")
                    return decision
        except Exception as e:
            logger.debug(f"Emergency check 跳过: {e}")

        # Dynamic exit evaluation
        dr = self._get_dynamic_risk()
        if dr:
            try:
                exit_result = dr.evaluate(position, market_data)
                if exit_result and exit_result.get("should_exit"):
                    decision = RiskDecision(
                        allowed=True,
                        source="dynamic_risk",
                        reason=exit_result.get("reason", "动态退出触发"),
                        details=exit_result,
                    )
                    self._log(decision, symbol, "exit")
                    return decision
            except Exception as e:
                logger.debug(f"DynamicRiskExit.evaluate 跳过: {e}")

        return None  # 无需退出

    def _log(self, decision: RiskDecision, symbol: str, action: str):
        """审计日志"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "action": action,
            "allowed": decision.allowed,
            "source": decision.source,
            "reason": decision.reason,
        }
        self._audit_log.append(entry)
        if len(self._audit_log) > 500:
            self._audit_log = self._audit_log[-250:]

        level = logging.INFO if not decision.allowed else logging.DEBUG
        logger.log(level, f"[RiskGateway] {action} {symbol}: "
                          f"{'✅' if decision.allowed else '🚫'} "
                          f"[{decision.source}] {decision.reason}")

    def get_audit_log(self, limit: int = 50) -> List[Dict]:
        return self._audit_log[-limit:]

    def get_status(self) -> Dict[str, Any]:
        mc = self._get_market_context()
        ctx = mc.get_context() if mc else None
        return {
            "market_context": ctx.to_dict() if ctx else None,
            "dynamic_risk_loaded": self._dynamic_risk is not None,
            "risk_engine_loaded": self._risk_engine is not None,
            "audit_log_size": len(self._audit_log),
        }
