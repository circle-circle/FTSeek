"""
策略纯函数注册表 — Karpathy 风格

策略 = 纯函数 + YAML 配置。每个策略:
  - 签名: (SymbolContext, config: dict) -> Optional[SignalCandidate]
  - 零外部副作用, 不读数据库, 不发网络请求
  - 可独立单元测试, 可并行执行, 可 A/B 对比

与 app/strategy/ (BaseStrategy 类体系) 的关系:
  - app/strategy/ 是管理接口 (注册/绩效追踪/调优)
  - app/strategy/pure/ 是信号生成纯函数
  - 两者互补, 通过策略名桥接
"""

import logging
from typing import Optional, Dict, Any, Callable, List

from app.core.pipeline_contracts import SymbolContext, SignalCandidate

logger = logging.getLogger(__name__)

# 全局注册表: name -> func
REGISTRY: Dict[str, Callable] = {}


def register(name: str):
    """注册策略纯函数装饰器.

    用法:
        @register("breakout")
        def breakout(ctx, cfg) -> Optional[SignalCandidate]:
            ...
    """
    def decorator(func):
        REGISTRY[name] = func
        return func
    return decorator


def evaluate(name: str, ctx: SymbolContext, config: Optional[Dict[str, Any]] = None) -> Optional[SignalCandidate]:
    """
    执行指定策略纯函数 (单策略入口).

    Args:
        name: "fast_lane" / "technical" / "llm_vote" 等已注册策略名
        ctx: SymbolContext 增强行情快照
        config: 策略参数 (从 unified_config.yaml > strategies.<name> 加载)

    Returns:
        SignalCandidate 或 None
    """
    func = REGISTRY.get(name)
    if func is None:
        logger.warning(f"[纯策略] 未注册: {name} | 可用: {list(REGISTRY.keys())}")
        return None
    cfg = config or _load_yaml_config(name)
    if not cfg.get("enabled", True):
        return None
    try:
        sig = func(ctx, cfg)
        # R8 修复 (2026-05-17): 候选产出时自动填 trace_id 和 factor_detail
        # 不依赖下游临时构造, 让归因链路在策略源头闭环
        if sig is not None:
            _enrich_candidate(sig, ctx, name)
        return sig
    except Exception as e:
        logger.error(f"[纯策略] {name} 异常: {e}", exc_info=True)
        return None


def evaluate_all(ctx: SymbolContext,
                 config_loader: Optional[Callable[[str], Dict]] = None,
                 ) -> List[SignalCandidate]:
    """对全部已注册 + enabled 策略评估, 收集非 None 候选 (多策略入口).

    用法:
        from app.strategy.pure import evaluate_all
        candidates = evaluate_all(ctx)
        # candidates 交给 SignalArbitrator / PortfolioConstructor 仲裁

    与 evaluate(name, ctx) 区别:
        - evaluate 是"指定策略", 用于 stages/signal_stage 当前 first-hit 路径
        - evaluate_all 是"全部并行", 未来主流程切换到 Registry 模式时用

    失败的策略不阻断其他 — error 写日志, candidates 继续返回.
    """
    out: List[SignalCandidate] = []
    for name in list(REGISTRY.keys()):
        try:
            cfg = config_loader(name) if config_loader else _load_yaml_config(name)
            if not cfg.get("enabled", True):
                continue
            sig = REGISTRY[name](ctx, cfg)
            if sig is not None:
                _enrich_candidate(sig, ctx, name)
                out.append(sig)
        except Exception as e:
            logger.error(f"[纯策略·evaluate_all·{name}] 异常 (跳过): {e}")
    return out


def _enrich_candidate(sig: SignalCandidate, ctx: SymbolContext, name: str) -> None:
    """R8 修复 (2026-05-17): 候选自动 enrich — 填 trace_id / factor_detail / signal_tags.

    每个候选必带 trace_id (全链路追溯).
    factor_detail 从 ctx 透传 (假设 MarketData stage 已注入).
    signal_tags 从 SignalCandidate 现有字段或 strategy_id 推断.
    """
    # 1. trace_id (若策略未设)
    if not sig.trace_id:
        try:
            from datetime import datetime, timezone, timedelta
            import uuid
            et_date = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y%m%d")
            sig.trace_id = f"US-{et_date}-{uuid.uuid4().hex[:6].upper()}"
        except Exception:
            pass
    # 2. factor_detail 从 ctx 透传 (若策略未填)
    if not sig.factor_detail:
        # SymbolContext 可能在 metadata 里有 _factor_detail (IntradayScorer 等填的)
        ctx_meta = getattr(ctx, "metadata", None) or {}
        sig.factor_detail = ctx_meta.get("_factor_detail", {}) or {}
    # 3. signal_tags 兜底
    if not sig.signal_tags and sig.factor_detail:
        sig.signal_tags = list(sig.factor_detail.keys())
    if not sig.signal_tags:
        sig.signal_tags = [name]  # 至少有策略名作 tag


def _load_yaml_config(name: str) -> Dict[str, Any]:
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(f"strategies.{name}") or {}
    except Exception:
        pass
    return _BUILTIN_DEFAULTS.get(name, {"enabled": True})


# 延迟导入 — 触发 @register 装饰器, 填充 REGISTRY
from app.strategy.pure import fast_lane    # noqa: E402, F401
from app.strategy.pure import technical    # noqa: E402, F401
from app.strategy.pure import llm_vote     # noqa: E402, F401
from app.strategy.pure import trend_first  # noqa: E402, F401  # RFC-054
from app.strategy.pure import orb          # noqa: E402, F401  # ORB Opening Range Breakout


# 注册内置默认配置 (供 YAML 不可用时)
_BUILTIN_DEFAULTS = {
    "fast_lane": {
        "enabled": True, "min_confidence": 0.70, "require_entry_zone": True,
    },
    "technical": {
        "enabled": True, "min_signals": 2,
        "indicators": ["rsi", "macd", "bollinger", "supertrend"],
        "weights": {"rsi": 0.30, "macd": 0.25, "bollinger": 0.25, "supertrend": 0.20},
        "min_score": 10,
    },
    "llm_vote": {
        "enabled": True, "min_consensus": 0.30, "min_confidence": 0.50,
    },
    "trend_first": {
        "enabled": True, "observe_only": True, "min_sector_score": 40,
        "min_score": 55, "max_positions": 3,
    },
    "orb": {
        "enabled": True,
        "min_range_width_pct": 0.3,
        "max_range_width_pct": 2.5,
        "breakout_buffer_pct": 0.1,
        "max_chase_pct": 1.5,
        "stop_loss_atr_mult": 1.5,
        "min_risk_reward": 1.5,
        "min_volume_ratio": 0,
        "entry_end_et": "11:00",
        "no_entry_after_et": "15:30",
    },
}
