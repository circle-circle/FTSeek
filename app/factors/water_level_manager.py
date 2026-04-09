"""
资金水位管理器 - 动态风险调整机制

功能:
- 根据资金水位动态调整仓位上限
- 交易频率限制
- 风险预算管理
- 质量要求联动

水位等级:
- GREEN (95%+): 安全区，标准风险
- YELLOW (85-95%): 警戒区，降低风险
- ORANGE (75-85%): 危险区，严格风控
- RED (<75%): 熔断区，停止交易
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, List
from enum import Enum
from datetime import datetime
import logging

try:
    from .quality_scorer import QualityClass
except ImportError:
    from app.factors.quality_scorer import QualityClass

logger = logging.getLogger("ftseek.water_level")


class WaterLevel(Enum):
    """资金水位等级"""
    GREEN = "green"      # 安全区 (95%+)
    YELLOW = "yellow"    # 警戒区 (85-95%)
    ORANGE = "orange"    # 危险区 (75-85%)
    RED = "red"          # 熔断区 (<75%)


@dataclass
class WaterLevelConfig:
    """水位等级配置"""
    level: WaterLevel
    min_ratio: float           # 水位下限
    max_ratio: float           # 水位上限

    # 仓位限制
    max_single_position: float
    max_total_position: float

    # 交易频率限制
    max_daily_trades: int

    # 风险限制
    risk_per_trade: float
    max_daily_loss: float

    # 质量要求
    quality_min_class: str     # "A" / "B" / "C" / "JUNK"

    # 杠杆系数
    leverage_factor: float

    # 特殊动作
    action: str = "TRADE"      # "TRADE" / "LIQUIDATE" / "HALT"


@dataclass
class WaterLevelState:
    """水位状态"""
    current_ratio: float           # 当前水位
    current_level: WaterLevel      # 当前等级
    previous_level: Optional[WaterLevel]  # 上一等级
    level_change_time: Optional[datetime]  # 等级变化时间

    # 账户数据
    current_capital: float
    initial_capital: float
    peak_capital: float
    drawdown: float                # 当前回撤

    # 当日数据
    today_trades: int
    today_pnl: float
    today_loss: float

    # 可用风险预算
    remaining_daily_loss: float
    remaining_trades: int

    # 建议操作
    suggested_action: str          # "normal" / "reduce_risk" / "halt"
    notes: str = ""

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'current_ratio': round(self.current_ratio * 100, 2),
            'current_level': self.current_level.value,
            'previous_level': self.previous_level.value if self.previous_level else None,
            'current_capital': round(self.current_capital, 2),
            'initial_capital': self.initial_capital,
            'peak_capital': self.peak_capital,
            'drawdown': round(self.drawdown * 100, 2),
            'today_trades': self.today_trades,
            'today_pnl': round(self.today_pnl * 100, 2),
            'today_loss': round(self.today_loss * 100, 2),
            'remaining_daily_loss': round(self.remaining_daily_loss * 100, 2),
            'remaining_trades': self.remaining_trades,
            'suggested_action': self.suggested_action,
            'notes': self.notes,
        }


class WaterLevelManager:
    """
    资金水位管理器

    根据资金水位动态调整:
    - 仓位上限
    - 交易频率
    - 风险预算
    - 质量要求
    """

    # 默认配置
    DEFAULT_CONFIGS = {
        WaterLevel.GREEN: WaterLevelConfig(
            level=WaterLevel.GREEN,
            min_ratio=0.95,
            max_ratio=1.05,
            max_single_position=0.20,
            max_total_position=0.75,
            max_daily_trades=5,
            risk_per_trade=0.02,
            max_daily_loss=0.03,
            quality_min_class='C',
            leverage_factor=1.0,
            action='TRADE',
        ),
        WaterLevel.YELLOW: WaterLevelConfig(
            level=WaterLevel.YELLOW,
            min_ratio=0.85,
            max_ratio=0.95,
            max_single_position=0.15,
            max_total_position=0.60,
            max_daily_trades=3,
            risk_per_trade=0.015,
            max_daily_loss=0.02,
            quality_min_class='B',
            leverage_factor=0.75,
            action='REDUCE_RISK',
        ),
        WaterLevel.ORANGE: WaterLevelConfig(
            level=WaterLevel.ORANGE,
            min_ratio=0.75,
            max_ratio=0.85,
            max_single_position=0.10,
            max_total_position=0.40,
            max_daily_trades=1,
            risk_per_trade=0.01,
            max_daily_loss=0.01,
            quality_min_class='A',
            leverage_factor=0.50,
            action='HALT',
        ),
        WaterLevel.RED: WaterLevelConfig(
            level=WaterLevel.RED,
            min_ratio=0.0,
            max_ratio=0.75,
            max_single_position=0.00,
            max_total_position=0.00,
            max_daily_trades=0,
            risk_per_trade=0.00,
            max_daily_loss=0.00,
            quality_min_class='JUNK',
            leverage_factor=0.0,
            action='LIQUIDATE',
        ),
    }

    # 质量等级映射
    QUALITY_CLASS_ORDER = {
        'A': 3,
        'B': 2,
        'C': 1,
        'JUNK': 0,
    }

    def __init__(self, initial_capital: float, configs: Dict = None):
        """
        初始化水位管理器

        Args:
            initial_capital: 初始资金
            configs: 水位配置字典
        """
        self.initial_capital = initial_capital
        self.configs = configs or self.DEFAULT_CONFIGS.copy()

        # 账户状态
        self.current_capital = initial_capital
        self.peak_capital = initial_capital

        # 当日统计
        self.today_date = datetime.now().strftime("%Y-%m-%d")
        self.today_trades = 0
        self.today_pnl = 0.0
        self.today_loss = 0.0

        # 水位状态
        self._last_level: Optional[WaterLevel] = None
        self._last_level_change: Optional[datetime] = None
        self._level_history: List[Tuple[datetime, WaterLevel]] = []

        logger.info(
            f"资金水位管理器已初始化 | "
            f"初始资金={initial_capital:,.0f}"
        )

    def update_capital(
        self,
        current_capital: float,
        today_trades: int = None,
        today_pnl: float = None,
        today_loss: float = None
    ):
        """
        更新资金状态

        Args:
            current_capital: 当前资金
            today_trades: 今日交易次数 (可选)
            today_pnl: 今日盈亏 (可选)
            today_loss: 今日亏损 (可选)
        """
        old_capital = self.current_capital
        self.current_capital = current_capital
        self.peak_capital = max(self.peak_capital, current_capital)

        # 重置日统计 (如果日期变化)
        self._reset_daily_if_needed()

        # 更新日统计
        if today_trades is not None:
            self.today_trades = today_trades
        if today_pnl is not None:
            self.today_pnl = today_pnl
        if today_loss is not None:
            self.today_loss = today_loss

        # 检查水位等级变化
        self._check_level_change()

        # 记录资金变化
        if current_capital != old_capital:
            change = current_capital - old_capital
            change_pct = change / old_capital * 100 if old_capital > 0 else 0
            logger.debug(
                f"资金更新：{old_capital:,.0f} -> {current_capital:,.0f} "
                f"({change_pct:+.2f}%)"
            )

    def _reset_daily_if_needed(self):
        """如果日期变化，重置日统计"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.today_date:
            old_trades = self.today_trades
            old_loss = self.today_loss
            self.today_trades = 0
            self.today_pnl = 0.0
            self.today_loss = 0.0
            self.today_date = today
            logger.info(
                f"已重置日统计 (昨日：{old_trades}笔交易，亏损{old_loss:,.0f})"
            )

    def _check_level_change(self):
        """检查水位等级变化"""
        current_level = self.get_current_level()

        if self._last_level is None or current_level != self._last_level:
            old_level = self._last_level
            self._last_level = current_level
            self._last_level_change = datetime.now()

            self._level_history.append((self._last_level_change, current_level))

            # 记录等级变化日志
            if old_level:
                direction = "↓" if self._level_value(current_level) < self._level_value(old_level) else "↑"
                logger.warning(
                    f"资金水位等级变化：{old_level.value} → {current_level.value} {direction}"
                )

    def _level_value(self, level: WaterLevel) -> int:
        """获取水位等级的数值 (用于比较)"""
        return {
            WaterLevel.RED: 0,
            WaterLevel.ORANGE: 1,
            WaterLevel.YELLOW: 2,
            WaterLevel.GREEN: 3,
        }.get(level, 0)

    def get_current_level(self) -> WaterLevel:
        """获取当前水位等级"""
        ratio = self.current_capital / self.initial_capital

        if ratio >= 0.95:
            return WaterLevel.GREEN
        elif ratio >= 0.85:
            return WaterLevel.YELLOW
        elif ratio >= 0.75:
            return WaterLevel.ORANGE
        else:
            return WaterLevel.RED

    def get_current_config(self) -> WaterLevelConfig:
        """获取当前水位等级的配置"""
        level = self.get_current_level()
        return self.configs[level]

    def can_trade(self, quality_class: str = None) -> Tuple[bool, str]:
        """
        判断是否可以交易

        Args:
            quality_class: 标的质量等级 (可选)

        Returns:
            (是否可以交易，原因)
        """
        config = self.get_current_config()
        level = self.get_current_level()

        # 1. 检查是否被禁止交易
        if config.max_daily_trades <= 0:
            return False, f"水位{level.value}禁止开新仓"

        if config.max_single_position <= 0:
            return False, f"水位{level.value}仓位上限为 0"

        # 2. 检查日交易次数
        if self.today_trades >= config.max_daily_trades:
            return False, f"今日交易次数已达上限 ({config.max_daily_trades})"

        # 3. 检查日亏损
        max_loss_amount = config.max_daily_loss * self.initial_capital
        if self.today_loss >= max_loss_amount:
            return False, f"今日亏损已达上限 ({config.max_daily_loss:.0%})"

        # 4. 检查质量要求
        if quality_class:
            min_class = config.quality_min_class
            if self._quality_below_minimum(quality_class, min_class):
                return False, f"水位{level.value}要求最低质量{min_class}级"

        return True, "允许交易"

    def can_buy_quality(self, quality_class: QualityClass) -> Tuple[bool, str]:
        """
        判断是否可以买入指定质量的股票

        Args:
            quality_class: 质量等级

        Returns:
            (是否可以买入，原因)
        """
        config = self.get_current_config()
        level = self.get_current_level()

        min_class = config.quality_min_class
        quality_value = self._quality_class_to_value(quality_class.value)
        min_value = self.QUALITY_CLASS_ORDER.get(min_class, 0)

        if quality_value < min_value:
            return False, f"水位{level.value}要求最低质量{min_class}级"

        return self.can_trade(quality_class.value)

    def get_max_position(
        self,
        quality_class: str = None,
        quality_score: float = None
    ) -> float:
        """
        获取最大仓位比例

        Args:
            quality_class: 标的质量等级 (可选)
            quality_score: 质量评分 (可选，用于额外调整)

        Returns:
            最大仓位比例 (0-1)
        """
        config = self.get_current_config()
        base_max = config.max_single_position

        # 质量调整：优质股可提高仓位
        if quality_class:
            quality_multiplier = self._get_quality_multiplier(quality_class)
            # 但不能超过配置上限
            adjusted = min(base_max * quality_multiplier, base_max)
            return adjusted * config.leverage_factor

        return base_max * config.leverage_factor

    def get_max_total_position(self) -> float:
        """获取最大总仓位比例"""
        config = self.get_current_config()
        return config.max_total_position * config.leverage_factor

    def get_risk_per_trade(self) -> float:
        """获取单笔风险比例"""
        config = self.get_current_config()
        return config.risk_per_trade

    def get_max_daily_loss(self) -> float:
        """获取最大日亏损比例"""
        config = self.get_current_config()
        return config.max_daily_loss

    def get_remaining_risk_budget(self) -> Dict:
        """获取剩余风险预算"""
        config = self.get_current_config()

        remaining_loss = max(
            0,
            config.max_daily_loss * self.initial_capital - self.today_loss
        )
        remaining_trades = max(0, config.max_daily_trades - self.today_trades)

        return {
            'remaining_daily_loss': remaining_loss,
            'remaining_daily_loss_pct': remaining_loss / self.initial_capital * 100,
            'remaining_trades': remaining_trades,
        }

    def get_state(self) -> WaterLevelState:
        """获取水位状态"""
        level = self.get_current_level()
        config = self.get_current_config()
        ratio = self.current_capital / self.initial_capital
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital

        # 计算剩余风险预算
        remaining_daily_loss = max(
            0,
            config.max_daily_loss * self.initial_capital - self.today_loss
        )
        remaining_trades = max(0, config.max_daily_trades - self.today_trades)

        # 确定建议操作
        if level == WaterLevel.RED:
            suggested_action = "LIQUIDATE"
            notes = "触发熔断，禁止所有交易"
        elif level == WaterLevel.ORANGE:
            suggested_action = "HALT"
            notes = "高风险状态，仅可买入 A 级标的"
        elif level == WaterLevel.YELLOW:
            suggested_action = "REDUCE_RISK"
            notes = "警戒状态，降低风险敞口"
        else:
            suggested_action = "NORMAL"
            notes = "正常状态"

        return WaterLevelState(
            current_ratio=ratio,
            current_level=level,
            previous_level=self._last_level,
            level_change_time=self._last_level_change,
            current_capital=self.current_capital,
            initial_capital=self.initial_capital,
            peak_capital=self.peak_capital,
            drawdown=drawdown,
            today_trades=self.today_trades,
            today_pnl=self.today_pnl,
            today_loss=self.today_loss,
            remaining_daily_loss=remaining_daily_loss,
            remaining_trades=remaining_trades,
            suggested_action=suggested_action,
            notes=notes,
        )

    def get_level_history(self) -> List[Dict]:
        """获取水位等级历史"""
        return [
            {'timestamp': ts.isoformat(), 'level': level.value}
            for ts, level in self._level_history
        ]

    def record_trade(self, pnl: float = 0.0, loss: float = 0.0):
        """
        记录交易

        Args:
            pnl: 盈亏金额 (正=盈利，负=亏损)
            loss: 亏损金额 (正数)
        """
        self.today_trades += 1
        self.today_pnl += pnl
        self.today_loss += loss

        logger.debug(
            f"记录交易：今日={self.today_trades}笔，"
            f"盈亏={self.today_pnl:,.0f}, 亏损={self.today_loss:,.0f}"
        )

    def _quality_below_minimum(self, quality_class: str, min_class: str) -> bool:
        """判断质量是否低于最低要求"""
        quality_value = self.QUALITY_CLASS_ORDER.get(quality_class, 0)
        min_value = self.QUALITY_CLASS_ORDER.get(min_class, 0)
        return quality_value < min_value

    def _quality_class_to_value(self, quality_class: str) -> int:
        """将质量等级转换为数值"""
        return self.QUALITY_CLASS_ORDER.get(quality_class, 0)

    def _get_quality_multiplier(self, quality_class: str) -> float:
        """
        获取质量调整系数

        优质股可获得更高的仓位系数
        """
        multipliers = {
            'A': 1.2,   # 优质股 +20%
            'B': 1.0,   # 良好股 基准
            'C': 0.8,   # 一般股 -20%
            'JUNK': 0.0,  # 垃圾股 不允许买入
        }
        return multipliers.get(quality_class, 1.0)


# 单例实例
_water_level_manager: Optional[WaterLevelManager] = None


def get_water_level_manager(initial_capital: float = None, configs: Dict = None) -> WaterLevelManager:
    """获取 WaterLevelManager 单例实例"""
    global _water_level_manager
    if _water_level_manager is None:
        if initial_capital is None:
            raise ValueError("初始化 WaterLevelManager 需要提供 initial_capital")
        _water_level_manager = WaterLevelManager(initial_capital, configs)
    return _water_level_manager


def reset_water_level_manager():
    """重置 WaterLevelManager 单例 (用于测试)"""
    global _water_level_manager
    _water_level_manager = None
