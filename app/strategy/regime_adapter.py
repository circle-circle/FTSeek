"""
Regime Adapter — 市场状态自适应策略切换 (Task #13)

职责:
  1. 维护 regime → strategy_group 权重矩阵 (signal scoring 阶段乘以系数)
  2. HIGH_VOL regime 自动仓位缩减 (默认 50%)
  3. regime 变化事件处理 (持仓审查 + 告警)
  4. 与 PositionSizer.VOLATILITY_ADJUSTED 协同 (regime scale × ATR scale)

设计原则:
  - 不修改 regime detection 本身, 仅消费其输出
  - 不改变策略信号生成逻辑, 仅在评分阶段乘权重
  - 配置驱动: 权重矩阵 / HIGH_VOL 缩减比例从 unified_config.yaml 读取
  - 单例模式: 全局唯一实例, 避免重复实例化

作者: FTSeek Team
日期: 2026-05-27 (Task #13)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


# ============================================================
#  Regime 标准化 — 兼容多种枚举来源
# ============================================================
# app/core/models.MarketRegime: TREND_UP / TREND_DOWN / RANGE / HIGH_VOL / LOW_VOL
# app/risk/market_regime_detector: TRENDING_UP / TRENDING_DOWN / RANGING /
#                                  HIGH_VOL_CHOP / EXTREME / TRANSITION / UNKNOWN
# 统一为 4 个核心 regime: TREND_UP / TREND_DOWN / RANGE / HIGH_VOL

REGIME_ALIASES: Dict[str, str] = {
    # core models
    "TREND_UP": "TREND_UP",
    "TREND_DOWN": "TREND_DOWN",
    "RANGE": "RANGE",
    "HIGH_VOL": "HIGH_VOL",
    "LOW_VOL": "RANGE",  # 低波归入 RANGE 处理
    # market_regime_detector
    "TRENDING_UP": "TREND_UP",
    "TRENDING_DOWN": "TREND_DOWN",
    "TRENDING": "TREND_UP",
    "RANGING": "RANGE",
    "HIGH_VOL_CHOP": "HIGH_VOL",
    "EXTREME": "HIGH_VOL",
    "TRANSITION": "RANGE",
    "UNKNOWN": "RANGE",
}


def normalize_regime(regime: Any) -> str:
    """将不同来源的 regime 标识归一化到核心 4 类."""
    if regime is None:
        return "RANGE"
    val = regime.value if hasattr(regime, "value") else str(regime)
    return REGIME_ALIASES.get(val.upper(), "RANGE")


# ============================================================
#  默认配置 (YAML 不可用时使用)
# ============================================================
# 权重矩阵: regime → {strategy_group: weight (0~1)}
# 1.0 = 完全信任, 0.3 = 强烈降权但保留, 0 = 不参与
DEFAULT_REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    "TREND_UP":   {"trend_following": 1.0, "mean_reversion": 0.3,
                   "sentiment": 0.7, "other": 0.6},
    "TREND_DOWN": {"trend_following": 1.0, "mean_reversion": 0.3,
                   "sentiment": 0.7, "other": 0.6},
    "RANGE":      {"trend_following": 0.3, "mean_reversion": 1.0,
                   "sentiment": 0.5, "other": 0.6},
    "HIGH_VOL":   {"trend_following": 0.5, "mean_reversion": 0.2,
                   "sentiment": 0.4, "other": 0.3},
}

# strategy_group → 默认支持的 regime 列表 (供 supports_regime 兜底)
DEFAULT_GROUP_REGIME_SUPPORT: Dict[str, List[str]] = {
    "trend": ["TREND_UP", "TREND_DOWN"],
    "trend_following": ["TREND_UP", "TREND_DOWN"],
    "reversion": ["RANGE"],
    "mean_reversion": ["RANGE"],
    "sentiment": ["TREND_UP", "TREND_DOWN", "RANGE"],
    "other": ["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL"],
}

# strategy_id 关键词 → 规范化 strategy_group (用于权重矩阵 key)
GROUP_NAME_NORMALIZE: Dict[str, str] = {
    "trend": "trend_following",
    "trend_following": "trend_following",
    "reversion": "mean_reversion",
    "mean_reversion": "mean_reversion",
    "sentiment": "sentiment",
    "news": "sentiment",
    "llm": "sentiment",
    "other": "other",
}

# HIGH_VOL 仓位缩减比例 (默认 50%)
DEFAULT_HIGH_VOL_POSITION_SCALE: float = 0.5

# 各 regime 的整体仓位缩放 (与 weight 矩阵正交, 应用于最终 sizing)
DEFAULT_REGIME_POSITION_SCALE: Dict[str, float] = {
    "TREND_UP": 1.0,
    "TREND_DOWN": 0.8,    # 做空成本更高 + 反弹风险
    "RANGE": 0.7,
    "HIGH_VOL": 0.5,      # 高波动强制缩减
}


# ============================================================
#  数据结构
# ============================================================
@dataclass
class RegimeAdaptationResult:
    """Regime 自适应输出 (供 scorer / sizer 消费)."""
    regime: str
    weight: float                  # signal scoring 权重系数
    position_scale: float          # 仓位缩放系数
    strategy_group: str
    supported: bool                # 该策略是否适合当前 regime
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "weight": round(self.weight, 3),
            "position_scale": round(self.position_scale, 3),
            "strategy_group": self.strategy_group,
            "supported": self.supported,
            "notes": self.notes,
        }


# ============================================================
#  RegimeAdapter — 单例
# ============================================================
class RegimeAdapter:
    """市场状态自适应适配器 (单例)."""

    _instance: Optional["RegimeAdapter"] = None

    def __new__(cls) -> "RegimeAdapter":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._weights = dict(DEFAULT_REGIME_WEIGHTS)
        self._position_scales = dict(DEFAULT_REGIME_POSITION_SCALE)
        self._high_vol_scale = DEFAULT_HIGH_VOL_POSITION_SCALE
        self._enabled = True
        self._last_regime: Optional[str] = None
        self._initialized = True
        self._load_config()

    # ----------------------------------------------------------
    #  配置加载
    # ----------------------------------------------------------
    def _load_config(self) -> None:
        """从 unified_config.yaml > regime_adaptation 加载配置."""
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            section = cfg.get_dotted("regime_adaptation", {}) or {}
        except Exception as e:
            logger.debug(f"[RegimeAdapter] 配置加载失败 (使用默认): {e}")
            return

        self._enabled = bool(section.get("enabled", True))
        if "weights" in section and isinstance(section["weights"], dict):
            # 合并: 用户配置覆盖默认, 缺失 group 走默认
            for regime, group_weights in section["weights"].items():
                regime_norm = normalize_regime(regime)
                if regime_norm not in self._weights:
                    self._weights[regime_norm] = {}
                if isinstance(group_weights, dict):
                    self._weights[regime_norm].update(group_weights)

        if "position_scales" in section and isinstance(section["position_scales"], dict):
            for regime, scale in section["position_scales"].items():
                self._position_scales[normalize_regime(regime)] = float(scale)

        self._high_vol_scale = float(
            section.get("high_vol_position_scale", DEFAULT_HIGH_VOL_POSITION_SCALE)
        )
        logger.info(
            f"[RegimeAdapter] 配置加载完成 | enabled={self._enabled} | "
            f"high_vol_scale={self._high_vol_scale} | "
            f"regimes={list(self._weights.keys())}"
        )

    def reload(self) -> None:
        """运行时热重载配置."""
        self._weights = dict(DEFAULT_REGIME_WEIGHTS)
        self._position_scales = dict(DEFAULT_REGIME_POSITION_SCALE)
        self._high_vol_scale = DEFAULT_HIGH_VOL_POSITION_SCALE
        self._load_config()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ----------------------------------------------------------
    #  权重查询
    # ----------------------------------------------------------
    def get_strategy_weight(self, strategy_group: str, regime: Any) -> float:
        """获取 (regime, strategy_group) 对应的权重系数 (0~1)."""
        if not self._enabled:
            return 1.0
        regime_norm = normalize_regime(regime)
        group_norm = GROUP_NAME_NORMALIZE.get(
            (strategy_group or "other").lower(), "other"
        )
        regime_weights = self._weights.get(regime_norm, {})
        # 权重矩阵未列出的 group 默认为 0.6 (保守降权)
        return float(regime_weights.get(group_norm, 0.6))

    def get_position_scale(self, regime: Any) -> float:
        """获取 regime 对应的仓位缩放系数."""
        if not self._enabled:
            return 1.0
        regime_norm = normalize_regime(regime)
        # HIGH_VOL 走专用配置 (覆盖 position_scales 表)
        if regime_norm == "HIGH_VOL":
            return self._high_vol_scale
        return float(self._position_scales.get(regime_norm, 1.0))

    def get_high_vol_scale(self) -> float:
        return self._high_vol_scale

    # ----------------------------------------------------------
    #  适配评估 (scorer 入口)
    # ----------------------------------------------------------
    def adapt(
        self,
        strategy_group: str,
        regime: Any,
        supported_regimes: Optional[List[str]] = None,
    ) -> RegimeAdaptationResult:
        """
        综合评估单个策略在当前 regime 下的适配结果.

        Args:
            strategy_group: 策略族名 (trend/reversion/sentiment/other 或别名)
            regime: 当前市场 regime (任意来源, 自动归一化)
            supported_regimes: 策略声明支持的 regime 列表 (来自 supports_regime)

        Returns:
            RegimeAdaptationResult
        """
        regime_norm = normalize_regime(regime)
        group_norm = GROUP_NAME_NORMALIZE.get(
            (strategy_group or "other").lower(), "other"
        )
        weight = self.get_strategy_weight(group_norm, regime_norm)
        pos_scale = self.get_position_scale(regime_norm)

        # 策略 supports 检查
        if supported_regimes is None:
            supported_regimes = DEFAULT_GROUP_REGIME_SUPPORT.get(
                group_norm, ["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL"]
            )
        supported = (regime_norm in supported_regimes) or ("ALL" in supported_regimes)

        notes: List[str] = []
        if not supported:
            # 不支持当前 regime: 权重再砍半
            weight *= 0.5
            notes.append(f"strategy_group={group_norm} 不支持 regime={regime_norm}, 权重×0.5")
        if regime_norm == "HIGH_VOL":
            notes.append(f"HIGH_VOL: position_scale={pos_scale:.2f}")

        return RegimeAdaptationResult(
            regime=regime_norm,
            weight=weight,
            position_scale=pos_scale,
            strategy_group=group_norm,
            supported=supported,
            notes=notes,
        )

    def apply_score(
        self,
        base_score: float,
        strategy_group: str,
        regime: Any,
        supported_regimes: Optional[List[str]] = None,
    ) -> Tuple[float, RegimeAdaptationResult]:
        """对原始信号评分应用 regime 权重, 返回 (调整后分数, 适配结果)."""
        result = self.adapt(strategy_group, regime, supported_regimes)
        adjusted = base_score * result.weight
        return adjusted, result

    # ----------------------------------------------------------
    #  Regime 变化事件处理
    # ----------------------------------------------------------
    def on_regime_change(
        self,
        old_regime: Any,
        new_regime: Any,
        event_store: Optional[Any] = None,
        open_positions: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """
        Regime 变化事件处理:
          1. 记录 TradeEvent (event_type=regime_changed_review)
          2. 输出告警 "Regime changed: old → new, reviewing N open positions"
          3. 标记不再适合新 regime 的持仓 (供上层做 review)

        Args:
            old_regime / new_regime: 任意来源的 regime 标识
            event_store: 可选的 EventStore 实例
            open_positions: 可选的当前持仓列表 (每项需有 strategy_id 或 strategy_group)

        Returns:
            review summary dict
        """
        old_norm = normalize_regime(old_regime)
        new_norm = normalize_regime(new_regime)

        # 防抖: 归一化后相同 regime 不告警
        if old_norm == new_norm:
            return {"changed": False, "regime": new_norm, "n_positions": 0,
                    "to_review": []}

        positions = open_positions or []
        n_positions = len(positions)
        to_review: List[Dict[str, Any]] = []

        for pos in positions:
            group = self._extract_group(pos)
            supported_regimes = self._extract_supported_regimes(pos)
            result = self.adapt(group, new_norm, supported_regimes)
            if (not result.supported) or result.weight < 0.5:
                to_review.append({
                    "symbol": getattr(pos, "symbol", None) or
                              (pos.get("symbol") if isinstance(pos, dict) else None),
                    "strategy_group": result.strategy_group,
                    "weight_under_new": result.weight,
                    "supported": result.supported,
                })

        alert_msg = (
            f"Regime changed: {old_norm} → {new_norm}, "
            f"reviewing {n_positions} open positions "
            f"({len(to_review)} flagged for review)"
        )
        logger.warning(f"[RegimeAdapter] {alert_msg}")

        if new_norm == "HIGH_VOL":
            logger.warning(
                f"[RegimeAdapter] HIGH_VOL regime detected, "
                f"position size reduced to {int(self._high_vol_scale * 100)}%"
            )

        # 写入事件流
        if event_store is not None:
            try:
                from app.core.models import TradeEvent, AlertLevel
                from app.utils.timezone import now_utc
                event_store.append(TradeEvent(
                    event_type="regime_changed_review",
                    data={
                        "old_regime": old_norm,
                        "new_regime": new_norm,
                        "open_positions": n_positions,
                        "flagged": to_review,
                        "high_vol_position_scale":
                            self._high_vol_scale if new_norm == "HIGH_VOL" else None,
                        "message": alert_msg,
                        "ts": now_utc().isoformat(),
                    },
                    source="strategy.regime_adapter",
                    severity=(AlertLevel.WARNING if to_review or new_norm == "HIGH_VOL"
                              else AlertLevel.INFO),
                ))
            except Exception as e:
                logger.debug(f"[RegimeAdapter] 写事件失败 (忽略): {e}")

        self._last_regime = new_norm
        return {
            "changed": True,
            "old_regime": old_norm,
            "new_regime": new_norm,
            "n_positions": n_positions,
            "to_review": to_review,
            "alert": alert_msg,
        }

    @staticmethod
    def _extract_group(pos: Any) -> str:
        """从持仓对象提取 strategy_group (兼容 dict / object / Position)."""
        if pos is None:
            return "other"
        if isinstance(pos, dict):
            return (
                pos.get("strategy_group")
                or pos.get("strategy_type")
                or pos.get("strategy_id")
                or "other"
            )
        return (
            getattr(pos, "strategy_group", None)
            or getattr(pos, "strategy_type", None)
            or getattr(pos, "strategy_id", None)
            or "other"
        )

    @staticmethod
    def _extract_supported_regimes(pos: Any) -> Optional[List[str]]:
        if pos is None:
            return None
        if isinstance(pos, dict):
            v = pos.get("supported_regimes")
            return list(v) if v else None
        v = getattr(pos, "supported_regimes", None)
        return list(v) if v else None


# ============================================================
#  全局便捷函数
# ============================================================
def get_regime_adapter() -> RegimeAdapter:
    """获取全局 RegimeAdapter 单例."""
    return RegimeAdapter()


def apply_regime_position_scale(
    base_shares: int,
    regime: Any,
    high_vol_floor: int = 1,
) -> Tuple[int, float, str]:
    """
    对计算好的仓位股数应用 regime 缩放.

    Args:
        base_shares: 原始计算股数
        regime: 当前 regime (任意来源)
        high_vol_floor: HIGH_VOL 后最小保留股数 (避免 0 化)

    Returns:
        (adjusted_shares, scale, note)
    """
    adapter = get_regime_adapter()
    if not adapter.enabled or base_shares <= 0:
        return base_shares, 1.0, ""

    regime_norm = normalize_regime(regime)
    scale = adapter.get_position_scale(regime_norm)
    if scale >= 1.0:
        return base_shares, scale, ""

    adjusted = max(high_vol_floor, int(base_shares * scale))
    note = f"regime={regime_norm} scale={scale:.2f} ({base_shares}→{adjusted})"
    if regime_norm == "HIGH_VOL":
        logger.info(
            f"[RegimeAdapter] HIGH_VOL regime detected, "
            f"position size reduced to {int(scale * 100)}% "
            f"({base_shares} → {adjusted} shares)"
        )
    return adjusted, scale, note
