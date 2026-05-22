"""策略路由模块

将 LLM 盘前分析结果路由到不同通道:
  - confidence >= 0.75 && relevance >= 0.75 → 自动生成 Signal → DecisionBuffer
  - confidence 0.55~0.75 → DingTalk ActionCard (人工确认)
  - confidence < 0.55 → DingTalk info card (仅观察)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

logger = logging.getLogger("ftseek.pipeline.strategy_router")


@dataclass
class StrategyRouter:
    """策略路由器

    根据置信度和相关性评分决定 LLM 输出的去向。
    """

    config: Dict = field(default_factory=dict)
    auto_execute_threshold: float = 0.70  # 2026-05-22: 降低阈值，增加交易机会 (原 0.75/0.80)
    review_threshold: float = 0.55

    def __post_init__(self):
        self.config = self.config or {}
        self.auto_execute_threshold = self.config.get(
            "auto_execute_threshold", self.auto_execute_threshold
        )
        self.review_threshold = self.config.get(
            "review_threshold", self.review_threshold
        )

        # Phase 2: 政策一票否决配置
        self.policy_veto = self.config.get("policy_veto", {
            "enabled": False,
            "threshold": 0.80,
            "veto_actions": ["BUY"]
        })

    def route(self, assessment: Any, market_context: Optional[Dict] = None) -> str:
        """路由单个评估结果

        Args:
            assessment: PreMarketAssessment 实例
            market_context: 可选的市场上下文（用于做空趋势检查）

        Returns:
            "auto" / "review" / "observe"
        """
        # Phase 2: 检查政策一票否决
        if self._check_policy_veto(assessment):
            logger.warning(f"[Policy Veto] 信号 {getattr(assessment, 'focus_symbols', [])} 被政策否决")
            return "observe"

        # ── 做空门禁检查 (新增) ──
        direction = getattr(assessment, "direction", "")
        if direction in ("SELL", "SHORT"):
            from app.pipeline.short_gate import get_short_gate
            gate = get_short_gate()
            route, reason = gate.check(assessment, context=market_context)
            if route == "skip":
                # 非做空信号，走正常路由
                pass
            elif route == "observe":
                logger.info(f"[ShortGate] 🚫 {direction} 信号被门禁拦截: {reason}")
                return "observe"  # 降级为观察
            else:
                # "auto" 或 "review" — 做空门禁已确认质量，直接放行
                logger.info(f"[ShortGate] ✅ {direction} {route}: {reason}")
                return route

        if assessment.should_auto_execute():
            return "auto"
        elif assessment.should_review():
            return "review"
        else:
            return "observe"

    def _check_policy_veto(self, assessment: Any) -> bool:
        """检查是否触发政策一票否决"""
        if not self.policy_veto.get("enabled", False):
            return False

        # 获取方向
        direction = getattr(assessment, "direction", "")
        if direction not in self.policy_veto.get("veto_actions", ["BUY"]):
            return False

        # 获取政策风险评分 (如果 LLM 或 RelevanceScorer 已提供)
        policy_risk = getattr(assessment, "policy_risk_score", 0) or getattr(assessment, "policy_relevance", 0)
        
        # 如果政策风险 > 阈值 (如 0.80)，触发否决
        if policy_risk > self.policy_veto.get("threshold", 0.80):
            return True

        # 兜底: 检查 risk_flags 是否包含强负面政策
        flags = getattr(assessment, "risk_flags", [])
        if any("REGULATION" in f.upper() or "POLICY" in f.upper() for f in flags):
            return True

        return False

    def route_batch(self, assessments: List[Any], market_context: Optional[Dict] = None) -> Dict[str, List[Any]]:
        """批量路由"""
        result = {"auto": [], "review": [], "observe": []}
        for a in assessments:
            channel = self.route(a, market_context=market_context)
            result[channel].append(a)

        logger.info(
            f"[StrategyRouter] 路由: auto={len(result['auto'])}, "
            f"review={len(result['review'])}, observe={len(result['observe'])}"
        )
        return result


@dataclass
class PreMarketSignalConverter:
    """将 PreMarketAssessment 转换为 Signal 对象

    用于自动执行通道和人工确认后通道。
    """

    def convert(self, assessment: Any) -> Optional[Any]:
        """转换为 Signal 对象

        Args:
            assessment: PreMarketAssessment 实例

        Returns:
            Signal 对象，若方向为 WATCH/NO_TRADE 则返回 None
        """
        try:
            from app.strategy.base import Signal, SignalAction

            direction = getattr(assessment, "direction", "WATCH")
            if direction in ("WATCH", "NO_TRADE", "NEUTRAL"):
                return None

            # 方向映射
            if direction == "BUY":
                action = SignalAction.BUY
            elif direction == "SELL":
                action = SignalAction.SELL
            else:
                return None

            # 优先级: auto=4 (CRITICAL), review=3 (HIGH)
            priority = 4 if assessment.should_auto_execute() else 3

            # 焦点标的 (取第一个)
            symbols = getattr(assessment, "focus_symbols", [])
            code = symbols[0] if symbols else ""
            if not code:
                logger.warning(
                    f"[SignalConverter] 无目标标的，跳过: {assessment.event_pack_id}"
                )
                return None

            # 过期时间: 根据 duration 设置
            expire_at = self._expire_at(assessment)

            # reason
            theme = getattr(assessment, "recommendation", "")[:100]
            reason = (
                f"盘前LLM分析: {theme} | "
                f"confidence={assessment.confidence:.2f} | "
                f"relevance={assessment.relevance_score:.2f} | "
                f"direction={direction}"
            )

            signal = Signal(
                code=code,
                action=action,
                price=0.0,  # 市价单
                quantity=0,  # 由仓位管理器决定
                reason=reason,
                strategy="premarket_analyst",
                priority=priority,
                timestamp=time.time(),
                expire_at=expire_at,
            )

            # 附加完整评估参考
            signal.extra = {
                "assessment": assessment.to_dict(),
                "event_pack_id": getattr(assessment, "event_pack_id", ""),
                "focus_symbols": symbols,
                "entry_style": getattr(assessment, "entry_style", ""),
                "exit_style": getattr(assessment, "exit_style", ""),
                "position_ratio": getattr(assessment, "position_ratio", 0.0),
                "risk_flags": getattr(assessment, "risk_flags", []),
                "trading_thesis": getattr(assessment, "trading_thesis", []),
            }

            logger.info(
                f"[SignalConverter] 生成信号: {code} {direction} "
                f"(priority={priority}, strategy=premarket_analyst)"
            )
            return signal

        except ImportError as e:
            logger.error(f"[SignalConverter] 导入失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[SignalConverter] 转换异常: {e}")
            return None

    def _expire_at(self, assessment: Any) -> float:
        """根据 duration 计算过期时间"""
        duration = getattr(assessment, "duration", "intraday")
        now = time.time()

        if duration == "intraday":
            # 当日有效 (到 16:00 HKT ≈ 28800 秒后)
            return now + 28800
        elif duration == "1-3days":
            return now + 86400 * 2
        else:
            return now + 86400 * 7  # 1 week

    def to_dingtalk_review_card(self, assessment: Any) -> Dict:
        """生成 DingTalk ActionCard 数据

        用于人工确认通道的 ActionCard 推送。
        """
        direction = getattr(assessment, "direction", "WATCH")
        confidence = getattr(assessment, "confidence", 0.0)
        symbols = getattr(assessment, "focus_symbols", [])
        theme = getattr(assessment, "recommendation", "")[:80]

        # 颜色标记
        color = "#FF0000" if direction == "SELL" else "#00AA00" if direction == "BUY" else "#999"

        return {
            "title": f"盘前分析确认 [{direction}] {theme}",
            "text": (
                f"## 盘前分析待确认\n\n"
                f"**标的**: {', '.join(symbols) or '无'}\n"
                f"**方向**: <font color='{color}'>{direction}</font>\n"
                f"**置信度**: {confidence:.2f}\n"
                f"**建议仓位**: {getattr(assessment, 'position_ratio', 0.0):.0%}\n"
                f"**入场方式**: {getattr(assessment, 'entry_style', '-')}\n"
                f"**退出方式**: {getattr(assessment, 'exit_style', '-')}\n"
                f"**建议**: {theme}\n"
                f"**风险标记**: {', '.join(getattr(assessment, 'risk_flags', ['无']))}\n\n"
                f"请确认是否执行此交易建议。"
            ),
            "assessment_id": getattr(assessment, "event_pack_id", ""),
            "single_title": "确认执行",
            "single_url": "",  # 由 DingTalk API 动态生成
        }
