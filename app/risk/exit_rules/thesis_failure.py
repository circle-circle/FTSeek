"""
论文失效退出规则 (Task #75)

当入场论文(thesis)的失效条件被满足时，立即触发退出。
比固定ATR止损更精准：不是"亏了X%就走"，而是"入场理由不成立了就走"。

设计要点:
  1. 仅对 position.metadata.thesis_type 已声明的持仓生效，
     无 thesis_type 的持仓直接返回 None，不影响现有退出链路。
  2. THESIS_FAILURE_MAP 集中维护各类入场论文的失效判定:
       - trend_following     收在 MA20 反向 (grace_bars=2)
       - rsi_reversal        RSI 重新进入极端区 (grace_bars=1)
       - vwap_mean_reversion 偏离扩大超 2% (grace_bars=0)
       - breakout            回到突破位下方 (grace_bars=1)
  3. 通过 trail_state["thesis_fail_streak"] 记录连续失败次数，
     仅当 streak > grace_bars 时才真正触发退出，过滤掉单根 K 线噪音。
  4. 任何异常都吞掉返回 None — 入场论文失效检查不能阻断主链路。
  5. 默认 close_pct=1.0 (全平), 通过 ExitRulePriority.THESIS_FAILURE=35
     与 EXCLUSIVE_RULES 让其与 trailing/graduated/time_stop 互斥。

接口对齐:
  - check(position, market_context, trail_state) -> Optional[dict]
  - 同时提供 should_exit / 顶层函数 check_thesis_failure，
    保持与 BaseExitRule / position_exit_monitor 的注册风格一致。
"""
import logging
from typing import Optional, Dict, Any

from .base import BaseExitRule

logger = logging.getLogger("ftseek.exit_monitor")


# 入场论文 → 失效检查配置
THESIS_FAILURE_MAP: Dict[str, Dict[str, Any]] = {
    "trend_following": {
        "description": "趋势跟踪: 价格收在MA20下方表示趋势破坏",
        "check_fn": "_check_trend_failure",
        "grace_bars": 2,
    },
    "rsi_reversal": {
        "description": "RSI反转: RSI重新进入极端区表示反转失败",
        "check_fn": "_check_rsi_failure",
        "grace_bars": 1,
    },
    "vwap_mean_reversion": {
        "description": "VWAP回归: 偏离扩大表示回归失败",
        "check_fn": "_check_vwap_failure",
        "grace_bars": 0,
    },
    "breakout": {
        "description": "突破: 价格回到突破位下方表示假突破",
        "check_fn": "_check_breakout_failure",
        "grace_bars": 1,
    },
}


def _normalize_direction(direction: Any) -> str:
    """统一 direction 为 'LONG' / 'SHORT' / '' 字符串。

    兼容 Direction(str, Enum) / 纯字符串 / None。
    """
    if direction is None:
        return ""
    if hasattr(direction, "value"):
        return str(direction.value).upper()
    return str(direction).upper()


def _get_position_thesis(position) -> tuple:
    """从 position 元数据中提取 (thesis_type, thesis_anchor)。

    优先读取 position.metadata，其次读取 trail_state 中的回写值，
    任何缺失都返回 (None, {})。
    """
    metadata = getattr(position, "metadata", None) or {}
    thesis_type = metadata.get("thesis_type")
    thesis_anchor = metadata.get("thesis_anchor") or {}

    # 兼容 trail_state 中暂存的论文（部分链路可能写入 trail_state）
    if not thesis_type:
        trail = getattr(position, "trail_state", None) or {}
        thesis_type = trail.get("thesis_type")
        if not thesis_anchor:
            thesis_anchor = trail.get("thesis_anchor") or {}

    if isinstance(thesis_anchor, dict):
        return thesis_type, thesis_anchor
    return thesis_type, {}


