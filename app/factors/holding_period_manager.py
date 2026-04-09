"""
持仓周期管理器 - 基于质量等级的差异化持仓管理

功能:
- 根据质量等级设定持仓期限
- 质量复查机制
- 恶化时离场决策

持仓期限配置:
- Class A (80+): 长期持有，无时间限制，仅估值止盈
- Class B (60-79): 中期持有，7 天 (168 小时)
- Class C (40-59): 短期持有，48 小时
- JUNK (<40): 尽快离场，24 小时
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging

try:
    from .quality_scorer import QualityClass, QualityScore
except ImportError:
    from app.factors.quality_scorer import QualityClass, QualityScore

logger = logging.getLogger("ftseek.holding_period")


class HoldingTier(Enum):
    """持仓期限等级"""
    LONG_TERM = "long"     # 长期 (无限制，仅估值止盈)
    MID_TERM = "mid"       # 中期 (7 天)
    SHORT_TERM = "short"   # 短期 (48 小时)
    EXIT_NOW = "exit"      # 立即离场


@dataclass
class HoldingPeriodConfig:
    """持仓期限配置"""
    tier: HoldingTier
    max_hours: Optional[int]  # None=无限制
    review_interval_hours: int  # 复查间隔
    exit_on_deterioration: bool  # 恶化时离场
    allow_extension: bool  # 是否允许延期


@dataclass
class PositionRecord:
    """持仓记录"""
    code: str
    entry_time: datetime
    entry_price: float
    quality_class: QualityClass
    current_price: float = 0.0
    last_review: Optional[datetime] = None
    quality_history: List[QualityScore] = field(default_factory=list)
    notes: str = ""


class HoldingPeriodManager:
    """
    持仓周期管理器

    根据质量等级设定不同的持仓期限:
    - Class A: 长期持有，移除时间止盈
    - Class B: 中期持有，7 天
    - Class C: 短期持有，48 小时
    - JUNK: 尽快离场，24 小时
    """

    # 默认配置
    DEFAULT_CONFIG = {
        QualityClass.CLASS_A: HoldingPeriodConfig(
            tier=HoldingTier.LONG_TERM,
            max_hours=None,  # 无限制
            review_interval_hours=24,
            exit_on_deterioration=True,
            allow_extension=True,
        ),
        QualityClass.CLASS_B: HoldingPeriodConfig(
            tier=HoldingTier.MID_TERM,
            max_hours=168,  # 7 天
            review_interval_hours=12,
            exit_on_deterioration=True,
            allow_extension=True,
        ),
        QualityClass.CLASS_C: HoldingPeriodConfig(
            tier=HoldingTier.SHORT_TERM,
            max_hours=48,
            review_interval_hours=6,
            exit_on_deterioration=True,
            allow_extension=False,
        ),
        QualityClass.JUNK: HoldingPeriodConfig(
            tier=HoldingTier.EXIT_NOW,
            max_hours=24,  # 尽快离场
            review_interval_hours=4,
            exit_on_deterioration=True,
            allow_extension=False,
        ),
    }

    def __init__(self, config: Dict = None):
        """
        初始化持仓周期管理器

        Args:
            config: 可选的配置字典，可覆盖默认持仓期限配置
        """
        self._positions: Dict[str, PositionRecord] = {}
        self._config = self.DEFAULT_CONFIG.copy()

        if config:
            self._update_config(config)

        logger.info("HoldingPeriodManager 初始化完成")

    def _update_config(self, config: Dict):
        """更新配置"""
        if 'holding_periods' in config:
            for class_name, period_config in config['holding_periods'].items():
                class_ = QualityClass[class_name]
                if class_:
                    self._config[class_] = HoldingPeriodConfig(
                        tier=HoldingTier(period_config.get('tier', 'short')),
                        max_hours=period_config.get('max_hours'),
                        review_interval_hours=period_config.get('review_interval_hours', 24),
                        exit_on_deterioration=period_config.get('exit_on_deterioration', True),
                        allow_extension=period_config.get('allow_extension', False),
                    )

    def add_position(
        self,
        code: str,
        quality_class: QualityClass,
        entry_price: float,
        entry_time: datetime = None
    ):
        """
        添加持仓记录

        Args:
            code: 股票代码
            quality_class: 质量等级
            entry_price: 买入价格
            entry_time: 买入时间 (默认当前时间)
        """
        if entry_time is None:
            entry_time = datetime.now()

        self._positions[code] = PositionRecord(
            code=code,
            entry_time=entry_time,
            entry_price=entry_price,
            quality_class=quality_class,
            last_review=entry_time,
        )

        config = self.get_config(quality_class)
        logger.info(
            f"新建持仓：{code}, 等级={quality_class.value}, "
            f"期限={config.max_hours or '无限制'}h, 价格={entry_price:.2f}"
        )

    def update_price(self, code: str, price: float):
        """更新持仓价格"""
        if code in self._positions:
            self._positions[code].current_price = price

    def update_quality(
        self,
        code: str,
        new_quality: QualityClass,
        new_score: QualityScore = None
    ) -> Tuple[bool, str]:
        """
        更新持仓的质量等级

        Args:
            code: 股票代码
            new_quality: 新的质量等级
            new_score: 新的质量评分 (可选)

        Returns:
            (是否恶化，原因)
        """
        if code not in self._positions:
            return False, ""

        pos = self._positions[code]
        old_quality = pos.quality_class
        pos.quality_class = new_quality

        if new_score:
            pos.quality_history.append(new_score)

        # 判断是否恶化
        if self._is_deteriorated(old_quality, new_quality):
            logger.warning(
                f"{code} 质量恶化：{old_quality.value} -> {new_quality.value}"
            )
            return True, f"质量等级下降：{old_quality.value} -> {new_quality.value}"

        # 记录复查
        pos.last_review = datetime.now()

        return False, ""

    def should_exit(self, code: str) -> Tuple[bool, str]:
        """
        判断是否应该离场

        Returns:
            (是否离场，原因)

        离场条件:
        1. 质量恶化 (最高优先级)
        2. 持仓时间到期
        3. JUNK 级别
        """
        if code not in self._positions:
            return False, ""

        pos = self._positions[code]
        config = self.get_config(pos.quality_class)

        # JUNK 级别直接建议离场
        if pos.quality_class == QualityClass.JUNK:
            return True, "质量评级为 JUNK (垃圾企业)"

        # 检查时间到期
        if config.max_hours is not None:
            elapsed = datetime.now() - pos.entry_time
            hours_elapsed = elapsed.total_seconds() / 3600

            if hours_elapsed >= config.max_hours:
                return True, f"持仓时间到期 ({hours_elapsed:.1f}/{config.max_hours}小时)"

        return False, ""

    def needs_review(self, code: str) -> bool:
        """是否需要质量复查"""
        if code not in self._positions:
            return False

        pos = self._positions[code]
        config = self.get_config(pos.quality_class)

        if pos.last_review is None:
            return True

        elapsed = datetime.now() - pos.last_review
        hours_elapsed = elapsed.total_seconds() / 3600

        return hours_elapsed >= config.review_interval_hours

    def get_position_info(self, code: str) -> Optional[Dict]:
        """
        获取持仓信息

        Returns:
            持仓信息字典，包括:
            - code: 股票代码
            - entry_time: 买入时间
            - holding_hours: 已持仓小时数
            - max_hours: 最大持仓小时数
            - tier: 持仓期限等级
            - quality_class: 质量等级
            - pnl_pct: 盈亏比例
            - needs_review: 是否需要复查
            - should_exit: 是否应该离场
        """
        if code not in self._positions:
            return None

        pos = self._positions[code]
        elapsed = datetime.now() - pos.entry_time
        hours = elapsed.total_seconds() / 3600

        config = self.get_config(pos.quality_class)
        pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price if pos.current_price > 0 else 0

        return {
            'code': code,
            'entry_time': pos.entry_time.isoformat(),
            'holding_hours': round(hours, 2),
            'max_hours': config.max_hours,
            'tier': config.tier.value,
            'quality_class': pos.quality_class.value,
            'pnl_pct': round(pnl_pct * 100, 2),
            'needs_review': self.needs_review(code),
            'should_exit': self.should_exit(code)[0],
        }

    def get_all_positions(self) -> List[Dict]:
        """获取所有持仓信息"""
        return [
            info for info in
            (self.get_position_info(code) for code in list(self._positions.keys()))
            if info is not None
        ]

    def remove_position(self, code: str, reason: str = ""):
        """移除持仓记录"""
        if code in self._positions:
            pos = self._positions[code]
            logger.info(f"移除持仓：{code}, 原因={reason or '未知'}")
            del self._positions[code]

    def get_config(self, quality_class: QualityClass) -> HoldingPeriodConfig:
        """获取指定质量等级的配置"""
        return self._config.get(quality_class, self.DEFAULT_CONFIG[QualityClass.CLASS_C])

    def _is_deteriorated(self, old: QualityClass, new: QualityClass) -> bool:
        """判断质量是否恶化"""
        class_order = {
            QualityClass.CLASS_A: 4,
            QualityClass.CLASS_B: 3,
            QualityClass.CLASS_C: 2,
            QualityClass.JUNK: 1,
        }
        return class_order.get(new, 0) < class_order.get(old, 0)

    def get_expiry_alerts(self) -> List[Dict]:
        """
        获取即将到期的持仓预警

        Returns:
            即将到期 (24 小时内) 的持仓列表
        """
        alerts = []
        now = datetime.now()

        for code, pos in self._positions.items():
            config = self.get_config(pos.quality_class)

            if config.max_hours is None:
                continue  # 无限制

            elapsed = now - pos.entry_time
            hours_elapsed = elapsed.total_seconds() / 3600
            hours_remaining = config.max_hours - hours_elapsed

            if 0 < hours_remaining <= 24:
                alerts.append({
                    'code': code,
                    'quality_class': pos.quality_class.value,
                    'hours_remaining': round(hours_remaining, 2),
                    'expiry_time': pos.entry_time + timedelta(hours=config.max_hours),
                    'pnl_pct': round(
                        (pos.current_price - pos.entry_price) / pos.entry_price * 100
                        if pos.current_price > 0 else 0,
                        2
                    ),
                })

        return sorted(alerts, key=lambda x: x['hours_remaining'])


# 单例实例
_holding_period_manager: Optional[HoldingPeriodManager] = None


def get_holding_period_manager(config: Dict = None) -> HoldingPeriodManager:
    """获取 HoldingPeriodManager 单例实例"""
    global _holding_period_manager
    if _holding_period_manager is None:
        _holding_period_manager = HoldingPeriodManager(config)
    return _holding_period_manager


def reset_holding_period_manager():
    """重置 HoldingPeriodManager 单例 (用于测试)"""
    global _holding_period_manager
    _holding_period_manager = None
