"""
质量评分器 - 基于巴菲特选股标准

功能:
- 计算 Quality Score (基本面因子综合得分)
- 企业分级 (A/B/C/JUNK)
- 质量变化追踪

评分维度:
- ROE (30%): 连续 5 年平均 ROE
- 负债率 (20%): 资产负债率
- 现金流 (20%): 经营现金流
- 毛利率 (15%): 毛利率及趋势
- 增长 (15%): 营收增长率
"""

from dataclasses import dataclass
from typing import Optional, Dict, List
from enum import Enum
import logging

logger = logging.getLogger("ftseek.quality_scorer")


class QualityClass(Enum):
    """质量等级"""
    CLASS_A = "A"      # 80+ 优质企业
    CLASS_B = "B"      # 60-79 良好企业
    CLASS_C = "C"      # 40-59 一般企业
    JUNK = "JUNK"      # <40 垃圾企业 (过滤)


@dataclass
class QualityScore:
    """质量评分结果"""
    score: float              # 综合得分 (0-100)
    class_: QualityClass      # 质量等级
    roe_score: float          # ROE 得分
    debt_score: float         # 负债率得分
    cashflow_score: float     # 现金流得分
    margin_score: float       # 毛利率得分
    growth_score: float       # 增长得分
    notes: str = ""           # 备注


