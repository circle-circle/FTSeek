"""
FTSeek-US 市场配置文件

由于 FTSeek-US 仅处理美股，这是 MarketProfile 的简化版本，
直接提供美股预设配置，同时保持与港股版本的接口一致性。
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek.market_profile")


@dataclass
class SignalFusionWeights:
    """信号融合权重配置"""
    technical_weight: float = 0.5
    sentiment_weight: float = 0.2
    macro_weight: float = 0.3
    policy_weight: float = 0.0
    veto_policy_negative: bool = False


@dataclass
class MarketProfile:
    """美股市场配置 (FTSeek-US 专用)"""

    type: str = "US"
    name: str = "美股"
    timezone: str = "America/New_York"
    day_trading: bool = True
    pdt_rules: bool = True
    pdt_threshold: float = 25000
    max_day_trades: int = 3
    lot_size_rules: bool = False
    min_lot_size: int = 1
    stamp_duty: float = 0.0
    commission_rate: float = 0.0
    auction_strategy: bool = False
    signal_fusion: SignalFusionWeights = field(default_factory=SignalFusionWeights)
    llm_prompt_suffix: str = (
        "你是美股策略专家。美股是高度机构化、数据驱动的市场。\n"
        "分析时必须考虑：\n"
        "1. 技术面权重高于政策面，VIX 和宏观数据是核心因子\n"
        "2. 遵守 FINRA PDT 规则：资金 < $25k 限 3 次日内交易/5 日\n"
        "3. 美股最小交易单位 1 股，无需整手对齐\n"
        "4. 关注美联储政策、CPI、非农等宏观数据\n"
        "5. 注意期权市场和做空机制的影响\n"
        "6. 盘前 (04:00-09:30 ET) 和盘后 (16:00-20:00 ET) 流动性较差"
    )

    @classmethod
    def get(cls, config_overrides: Optional[Dict] = None) -> 'MarketProfile':
        """获取美股配置"""
        profile = cls()
        if config_overrides:
            profile._apply_overrides(config_overrides)
        logger.info(
            f"[MarketProfile] {profile.name} 配置已加载 | "
            f"技术权重={profile.signal_fusion.technical_weight}"
        )
        return profile

    def _apply_overrides(self, overrides: Dict):
        if "signal_fusion" in overrides:
            sf = overrides["signal_fusion"]
            self.signal_fusion = SignalFusionWeights(
                technical_weight=sf.get("technical_weight", self.signal_fusion.technical_weight),
                sentiment_weight=sf.get("sentiment_weight", self.signal_fusion.sentiment_weight),
                macro_weight=sf.get("macro_weight", self.signal_fusion.macro_weight),
                policy_weight=sf.get("policy_weight", self.signal_fusion.policy_weight),
                veto_policy_negative=sf.get("veto_policy_negative", self.signal_fusion.veto_policy_negative),
            )
        if "llm_prompt_suffix" in overrides:
            self.llm_prompt_suffix = overrides["llm_prompt_suffix"]

    def get_fusion_weight(self, factor: str) -> float:
        return getattr(self.signal_fusion, f"{factor}_weight", 0.0)

    def should_veto(self, policy_risk: float, threshold: float = 0.8) -> bool:
        return False  # 美股无政策否决

    def get_dynamic_weights(self, vix: float = 20.0) -> SignalFusionWeights:
        """根据 VIX 动态调整权重"""
        w = SignalFusionWeights(
            technical_weight=self.signal_fusion.technical_weight,
            sentiment_weight=self.signal_fusion.sentiment_weight,
            macro_weight=self.signal_fusion.macro_weight,
            policy_weight=self.signal_fusion.policy_weight,
            veto_policy_negative=self.signal_fusion.veto_policy_negative,
        )

        if vix < 15:
            w.technical_weight = min(0.7, w.technical_weight + 0.15)
            w.macro_weight = max(0.1, w.macro_weight - 0.1)
            w.sentiment_weight = max(0.1, w.sentiment_weight - 0.05)
        elif vix > 30:
            w.technical_weight = max(0.2, w.technical_weight - 0.15)
            w.macro_weight = min(0.5, w.macro_weight + 0.1)
            w.sentiment_weight = min(0.4, w.sentiment_weight + 0.05)
        elif vix > 25:
            w.technical_weight = max(0.3, w.technical_weight - 0.05)
            w.macro_weight = min(0.4, w.macro_weight + 0.05)

        # 归一化
        total = w.technical_weight + w.sentiment_weight + w.macro_weight
        if total > 0:
            scale = (1.0 - w.policy_weight) / total
            w.technical_weight *= scale
            w.sentiment_weight *= scale
            w.macro_weight *= scale

        logger.debug(
            f"[MarketProfile-US] 动态权重 | VIX={vix:.1f} | "
            f"技术={w.technical_weight:.2f} 情绪={w.sentiment_weight:.2f} "
            f"宏观={w.macro_weight:.2f}"
        )
        return w


_cache: Optional[MarketProfile] = None

def get_market_profile(config_overrides: Optional[Dict] = None) -> MarketProfile:
    """获取美股配置 (带缓存)"""
    global _cache
    if _cache is None:
        _cache = MarketProfile.get(config_overrides)
    return _cache
