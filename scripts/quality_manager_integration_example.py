"""
质量管理器集成示例

展示如何在 FTSeek 交易流程中集成质量管理器:
1. 买入前的质量检查
2. 水位联动的仓位控制
3. 持仓期间的止盈止损监控
4. 估值止盈触发

此脚本可作为后续集成到 data_subscriber.py 的参考
"""

from typing import Optional, Dict, Tuple
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ftseek.integration_example")


# ============================================================
# 模拟数据结构
# ============================================================

@dataclass
class Signal:
    """交易信号"""
    code: str
    action: str  # BUY / SELL
    price: float
    confidence: float = 0.5


@dataclass
class Fundamentals:
    """基本面数据"""
    roe_5y_avg: float = 0.15       # 5 年平均 ROE
    debt_ratio: float = 0.40       # 负债率
    operating_cashflow: float = 1e8  # 经营现金流
    gross_margin: float = 0.35     # 毛利率
    margin_trend: float = 0.02     # 毛利率趋势
    revenue_growth_3y: float = 0.12  # 3 年增长
    eps: float = 2.5               # 每股收益
    pe_ttm: float = 20.0           # 当前 PE
    growth_rate: float = 0.15      # 预期增长率
    book_value: float = 15.0       # 每股净资产


# ============================================================
# 质量管理器集成类
# ============================================================