class ThesisFailureRule(BaseExitRule):
    """论文失效退出规则 (Task #75)

    PRIORITY = 35：介于 DAY_TRADE(30) 与 TRAILING_STOP(40) 之间，
    早于追踪止损与时间止损触发，确保"入场理由不成立"先行清仓。
    """

    PRIORITY = 35
    NAME = "THESIS_FAILURE"
    rule_name = "THESIS_FAILURE"

    # ------------------------------------------------------------------
    # 各论文失效检查 — 纯函数风格，仅依赖入参
    # ------------------------------------------------------------------
    def _check_trend_failure(self, position, market_context: Dict[str, Any], anchor: Dict[str, Any]) -> bool:
        """趋势跟踪失效: 价格收在 MA20 反向 (留 0.5% 容忍)"""
        price = float(market_context.get("price") or market_context.get("current_price") or 0)
        ma20 = float(market_context.get("ma20") or anchor.get("ma20") or 0)
        direction = _normalize_direction(getattr(position, "direction", None))

        if price <= 0 or ma20 <= 0:
            return False

        if direction == "LONG" and price < ma20 * 0.995:
            return True
        if direction == "SHORT" and price > ma20 * 1.005:
            return True
        return False

    def _check_rsi_failure(self, position, market_context: Dict[str, Any], anchor: Dict[str, Any]) -> bool:
        """RSI 反转失效: RSI 重新进入极端区"""
        rsi_raw = market_context.get("rsi")
        if rsi_raw is None:
            return False
        try:
            rsi = float(rsi_raw)
        except (TypeError, ValueError):
            return False
        direction = _normalize_direction(getattr(position, "direction", None))

        if direction == "LONG" and rsi < 25:
            return True
        if direction == "SHORT" and rsi > 75:
            return True
        return False

    def _check_vwap_failure(self, position, market_context: Dict[str, Any], anchor: Dict[str, Any]) -> bool:
        """VWAP 回归失效: 偏离扩大超 2%"""
        price = float(market_context.get("price") or market_context.get("current_price") or 0)
        vwap = float(market_context.get("vwap") or anchor.get("vwap") or 0)
        if price <= 0 or vwap <= 0:
            return False

        direction = _normalize_direction(getattr(position, "direction", None))
        deviation = (price - vwap) / vwap

        if direction == "LONG" and deviation < -0.02:
            return True
        if direction == "SHORT" and deviation > 0.02:
            return True
        return False

    def _check_breakout_failure(self, position, market_context: Dict[str, Any], anchor: Dict[str, Any]) -> bool:
        """突破失效: 回到突破位反向"""
        price = float(market_context.get("price") or market_context.get("current_price") or 0)
        breakout_level = float(anchor.get("breakout_level") or 0)
        if price <= 0 or breakout_level <= 0:
            return False

        direction = _normalize_direction(getattr(position, "direction", None))
        if direction == "LONG" and price < breakout_level * 0.995:
            return True
        if direction == "SHORT" and price > breakout_level * 1.005:
            return True
        return False

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def check(
        self,
        position,
        market_context: Dict[str, Any],
        trail_state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """检查论文是否失效

        Args:
            position:       持仓对象，metadata 中含 thesis_type / thesis_anchor
            market_context: 当前市场快照 (含 price / rsi / ma20 / vwap …)
            trail_state:    追踪状态 dict，用于记录连续失败次数 (grace_bars)

        Returns:
            None: 论文未失效或不适用
            dict: {"reason": "THESIS_FAILURE_xxx", "close_pct": 1.0, ...}
        """
        try:
            thesis_type, anchor = _get_position_thesis(position)
            if not thesis_type:
                return None

            cfg = THESIS_FAILURE_MAP.get(thesis_type)
            if not cfg:
                logger.debug(
                    f"[THESIS_FAILURE] {getattr(position, 'symbol', '?')}: "
                    f"未知 thesis_type={thesis_type}, 跳过"
                )
                return None

            check_fn = getattr(self, cfg["check_fn"], None)
            if not callable(check_fn):
                return None

            failed = bool(check_fn(position, market_context or {}, anchor))

            # grace_bars 平滑：连续失败次数累计在 trail_state，避免单根噪音 K 线触发
            streak_key = "thesis_fail_streak"
            if not isinstance(trail_state, dict):
                trail_state = {}
            current_streak = int(trail_state.get(streak_key, 0))
            if failed:
                current_streak += 1
            else:
                current_streak = 0
            trail_state[streak_key] = current_streak

            grace_bars = int(cfg.get("grace_bars", 0))
            if not failed or current_streak <= grace_bars:
                return None

            # 触发：组装退出决策
            symbol = getattr(position, "symbol", "?")
            reason_code = f"THESIS_FAILURE_{thesis_type.upper()}"
            description = (
                f"{cfg['description']} | streak={current_streak} > grace_bars={grace_bars}"
            )
            logger.info(
                f"[THESIS_FAILURE] {symbol} thesis={thesis_type} 触发 "
                f"(streak={current_streak}, grace_bars={grace_bars})"
            )
            # 重置 streak，避免下一轮立即重复触发
            trail_state[streak_key] = 0

            current_price = float(
                market_context.get("price")
                or market_context.get("current_price")
                or 0
            )
            return {
                "triggered": True,
                "reason": reason_code,
                "reason_cn": f"入场论文失效: {cfg['description']}",
                "exit_price": current_price,
                "close_pct": 1.0,
                "close_ratio": 1.0,
                "description": description,
                "thesis_type": thesis_type,
            }
        except Exception as e:
            # 异常时返回 None，不阻断主退出链路
            logger.debug(
                f"[THESIS_FAILURE] {getattr(position, 'symbol', '?')} 检查异常 (返回 None): {e}"
            )
            return None

    # 兼容 BaseExitRule.should_exit 的标准接口
    def should_exit(
        self,
        position,
        market_data: Dict[str, Any],
        trail_state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return self.check(position, market_data, trail_state)


def check_thesis_failure(
    monitor,
    position,
    market_context: Dict[str, Any],
    trail_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """顶层函数式入口，便于以 lambda 形式注册到 ExitRuleRegistry。"""
    rule = ThesisFailureRule(monitor)
    return rule.check(position, market_context, trail_state)


__all__ = [
    "THESIS_FAILURE_MAP",
    "ThesisFailureRule",
    "check_thesis_failure",
]
