"""
港股特性适配模块

针对港股市场特点的专用适配层:
- T+0 交易 (当日可买卖)
- 无涨跌停限制
- 港币/美元汇率波动
- 流动性分层严重

最后更新：2026-03-15
"""
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.strategy.hk_specific")
logger.propagate = False  # 防止日志重复


# ==================== 数据模型 ====================

class LiquidityTier(Enum):
    """流动性层级"""
    TIER1 = "tier1"  # 高流动性 (腾讯/阿里/美团)
    TIER2 = "tier2"  # 中流动性 (京东/网易/百度)
    TIER3 = "tier3"  # 低流动性 (其他)


@dataclass
class HKMarketConfig:
    """港股市场配置"""
    t0_trading: bool = True
    no_price_limit: bool = True
    currency: str = "HKD"
    usd_hk_rate: float = 7.8
    
    # 流动性分层配置
    liquidity_tiers: Dict[str, Dict] = field(default_factory=dict)
    
    # 做空优化配置
    short_bias_factor: float = 1.2
    short_rsi_overbought: float = 65.0
    short_macd_threshold: float = 0.0008
    short_st_multiplier: float = 2.5


@dataclass
class IntradayPosition:
    """日内持仓记录 (T+0 专用)"""
    code: str
    action: SignalAction
    price: float
    quantity: int
    timestamp: datetime
    reversed: bool = False  # 是否已反向平仓
    reverse_signal: Optional[Signal] = None


# ==================== 港股特性适配引擎 ====================

