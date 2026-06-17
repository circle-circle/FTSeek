"""VIX 波动率检查 — 美股特有 (P1.3 RFC-060: 阈值配置化 + 诚实分级)

设计职责边界 (P1.3 澄清):
  - 真实仓位缩放: 由 RegimeAdapter 负责 (VIX>=25 → HIGH_VOL → 仓位×scale,
    见 app/strategy/regime_adapter.py + order_executor._get_order_qty)
  - 新仓硬阻 (主门禁): VIXGuard 在下单路径 order_executor.py (VIX>28 硬拒,
    含 Hysteresis 防抖), 由 feature_flags.vix_hard_blocking 控制
  - 本检查 (VIXCheck): USRiskPipeline 中的纵深防御层, 配置化阈值, 分级:
      VIX > extreme (默认40): 极端恐慌, 禁新仓 (允许平仓)
      VIX >= block   (默认28): 高波动, 禁新仓 (与 VIXGuard 一致, 纵深冗余)
      VIX >= caution (默认25): 警戒, 放行 (仓位缩放另由 RegimeAdapter)
      VIX < caution:          正常
  阈值读自 config: risk.market_regime_filter.vix_levels (SSOT)
  (历史: 本方法 VIX>28 处曾只 logger.warning 后 _pass, 即"假降仓"死告警,
   实际不降仓 — P1.3 修正为诚实分级)
"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any
import logging

logger = logging.getLogger("ftseek_us.risk.checks.vix")


def _load_vix_levels() -> Dict[str, float]:
    """从 unified_config.yaml 读取 VIX 分级阈值 (SSOT), 失败回退硬编码默认.

    读取 risk.market_regime_filter.vix_levels, 兼容新 (caution/block/extreme)
    与旧 (low/mid/high/critical) 字段.
    """
    defaults = {"normal": 20.0, "caution": 25.0, "block": 28.0, "extreme": 40.0}
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        levels = cfg.get_dotted("market_regime_filter.vix_levels", {}) or {}
        if isinstance(levels, dict) and levels:
            out = dict(defaults)
            # 新字段
            if "caution" in levels:
                out["caution"] = float(levels["caution"])
            if "block" in levels:
                out["block"] = float(levels["block"])
            if "extreme" in levels:
                out["extreme"] = float(levels["extreme"])
            # 旧字段兼容 (RFC-056: critical=28, high=25)
            if "block" not in levels and "critical" in levels:
                out["block"] = float(levels["critical"])
            if "caution" not in levels and "high" in levels:
                out["caution"] = float(levels["high"])
            if "normal" not in levels and "low" in levels:
                out["normal"] = float(levels["low"])
            return out
    except Exception as e:
        logger.debug("[VIX] 阈值配置加载失败, 用默认: %s", e)
    return defaults


class VIXCheck(RiskCheck):
    """VIX 波动率分级门禁 (P1.3 RFC-060)."""

    def __init__(self, vix_extreme: float = None, vix_high: float = None, vix_normal: float = None):
        levels = _load_vix_levels()
        # 兼容旧构造参数 (显式传入优先于配置)
        self.vix_extreme = float(vix_extreme) if vix_extreme is not None else levels["extreme"]
        self.vix_block = float(vix_high) if vix_high is not None else levels["block"]
        self.vix_caution = levels["caution"]
        self.vix_normal = levels["normal"]

    @property
    def name(self) -> str:
        return "VIX"

    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        vix = context.get("vix", 0) if context else 0
        if vix <= 0:
            return self._pass(signal, "VIX 数据不可用，跳过")

        symbol = getattr(signal, 'symbol', '')

        # 平仓订单始终放行 (紧急平仓不可阻断): 检测是否已有持仓
        position_store = context.get("position_store") if context else None
        is_close = bool(position_store and position_store.has(symbol))

        # VIX > extreme: 极端恐慌, 禁新仓 (允许平仓)
        if vix > self.vix_extreme:
            if is_close:
                return self._pass(signal, f"VIX={vix:.1f} 极端，但允许平仓 {symbol}")
            return self._reject(
                signal, f"VIX={vix:.1f} > {self.vix_extreme} 极端恐慌，禁止开新仓"
            )

        # P1.3 (RFC-060): VIX >= block (默认28): 高波动, 禁新仓
        # (原代码此处 logger.warning 后 _pass = "假降仓"死告警, 实际不降仓; 改为诚实硬阻,
        #  与下单路径 VIXGuard 形成纵深冗余)
        if vix >= self.vix_block:
            if is_close:
                return self._pass(
                    signal, f"VIX={vix:.1f} ≥ {self.vix_block} 高波动，允许平仓 {symbol}"
                )
            return self._reject(
                signal,
                f"VIX={vix:.1f} ≥ {self.vix_block} 高波动，禁止开新仓 "
                f"(仓位缩放另由 RegimeAdapter 在 VIX≥{self.vix_caution} 处理)"
            )

        # VIX >= caution (默认25): 警戒, 放行 (实际仓位缩放由 RegimeAdapter 负责)
        if vix >= self.vix_caution:
            logger.info(
                "[VIX] VIX=%.1f 警戒(≥%.1f): 放行, 仓位缩放由 RegimeAdapter 处理",
                vix, self.vix_caution
            )
            return self._pass(
                signal,
                f"VIX={vix:.1f} 警戒，仓位由 RegimeAdapter 缩放",
                vix_level="caution",
            )

        return self._pass(signal, f"VIX={vix:.1f} 正常")