class QualityScorer:
    """
    质量评分器

    基于巴菲特选股标准:
    - 连续 5 年 ROE > 15%
    - 负债率 < 50%
    - 经营现金流为正
    - 毛利率稳定或提升
    - 营收持续增长
    """

    # 权重配置
    WEIGHTS = {
        'roe': 0.30,          # ROE 最重要
        'debt': 0.20,         # 负债率
        'cashflow': 0.20,     # 现金流
        'margin': 0.15,       # 毛利率
        'growth': 0.15,       # 增长
    }

    # 等级阈值 (可从配置加载)
    CLASS_THRESHOLDS = {
        'class_a': 80,
        'class_b': 60,
        'class_c': 40,
    }

    def __init__(self, config: Dict = None):
        """
        初始化质量评分器

        Args:
            config: 配置字典，可覆盖默认权重和阈值
        """
        self._score_cache: Dict[str, QualityScore] = {}
        self._history: Dict[str, List[QualityScore]] = {}  # 历史记录用于追踪变化

        # 加载配置
        if config:
            if 'weights' in config:
                self.WEIGHTS.update(config['weights'])
            if 'class_thresholds' in config:
                self.CLASS_THRESHOLDS.update(config['class_thresholds'])

        logger.info(f"QualityScorer 初始化完成，权重配置：{self.WEIGHTS}")

    def calculate(self, code: str, fundamentals: Dict) -> Optional[QualityScore]:
        """
        计算质量评分

        Args:
            code: 股票代码 (e.g., "HK.00700")
            fundamentals: 基本面数据字典
                - roe_5y_avg: 5 年平均 ROE (%)
                - debt_ratio: 负债率 (0-1)
                - operating_cashflow: 经营现金流 (元)
                - gross_margin: 毛利率 (0-1)
                - margin_trend: 毛利率趋势 (+/-)
                - revenue_growth_3y: 3 年营收增长率 (0-1)

        Returns:
            QualityScore 评分结果，数据不足时返回 None
        """
        # 1. ROE 得分 (30%)
        roe = fundamentals.get('roe_5y_avg', 0)
        roe_score = self._score_roe(roe)

        # 2. 负债率得分 (20%)
        debt = fundamentals.get('debt_ratio', 1)
        debt_score = self._score_debt(debt)

        # 3. 现金流得分 (20%)
        cf = fundamentals.get('operating_cashflow', 0)
        cf_score = self._score_cashflow(cf)

        # 4. 毛利率得分 (15%)
        margin = fundamentals.get('gross_margin', 0)
        margin_trend = fundamentals.get('margin_trend', 0)
        margin_score = self._score_margin(margin, margin_trend)

        # 5. 增长得分 (15%)
        growth = fundamentals.get('revenue_growth_3y', 0)
        growth_score = self._score_growth(growth)

        # 综合得分
        total = (
            roe_score * self.WEIGHTS['roe'] +
            debt_score * self.WEIGHTS['debt'] +
            cf_score * self.WEIGHTS['cashflow'] +
            margin_score * self.WEIGHTS['margin'] +
            growth_score * self.WEIGHTS['growth']
        )

        # 确定等级
        class_ = self._determine_class(total)

        # 生成备注
        notes = self._generate_notes(code, class_, roe, debt, cf)

        result = QualityScore(
            score=round(total, 2),
            class_=class_,
            roe_score=round(roe_score, 2),
            debt_score=round(debt_score, 2),
            cashflow_score=round(cf_score, 2),
            margin_score=round(margin_score, 2),
            growth_score=round(growth_score, 2),
            notes=notes,
        )

        # 缓存结果
        self._score_cache[code] = result

        # 记录历史
        if code not in self._history:
            self._history[code] = []
        self._history[code].append(result)
        if len(self._history[code]) > 10:
            self._history[code] = self._history[code][-10:]

        logger.info(f"质量评分：{code} -> {total:.1f} ({class_.value})")
        return result

    def get_cached(self, code: str) -> Optional[QualityScore]:
        """获取缓存的评分结果"""
        return self._score_cache.get(code)

    def get_history(self, code: str) -> List[QualityScore]:
        """获取历史评分记录"""
        return self._history.get(code, [])

    def is_deteriorated(self, code: str, old_score: QualityScore, new_score: QualityScore) -> bool:
        """
        判断质量是否恶化

        恶化条件:
        - 等级下降 (A->B, B->C, C->JUNK)
        - 同等级但分数下降超过 10 分
        """
        # 等级下降
        class_order = {
            QualityClass.CLASS_A: 4,
            QualityClass.CLASS_B: 3,
            QualityClass.CLASS_C: 2,
            QualityClass.JUNK: 1,
        }

        if class_order.get(new_score.class_, 0) < class_order.get(old_score.class_, 0):
            logger.warning(f"{code} 质量等级下降：{old_score.class_.value} -> {new_score.class_.value}")
            return True

        # 同等级但分数大幅下降
        if new_score.class_ == old_score.class_ and (old_score.score - new_score.score) > 10:
            logger.warning(f"{code} 质量分数大幅下降：{old_score.score} -> {new_score.score}")
            return True

        return False

    def _score_roe(self, roe: float) -> float:
        """
        ROE 得分 (0-100)

        标准:
        - ROE >= 20%: 100 分 (优秀)
        - ROE >= 15%: 80-100 分 (良好)
        - ROE >= 10%: 60-80 分 (一般)
        - ROE >= 5%: 40-60 分 (较差)
        - ROE < 5%: 0-40 分 (差)
        """
        if roe >= 20:
            return 100
        elif roe >= 15:
            return 80 + (roe - 15) * 4  # 15-20 → 80-100
        elif roe >= 10:
            return 60 + (roe - 10) * 4  # 10-15 → 60-80
        elif roe >= 5:
            return 40 + (roe - 5) * 4   # 5-10 → 40-60
        else:
            return max(0, roe * 8)

    def _score_debt(self, debt_ratio: float) -> float:
        """
        负债率得分 (0-100)

        标准:
        - 负债率 <= 30%: 100 分 (非常安全)
        - 负债率 <= 50%: 80-100 分 (安全)
        - 负债率 <= 70%: 60-80 分 (一般)
        - 负债率 > 70%: 0-60 分 (高风险)
        """
        if debt_ratio <= 0.3:
            return 100
        elif debt_ratio <= 0.5:
            return 80 + (0.5 - debt_ratio) * 100  # 0.3-0.5 → 100-80
        elif debt_ratio <= 0.7:
            return 60 + (0.7 - debt_ratio) * 100  # 0.5-0.7 → 80-60
        else:
            return max(0, 60 - (debt_ratio - 0.7) * 200)

    def _score_cashflow(self, cashflow: float) -> float:
        """
        现金流得分 (0-100)

        标准:
        - 经营现金流 > 0: 100 分
        - 经营现金流 <= 0: 0 分
        """
        return 100 if cashflow > 0 else 0

    def _score_margin(self, margin: float, trend: float) -> float:
        """
        毛利率得分 (0-100)

        标准:
        - 毛利率 >= 50%: 100 分 (高毛利)
        - 毛利率 >= 30%: 80-100 分 (良好)
        - 毛利率 >= 15%: 60-80 分 (一般)
        - 毛利率 < 15%: 0-60 分 (低毛利)

        趋势调整:
        - 毛利率提升：+20 分
        - 毛利率下滑：-10 分
        """
        base = min(100, margin * 2)  # 毛利率 50% → 100 分
        trend_bonus = 20 if trend > 0 else -10
        return max(0, min(100, base + trend_bonus))

    def _score_growth(self, growth: float) -> float:
        """
        增长得分 (0-100)

        标准:
        - 增长 >= 20%: 100 分 (高增长)
        - 增长 >= 10%: 80-100 分 (良好增长)
        - 增长 >= 5%: 60-80 分 (稳定增长)
        - 增长 >= 0%: 40-60 分 (无增长但不衰退)
        - 增长 < 0%: 0-40 分 (衰退)
        """
        if growth >= 0.20:
            return 100
        elif growth >= 0.10:
            return 80 + (growth - 0.10) * 200  # 10-20% → 80-100
        elif growth >= 0.05:
            return 60 + (growth - 0.05) * 400  # 5-10% → 60-80
        elif growth >= 0:
            return 40 + growth * 400           # 0-5% → 40-60
        else:
            return max(0, 40 + growth * 400)

    def _determine_class(self, score: float) -> QualityClass:
        """确定质量等级"""
        if score >= self.CLASS_THRESHOLDS['class_a']:
            return QualityClass.CLASS_A
        elif score >= self.CLASS_THRESHOLDS['class_b']:
            return QualityClass.CLASS_B
        elif score >= self.CLASS_THRESHOLDS['class_c']:
            return QualityClass.CLASS_C
        else:
            return QualityClass.JUNK

    def _generate_notes(self, code: str, class_: QualityClass, roe: float, debt: float, cf: float) -> str:
        """生成备注信息"""
        notes = []

        if class_ == QualityClass.CLASS_A:
            notes.append("优质企业")
            if roe >= 20:
                notes.append(f"ROE 优秀 ({roe:.1f}%)")
            if debt <= 0.3:
                notes.append(f"负债率低 ({debt:.1%})")
            if cf > 0:
                notes.append("现金流健康")

        elif class_ == QualityClass.CLASS_B:
            notes.append("良好企业")
            if roe >= 15:
                notes.append(f"ROE 良好 ({roe:.1f}%)")
            if debt <= 0.5:
                notes.append(f"负债率可控 ({debt:.1%})")

        elif class_ == QualityClass.CLASS_C:
            notes.append("一般企业")
            if roe < 10:
                notes.append(f"ROE 偏低 ({roe:.1f}%)")
            if debt > 0.6:
                notes.append(f"负债率偏高 ({debt:.1%})")

        else:  # JUNK
            notes.append("垃圾企业，建议规避")
            if roe < 0:
                notes.append(f"ROE 为负 ({roe:.1f}%)")
            if debt > 0.7:
                notes.append(f"负债率过高 ({debt:.1%})")
            if cf <= 0:
                notes.append("现金流为负")

        return "; ".join(notes)


# 单例实例
_quality_scorer: Optional[QualityScorer] = None


def get_quality_scorer(config: Dict = None) -> QualityScorer:
    """获取 QualityScorer 单例实例"""
    global _quality_scorer
    if _quality_scorer is None:
        _quality_scorer = QualityScorer(config)
    return _quality_scorer


def reset_quality_scorer():
    """重置 QualityScorer 单例 (用于测试)"""
    global _quality_scorer
    _quality_scorer = None
