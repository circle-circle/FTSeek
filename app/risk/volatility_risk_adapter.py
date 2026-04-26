"""
波动率风险适配器 (Volatility Risk Adapter) - Phase 3 集成

功能:
- 根据市场波动率动态调整风控参数
- 高波动时自动收紧风控
- 低波动时适度放松风控
"""
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.risk.volatility_adapter")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


@dataclass
class VolatilityRiskConfig:
    """波动率风控配置"""
    # 基础参数 (正常波动状态)
    base_max_position: float = 0.18       # 2026-04-16 对齐 config.yaml (0.18)
    base_max_total_position: float = 0.75 # 2026-04-16 对齐 config.yaml (0.75)
    base_max_daily_loss: float = 0.03     # 2026-04-16 对齐 config.yaml (0.03)
    base_max_drawdown: float = 0.12       # 2026-04-16 对齐 config.yaml (0.12)
    base_stop_loss: float = 0.03          # 止损 3%
    base_take_profit: float = 0.10        # 止盈 10%

    # 波动率调整系数
    low_volatility_boost: float = 1.25    # 低波动放宽 25%
    high_volatility_cut: float = 0.60     # 高波动收紧 40%
    extreme_volatility_cut: float = 0.30  # 极端波动收紧 70%


class VolatilityRiskAdapter:
    """
    波动率风险适配器

    根据 VIX 状态动态调整风控参数
    """

    def __init__(self, config: Dict = None):
        """
        初始化适配器

        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.vol_config = VolatilityRiskConfig()

        # 覆盖配置
        if 'max_position' in self.config:
            self.vol_config.base_max_position = self.config['max_position']
        if 'max_total_position' in self.config:
            self.vol_config.base_max_total_position = self.config['max_total_position']
        if 'max_daily_loss' in self.config:
            self.vol_config.base_max_daily_loss = self.config['max_daily_loss']
        if 'max_drawdown' in self.config:
            self.vol_config.base_max_drawdown = self.config['max_drawdown']

        # 组件引用 (延迟初始化)
        self._vix_monitor = None

        # 状态缓存
        self._last_adjustment: Optional[datetime] = None
        self._current_state: str = "normal"
        self._current_params: Dict = {}

        logger.info("波动率风险适配器已初始化")

    @property
    def vix_monitor(self):
        """延迟加载 VIXMonitor"""
        if self._vix_monitor is None:
            try:
                from app.risk.vix_monitor import get_vix_monitor
                self._vix_monitor = get_vix_monitor()
                logger.info("VIXMonitor 已加载")
            except ImportError as e:
                logger.warning(f"VIXMonitor 加载失败：{e}")
                return None
        return self._vix_monitor

    def get_adjusted_params(self, force: bool = False) -> Dict:
        """
        获取调整后的风控参数

        Args:
            force: 是否强制刷新

        Returns:
            调整后的参数
        """
        now = datetime.now()

        # 检查是否需要刷新 (最多每分钟刷新一次)
        if not force and self._last_adjustment:
            if (now - self._last_adjustment).total_seconds() < 60:
                return self._current_params

        # 获取波动率状态
        vol_state = self._get_volatility_state()

        # 计算调整系数
        adjustment_factor = self._get_adjustment_factor(vol_state)

        # 应用调整
        adjusted = {
            'max_single_position': self._adjust(
                self.vol_config.base_max_position,
                adjustment_factor,
                min_val=0.05,  # 最少 5%
                max_val=0.30,  # 最多 30%
            ),
            'max_total_position': self._adjust(
                self.vol_config.base_max_total_position,
                adjustment_factor,
                min_val=0.30,
                max_val=1.0,
            ),
            'max_daily_loss': self._adjust(
                self.vol_config.base_max_daily_loss,
                adjustment_factor,
                min_val=0.02,
                max_val=0.10,
                inverse=True,  # 日损上限要反向调整 (越波动越小)
            ),
            'max_drawdown': self._adjust(
                self.vol_config.base_max_drawdown,
                adjustment_factor,
                min_val=0.08,
                max_val=0.25,
                inverse=True,
            ),
            'stop_loss': self._adjust(
                self.vol_config.base_stop_loss,
                adjustment_factor * 0.8,  # 止损调整幅度小一些
                min_val=0.02,
                max_val=0.08,
                inverse=True,  # 波动越大止损越小 (防止被震仓)
            ),
            'take_profit': self._adjust(
                self.vol_config.base_take_profit,
                adjustment_factor * 1.2,  # 止盈可以放宽一些
                min_val=0.05,
                max_val=0.20,
            ),
        }

        # 添加波动率信息
        adjusted['volatility_state'] = vol_state
        adjusted['adjustment_factor'] = adjustment_factor
        adjusted['updated_at'] = now.isoformat()

        # 缓存
        self._current_state = vol_state
        self._current_params = adjusted
        self._last_adjustment = now

        logger.info(
            f"风控参数调整 | 波动状态:{vol_state} | "
            f"调整因子:{adjustment_factor:.2f} | "
            f"单仓上限:{adjusted['max_single_position']:.0%} | "
            f"日损上限:{adjusted['max_daily_loss']:.0%}"
        )

        return adjusted

    def _get_volatility_state(self) -> str:
        """获取波动率状态"""
        if self.vix_monitor:
            state = self.vix_monitor.get_state()
            return state.state
        return "normal"  # 默认正常

    def _get_adjustment_factor(self, vol_state: str) -> float:
        """
        获取调整系数

        Args:
            vol_state: 波动率状态

        Returns:
            调整系数 (0.3-1.25)
        """
        factors = {
            "low": self.vol_config.low_volatility_boost,      # 1.25
            "normal": 1.0,                                     # 1.0
            "high": self.vol_config.high_volatility_cut,       # 0.60
            "extreme": self.vol_config.extreme_volatility_cut, # 0.30
        }
        return factors.get(vol_state, 1.0)

    def _adjust(self, base_value: float,
                factor: float,
                min_val: float = 0.0,
                max_val: float = 1.0,
                inverse: bool = False) -> float:
        """
        应用调整

        Args:
            base_value: 基础值
            factor: 调整系数
            min_val: 最小值
            max_val: 最大值
            inverse: 是否反向调整 (用于止损/止盈)

        Returns:
            调整后的值
        """
        if inverse:
            # 反向调整：波动越大，值越小
            # factor < 1 时，值变大 (更严格)
            # factor > 1 时，值变小 (更宽松)
            adjusted = base_value / factor if factor > 0 else base_value
        else:
            # 正向调整：波动越大，值越小
            adjusted = base_value * factor

        # 限制范围
        return max(min_val, min(max_val, adjusted))

    def check_volatility_override(self, signal_code: str,
                                  action: str,
                                  original_confidence: float) -> Tuple[bool, Dict]:
        """
        波动率覆盖检查

        在高波动/极端波动时，可以:
        1. 降低置信度要求
        2. 强制降低仓位
        3. 跳过某些信号

        Args:
            signal_code: 股票代码
            action: 信号方向
            original_confidence: 原始置信度

        Returns:
            (是否通过，覆盖详情)
        """
        vol_state = self._get_volatility_state()
        result = {
            'passed': True,
            'adjusted_confidence': original_confidence,
            'position_override': None,
            'reason': '',
            'volatility_state': vol_state,
        }

        # 极端波动时的特殊处理
        if vol_state == "extreme":
            # 降低置信度要求 (因为波动大，机会难得)
            result['adjusted_confidence'] = min(1.0, original_confidence * 1.2)
            # 强制降低仓位
            result['position_override'] = 0.10  # 最多 10% 仓位
            result['reason'] = '极端波动：降低仓位要求'

            logger.warning(
                f"[极端波动] {signal_code} {action} | "
                f"置信度调整：{original_confidence:.2f} -> {result['adjusted_confidence']:.2f} | "
                f"仓位上限：10%"
            )

        # 高波动时的处理
        elif vol_state == "high":
            # 轻微降低置信度
            result['adjusted_confidence'] = min(1.0, original_confidence * 1.1)
            result['reason'] = '高波动：正常处理'

            logger.info(
                f"[高波动] {signal_code} {action} | "
                f"置信度调整：{original_confidence:.2f} -> {result['adjusted_confidence']:.2f}"
            )

        # 低波动时的处理
        elif vol_state == "low":
            # 低波动时提高标准 (避免过度交易)
            result['adjusted_confidence'] = original_confidence * 0.9
            result['reason'] = '低波动：提高标准'

            logger.debug(
                f"[低波动] {signal_code} {action} | "
                f"置信度调整：{original_confidence:.2f} -> {result['adjusted_confidence']:.2f}"
            )

        return result['passed'], result

    def get_risk_budget(self, portfolio_value: float) -> Dict:
        """
        获取风险预算

        Args:
            portfolio_value: 组合市值

        Returns:
            风险预算详情
        """
        params = self.get_adjusted_params()

        return {
            'portfolio_value': portfolio_value,
            'max_single_position_value': portfolio_value * params['max_single_position'],
            'max_total_position_value': portfolio_value * params['max_total_position'],
            'max_daily_loss_value': portfolio_value * params['max_daily_loss'],
            'max_drawdown_value': portfolio_value * params['max_drawdown'],
            'volatility_state': params['volatility_state'],
            'adjustment_factor': params['adjustment_factor'],
        }

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        return {
            'current_state': self._current_state,
            'last_adjustment': self._last_adjustment.isoformat() if self._last_adjustment else None,
            'current_params': self._current_params,
            'vix_monitor': 'loaded' if self.vix_monitor else 'not_loaded',
        }


# 全局单例
_adapter = None


def get_volatility_risk_adapter(config: Dict = None) -> VolatilityRiskAdapter:
    """获取波动率风险适配器单例"""
    global _adapter
    if _adapter is None:
        _adapter = VolatilityRiskAdapter(config)
    return _adapter


if __name__ == "__main__":
    # 测试
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    adapter = get_volatility_risk_adapter()

    print("\n波动率风控参数测试:")
    print("=" * 60)

    # 测试不同 VIX 状态下的参数调整
    test_vix_values = [10, 20, 30, 40, 50]

    for vix in test_vix_values:
        if adapter.vix_monitor:
            adapter.vix_monitor.update_vix(vix)

        params = adapter.get_adjusted_params(force=True)

        print(f"\nVIX = {vix}:")
        print(f"  波动状态：{params['volatility_state']}")
        print(f"  调整因子：{params['adjustment_factor']:.2f}")
        print(f"  单仓上限：{params['max_single_position']:.0%}")
        print(f"  总仓位上限：{params['max_total_position']:.0%}")
        print(f"  日损上限：{params['max_daily_loss']:.0%}")
        print(f"  止损：{params['stop_loss']:.0%}")
        print(f"  止盈：{params['take_profit']:.0%}")

    # 测试覆盖检查
    print("\n" + "=" * 60)
    print("波动率覆盖检查测试:")

    for vix in [20, 40]:
        if adapter.vix_monitor:
            adapter.vix_monitor.update_vix(vix)

        passed, override = adapter.check_volatility_override(
            signal_code="HK.00700",
            action="BUY",
            original_confidence=0.75,
        )

        print(f"\nVIX = {vix}:")
        print(f"  原始置信度：{0.75:.2f}")
        print(f"  调整后置信度：{override['adjusted_confidence']:.2f}")
        print(f"  仓位覆盖：{override['position_override']}")
        print(f"  原因：{override['reason']}")
