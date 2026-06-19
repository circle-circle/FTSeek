"""
MinConfidenceResolver — min_confidence 单一真相源 (F4 2026-05-14)

旧问题:
- yaml 中 14 处 `min_confidence`, 代码各凭意愿读不同节点
- ConfigWriter 调参只改 risk.signal_quality.min_confidence, 其他 13 处不动
- 真正生效的门槛是哪一个? 无人能说清

新设计:
- 所有调用方走 `get_min_confidence_resolver().for_phase(phase)`
- 内部按优先级查询: lesson_injector 动态约束 > phase-specific yaml > 全局 SSOT > 兜底常量
- yaml 中 phase-specific 节点保留 (允许细分场景), 但调用必须走 resolver
- ConfigWriter 仍然只调全局 SSOT (`risk.signal_quality.min_confidence`)

Phase 列表 (语义):
- "intraday"     盘中信号 (主路径) — strategies.defaults.min_confidence
- "premarket"    盘前 LLM 投票门槛 — strategies.voting_llm.min_confidence
- "fast_lane"    快车道 (高质量短线) — strategies.fast_lane.min_confidence
- "shadow"       影子账户 (放宽门槛验证假设)
- "ssot"         全局 SSOT (ConfigWriter 唯一可调) — risk.signal_quality.min_confidence
- "default"      未分类 → 等同 "ssot"
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Phase → yaml 节点映射 (DSL)
_PHASE_YAML_MAP = {
    "intraday":   "strategies.defaults.min_confidence",
    "premarket":  "strategies.voting_llm.min_confidence",
    "fast_lane":  "strategies.fast_lane.min_confidence",
    "shadow":     "strategies.defaults.min_confidence",
}

_SSOT_PATH = "risk.signal_quality.min_confidence"
_FALLBACK = 0.5


class MinConfidenceResolver:
    """min_confidence 解析器 (无状态, 每次现查 — 让 ConfigWriter 调参立即生效)"""

    def __init__(self, lesson_injector=None):
        self._lesson_injector = lesson_injector

    def set_lesson_injector(self, lesson_injector):
        """运行时挂载 lesson_injector (避免循环依赖)"""
        self._lesson_injector = lesson_injector

    def for_phase(self, phase: str = "default", symbol: Optional[str] = None) -> float:
        """
        返回当前 phase 的有效 min_confidence

        优先级:
          1. lesson_injector 动态约束 (按 symbol)
          2. phase-specific yaml 节点
          3. 全局 SSOT (risk.signal_quality.min_confidence)
          4. _FALLBACK (0.5)
        """
        try:
            from app.core.config_container import get_config
            cfg = get_config()
        except Exception:
            cfg = None

        # Step 1: lesson_injector 动态约束 (extra_confidence 会被叠加)
        extra = 0.0
        try:
            if self._lesson_injector is not None and symbol:
                constraints = self._lesson_injector.generate_trade_constraints(
                    symbol=symbol, session="open"
                )
                extra = float(constraints.get("extra_confidence", 0) or 0.0)
        except Exception as e:
            logger.debug(f"[MinConfResolver] lesson_injector 查询失败: {e}")

        # Step 2: phase-specific
        base = None
        if cfg is not None and phase != "ssot":
            yaml_path = _PHASE_YAML_MAP.get(phase)
            if yaml_path:
                base = cfg.get_dotted(yaml_path, None)
                if base is not None:
                    try:
                        base = float(base)
                    except (TypeError, ValueError):
                        base = None

        # Step 3: SSOT (ConfigWriter 唯一调的节点)
        if base is None and cfg is not None:
            base = cfg.get_dotted(_SSOT_PATH, None)
            if base is not None:
                try:
                    base = float(base)
                except (TypeError, ValueError):
                    base = None

        # Step 4: 兜底
        if base is None:
            base = _FALLBACK

        result = min(1.0, base + extra)
        return result

    def ssot(self) -> float:
        """返回全局 SSOT 值 (用于 audit/dashboard)"""
        return self.for_phase("ssot")


_resolver: Optional[MinConfidenceResolver] = None


def get_min_confidence_resolver() -> MinConfidenceResolver:
    """全局单例 — 各调用方共享同一 resolver, lesson_injector 只需挂一次"""
    global _resolver
    if _resolver is None:
        _resolver = MinConfidenceResolver()
    return _resolver