class QualityAwareTradingSystem:
    """
    集成质量管理器的交易系统示例

    展示如何在买入、持仓、卖出各环节使用质量管理器
    """

    def __init__(self, initial_capital: float = 1000000):
        """
        初始化交易系统

        Args:
            initial_capital: 初始资金
        """
        logger.info("初始化质量管理器...")

        # 导入质量管理器
        from app.factors import (
            get_quality_scorer,
            get_holding_period_manager,
            get_water_level_manager,
            get_profit_loss_manager,
            get_valuation_calculator,
            QualityClass,
        )

        # 初始化各模块
        self.quality_scorer = get_quality_scorer()
        self.holding_period = get_holding_period_manager()
        self.water_level = get_water_level_manager(initial_capital=initial_capital)
        self.profit_loss = get_profit_loss_manager(water_level_manager=self.water_level)
        self.valuation = get_valuation_calculator()

        self.QualityClass = QualityClass

        # 当前持仓
        self.positions: Dict[str, Dict] = {}

        logger.info("质量管理器初始化完成")

    def on_buy_signal(self, signal: Signal) -> Optional[Dict]:
        """
        处理买入信号

        流程:
        1. 质量评分 - 过滤垃圾股
        2. 水位检查 - 确定是否允许交易
        3. 估值检查 - 确保有安全边际
        4. 计算仓位 - 根据水位和质量调整
        5. 执行买入
        6. 记录持仓

        Args:
            signal: 买入信号

        Returns:
            买入决策结果，或 None (拒绝买入)
        """
        logger.info(f"收到买入信号：{signal.code} @ {signal.price}")

        # 1. 质量评分
        quality = self._check_quality(signal.code)
        if quality is None:
            logger.warning(f"质量评分失败：{signal.code}")
            return None

        if quality.class_ == self.QualityClass.JUNK:
            logger.warning(f"质量评级为 JUNK，拒绝买入：{signal.code}")
            return None

        logger.info(f"质量评分：{quality.score} ({quality.class_.value}) - {quality.notes}")

        # 2. 水位检查
        can_trade, reason = self._check_water_level(quality.class_)
        if not can_trade:
            logger.warning(f"水位限制，拒绝买入：{signal.code} - {reason}")
            return None

        # 3. 估值检查
        valuation = self._check_valuation(signal.code, signal.price)
        if valuation is None:
            logger.warning(f"估值计算失败：{signal.code}")
            return None

        if valuation.signal.value in ('SELL', 'STRONG_SELL'):
            logger.warning(f"估值过高，拒绝买入：{signal.code} - {valuation.notes}")
            return None

        # 4. 计算仓位
        position_size = self._calculate_position_size(quality.class_, signal.price)

        # 5. 模拟执行买入
        logger.info(f"执行买入：{signal.code}, 数量={position_size}, 价格={signal.price}")

        # 6. 记录持仓
        self._record_position(signal.code, signal.price, position_size, quality.class_)

        return {
            'code': signal.code,
            'action': 'BUY',
            'price': signal.price,
            'quantity': position_size,
            'quality_class': quality.class_.value,
            'quality_score': quality.score,
            'valuation_signal': valuation.signal.value,
            'reason': f"质量{quality.class_.value}级，估值{valuation.signal.value}",
        }

    def on_price_update(self, code: str, price: float):
        """
        处理价格更新

        流程:
        1. 更新持仓价格
        2. 检查止盈止损
        3. 检查持仓时间
        4. 更新水位

        Args:
            code: 股票代码
            price: 当前价格
        """
        if code not in self.positions:
            return

        # 1. 更新持仓价格
        self.profit_loss.update_price(code, price)
        self.holding_period.update_price(code, price)

        # 2. 检查止盈止损
        exit_signal = self.profit_loss.check_exit(code, price)
        if exit_signal:
            logger.info(f"触发离场信号：{code} - {exit_signal.reason}")
            self._execute_exit(code, exit_signal)
            return

        # 检查分批止盈
        partial_signal = self.profit_loss.check_partial_exit(code)
        if partial_signal:
            logger.info(f"触发分批止盈：{code} - 止盈{partial_signal.ratio:.0%}")
            self._execute_partial_exit(code, partial_signal)

        # 3. 检查持仓时间
        should_exit, reason = self.holding_period.should_exit(code)
        if should_exit:
            logger.info(f"持仓时间到期：{code} - {reason}")
            self._execute_exit(code, {'reason': reason})
            return

        # 4. 更新水位 (根据持仓盈亏)
        self._update_water_level()

    def _check_quality(self, code: str):
        """质量检查"""
        # 模拟获取基本面数据
        fundamentals = Fundamentals()

        # 根据不同股票调整基本面数据 (模拟)
        if '00700' in code:  # 腾讯 - 优质股
            fundamentals.roe_5y_avg = 0.25
            fundamentals.debt_ratio = 0.25
            fundamentals.gross_margin = 0.45
        elif '09988' in code:  # 阿里 - 良好股
            fundamentals.roe_5y_avg = 0.15
            fundamentals.debt_ratio = 0.35
            fundamentals.gross_margin = 0.35

        return self.quality_scorer.calculate(code, {
            'roe_5y_avg': fundamentals.roe_5y_avg * 100,  # 转换为百分比
            'debt_ratio': fundamentals.debt_ratio,
            'operating_cashflow': fundamentals.operating_cashflow,
            'gross_margin': fundamentals.gross_margin,
            'margin_trend': fundamentals.margin_trend,
            'revenue_growth_3y': fundamentals.revenue_growth_3y,
        })

    def _check_water_level(self, quality_class) -> Tuple[bool, str]:
        """水位检查"""
        return self.water_level.can_trade(quality_class.value)

    def _check_valuation(self, code: str, price: float):
        """估值检查"""
        fundamentals = Fundamentals()
        fundamentals_dict = {
            'eps': fundamentals.eps,
            'pe_ttm': fundamentals.pe_ttm,
            'growth_rate': fundamentals.growth_rate,
            'book_value': fundamentals.book_value,
            'roe': fundamentals.roe_5y_avg,
        }

        # 添加 PE 历史数据 (模拟)
        for pe in [18, 20, 22, 19, 21, 23, 20, 19, 22, 20]:
            self.valuation.add_pe_history(code, pe)

        return self.valuation.calculate(code, price, fundamentals_dict)

    def _calculate_position_size(self, quality_class, price: float) -> int:
        """计算仓位大小"""
        # 获取水位允许的最大仓位
        max_position_pct = self.water_level.get_max_position(quality_class.value)

        # 模拟总资金 100 万
        total_capital = 1000000
        max_amount = total_capital * max_position_pct

        # 计算股数
        quantity = int(max_amount / price / 100) * 100  # 取整到 1 手

        logger.info(f"仓位计算：质量={quality_class.value}, 最大仓位={max_position_pct:.0%}, "
                   f"数量={quantity} 股，金额={quantity * price:.0f}")

        return quantity

    def _record_position(self, code: str, price: float, quantity: int, quality_class):
        """记录持仓"""
        self.positions[code] = {
            'code': code,
            'entry_price': price,
            'quantity': quantity,
            'quality_class': quality_class,
        }

        # 添加到管理器
        self.holding_period.add_position(code, quality_class, price)
        self.profit_loss.add_position(code, price, quantity, quality_class)

        logger.info(f"持仓记录：{code}, 价格={price}, 数量={quantity}, 质量={quality_class.value}")

    def _execute_exit(self, code: str, signal: Dict):
        """执行离场"""
        if code not in self.positions:
            return

        pos = self.positions[code]
        logger.info(f"执行离场：{code}, 原因={signal.get('reason', '未知')}")

        # 从管理器中移除
        self.holding_period.remove_position(code, signal.get('reason', ''))
        self.profit_loss.remove_position(code, signal.get('reason', ''))

        # 删除持仓记录
        del self.positions[code]

    def _execute_partial_exit(self, code: str, signal):
        """执行分批止盈"""
        if code not in self.positions:
            return

        pos = self.positions[code]
        sell_quantity = int(pos['quantity'] * signal.ratio)

        logger.info(f"分批止盈：{code}, 卖出{sell_quantity}股 ({signal.ratio:.0%}), "
                   f"价格={signal.price}, 盈亏={signal.pnl_pct:.2%}")

        # 更新持仓数量
        pos['quantity'] -= sell_quantity

        # 更新管理器
        self.profit_loss.reduce_position(code, sell_quantity)

    def _update_water_level(self):
        """更新水位"""
        # 模拟计算当前资金 (初始 + 盈亏)
        total_pnl = sum(
            self.profit_loss.get_position_info(code).get('pnl_amount', 0)
            for code in self.positions
            if self.profit_loss.get_position_info(code)
        )

        current_capital = 1000000 + total_pnl
        self.water_level.update_capital(current_capital)

        state = self.water_level.get_state()
        logger.debug(f"水位状态：{state.current_ratio:.0%}, 等级={state.current_level.value}")


