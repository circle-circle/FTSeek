"""
止盈止损管理器 - "落袋为安"理念融合

功能:
- 三层止盈架构：分批止盈 + 趋势止盈 + 估值止盈
- 动态止损：初始止损 → 保本止损 → 追踪止盈
- 质量等级差异化止盈
- 水位联动止盈

核心理念:
- 让利润奔跑 (Let profits run)
- 快速止损 (Cut losses quickly)
- 分批止盈 (Scale out)
- 成本价止损 (Stop loss at breakeven)
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, List
from enum import Enum
from datetime import datetime
import logging

try:
    from .quality_scorer import QualityClass
    from .water_level_manager import WaterLevel, WaterLevelManager
except ImportError:
    from app.factors.quality_scorer import QualityClass
    from app.factors.water_level_manager import WaterLevel, WaterLevelManager

logger = logging.getLogger("ftseek.profit_loss")


class ExitType(Enum):
    """离场类型"""
    STOP_LOSS = "stop_loss"           # 止损
    PARTIAL_PROFIT = "partial_profit" # 分批止盈
    TREND_EXIT = "trend_exit"         # 趋势止盈
    VALUATION_EXIT = "valuation_exit" # 估值止盈
    TIME_EXIT = "time_exit"           # 时间止盈
    QUALITY_EXIT = "quality_exit"     # 质量恶化


@dataclass
class ExitSignal:
    """离场信号"""
    code: str
    exit_type: ExitType
    ratio: float               # 离场比例 (0-1)
    price: float
    pnl_pct: float             # 盈亏比例
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict = field(default_factory=dict)


@dataclass
class PositionExitConfig:
    """持仓离场配置"""
    # 初始止损
    initial_stop_pct: float = 0.03  # 3% 初始止损

    # 保本止损
    breakeven_trigger: float = 0.03  # 盈利 3% 后触发
    breakeven_level: float = 0.01    # 止损上移至成本价 +1%

    # 追踪止盈
    trailing_trigger: float = 0.05   # 盈利 5% 后启动
    trailing_pct: float = 0.03       # 从最高点回撤 3% 止盈

    # 分批止盈
    partial_levels: List[Tuple[float, float]] = field(
        default_factory=lambda: [(0.05, 0.25), (0.10, 0.25)]
    )  # [(触发盈利，止盈比例), ...]

    # 分级止损
    tiered_stops: List[Tuple[float, float]] = field(
        default_factory=lambda: [(0.10, 0.05), (0.20, 0.12), (0.30, 0.20)]
    )  # [(盈利水平，止损锁定), ...]


class ProfitLossManager:
    """
    止盈止损管理器

    三层止盈架构:
    1. 分批止盈 (落袋为安): 5%/10% 各止盈 25%
    2. 趋势止盈 (让利润奔跑): SuperTrend/MACD 信号止盈 50%
    3. 估值止盈 (高估才卖): PE>80% 清仓

    动态止损:
    1. 初始止损：-3%
    2. 保本止损：盈利 3% 后移至成本价 +1%
    3. 追踪止盈：盈利 5% 后启动，回撤 3% 止盈
    """

    # 按质量等级的止盈配置
    QUALITY_EXIT_CONFIG = {
        QualityClass.CLASS_A: PositionExitConfig(
            initial_stop_pct=0.05,       # 优质股给更多波动空间
            breakeven_trigger=0.05,
            breakeven_level=0.02,
            trailing_trigger=0.10,
            trailing_pct=0.05,
            partial_levels=[],           # 优质股不分批，一次性持有
            tiered_stops=[(0.15, 0.08), (0.25, 0.15)],
        ),
        QualityClass.CLASS_B: PositionExitConfig(
            initial_stop_pct=0.03,
            breakeven_trigger=0.03,
            breakeven_level=0.01,
            trailing_trigger=0.05,
            trailing_pct=0.03,
            partial_levels=[(0.10, 0.30), (0.20, 0.30)],
            tiered_stops=[(0.10, 0.05), (0.20, 0.12)],
        ),
        QualityClass.CLASS_C: PositionExitConfig(
            initial_stop_pct=0.03,
            breakeven_trigger=0.03,
            breakeven_level=0.01,
            trailing_trigger=0.05,
            trailing_pct=0.03,
            partial_levels=[(0.10, 1.0)],  # 一次性止盈
            tiered_stops=[],
        ),
        QualityClass.JUNK: PositionExitConfig(
            initial_stop_pct=0.02,       # 垃圾股 tighter 止损
            breakeven_trigger=0.02,
            breakeven_level=0.005,
            trailing_trigger=0.03,
            trailing_pct=0.02,
            partial_levels=[(0.05, 1.0)],  # 快速止盈
            tiered_stops=[],
        ),
    }

    def __init__(
        self,
        water_level_manager: WaterLevelManager = None,
        config: Dict = None
    ):
        """
        初始化止盈止损管理器

        Args:
            water_level_manager: 水位管理器 (可选)
            config: 配置字典
        """
        self._positions: Dict[str, Dict] = {}  # code -> position info
        self._water_level = water_level_manager
        self._config = config or {}

        logger.info("ProfitLossManager 初始化完成")

    def add_position(
        self,
        code: str,
        entry_price: float,
        quantity: int,
        quality_class: QualityClass,
        entry_time: datetime = None
    ):
        """
        添加持仓记录

        Args:
            code: 股票代码
            entry_price: 买入价格
            quantity: 买入数量
            quality_class: 质量等级
            entry_time: 买入时间
        """
        if entry_time is None:
            entry_time = datetime.now()

        # 获取该质量等级的止盈配置
        exit_config = self.QUALITY_EXIT_CONFIG.get(
            quality_class,
            self.QUALITY_EXIT_CONFIG[QualityClass.CLASS_C]
        )

        self._positions[code] = {
            'code': code,
            'entry_price': entry_price,
            'quantity': quantity,
            'remaining_quantity': quantity,
            'quality_class': quality_class,
            'entry_time': entry_time,
            'exit_config': exit_config,
            'current_price': entry_price,
            'peak_price': entry_price,
            'pnl_pct': 0.0,
            'pnl_amount': 0.0,
            'stop_loss_price': entry_price * (1 - exit_config.initial_stop_pct),
            'breakeven_triggered': False,
            'trailing_triggered': False,
            'partial_exits': [],  # 已执行的分批止盈记录
            'notes': "",
        }

        logger.info(
            f"新建持仓：{code}, 价格={entry_price:.2f}, "
            f"质量={quality_class.value}, 初始止损={exit_config.initial_stop_pct:.0%}"
        )

    def update_price(self, code: str, price: float):
        """
        更新持仓价格

        Args:
            code: 股票代码
            price: 当前价格
        """
        if code not in self._positions:
            return

        pos = self._positions[code]
        old_price = pos['current_price']
        pos['current_price'] = price

        # 更新最高价 (用于追踪止盈)
        if price > pos['peak_price']:
            pos['peak_price'] = price

        # 更新盈亏
        pos['pnl_pct'] = (price - pos['entry_price']) / pos['entry_price']
        pos['pnl_amount'] = (price - pos['entry_price']) * pos['remaining_quantity']

        # 动态调整止损价
        self._update_stop_loss(code)

    def _update_stop_loss(self, code: str):
        """动态调整止损价"""
        pos = self._positions[code]
        config = pos['exit_config']
        pnl_pct = pos['pnl_pct']

        # 1. 检查是否触发保本止损
        if not pos['breakeven_triggered']:
            if pnl_pct >= config.breakeven_trigger:
                pos['stop_loss_price'] = pos['entry_price'] * (1 + config.breakeven_level)
                pos['breakeven_triggered'] = True
                logger.debug(f"{code} 触发保本止损，止损价={pos['stop_loss_price']:.2f}")

        # 2. 检查是否触发追踪止盈
        if not pos['trailing_triggered']:
            if pnl_pct >= config.trailing_trigger:
                pos['trailing_triggered'] = True
                logger.debug(f"{code} 触发追踪止盈")

        # 3. 追踪止盈逻辑
        if pos['trailing_triggered']:
            # 止损价 = 最高价 × (1 - trailing_pct)
            trailing_stop = pos['peak_price'] * (1 - config.trailing_pct)
            if trailing_stop > pos['stop_loss_price']:
                pos['stop_loss_price'] = trailing_stop

        # 4. 分级止损逻辑
        for profit_level, stop_level in config.tiered_stops:
            if pnl_pct >= profit_level:
                new_stop = pos['entry_price'] * (1 + stop_level)
                if new_stop > pos['stop_loss_price']:
                    pos['stop_loss_price'] = new_stop
                    logger.debug(
                        f"{code} 触发分级止损 (盈利{profit_level:.0%}), "
                        f"止损价={new_stop:.2f}"
                    )

    def check_exit(self, code: str, current_price: float) -> Optional[ExitSignal]:
        """
        检查是否应该离场

        Args:
            code: 股票代码
            current_price: 当前价格

        Returns:
            ExitSignal 离场信号，或 None
        """
        if code not in self._positions:
            return None

        pos = self._positions[code]
        config = pos['exit_config']
        pnl_pct = pos['pnl_pct']

        # 1. 检查止损
        if current_price <= pos['stop_loss_price']:
            return ExitSignal(
                code=code,
                exit_type=ExitType.STOP_LOSS,
                ratio=1.0,
                price=current_price,
                pnl_pct=pnl_pct,
                reason=f"触发止损价 {pos['stop_loss_price']:.2f}",
                metadata={'stop_price': pos['stop_loss_price']},
            )

        # 2. 检查分批止盈
        for trigger_profit, exit_ratio in config.partial_levels:
            # 检查是否已执行过该级别的止盈
            already_executed = any(
                abs(record['trigger'] - trigger_profit) < 0.001
                for record in pos['partial_exits']
            )

            if not already_executed and pnl_pct >= trigger_profit:
                # 记录已执行
                pos['partial_exits'].append({
                    'trigger': trigger_profit,
                    'ratio': exit_ratio,
                    'time': datetime.now(),
                    'price': current_price,
                })

                return ExitSignal(
                    code=code,
                    exit_type=ExitType.PARTIAL_PROFIT,
                    ratio=exit_ratio,
                    price=current_price,
                    pnl_pct=pnl_pct,
                    reason=f"达到止盈目标 {trigger_profit:.0%}",
                    metadata={'trigger_profit': trigger_profit},
                )

        # 3. 检查时间止盈 (由外部 HoldingPeriodManager 判断)
        # 这里只返回信号，不主动判断

        return None

    def check_partial_exit(self, code: str) -> Optional[ExitSignal]:
        """
        检查是否触发分批止盈

        Returns:
            ExitSignal 或 None
        """
        if code not in self._positions:
            return None

        pos = self._positions[code]
        config = pos['exit_config']
        pnl_pct = pos['pnl_pct']
        current_price = pos['current_price']

        for trigger_profit, exit_ratio in config.partial_levels:
            already_executed = any(
                abs(record['trigger'] - trigger_profit) < 0.001
                for record in pos['partial_exits']
            )

            if not already_executed and pnl_pct >= trigger_profit:
                pos['partial_exits'].append({
                    'trigger': trigger_profit,
                    'ratio': exit_ratio,
                    'time': datetime.now(),
                    'price': current_price,
                })

                return ExitSignal(
                    code=code,
                    exit_type=ExitType.PARTIAL_PROFIT,
                    ratio=exit_ratio,
                    price=current_price,
                    pnl_pct=pnl_pct,
                    reason=f"落袋为安：达到{trigger_profit:.0%}止盈",
                )

        return None

    def on_trend_signal(self, code: str, signal_type: str) -> Optional[ExitSignal]:
        """
        处理趋势信号 (SuperTrend/MACD 转向)

        Args:
            code: 股票代码
            signal_type: 信号类型 ("supertrend_turn", "macd_dead_cross")

        Returns:
            ExitSignal 或 None
        """
        if code not in self._positions:
            return None

        pos = self._positions[code]

        if pos['remaining_quantity'] <= 0:
            return None

        return ExitSignal(
            code=code,
            exit_type=ExitType.TREND_EXIT,
            ratio=0.50,  # 趋势止盈通常为 50%
            price=pos['current_price'],
            pnl_pct=pos['pnl_pct'],
            reason=f"趋势转向信号：{signal_type}",
            metadata={'signal_type': signal_type},
        )

    def on_valuation_signal(self, code: str, pe_percentile: float) -> Optional[ExitSignal]:
        """
        处理估值信号

        Args:
            code: 股票代码
            pe_percentile: PE 历史分位点

        Returns:
            ExitSignal 或 None
        """
        if code not in self._positions:
            return None

        pos = self._positions[code]

        # 只有优质股才使用估值止盈
        if pos['quality_class'] != QualityClass.CLASS_A:
            return None

        if pe_percentile <= 0.80:
            return None

        return ExitSignal(
            code=code,
            exit_type=ExitType.VALUATION_EXIT,
            ratio=1.0,
            price=pos['current_price'],
            pnl_pct=pos['pnl_pct'],
            reason=f"估值高估：PE 分位点{pe_percentile:.0%} > 80%",
            metadata={'pe_percentile': pe_percentile},
        )

    def on_quality_deterioration(self, code: str) -> Optional[ExitSignal]:
        """
        处理质量恶化信号

        Args:
            code: 股票代码

        Returns:
            ExitSignal 或 None
        """
        if code not in self._positions:
            return None

        pos = self._positions[code]

        return ExitSignal(
            code=code,
            exit_type=ExitType.QUALITY_EXIT,
            ratio=1.0,
            price=pos['current_price'],
            pnl_pct=pos['pnl_pct'],
            reason="基本面质量恶化",
        )

    def reduce_position(self, code: str, sold_quantity: int):
        """
        减少持仓数量 (止盈/止损后调用)

        Args:
            code: 股票代码
            sold_quantity: 卖出数量
        """
        if code not in self._positions:
            return

        pos = self._positions[code]
        pos['remaining_quantity'] -= sold_quantity

        if pos['remaining_quantity'] <= 0:
            pos['remaining_quantity'] = 0
            logger.info(f"{code} 已清仓")

    def get_position_info(self, code: str) -> Optional[Dict]:
        """获取持仓信息"""
        if code not in self._positions:
            return None

        pos = self._positions[code]
        config = pos['exit_config']

        return {
            'code': code,
            'entry_price': pos['entry_price'],
            'current_price': pos['current_price'],
            'peak_price': pos['peak_price'],
            'pnl_pct': round(pos['pnl_pct'] * 100, 2),
            'pnl_amount': round(pos['pnl_amount'], 2),
            'quantity': pos['quantity'],
            'remaining_quantity': pos['remaining_quantity'],
            'stop_loss_price': round(pos['stop_loss_price'], 2),
            'breakeven_triggered': pos['breakeven_triggered'],
            'trailing_triggered': pos['trailing_triggered'],
            'quality_class': pos['quality_class'].value,
            'partial_exits': pos['partial_exits'],
            'exit_config_summary': {
                'initial_stop': config.initial_stop_pct,
                'breakeven_trigger': config.breakeven_trigger,
                'trailing_trigger': config.trailing_trigger,
                'partial_levels': config.partial_levels,
            },
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
            logger.info(
                f"移除持仓：{code}, 原因={reason or '未知'}, "
                f"盈亏={pos['pnl_pct']:.2%}"
            )
            del self._positions[code]


# 单例实例
_profit_loss_manager: Optional[ProfitLossManager] = None


def get_profit_loss_manager(
    water_level_manager: WaterLevelManager = None,
    config: Dict = None
) -> ProfitLossManager:
    """获取 ProfitLossManager 单例实例"""
    global _profit_loss_manager
    if _profit_loss_manager is None:
        _profit_loss_manager = ProfitLossManager(water_level_manager, config)
    return _profit_loss_manager


def reset_profit_loss_manager():
    """重置 ProfitLossManager 单例 (用于测试)"""
    global _profit_loss_manager
    _profit_loss_manager = None
