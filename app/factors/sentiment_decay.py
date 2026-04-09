"""
情绪得分衰减函数 (Sentiment Decay Function)

功能:
- 计算情绪得分随时间的衰减
- 支持指数衰减和线性衰减模式
- 提供衰减因子计算工具
"""
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
import math

logger = logging.getLogger("ftseek.factors.sentiment_decay")
logger.propagate = False


# 衰减模式
# exponential: 指数衰减 (半衰期模式)
# linear: 线性衰减 (固定速率)
# step: 阶梯式衰减 (特定时间点后快速衰减)

DECAY_MODES = ["exponential", "linear", "step"]


@dataclass
class DecayConfig:
    """衰减配置"""
    mode: str = "exponential"       # 衰减模式
    half_life_minutes: int = 30     # 半衰期 (分钟) - 仅 exponential
    decay_rate: float = 0.05        # 每分钟衰减率 (0-1) - 仅 linear
    step_times: Tuple[int, ...] = (30, 60, 120)  # 阶梯时间点 (分钟) - 仅 step
    min_score_factor: float = 0.1   # 最小得分因子 (衰减后最低保留比例)


class SentimentDecay:
    """
    情绪得分衰减计算器

    根据新闻发布时间计算衰减后的情绪得分
    """

    def __init__(self, config: Optional[DecayConfig] = None):
        """
        初始化衰减计算器

        Args:
            config: 衰减配置
        """
        self.config = config or DecayConfig()
        logger.info(f"情绪衰减计算器已初始化 | 模式：{self.config.mode}")

    def calculate_decay_factor(self, age_minutes: int) -> float:
        """
        计算衰减因子

        Args:
            age_minutes: 新闻发布时间距今的分钟数

        Returns:
            衰减因子 (0.0-1.0)
        """
        if age_minutes < 0:
            return 1.0  # 未来时间，不衰减

        if self.config.mode == "exponential":
            return self._exponential_decay(age_minutes)
        elif self.config.mode == "linear":
            return self._linear_decay(age_minutes)
        elif self.config.mode == "step":
            return self._step_decay(age_minutes)
        else:
            logger.warning(f"未知衰减模式：{self.config.mode}, 使用指数衰减")
            return self._exponential_decay(age_minutes)

    def _exponential_decay(self, age_minutes: int) -> float:
        """
        指数衰减

        公式：factor = 0.5 ^ (age / half_life)
        """
        half_life = self.config.half_life_minutes
        if half_life <= 0:
            return 1.0

        factor = 0.5 ** (age_minutes / half_life)
        return max(factor, self.config.min_score_factor)

    def _linear_decay(self, age_minutes: int) -> float:
        """
        线性衰减

        公式：factor = 1 - (decay_rate * age)
        """
        decay_rate = self.config.decay_rate
        factor = 1.0 - (decay_rate * age_minutes)
        return max(factor, self.config.min_score_factor)

    def _step_decay(self, age_minutes: int) -> float:
        """
        阶梯式衰减

        在特定时间点后快速衰减：
        - 0-30 分钟：100%
        - 30-60 分钟：70%
        - 60-120 分钟：40%
        - >120 分钟：20%
        """
        step_times = self.config.step_times

        if age_minutes < step_times[0]:
            return 1.0
        elif age_minutes < step_times[1] if len(step_times) > 1 else False:
            return 0.7
        elif age_minutes < step_times[2] if len(step_times) > 2 else False:
            return 0.4
        else:
            return self.config.min_score_factor

    def decay_score(self, original_score: float, age_minutes: int) -> float:
        """
        计算衰减后的情绪得分

        Args:
            original_score: 原始情绪得分 (-1 到 1)
            age_minutes: 新闻发布时间距今的分钟数

        Returns:
            衰减后的得分
        """
        decay_factor = self.calculate_decay_factor(age_minutes)
        decayed_score = original_score * decay_factor
        return decayed_score

    def decay_sentiment_dict(self, sentiment_data: Dict,
                             published_at: str) -> Dict:
        """
        衰减情绪数据字典

        Args:
            sentiment_data: 情绪数据字典 (包含 sentiment_score)
            published_at: 发布时间 (ISO 格式)

        Returns:
            衰减后的数据字典 (新增 decayed_score, age_minutes, decay_factor)
        """
        # 解析发布时间
        try:
            pub_time = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
            now = datetime.now(pub_time.tzinfo) if pub_time.tzinfo else datetime.now()
            age_minutes = int((now - pub_time).total_seconds() / 60)
        except Exception as e:
            logger.debug(f"时间解析失败：{e}")
            age_minutes = 0

        # 计算衰减
        original_score = sentiment_data.get('sentiment_score', 0.0)
        decayed_score = self.decay_score(original_score, age_minutes)
        decay_factor = self.calculate_decay_factor(age_minutes)

        # 返回增强数据
        result = sentiment_data.copy()
        result.update({
            'decayed_score': decayed_score,
            'age_minutes': age_minutes,
            'decay_factor': decay_factor,
            'original_score': original_score,
        })

        return result

    def batch_decay(self, news_list: list) -> list:
        """
        批量计算新闻情绪衰减

        Args:
            news_list: 新闻列表 (每条包含 sentiment_score, published_at)

        Returns:
            衰减后的新闻列表 (按衰减后得分排序)
        """
        decayed_news = []

        for news in news_list:
            if isinstance(news, dict):
                decayed = self.decay_sentiment_dict(news, news.get('published_at', ''))
            else:
                # NewsItem 对象
                try:
                    pub_time = datetime.fromisoformat(
                        news.published_at.replace('Z', '+00:00')
                        if news.published_at else datetime.now().isoformat()
                    )
                    now = datetime.now(pub_time.tzinfo) if pub_time.tzinfo else datetime.now()
                    age_minutes = int((now - pub_time).total_seconds() / 60)

                    original_score = getattr(news, 'sentiment_score', 0.0)
                    decayed_score = self.decay_score(original_score, age_minutes)
                    decay_factor = self.calculate_decay_factor(age_minutes)

                    # 添加衰减属性
                    if hasattr(news, 'decayed_score'):
                        news.decayed_score = decayed_score
                    else:
                        news.__dict__['decayed_score'] = decayed_score

                    if hasattr(news, 'decay_factor'):
                        news.decay_factor = decay_factor
                    else:
                        news.__dict__['decay_factor'] = decay_factor

                    if hasattr(news, 'age_minutes'):
                        news.age_minutes = age_minutes
                    else:
                        news.__dict__['age_minutes'] = age_minutes

                    decayed = news
                except Exception as e:
                    logger.debug(f"NewsItem 衰减计算失败：{e}")
                    decayed = news

            decayed_news.append(decayed)

        # 按衰减后得分排序 (高的在前)
        def get_decayed_score(x):
            """获取衰减后得分 (兼容 dict 和对象)"""
            if hasattr(x, 'decayed_score'):
                return getattr(x, 'decayed_score')
            elif isinstance(x, dict):
                return x.get('decayed_score', 0)
            return 0

        decayed_news.sort(key=get_decayed_score, reverse=True)

        return decayed_news

    def get_decay_table(self, max_minutes: int = 120, step: int = 10) -> list:
        """
        生成衰减对照表

        Args:
            max_minutes: 最大时间 (分钟)
            step: 步长 (分钟)

        Returns:
            [(age, factor, description), ...]
        """
        table = []

        for age in range(0, max_minutes + 1, step):
            factor = self.calculate_decay_factor(age)
            percentage = factor * 100

            if percentage >= 80:
                desc = "高时效"
            elif percentage >= 50:
                desc = "中时效"
            elif percentage >= 30:
                desc = "低时效"
            else:
                desc = "过时"

            table.append((age, factor, desc))

        return table