# ============================================================
# 使用示例
# ============================================================

def run_example():
    """运行集成示例"""
    print("="*60)
    print("质量管理器集成示例")
    print("="*60)

    # 创建交易系统
    system = QualityAwareTradingSystem(initial_capital=1000000)

    # 模拟买入信号
    print("\n[场景 1] 买入腾讯控股 (优质股)")
    signal_tencent = Signal('HK.00700', 'BUY', 500.0, confidence=0.8)
    result = system.on_buy_signal(signal_tencent)
    if result:
        print(f"  ✓ 买入成功：{result['quantity']}股 @ {result['price']}")

    # 模拟买入信号 - 阿里
    print("\n[场景 2] 买入阿里巴巴 (良好股)")
    signal_alibaba = Signal('HK.09988', 'BUY', 100.0, confidence=0.7)
    result = system.on_buy_signal(signal_alibaba)
    if result:
        print(f"  ✓ 买入成功：{result['quantity']}股 @ {result['price']}")

    # 模拟价格上涨
    print("\n[场景 3] 腾讯价格上涨 5%")
    system.on_price_update('HK.00700', 525.0)

    # 模拟价格下跌
    print("\n[场景 4] 阿里价格下跌 3%")
    system.on_price_update('HK.09988', 97.0)

    # 打印持仓信息
    print("\n[当前持仓]")
    for code, pos in system.positions.items():
        info = system.profit_loss.get_position_info(code)
        if info:
            print(f"  {code}: {info['quantity']}股，盈亏={info['pnl_pct']:.2f}%")

    # 打印水位状态
    print("\n[资金水位]")
    state = system.water_level.get_state()
    print(f"  当前水位：{state.current_ratio:.0%}")
    print(f"  等级：{state.current_level.value}")
    print(f"  剩余风险预算：{state.remaining_daily_loss:.0f}")

    print("\n" + "="*60)
    print("示例运行完成")
    print("="*60)


if __name__ == '__main__':
    run_example()
