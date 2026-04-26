"""
市场配置文件 (Market Profile)

统一封装港股/美股的差异化特征：
- 交易规则 (T+0/T+1, 整手, PDT)
- 信号融合权重
- LLM Prompt 指令
- 风控参数

使用:
    profile = MarketProfile.get("HK")
    profile = MarketProfile.get("US")
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, ClassVar
from dataclasses import InitVar

logger = logging.getLogger("ftseek.market_profile")


@dataclass
class SignalFusionWeights:
    """信号融合权重配置"""
    technical_weight: float = 0.5
    sentiment_weight: float = 0.3
    macro_weight: float = 0.2
    policy_weight: float = 0.0
    veto_policy_negative: bool = False  # 政策负面是否一票否决


@dataclass
class MarketProfile:
    """市场配置文件"""
    
    # 市场预设配置 (类变量，非 dataclass field)
    _MARKET_CONFIGS: ClassVar[Dict[str, Dict[str, Any]]] = {
        "HK": {
            "name": "港股",
            "timezone": "Asia/Hong_Kong",
            "trading_hours": "09:30-12:00, 13:00-16:00",
            "settlement": "T+2",
            "day_trading": True,       # T+0 回转
            "pdt_rules": False,        # 无 PDT 限制
            "lot_size_rules": True,    # 每手股数不同
            "min_lot_size": 100,       # 默认每手
            "stamp_duty": 0.0013,      # 印花税 0.13%
            "commission_rate": 0.0003, # 佣金
            "auction_strategy": True,  # 竞价策略
            "signal_fusion": SignalFusionWeights(
                technical_weight=0.3,
                sentiment_weight=0.2,
                macro_weight=0.1,
                policy_weight=0.4,
                veto_policy_negative=True,
            ),
            "llm_prompt_suffix": (
                "你是港股策略专家。港股受政策面影响极大（南向资金、大陆政策、汇率变动）。\n"
                "分析时必须考虑：\n"
                "1. 政策敏感度极高，政策负面信号应一票否决\n"
                "2. 流动性分层明显，小盘股流动性风险高于美股\n"
                "3. T+0 回转交易，日内反手是常见策略\n"
                "4. 注意每手股数差异，避免碎股下单失败\n"
                "5. 关注恒生指数期货和 A 股联动效应"
            ),
        },
        "US": {
            "name": "美股",
            "timezone": "America/New_York",
            "trading_hours": "09:30-16:00",
            "settlement": "T+1",
            "day_trading": True,
            "pdt_rules": True,         # FINRA PDT 规则
            "pdt_threshold": 25000,    # $25k 豁免门槛
            "max_day_trades": 3,       # 5 交易日最多 3 次
            "lot_size_rules": False,   # 最小 1 股
            "min_lot_size": 1,
            "stamp_duty": 0.0,
            "commission_rate": 0.0,    # 大部分券商零佣金
            "auction_strategy": False,
            "signal_fusion": SignalFusionWeights(
                technical_weight=0.5,
                sentiment_weight=0.2,
                macro_weight=0.3,
                policy_weight=0.0,
                veto_policy_negative=False,
            ),
            "llm_prompt_suffix": (
                "你是美股策略专家。美股是高度机构化、数据驱动的市场。\n"
                "分析时必须考虑：\n"
                "1. 技术面权重高于政策面，VIX 和宏观数据是核心因子\n"
                "2. 遵守 FINRA PDT 规则：资金 < $25k 限 3 次日内交易/5 日\n"
                "3. 美股最小交易单位 1 股，无需整手对齐\n"
                "4. 关注美联储政策、CPI、非农等宏观数据\n"
                "5. 注意期权市场和做空机制的影响\n"
                "6. 盘前 (04:00-09:30 ET) 和盘后 (16:00-20:00 ET) 流动性较差"
            ),
        },
    }

    type: str = "HK"
    name: str = "港股"
    timezone: str = "Asia/Hong_Kong"
    day_trading: bool = True
    pdt_rules: bool = False
    pdt_threshold: float = 25000
    max_day_trades: int = 3
    lot_size_rules: bool = True
    min_lot_size: int = 100
    stamp_duty: float = 0.0013
    commission_rate: float = 0.0003
    auction_strategy: bool = True
    signal_fusion: SignalFusionWeights = field(default_factory=SignalFusionWeights)
    llm_prompt_suffix: str = ""

    @classmethod
    def get(cls, market_type: str, config_overrides: Optional[Dict] = None) -> 'MarketProfile':
        """
        获取市场配置文件

        Args:
            market_type: 'HK' 或 'US'
            config_overrides: 可选的覆盖配置 (从 YAML 加载)

        Returns:
            MarketProfile 实例
        """
        cfg = cls._MARKET_CONFIGS.get(market_type.upper())
        if cfg is None:
            logger.warning(f"[MarketProfile] 未知市场类型 '{market_type}', 默认使用 HK 配置")
            cfg = cls._MARKET_CONFIGS["HK"]

        profile = cls(
            type=market_type.upper(),
            name=cfg["name"],
            timezone=cfg["timezone"],
            day_trading=cfg["day_trading"],
            pdt_rules=cfg["pdt_rules"],
            pdt_threshold=cfg.get("pdt_threshold", 25000),
            max_day_trades=cfg.get("max_day_trades", 3),
            lot_size_rules=cfg["lot_size_rules"],
            min_lot_size=cfg["min_lot_size"],
            stamp_duty=cfg["stamp_duty"],
            commission_rate=cfg["commission_rate"],
            auction_strategy=cfg["auction_strategy"],
            signal_fusion=cfg["signal_fusion"],
            llm_prompt_suffix=cfg["llm_prompt_suffix"],
        )

        # 应用 YAML 覆盖配置
        if config_overrides:
            profile._apply_overrides(config_overrides)

        logger.info(
            f"[MarketProfile] {profile.name} 配置已加载 | "
            f"PDT={'是' if profile.pdt_rules else '否'} | "
            f"整手={'是' if profile.lot_size_rules else '否'} | "
            f"技术权重={profile.signal_fusion.technical_weight}"
        )
        return profile

    def _apply_overrides(self, overrides: Dict):
        """应用 YAML 覆盖配置"""
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
        """获取指定因子的融合权重"""
        return getattr(self.signal_fusion, f"{factor}_weight", 0.0)

    def should_veto(self, policy_risk: float, threshold: float = 0.8) -> bool:
        """判断是否应否决 (仅港股政策负面)"""
        if not self.signal_fusion.veto_policy_negative:
            return False
        return policy_risk >= threshold

    def get_dynamic_weights(self, vix: float = 20.0, southbound_net: float = 0.0) -> SignalFusionWeights:
        """
        根据市场 Regime 动态调整信号融合权重

        Args:
            vix: VIX 恐慌指数 (美股)
            southbound_net: 南向资金净流入 (港股, 亿元)

        Returns:
            调整后的 SignalFusionWeights
        """
        w = SignalFusionWeights(
            technical_weight=self.signal_fusion.technical_weight,
            sentiment_weight=self.signal_fusion.sentiment_weight,
            macro_weight=self.signal_fusion.macro_weight,
            policy_weight=self.signal_fusion.policy_weight,
            veto_policy_negative=self.signal_fusion.veto_policy_negative,
        )

        if self.type == "US":
            # 美股: VIX 驱动权重调整
            if vix < 15:
                # 低波动 (Risk-On): 技术面更可靠
                w.technical_weight = min(0.7, w.technical_weight + 0.15)
                w.macro_weight = max(0.1, w.macro_weight - 0.1)
                w.sentiment_weight = max(0.1, w.sentiment_weight - 0.05)
            elif vix > 30:
                # 高波动 (Risk-Off): 宏观和情绪更重要
                w.technical_weight = max(0.2, w.technical_weight - 0.15)
                w.macro_weight = min(0.5, w.macro_weight + 0.1)
                w.sentiment_weight = min(0.4, w.sentiment_weight + 0.05)
            elif vix > 25:
                # 中高波动
                w.technical_weight = max(0.3, w.technical_weight - 0.05)
                w.macro_weight = min(0.4, w.macro_weight + 0.05)

        elif self.type == "HK":
            # 港股: 南向资金驱动权重调整
            if southbound_net > 80:
                # 强劲流入: 技术面+宏观共振
                w.technical_weight = min(0.5, w.technical_weight + 0.1)
                w.macro_weight = min(0.3, w.macro_weight + 0.1)
                w.sentiment_weight = max(0.1, w.sentiment_weight - 0.1)
            elif southbound_net < -80:
                # 强劲流出: 政策风险上升
                w.policy_weight = min(0.6, w.policy_weight + 0.15)
                w.technical_weight = max(0.1, w.technical_weight - 0.1)
                w.sentiment_weight = min(0.3, w.sentiment_weight + 0.05)
            elif southbound_net < -30:
                # 温和流出
                w.policy_weight = min(0.5, w.policy_weight + 0.05)

        # 归一化权重 (确保总和=1.0, 排除 policy_weight)
        total = w.technical_weight + w.sentiment_weight + w.macro_weight
        if total > 0:
            scale = (1.0 - w.policy_weight) / total
            w.technical_weight *= scale
            w.sentiment_weight *= scale
            w.macro_weight *= scale

        logger.debug(
            f"[MarketProfile-{self.type}] 动态权重 | "
            f"VIX={vix:.1f} 南向={southbound_net:.0f} | "
            f"技术={w.technical_weight:.2f} 情绪={w.sentiment_weight:.2f} "
            f"宏观={w.macro_weight:.2f} 政策={w.policy_weight:.2f}"
        )
        return w


# 全局缓存
_cache: Dict[str, MarketProfile] = {}

def get_market_profile(market_type: str, config_overrides: Optional[Dict] = None) -> MarketProfile:
    """获取市场配置 (带缓存)"""
    key = market_type.upper()
    if key not in _cache:
        _cache[key] = MarketProfile.get(key, config_overrides)
    return _cache[key]