class HKSpecificAdapter:
    """
    港股特性适配引擎
    
    功能:
    1. T+0 交易支持 (允许日内反向信号)
    2. 无涨跌停适配 (扩大止损范围)
    3. 流动性分层 (不同层级不同仓位)
    4. 汇率对冲 (港币/美元波动)
    """
    
    def __init__(self, config: HKMarketConfig = None):
        self.config = config or HKMarketConfig()
        
        # 日内持仓追踪 (T+0)
        self._intraday_positions: Dict[str, List[IntradayPosition]] = {}
        
        # 流动性层级缓存
        self._liquidity_cache: Dict[str, LiquidityTier] = {}
        
        # 初始化流动性分层
        self._init_liquidity_tiers()
        
        logger.info("港股特性适配引擎初始化完成")
        logger.info(f"  - T+0 交易：{'启用' if self.config.t0_trading else '禁用'}")
        logger.info(f"  - 无涨跌停：{'启用' if self.config.no_price_limit else '禁用'}")
        logger.info(f"  - 做空增强因子：{self.config.short_bias_factor}x")
    
    def _init_liquidity_tiers(self):
        """初始化流动性分层配置"""
        # 默认配置 (可从配置文件加载)
        default_tiers = {
            "tier1": {
                "codes": ["HK.00700", "HK.09988", "HK.03690", "HK.01810", "HK.09992", "HK.00981"],
                "max_position": 0.25,
                "min_confidence": 0.55
            },
            "tier2": {
                "codes": ["HK.09618", "HK.09999", "HK.09888", "HK.01024"],
                "max_position": 0.15,
                "min_confidence": 0.60
            },
            "tier3": {
                "default": True,
                "max_position": 0.08,
                "min_confidence": 0.65
            }
        }
        
        for tier_name, tier_config in default_tiers.items():
            for code in tier_config.get("codes", []):
                self._liquidity_cache[code] = LiquidityTier(tier_name)
        
        logger.info(f"流动性分层已加载：{len(self._liquidity_cache)} 只股票")
    
    # ==================== T+0 交易适配 ====================
    
    def allow_intraday_reversal(self, code: str, new_signal: Signal) -> bool:
        """
        判断是否允许日内反向交易 (T+0)
        
        Args:
            code: 股票代码
            new_signal: 新信号
            
        Returns:
            是否允许
        """
        if not self.config.t0_trading:
            return False
        
        if code not in self._intraday_positions:
            return True  # 无持仓，允许
        
        positions = self._intraday_positions[code]
        
        # 检查是否有未平仓的日内持仓
        for pos in positions:
            if pos.timestamp.date() == datetime.now().date() and not pos.reversed:
                # 有日内持仓，检查是否允许反向
                if new_signal.action == SignalAction.BUY and pos.action == SignalAction.SELL:
                    logger.info(f"[T+0] {code} 允许日内平空仓")
                    return True
                if new_signal.action == SignalAction.SELL and pos.action == SignalAction.BUY:
                    logger.info(f"[T+0] {code} 允许日内平多仓")
                    return True
        
        return True  # 无冲突
    
    def record_intraday_position(self, signal: Signal):
        """记录日内持仓"""
        code = signal.code
        
        if code not in self._intraday_positions:
            self._intraday_positions[code] = []
        
        position = IntradayPosition(
            code=code,
            action=signal.action,
            price=signal.price,
            quantity=signal.quantity,
            timestamp=datetime.now()
        )
        
        self._intraday_positions[code].append(position)
        action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
        logger.debug(f"[T+0] 记录日内持仓：{code} {action_str} @ {signal.price}")
    
    def mark_intraday_reversal(self, code: str, reverse_signal: Signal):
        """标记日内反向平仓"""
        if code not in self._intraday_positions:
            return
        
        positions = self._intraday_positions[code]
        
        # 找到最近的未平仓持仓
        for pos in reversed(positions):
            if not pos.reversed and pos.timestamp.date() == datetime.now().date():
                pos.reversed = True
                pos.reverse_signal = reverse_signal
                pos_action_str = pos.action.value if hasattr(pos.action, 'value') else str(pos.action)
                rev_action_str = reverse_signal.action.value if hasattr(reverse_signal.action, 'value') else str(reverse_signal.action)
                logger.info(f"[T+0] {code} 日内平仓：{pos_action_str} @ {pos.price} → "
                           f"{rev_action_str} @ {reverse_signal.price}")
                break
    
    def cleanup_intraday_positions(self):
        """清理过期的日内持仓记录 (每日收盘后)"""
        today = datetime.now().date()
        
        for code in list(self._intraday_positions.keys()):
            positions = self._intraday_positions[code]
            # 保留今日和昨日的记录 (用于统计)
            self._intraday_positions[code] = [
                p for p in positions 
                if p.timestamp.date() >= today - timedelta(days=1)
            ]
        
        logger.debug("[T+0] 清理过期日内持仓记录")
    
    # ==================== 无涨跌停适配 ====================
    
    def adjust_stop_loss_for_short(self, signal: Signal, atr_percent: float = None) -> Signal:
        """
        做空止损调整 (无涨跌停限制)
        
        港股可能单日下跌 20%+，做空止损需要更宽松
        
        Args:
            signal: 原始信号
            atr_percent: ATR 波动率百分比
            
        Returns:
            调整后的信号
        """
        if not self.config.no_price_limit:
            return signal
        
        if signal.action != SignalAction.SELL:
            return signal
        
        # 基础止损 -8% (vs 做多 -5%)
        base_stop_loss = -0.08
        
        # 高波动时进一步放宽
        if atr_percent and atr_percent > 0.05:  # ATR>5%
            base_stop_loss = -0.10  # 放宽至 -10%
            signal.extra['extended_stop_loss'] = True
            signal.extra['stop_loss_reason'] = "高波动放宽"
        
        signal.extra['stop_loss'] = base_stop_loss
        logger.debug(f"[无涨跌停] {signal.code} 做空止损调整：{base_stop_loss:.1%}")
        
        return signal
    
    def extreme_volatility_check(self, code: str, price_change: float) -> bool:
        """
        极端波动检查
        
        Args:
            code: 股票代码
            price_change: 价格变化百分比
            
        Returns:
            是否触发极端波动
        """
        # 港股单日波动可能超过 20%
        if abs(price_change) > 0.20:
            logger.warning(f"[极端波动] {code} 单日波动 {price_change:.1%}，建议降低仓位")
            return True
        
        return False
    
    # ==================== 流动性分层适配 ====================
    
    def get_liquidity_tier(self, code: str) -> LiquidityTier:
        """获取股票流动性层级"""
        if code in self._liquidity_cache:
            return self._liquidity_cache[code]
        
        # 未配置的股票默认 tier3
        return LiquidityTier.TIER3
    
    def adjust_position_for_liquidity(self, signal: Signal, base_quantity: int) -> int:
        """
        根据流动性调整仓位
        
        Args:
            signal: 原始信号
            base_quantity: 基础数量
            
        Returns:
            调整后的数量
        """
        tier = self.get_liquidity_tier(signal.code)
        
        if tier == LiquidityTier.TIER1:
            # 高流动性：不调整
            adjusted = base_quantity
            logger.debug(f"[流动性] {signal.code} Tier1，仓位不变")
        
        elif tier == LiquidityTier.TIER2:
            # 中流动性：减少 20%
            adjusted = int(base_quantity * 0.8)
            logger.debug(f"[流动性] {signal.code} Tier2，仓位 ×0.8")
        
        else:  # TIER3
            # 低流动性：减少 50%
            adjusted = int(base_quantity * 0.5)
            logger.debug(f"[流动性] {signal.code} Tier3，仓位 ×0.5")
        
        signal.extra['liquidity_tier'] = tier.value
        signal.extra['liquidity_adjustment'] = adjusted / base_quantity if base_quantity > 0 else 1.0
        
        return adjusted
    
    def get_max_position_ratio(self, code: str) -> float:
        """获取单股最大仓位比例"""
        tier = self.get_liquidity_tier(code)
        
        tier_configs = {
            LiquidityTier.TIER1: 0.25,
            LiquidityTier.TIER2: 0.15,
            LiquidityTier.TIER3: 0.08
        }
        
        return tier_configs.get(tier, 0.08)
    
    def get_min_confidence(self, code: str) -> float:
        """获取最小置信度要求"""
        tier = self.get_liquidity_tier(code)
        
        tier_configs = {
            LiquidityTier.TIER1: 0.40,  # 2026-03-19: 降低至 0.40 便于触发
            LiquidityTier.TIER2: 0.40,  # 2026-03-19: 降低至 0.40 便于触发
            LiquidityTier.TIER3: 0.40   # 2026-03-19: 降低至 0.40 便于触发
        }
        
        return tier_configs.get(tier, 0.65)
    
    # ==================== 汇率对冲 ====================
    
    def check_currency_risk(self, usd_hk_rate: float) -> Optional[str]:
        """
        检查汇率风险
        
        Args:
            usd_hk_rate: 当前美元/港币汇率
            
        Returns:
            风险警告 (如有)
        """
        peg_rate = self.config.usd_hk_rate
        deviation = (usd_hk_rate - peg_rate) / peg_rate
        
        if abs(deviation) > 0.02:  # 偏离>2%
            warning = f"汇率偏离 {deviation:.2%}，关注港币资产风险"
            logger.warning(f"[汇率风险] {warning}")
            return warning
        
        return None
    
    # ==================== 做空信号增强 ====================
    
    def enhance_short_signal(self, signal: Signal) -> Signal:
        """
        增强做空信号
        
        Args:
            signal: 做空信号
            
        Returns:
            增强后的信号
        """
        if signal.action != SignalAction.SELL:
            return signal
        
        # 应用做空增强因子
        if signal.extra.get('confidence'):
            signal.extra['confidence'] *= self.config.short_bias_factor
            signal.extra['confidence'] = min(1.0, signal.extra['confidence'])  # 上限 100%
        
        # 调整止盈止损
        signal.extra['take_profit'] = 0.20  # 做空止盈 +20%
        signal.extra['stop_loss'] = -0.08   # 做空止损 -8%
        
        signal.reason += f" [做空增强 ×{self.config.short_bias_factor}]"
        
        logger.info(f"[做空增强] {signal.code} 信号增强：置信度 ×{self.config.short_bias_factor}")
        
        return signal
    
    # ==================== 综合信号处理 ====================
    
    def process_signal(self, signal: Signal, atr_percent: float = None) -> Optional[Signal]:
        """
        综合处理信号 (所有港股特性适配)
        
        流程:
        1. T+0 检查
        2. 流动性调整
        3. 做空增强
        4. 止损调整
        
        Args:
            signal: 原始信号
            atr_percent: ATR 波动率
            
        Returns:
            处理后的信号 (None 表示过滤掉)
        """
        code = signal.code
        
        # Step 1: T+0 检查
        if not self.allow_intraday_reversal(code, signal):
            logger.info(f"[港股适配] {code} 日内交易冲突，过滤信号")
            return None
        
        # Step 2: 流动性调整
        if signal.quantity:
            signal.quantity = self.adjust_position_for_liquidity(signal, signal.quantity)
        
        # Step 3: 最小置信度检查
        min_conf = self.get_min_confidence(code)
        if signal.extra.get('confidence', 0) < min_conf:
            logger.info(f"[港股适配] {code} 置信度 {signal.extra.get('confidence', 0):.2f} "
                       f"< {min_conf}，过滤信号")
            return None
        
        # Step 4: 做空信号处理
        if signal.action == SignalAction.SELL:
            signal = self.enhance_short_signal(signal)
            signal = self.adjust_stop_loss_for_short(signal, atr_percent)
        
        # Step 5: 记录日内持仓
        self.record_intraday_position(signal)
        
        action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
        logger.info(f"[港股适配] {code} 信号处理完成：{action_str} "
                   f"_qty={signal.quantity} _conf={signal.extra.get('confidence', 'N/A')}")
        
        return signal


# ==================== 全局单例 ====================

_hk_adapter: Optional[HKSpecificAdapter] = None


def get_hk_adapter(config: HKMarketConfig = None) -> HKSpecificAdapter:
    """获取港股适配器单例"""
    global _hk_adapter
    if _hk_adapter is None:
        _hk_adapter = HKSpecificAdapter(config)
    return _hk_adapter


def reset_hk_adapter(config: HKMarketConfig = None):
    """重置港股适配器"""
    global _hk_adapter
    _hk_adapter = HKSpecificAdapter(config)
    return _hk_adapter
