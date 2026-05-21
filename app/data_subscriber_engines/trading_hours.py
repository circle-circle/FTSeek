"""
TradingHoursChecker — 交易时间判断引擎

职责:
- 检查当前是否为港股交易时间
- 支持高质量交易时段判断（排除开收盘噪音）
- 提供分时段质量信息

从 data_subscriber.py 提取，降低耦合，提升可测试性

依赖注入:
- market_hours_shadow: 影子模式验证器（可选）
- config_center: 配置中心（用于读取时间门控配置）
"""
import logging
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)


class TradingHoursChecker:
    """交易时间判断引擎 — 港股交易时段 + 质量门控"""

    def __init__(self, market_hours_shadow=None, config_center=None):
        """
        初始化交易时间检查器
        
        Args:
            market_hours_shadow: 影子模式验证器（可选）
            config_center: 配置中心实例（可选）
        """
        self._market_hours_shadow = market_hours_shadow
        self._config_center = config_center
        logger.info(f"[TradingHoursChecker] 初始化完成：shadow_mode={'enabled' if market_hours_shadow else 'disabled'}")

    def is_trading_hours(self) -> bool:
        """
        检查当前是否为港股交易时间

        港股交易时间:
        - 上午：09:30 - 12:00
        - 下午：13:00 - 16:00

        Returns:
            True: 在交易时间内
            False: 非交易时间 (收盘后、开盘前、周末)

        【2026-05-02】If Shadow Mode is active, delegates to validator.
        """
        # Shadow Mode: run both old and new logic, return OLD result
        if self._market_hours_shadow is not None:
            return self._market_hours_shadow.check()

        # Direct call (original logic preserved)
        return self._is_trading_hours_impl()

    def _is_trading_hours_impl(self) -> bool:
        """
        Original trading hours implementation.
        Extracted for Shadow Mode comparison.
        """
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()

        # 周末不交易
        if weekday >= 5:  # 5=周六，6=周日
            return False

        current_time = hour * 60 + minute

        # 上午交易时间：09:30 - 12:00 (570 - 720 分钟)
        morning_start = 9 * 60 + 30   # 09:30 = 570
        morning_end = 12 * 60         # 12:00 = 720

        # 下午交易时间：13:00 - 16:00 (780 - 960 分钟)
        afternoon_start = 13 * 60     # 13:00 = 780
        afternoon_end = 16 * 60       # 16:00 = 960

        if morning_start <= current_time < morning_end:
            return True
        if afternoon_start <= current_time < afternoon_end:
            return True

        return False

    def is_high_quality_trading_hours(self) -> bool:
        """
        检查是否为高质量交易时间（排除开收盘噪音时段）

        排除时段：
        - 开盘冲击期：09:30 - 09:45（流动性差、价格剧烈波动）
        - 收盘撮合期：15:45 - 16:00（做市商平仓、价格失真）

        Returns:
            True: 信号质量可靠的交易时段
            False: 噪音较高的时段
        """
        if not self.is_trading_hours():
            return False

        now = datetime.now()
        current_time = now.hour * 60 + now.minute

        # 排除开盘冲击期（09:30 - 09:45）— SSOT: strategy.time_gate.open_buffer
        open_buffer_start = 9 * 60 + 30   # 09:30
        open_buffer_end = 9 * 60 + 45   # 09:45

        # 排除收盘撮合期（15:45 - 16:00）— SSOT: strategy.time_gate.close_buffer
        if self._config_center:
            _cb_cfg = self._config_center.get('strategy.time_gate', {})
            _cb_s = _cb_cfg.get('close_buffer_start', '15:45')
            _cb_e = _cb_cfg.get('close_buffer_end', '16:00')
            close_buffer_start = int(_cb_s.split(':')[0]) * 60 + int(_cb_s.split(':')[1])
            close_buffer_end = int(_cb_e.split(':')[0]) * 60 + int(_cb_e.split(':')[1])
        else:
            # Fallback to hardcoded values
            close_buffer_start = 15 * 60 + 45  # 15:45
            close_buffer_end = 16 * 60         # 16:00

        if open_buffer_start <= current_time < open_buffer_end:
            return False  # 开盘波动期，信号噪音大
        if close_buffer_start <= current_time < close_buffer_end:
            return False  # 收盘撮合期，价格失真

        return True

    def get_trading_period_info(self) -> Dict[str, Any]:
        """
        P2 2026-04-27: 返回当前交易时段信息 (分时段质量门)
        
        Returns:
            {
                'period': 'open_noise' | 'main' | 'close_noise' | 'out_of_hours',
                'description': str,
            }
        """
        now = datetime.now()
        current_time = now.hour * 60 + now.minute
        
        # 周末
        if now.weekday() >= 5:
            return {'period': 'out_of_hours', 'description': '周末休市'}
        
        # 开盘前
        if current_time < 9 * 60 + 30:
            return {'period': 'out_of_hours', 'description': '盘前等待开盘'}
        
        # 开盘冲击期 (09:30 - 09:45)
        if current_time < 9 * 60 + 45:
            return {'period': 'open_noise', 'description': '开盘冲击期，流动性差'}
        
        # 上午主时段 (09:45 - 12:00)
        if current_time < 12 * 60:
            return {'period': 'main', 'description': '上午主交易时段'}
        
        # 午间休市 (12:00 - 13:00)
        if current_time < 13 * 60:
            return {'period': 'out_of_hours', 'description': '午间休市'}
        
        # 下午主时段 (13:00 - 15:45)
        if current_time < 15 * 60 + 45:
            return {'period': 'main', 'description': '下午主交易时段'}
        
        # 收盘撮合期 (15:45 - 16:00)
        if current_time < 16 * 60:
            return {'period': 'close_noise', 'description': '收盘撮合期，价格失真'}
        
        # 收盘后
        return {'period': 'out_of_hours', 'description': '已收盘'}