def get_decay_calculator(mode: str = "exponential",
                         half_life_minutes: int = 30) -> SentimentDecay:
    """
    获取衰减计算器

    Args:
        mode: 衰减模式
        half_life_minutes: 半衰期 (分钟)

    Returns:
        SentimentDecay 实例
    """
    config = DecayConfig(mode=mode, half_life_minutes=half_life_minutes)
    return SentimentDecay(config)


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("情绪得分衰减测试")
    print("=" * 60)

    # 测试指数衰减
    print("\n1. 指数衰减 (半衰期 30 分钟):")
    calc_exp = get_decay_calculator("exponential", half_life_minutes=30)
    table = calc_exp.get_decay_table(max_minutes=120, step=15)
    for age, factor, desc in table:
        print(f"   {age:3d}分钟 -> 因子：{factor:.3f} ({desc})")

    # 测试线性衰减
    print("\n2. 线性衰减 (每分钟 5%):")
    calc_linear = get_decay_calculator("linear")
    calc_linear.config.decay_rate = 0.05
    table = calc_linear.get_decay_table(max_minutes=120, step=15)
    for age, factor, desc in table:
        print(f"   {age:3d}分钟 -> 因子：{factor:.3f} ({desc})")

    # 测试阶梯衰减
    print("\n3. 阶梯衰减:")
    calc_step = get_decay_calculator("step")
    table = calc_step.get_decay_table(max_minutes=150, step=15)
    for age, factor, desc in table:
        print(f"   {age:3d}分钟 -> 因子：{factor:.3f} ({desc})")

    # 测试得分衰减
    print("\n4. 实际得分衰减示例:")
    original_score = 0.8
    for age in [0, 15, 30, 60, 90, 120]:
        decayed = calc_exp.decay_score(original_score, age)
        print(f"   原始得分 {original_score:.2f}, {age:3d}分钟 -> 衰减后：{decayed:.3f}")
