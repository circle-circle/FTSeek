"""
估值因子 - 安全边际计算

功能:
- 计算内在价值 (简化 DCF 模型)
- 计算 PE 历史分位点
- 计算安全边际
- 生成估值信号

基于巴菲特理念:
- 价格是你付出的，价值是你得到的
- 用 4 毛钱买 1 块钱的东西 (安全边际 60%)
- 只在有安全边际时买入
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("ftseek.valuation")


class ValuationSignal(Enum):
    """估值信号"""
    STRONG_BUY = "strong_buy"    # 强烈买入 (安全边际 > 50%)
    BUY = "buy"                  # 买入 (安全边际 30-50%)
    HOLD = "hold"                # 持有 (安全边际 -10% 到 30%)
    SELL = "sell"                # 卖出 (高估 10-30%)
    STRONG_SELL = "strong_sell"  # 强烈卖出 (高估 > 30%)


@dataclass
class ValuationResult:
    """估值计算结果"""
    code: str
    current_price: float

    # 内在价值
    intrinsic_value: float           # DCF 计算的内在价值
    intrinsic_value_graham: float    # 格雷厄姆公式估值
    intrinsic_value_avg: float       # 平均内在价值

    # PE 分析
    current_pe: float                # 当前 PE
    historical_pe_avg: float         # 历史平均 PE
    historical_pe_percentile: float  # PE 历史分位点 (0-1)

    # 安全边际
    margin_of_safety: float          # 安全边际 (相对于内在价值)
    margin_of_safety_pct: float      # 安全边际百分比

    # 估值信号
    signal: ValuationSignal          # 估值信号
    confidence: float                # 置信度 (0-1)

    # 备注
    notes: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'code': self.code,
            'current_price': round(self.current_price, 2),
            'intrinsic_value': round(self.intrinsic_value, 2),
            'intrinsic_value_graham': round(self.intrinsic_value_graham, 2),
            'intrinsic_value_avg': round(self.intrinsic_value_avg, 2),
            'current_pe': round(self.current_pe, 2),
            'historical_pe_avg': round(self.historical_pe_avg, 2),
            'historical_pe_percentile': round(self.historical_pe_percentile * 100, 1),
            'margin_of_safety': round(self.margin_of_safety, 2),
            'margin_of_safety_pct': round(self.margin_of_safety_pct * 100, 2),
            'signal': self.signal.value,
            'confidence': round(self.confidence * 100, 1),
            'notes': self.notes,
        }


class ValuationCalculator:
    """
    估值计算器

    提供多种估值方法:
    1. DCF 折现现金流模型
    2. 格雷厄姆公式
    3. PE 历史分位点
    """

    # DCF 默认参数
    DEFAULT_DCF_YEARS = 5             # 预测年限
    DEFAULT_DISCOUNT_RATE = 0.10      # 折现率 10%
    DEFAULT_TERMINAL_PE = 15          # 终值 PE
    DEFAULT_GROWTH_RATE = 0.10        # 默认增长率

    # 估值信号阈值
    SIGNAL_THRESHOLDS = {
        'strong_buy': 0.50,    # 安全边际 > 50%
        'buy': 0.30,           # 安全边际 > 30%
        'hold_low': -0.10,     # 安全边际 > -10%
        'hold_high': 0.30,     # 安全边际 < 30%
        'sell': 0.30,          # 高估 > 30%
        # > 30% = strong_sell
    }

    def __init__(self, config: Dict = None):
        """
        初始化估值计算器

        Args:
            config: 配置字典
        """
        self._pe_history: Dict[str, List[float]] = {}  # code -> [pe_values]
        self._config = config or {}

        # 加载配置
        if 'dcf_years' in self._config:
            self.DEFAULT_DCF_YEARS = self._config['dcf_years']
        if 'discount_rate' in self._config:
            self.DEFAULT_DISCOUNT_RATE = self._config['discount_rate']
        if 'terminal_pe' in self._config:
            self.DEFAULT_TERMINAL_PE = self._config['terminal_pe']

        logger.info(
            f"估值计算器初始化完成 | "
            f"DCF 年限={self.DEFAULT_DCF_YEARS}年，折现率={self.DEFAULT_DISCOUNT_RATE:.0%}"
        )

    def add_pe_history(self, code: str, pe: float):
        """
        添加 PE 历史记录

        Args:
            code: 股票代码
            pe: PE 值
        """
        if code not in self._pe_history:
            self._pe_history[code] = []

        self._pe_history[code].append(pe)

        # 保留最近 100 条数据
        if len(self._pe_history[code]) > 100:
            self._pe_history[code] = self._pe_history[code][-100:]

    def calculate(
        self,
        code: str,
        current_price: float,
        fundamentals: Dict
    ) -> Optional[ValuationResult]:
        """
        计算估值

        Args:
            code: 股票代码
            current_price: 当前价格
            fundamentals: 基本面数据
                - eps: 每股收益
                - pe_ttm: 当前 PE
                - growth_rate: 预期增长率
                - book_value: 每股净资产
                - roe: 净资产收益率

        Returns:
            ValuationResult 估值结果
        """
        eps = fundamentals.get('eps', 0)
        pe_ttm = fundamentals.get('pe_ttm', 0)
        growth_rate = fundamentals.get('growth_rate', self.DEFAULT_GROWTH_RATE)
        book_value = fundamentals.get('book_value', 0)
        roe = fundamentals.get('roe', 0)

        # 1. DCF 估值
        intrinsic_value_dcf = self._calculate_dcf(eps, growth_rate)

        # 2. 格雷厄姆估值
        intrinsic_value_graham = self._calculate_graham(eps, book_value, growth_rate)

        # 3. 平均内在价值
        if intrinsic_value_dcf > 0 and intrinsic_value_graham > 0:
            intrinsic_value_avg = (intrinsic_value_dcf + intrinsic_value_graham) / 2
        elif intrinsic_value_dcf > 0:
            intrinsic_value_avg = intrinsic_value_dcf
        elif intrinsic_value_graham > 0:
            intrinsic_value_avg = intrinsic_value_graham
        else:
            intrinsic_value_avg = 0

        # 4. PE 历史分位点
        pe_percentile = self._calculate_pe_percentile(code, pe_ttm)
        historical_pe_avg = self._get_historical_pe_avg(code)

        # 5. 安全边际
        if intrinsic_value_avg > 0:
            margin_of_safety = intrinsic_value_avg - current_price
            margin_of_safety_pct = margin_of_safety / current_price
        else:
            margin_of_safety = 0
            margin_of_safety_pct = 0

        # 6. 估值信号
        signal, confidence = self._determine_signal(
            margin_of_safety_pct,
            pe_percentile,
            intrinsic_value_avg
        )

        # 7. 生成备注
        notes = self._generate_notes(
            code, signal, margin_of_safety_pct, pe_percentile,
            intrinsic_value_dcf, intrinsic_value_graham
        )

        return ValuationResult(
            code=code,
            current_price=current_price,
            intrinsic_value=intrinsic_value_dcf,
            intrinsic_value_graham=intrinsic_value_graham,
            intrinsic_value_avg=intrinsic_value_avg,
            current_pe=pe_ttm,
            historical_pe_avg=historical_pe_avg if historical_pe_avg > 0 else pe_ttm,
            historical_pe_percentile=pe_percentile,
            margin_of_safety=margin_of_safety,
            margin_of_safety_pct=margin_of_safety_pct,
            signal=signal,
            confidence=confidence,
            notes=notes,
        )

    def _calculate_dcf(
        self,
        eps: float,
        growth_rate: float,
        years: int = None,
        discount_rate: float = None,
        terminal_pe: int = None
    ) -> float:
        """
        DCF 折现现金流估值

        公式：内在价值 = 未来 N 年收益总和 + 终值

        Args:
            eps: 每股收益
            growth_rate: 预期增长率
            years: 预测年限
            discount_rate: 折现率
            terminal_pe: 终值 PE

        Returns:
            内在价值
        """
        if eps <= 0:
            return 0

        years = years or self.DEFAULT_DCF_YEARS
        discount_rate = discount_rate or self.DEFAULT_DISCOUNT_RATE
        terminal_pe = terminal_pe or self.DEFAULT_TERMINAL_PE

        total_value = 0

        # 计算未来 N 年收益的折现值
        for i in range(1, years + 1):
            future_eps = eps * (1 + growth_rate) ** i
            discounted = future_eps / (1 + discount_rate) ** i
            total_value += discounted

        # 加上终值
        terminal_value = eps * (1 + growth_rate) ** years * terminal_pe
        terminal_value_discounted = terminal_value / (1 + discount_rate) ** years
        total_value += terminal_value_discounted

        return total_value

    def _calculate_graham(
        self,
        eps: float,
        book_value: float,
        growth_rate: float = 0
    ) -> float:
        """
        格雷厄姆估值公式

        简化公式：V = EPS × (8.5 + 2g)
        完整公式：V = (EPS × (8.5 + 2g) × 4.4) / Y

        其中:
        - EPS: 每股收益
        - g: 预期增长率 (%)
        - 8.5: 零增长 PE 基准
        - 4.4: 1962 年美国 AAA 债券利率
        - Y: 当前 AAA 债券利率 (简化为 4.4)

        Args:
            eps: 每股收益
            book_value: 每股净资产
            growth_rate: 预期增长率 (0-1)

        Returns:
            格雷厄姆估值
        """
        if eps <= 0:
            return 0

        # 简化公式
        g = growth_rate * 100  # 转换为百分比
        value = eps * (8.5 + 2 * g)

        # 如果提供了净资产，使用增强公式
        if book_value > 0:
            # 格雷厄姆 - 多德公式
            value = max(value, book_value)  # 至少不低于净资产

        return value

    def _calculate_pe_percentile(self, code: str, current_pe: float) -> float:
        """
        计算 PE 历史分位点

        Args:
            code: 股票代码
            current_pe: 当前 PE

        Returns:
            PE 历史分位点 (0-1)，表示当前 PE 高于历史多少比例
        """
        history = self._pe_history.get(code, [])

        if not history:
            return 0.5  # 无历史数据，返回中位数

        count_below = sum(1 for pe in history if pe < current_pe)
        return count_below / len(history)

    def _get_historical_pe_avg(self, code: str) -> float:
        """获取历史 PE 平均值"""
        history = self._pe_history.get(code, [])
        if not history:
            return 0
        return sum(history) / len(history)

    def _determine_signal(
        self,
        margin_of_safety_pct: float,
        pe_percentile: float,
        intrinsic_value: float
    ) -> Tuple[ValuationSignal, float]:
        """
        确定估值信号

        Args:
            margin_of_safety_pct: 安全边际百分比
            pe_percentile: PE 历史分位点
            intrinsic_value: 内在价值

        Returns:
            (估值信号，置信度)
        """
        confidence = 0.5  # 基础置信度

        # 基于安全边际判断信号
        if margin_of_safety_pct >= self.SIGNAL_THRESHOLDS['strong_buy']:
            signal = ValuationSignal.STRONG_BUY
            confidence = 0.9
        elif margin_of_safety_pct >= self.SIGNAL_THRESHOLDS['buy']:
            signal = ValuationSignal.BUY
            confidence = 0.7 + (margin_of_safety_pct - 0.3) * 2
        elif margin_of_safety_pct >= self.SIGNAL_THRESHOLDS['hold_low']:
            signal = ValuationSignal.HOLD
            confidence = 0.5
        elif margin_of_safety_pct >= self.SIGNAL_THRESHOLDS['sell']:
            signal = ValuationSignal.SELL
            confidence = 0.6 + (margin_of_safety_pct + 0.1) * 2
        else:
            signal = ValuationSignal.STRONG_SELL
            confidence = 0.8

        # PE 分位点调整置信度
        if pe_percentile > 0.80:  # PE 处于历史高位
            if signal in (ValuationSignal.BUY, ValuationSignal.STRONG_BUY):
                confidence *= 0.7  # 降低买入置信度
            elif signal in (ValuationSignal.SELL, ValuationSignal.STRONG_SELL):
                confidence *= 1.2  # 提高卖出置信度

        # 内在价值为 0 时降低置信度
        if intrinsic_value <= 0:
            confidence *= 0.5

        confidence = min(1.0, max(0.0, confidence))

        return signal, confidence

    def _generate_notes(
        self,
        code: str,
        signal: ValuationSignal,
        margin_of_safety_pct: float,
        pe_percentile: float,
        dcf_value: float,
        graham_value: float
    ) -> str:
        """生成备注信息"""
        notes = []

        # 信号说明
        signal_desc = {
            ValuationSignal.STRONG_BUY: "强烈买入",
            ValuationSignal.BUY: "买入",
            ValuationSignal.HOLD: "持有",
            ValuationSignal.SELL: "卖出",
            ValuationSignal.STRONG_SELL: "强烈卖出",
        }
        notes.append(f"信号：{signal_desc.get(signal, '未知')}")

        # 安全边际说明
        if margin_of_safety_pct > 0.5:
            notes.append(f"安全边际充足 ({margin_of_safety_pct:.0%})")
        elif margin_of_safety_pct > 0.2:
            notes.append(f"安全边际合理 ({margin_of_safety_pct:.0%})")
        elif margin_of_safety_pct > 0:
            notes.append(f"安全边际较低 ({margin_of_safety_pct:.0%})")
        else:
            notes.append(f"高估 ({abs(margin_of_safety_pct):.0%})")

        # PE 分位点说明
        if pe_percentile > 0.8:
            notes.append(f"PE 处于历史高位 ({pe_percentile:.0%})")
        elif pe_percentile > 0.6:
            notes.append(f"PE 处于中高水平 ({pe_percentile:.0%})")
        elif pe_percentile > 0.4:
            notes.append(f"PE 处于中等水平 ({pe_percentile:.0%})")
        elif pe_percentile > 0.2:
            notes.append(f"PE 处于中低水平 ({pe_percentile:.0%})")
        else:
            notes.append(f"PE 处于历史低位 ({pe_percentile:.0%})")

        # 估值方法说明
        if dcf_value > 0 and graham_value > 0:
            notes.append(f"DCF={dcf_value:.1f}, 格雷厄姆={graham_value:.1f}")

        return "; ".join(notes)

    def get_fair_value_range(
        self,
        code: str,
        intrinsic_value: float
    ) -> Tuple[float, float]:
        """
        计算合理估值区间

        Args:
            code: 股票代码
            intrinsic_value: 内在价值

        Returns:
            (合理下限，合理上限)
        """
        # 合理区间：内在价值的 ±20%
        lower = intrinsic_value * 0.8
        upper = intrinsic_value * 1.2
        return lower, upper


# 单例实例
_valuation_calculator: Optional[ValuationCalculator] = None


def get_valuation_calculator(config: Dict = None) -> ValuationCalculator:
    """获取 ValuationCalculator 单例实例"""
    global _valuation_calculator
    if _valuation_calculator is None:
        _valuation_calculator = ValuationCalculator(config)
    return _valuation_calculator


def reset_valuation_calculator():
    """重置 ValuationCalculator 单例 (用于测试)"""
    global _valuation_calculator
    _valuation_calculator = None
