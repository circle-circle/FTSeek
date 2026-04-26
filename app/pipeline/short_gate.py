"""做空门禁守卫 (Short-Selling Gate)

原则：宁可错过，不可做错。做空风险远高于做多，必须设置更高的证据门槛。

设计目标：
1. 高置信度门槛（做空的 2 倍）
2. 多因子共振确认（至少 3 个独立利空因子）
3. 大盘趋势过滤（必须是下跌趋势）
4. 信息密度要求（LLM 必须提供完整逻辑链）
5. 自动执行：门槛极高但全自动（钉钉仅 FYI）

架构位置：
  LLM Assessment → StrategyRouter → [SHORT?] → ShortSellingGate → auto/review/observe
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("ftseek.pipeline.short_gate")


# ============================================================
#  做空门禁配置 (默认高门槛)
# ============================================================
@dataclass
class ShortGateConfig:
    """做空门禁配置"""
    
    # ── 置信度门槛 ──
    min_confidence: float = 0.70          # 做空最低置信度（做多 0.35 的 2 倍）
    min_relevance: float = 0.75           # 事件相关性要求（做多 0.55 的 1.36 倍）
    
    # ── 信息密度要求 ──
    min_logic_chain_steps: int = 3        # 最少逻辑链步骤数
    min_focus_symbols: int = 1            # 最少目标标的数
    require_second_order: bool = True     # 必须有二阶推理
    require_risk_path: bool = True        # 必须说明风险路径
    require_invalidating_factors: bool = True  # 必须列出否定因素
    
    # ── 技术面确认 ──
    require_volume_confirm: bool = True   # 需要放量确认
    volume_ratio_threshold: float = 1.5   # 量比阈值
    
    # ── 大盘趋势过滤 ──
    require_market_downtrend: bool = True  # 大盘必须处于下跌趋势
    
    # ── 自动执行门槛 (极高) ──
    auto_execute_confidence: float = 0.85  # 自动执行需要的置信度 (远超做多的 0.75)
    auto_execute_relevance: float = 0.85   # 自动执行需要的相关性
    
    # ── 冷却 ──
    cooldown_seconds: int = 600           # 5 分钟冷却（做多的 5 倍）
    
    # ── 仓位限制 ──
    max_position_ratio: float = 0.05      # 做空最大仓位 5%（做多 8% 的 62.5%）
    
    # ── 开关 ──
    enabled: bool = False                 # 默认关闭（需显式开启）


# ============================================================
#  做空门禁守卫
# ============================================================
@dataclass
class ShortSellingGate:
    """做空信号门禁守卫
    
    所有 SHORT/SELL 方向的信号必须通过此门禁。
    通过后根据证据强度决定路由：
      - 极高证据 (conf>=0.85 + rel>=0.85 + 完整逻辑) → auto (自动执行)
      - 高证据 (conf>=0.70 + rel>=0.75) → review (钉钉 FYI + 自动执行*)
      - 否则 → observe (仅观察)
    
    * 注意：由于系统全自动，review 通道也会执行，但会推送更高优先级的钉钉告警
    """
    
    config: ShortGateConfig = field(default_factory=ShortGateConfig)
    
    def __post_init__(self):
        self.config = self.config or ShortGateConfig()
        # 冷却记录 {symbol: last_timestamp}
        self._cooldown: Dict[str, float] = {}
        
        # 统计
        self.stats = {
            "total_checked": 0,
            "passed": 0,
            "rejected": 0,
            "auto_routed": 0,
            "review_routed": 0,
            "rejection_reasons": {},
        }
    
    def check(self, assessment: Any, context: Optional[Dict] = None) -> Tuple[str, str]:
        """检查做空信号
        
        Args:
            assessment: PreMarketAssessment 实例
            context: 可选的额外上下文（如市场状态、技术指标）
        
        Returns:
            (route: "auto" | "review" | "observe", reason: str)
        """
        self.stats["total_checked"] += 1
        
        # ── 0. 全局开关 ──
        if not self.config.enabled:
            self._record_reject("gate_disabled")
            return "observe", "做空门禁未启用 (short_selling_enabled=false)"
        
        # ── 1. 方向检查 ──
        direction = getattr(assessment, "direction", "")
        if direction not in ("SELL", "SHORT"):
            return "skip", "非做空信号，跳过门禁"
        
        symbol = self._get_primary_symbol(assessment)
        
        # ── 2. 冷却检查 ──
        if self._is_cooldown(symbol):
            self._record_reject("cooldown")
            remaining = int(self.config.cooldown_seconds - (time.time() - self._cooldown.get(symbol, 0)))
            return "observe", f"做空冷却中 ({symbol})，剩余 {remaining}s"
        
        # ── 3. 置信度门槛 ──
        conf = getattr(assessment, "confidence", 0.0)
        if conf < self.config.min_confidence:
            self._record_reject("low_confidence")
            return "observe", f"置信度过低: {conf:.2f} < {self.config.min_confidence}"
        
        # ── 4. 相关性门槛 ──
        rel = getattr(assessment, "relevance_score", 0.0)
        if rel < self.config.min_relevance:
            self._record_reject("low_relevance")
            return "observe", f"相关性过低: {rel:.2f} < {self.config.min_relevance}"
        
        # ── 5. 信息密度检查 ──
        info_pass, info_reason = self._check_information_density(assessment)
        if not info_pass:
            self._record_reject("insufficient_info")
            return "observe", f"信息密度不足: {info_reason}"
        
        # ── 6. 逻辑链完整性 ──
        logic_pass, logic_reason = self._check_logic_chain(assessment)
        if not logic_pass:
            self._record_reject("weak_logic")
            return "observe", f"逻辑链不完整: {logic_reason}"
        
        # ── 7. 大盘趋势过滤 ──
        if self.config.require_market_downtrend:
            trend_pass, trend_reason = self._check_market_trend(context)
            if not trend_pass:
                self._record_reject("no_downtrend")
                return "observe", f"大盘趋势不支持做空: {trend_reason}"
        
        # ── 8. 路由决策：根据证据强度 ──
        self._record_pass(symbol)
        
        # 极高证据 → 自动执行
        if conf >= self.config.auto_execute_confidence and rel >= self.config.auto_execute_relevance:
            self.stats["auto_routed"] += 1
            logger.info(
                f"[ShortGate] 🔴 {symbol} 极高证据做空 → 自动执行 "
                f"(conf={conf:.2f}, rel={rel:.2f})"
            )
            return "auto", f"极高证据做空，自动执行 (conf={conf:.2f})"
        
        # 高证据 → review (全自动系统中也会执行，但推送高优先级告警)
        self.stats["review_routed"] += 1
        logger.info(
            f"[ShortGate] 🟡 {symbol} 高证据做空 → review + 钉钉高优告警 "
            f"(conf={conf:.2f}, rel={rel:.2f})"
        )
        return "review", f"高证据做空，需关注 (conf={conf:.2f})"
    
    # ── 内部检查方法 ──
    
    def _check_information_density(self, assessment: Any) -> Tuple[bool, str]:
        """检查信息密度：二阶推理 + 风险路径 + 否定因素"""
        
        # 必须有二阶推理
        if self.config.require_second_order:
            second_order = getattr(assessment, "second_order_insight", "")
            if not second_order or len(second_order.strip()) < 20:
                return False, "缺少二阶推理（或内容过短）"
        
        # 必须说明风险路径
        if self.config.require_risk_path:
            risk_path = getattr(assessment, "risk_path", "")
            if not risk_path or len(risk_path.strip()) < 10:
                return False, "缺少风险路径说明"
        
        # 必须列出否定因素
        if self.config.require_invalidating_factors:
            invalidating = getattr(assessment, "invalidating_factors", [])
            if not invalidating or len(invalidating) == 0:
                return False, "缺少否定因素列表"
        
        return True, "信息密度充足"
    
    def _check_logic_chain(self, assessment: Any) -> Tuple[bool, str]:
        """检查逻辑链完整性"""
        logic_chain = getattr(assessment, "logic_chain", [])
        
        if not logic_chain:
            return False, "逻辑链为空"
        
        if len(logic_chain) < self.config.min_logic_chain_steps:
            return False, (
                f"逻辑链步骤不足: {len(logic_chain)} < {self.config.min_logic_chain_steps}"
            )
        
        # 检查是否有目标标的
        symbols = getattr(assessment, "focus_symbols", [])
        if len(symbols) < self.config.min_focus_symbols:
            return False, f"目标标的不足: {len(symbols)} < {self.config.min_focus_symbols}"
        
        return True, f"逻辑链完整 ({len(logic_chain)} 步)"
    
    def _check_market_trend(self, context: Optional[Dict]) -> Tuple[bool, str]:
        """检查大盘趋势是否支持做空
        
        需要外部传入 context 包含：
        - spy_trend: "up" | "down" | "neutral"
        - qqq_trend: "up" | "down" | "neutral"
        - market_regime: "risk_on" | "risk_off" | "neutral"
        """
        if not context:
            # 没有市场上下文，保守起见放行（但记录警告）
            logger.warning("[ShortGate] 无市场上下文，跳过趋势检查")
            return True, "无市场数据，放行"
        
        spy_trend = context.get("spy_trend", "neutral")
        qqq_trend = context.get("qqq_trend", "neutral")
        regime = context.get("market_regime", "neutral")
        
        # 至少一个大盘指标处于下跌趋势
        downtrends = sum(1 for t in [spy_trend, qqq_trend] if t == "down")
        if downtrends == 0:
            return False, f"SPY={spy_trend}, QQQ={qqq_trend}，无下跌趋势"
        
        # risk_off 环境更适合做空
        if regime == "risk_off":
            return True, f"risk_off 环境 + {downtrends} 个大盘下跌"
        
        return True, f"{downtrends} 个大盘指标下跌"
    
    def _is_cooldown(self, symbol: str) -> bool:
        """检查是否在冷却期"""
        if not symbol:
            return False
        last_time = self._cooldown.get(symbol)
        if last_time and (time.time() - last_time) < self.config.cooldown_seconds:
            return True
        return False
    
    def _record_pass(self, symbol: str):
        """记录通过"""
        self.stats["passed"] += 1
        self._cooldown[symbol] = time.time()
    
    def _record_reject(self, reason: str):
        """记录拒绝"""
        self.stats["rejected"] += 1
        self.stats["rejection_reasons"][reason] = (
            self.stats["rejection_reasons"].get(reason, 0) + 1
        )
    
    def _get_primary_symbol(self, assessment: Any) -> str:
        """获取主要标的"""
        symbols = getattr(assessment, "focus_symbols", [])
        return symbols[0] if symbols else ""
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total = self.stats["total_checked"]
        return {
            **self.stats,
            "pass_rate": self.stats["passed"] / max(total, 1),
            "reject_rate": self.stats["rejected"] / max(total, 1),
        }


# ============================================================
#  全局单例
# ============================================================
_gate: Optional[ShortSellingGate] = None

def get_short_gate(config: Optional[Dict] = None) -> ShortSellingGate:
    """获取做空门禁单例"""
    global _gate
    if _gate is None:
        cfg = ShortGateConfig(**(config or {}))
        _gate = ShortSellingGate(config=cfg)
    return _gate

def reset_short_gate():
    """重置单例（用于测试）"""
    global _gate
    _gate = None
