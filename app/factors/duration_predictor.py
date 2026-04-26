#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影响持续时间预测器 (Impact Duration Predictor)

功能:
- 预测新闻事件的影响持续时间
- 基于历史数据的学习和验证
- 输出退出时机建议

核心逻辑:
新闻类型 + 情绪强度 + 传播速度 + 历史相似事件 → 预测持续时间

示例:
    predictor = ImpactDurationPredictor()
    result = predictor.predict(
        event_type='earnings',
        sentiment_score=-0.7,
        news_count=15,
        market_cap='large'
    )
    # result = {
    #     'expected_duration_minutes': 120,
    #     'confidence': 0.75,
    #     'exit_signal': '10:30 前退出',
    #     'reason': '负面财报影响通常持续 1-2 小时'
    # }
"""

import logging
from app.storage.repository import get_db
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from enum import Enum

logger = logging.getLogger("ftseek.factors.duration_predictor")
logger.propagate = False


class EventType(str, Enum):
    """事件类型"""
    EARNINGS = "earnings"           # 财报
    GUIDANCE = "guidance"           # 业绩指引
    MA = "ma"                       # 并购重组
    PRODUCT = "product"             # 新产品/技术
    REGULATION = "regulation"       # 监管政策
    EXECUTIVE = "executive"         # 高管变动
    LAWSUIT = "lawsuit"             # 诉讼/调查
    ANALYST = "analyst"             # 分析师评级
    MARKET_MOVE = "market_move"     # 市场异动
    MACRO = "macro"                 # 宏观经济
    GEOPOLITICAL = "geopolitical"   # 地缘政治


class DurationTier(str, Enum):
    """持续时间等级"""
    VERY_SHORT = "very_short"   # < 30 分钟
    SHORT = "short"             # 30-60 分钟
    MEDIUM = "medium"           # 1-4 小时
    LONG = "long"               # 4-24 小时
    VERY_LONG = "very_long"     # > 24 小时


@dataclass
class DurationPrediction:
    """持续时间预测结果"""
    # 预测结果
    expected_duration_minutes: int = 0  # 预期持续时间 (分钟)
    confidence: float = 0.0              # 置信度 (0-1)
    duration_tier: DurationTier = DurationTier.MEDIUM

    # 退出建议
    exit_signal: str = ""                # 退出时机建议
    exit_reason: str = ""

    # 预测依据
    similar_events_count: int = 0        # 相似事件数量
    avg_historical_duration: int = 0     # 历史平均持续时间
    prediction_factors: List[str] = field(default_factory=list)

    # 输入参数
    input_event_type: str = ""
    input_sentiment: float = 0.0
    input_news_count: int = 0

    # 元数据
    predicted_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            'expected_duration_minutes': self.expected_duration_minutes,
            'confidence': self.confidence,
            'duration_tier': self.duration_tier.value,
            'exit_signal': self.exit_signal,
            'exit_reason': self.exit_reason,
            'similar_events_count': self.similar_events_count,
            'avg_historical_duration': self.avg_historical_duration,
            'prediction_factors': self.prediction_factors,
            'input_event_type': self.input_event_type,
            'input_sentiment': self.input_sentiment,
            'input_news_count': self.input_news_count,
            'predicted_at': datetime.fromtimestamp(self.predicted_at).isoformat(),
        }


@dataclass
class HistoricalEvent:
    """历史事件记录"""
    event_id: str
    event_type: str
    sentiment_score: float
    news_count: int
    actual_duration_minutes: int
    max_price_impact_pct: float
    price_recovery_time_minutes: int
    recorded_at: float


class ImpactDurationPredictor:
    """
    影响持续时间预测器

    基于规则和 historical 数据的混合预测
    """

    # 基础持续时间 (分钟) - 按事件类型
    BASE_DURATIONS = {
        EventType.EARNINGS: 120,        # 财报：2 小时
        EventType.GUIDANCE: 90,         # 指引：1.5 小时
        EventType.MA: 240,              # 并购：4 小时
        EventType.PRODUCT: 60,          # 产品：1 小时
        EventType.REGULATION: 180,      # 监管：3 小时
        EventType.EXECUTIVE: 90,        # 高管：1.5 小时
        EventType.LAWSUIT: 180,         # 诉讼：3 小时
        EventType.ANALYST: 30,          # 评级：30 分钟
        EventType.MARKET_MOVE: 60,      # 市场异动：1 小时
        EventType.MACRO: 120,           # 宏观：2 小时
        EventType.GEOPOLITICAL: 300,    # 地缘政治：5 小时
    }

    # 情绪调整系数
    SENTIMENT_MULTIPLIERS = {
        'extreme_negative': 1.3,   # sentiment < -0.7
        'negative': 1.1,            # -0.7 <= sentiment < -0.3
        'neutral': 1.0,             # -0.3 <= sentiment <= 0.3
        'positive': 1.1,            # 0.3 < sentiment <= 0.7
        'extreme_positive': 1.3,    # sentiment > 0.7
    }

    def __init__(self, db_path: str = None):
        """
        初始化预测器

        Args:
            db_path: 历史事件数据库路径
        """
        self._db_path = db_path or str(Path(__file__).parent.parent / "data" / "event_duration.db")
        self._init_db()

        # 内存缓存的历史事件
        self._historical_events: List[HistoricalEvent] = []
        self._load_historical_events()

        # 缓存预测结果
        self._cache: Dict[str, Tuple[DurationPrediction, float]] = {}
        self._cache_ttl = 300  # 5 分钟

        logger.info(
            f"影响持续时间预测器已初始化 | "
            f"历史事件={len(self._historical_events)}, "
            f"db={self._db_path}"
        )

    def _init_db(self):
        """初始化数据库"""
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS historical_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    sentiment_score REAL,
                    news_count INTEGER,
                    actual_duration_minutes INTEGER,
                    max_price_impact_pct REAL,
                    price_recovery_time_minutes INTEGER,
                    recorded_at REAL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_event_type
                ON historical_events(event_type)
            """)
            conn.commit()
            conn.close()
            logger.debug(f"数据库已初始化：{self._db_path}")
        except Exception as e:
            logger.warning(f"数据库初始化失败：{e}")

    def _load_historical_events(self):
        """从数据库加载历史事件"""
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                SELECT event_id, event_type, sentiment_score, news_count,
                       actual_duration_minutes, max_price_impact_pct,
                       price_recovery_time_minutes, recorded_at
                FROM historical_events
                ORDER BY recorded_at DESC
                LIMIT 1000
            """)
            rows = cursor.fetchall()
            conn.close()

            self._historical_events = [
                HistoricalEvent(
                    event_id=row[0],
                    event_type=row[1],
                    sentiment_score=row[2],
                    news_count=row[3],
                    actual_duration_minutes=row[4],
                    max_price_impact_pct=row[5],
                    price_recovery_time_minutes=row[6],
                    recorded_at=row[7]
                )
                for row in rows
            ]
            logger.info(f"已加载 {len(self._historical_events)} 条历史事件")
        except Exception as e:
            logger.warning(f"加载历史事件失败：{e}")

    def predict(self,
               event_type: str,
               sentiment_score: float = 0.0,
               news_count: int = 1,
               market_cap: str = "large",
               volatility: float = 0.2,
               force_refresh: bool = False) -> DurationPrediction:
        """
        预测事件影响持续时间

        Args:
            event_type: 事件类型
            sentiment_score: 情绪评分 (-1 到 1)
            news_count: 相关新闻数量
            market_cap: 市值规模 (large/mid/small)
            volatility: 市场波动率
            force_refresh: 是否强制刷新

        Returns:
            DurationPrediction: 预测结果
        """
        # 检查缓存
        cache_key = f"{event_type}_{sentiment_score}_{news_count}"
        if not force_refresh:
            cached = self._cache.get(cache_key)
            if cached:
                result, expiry = cached
                if time.time() < expiry:
                    logger.debug(f"使用缓存预测结果：{cache_key}")
                    return result

        # 1. 获取基础持续时间
        base_duration = self._get_base_duration(event_type)

        # 2. 情绪调整
        sentiment_mult = self._get_sentiment_multiplier(sentiment_score)
        duration_after_sentiment = base_duration * sentiment_mult

        # 3. 新闻数量调整 (更多新闻 = 更长影响)
        news_mult = min(2.0, 1.0 + (news_count - 1) * 0.1)
        duration_after_news = duration_after_sentiment * news_mult

        # 4. 市值调整 (小市值股票影响更持久)
        cap_mult = self._get_market_cap_multiplier(market_cap)
        duration_after_cap = duration_after_news * cap_mult

        # 5. 波动率调整 (高波动率 = 更快出清)
        vol_mult = max(0.7, 1.0 - volatility * 0.5)
        final_duration = duration_after_cap * vol_mult

        # 6. 历史数据校准
        historical_calibrated = self._calibrate_with_history(
            event_type, sentiment_score, final_duration
        )

        # 7. 确定持续时间等级
        duration_tier = self._get_duration_tier(int(final_duration))

        # 8. 生成退出建议
        exit_signal, exit_reason = self._generate_exit_advice(
            event_type, duration_tier, int(final_duration)
        )

        # 9. 计算置信度
        confidence = self._calculate_confidence(
            event_type, sentiment_score, news_count
        )

        # 10. 收集预测因子
        prediction_factors = self._collect_prediction_factors(
            event_type, sentiment_score, news_count, market_cap, volatility
        )

        result = DurationPrediction(
            expected_duration_minutes=int(final_duration),
            confidence=confidence,
            duration_tier=duration_tier,
            exit_signal=exit_signal,
            exit_reason=exit_reason,
            similar_events_count=self._count_similar_events(event_type, sentiment_score),
            avg_historical_duration=self._get_avg_historical_duration(event_type),
            prediction_factors=prediction_factors,
            input_event_type=event_type,
            input_sentiment=sentiment_score,
            input_news_count=news_count,
        )

        # 缓存结果
        self._cache[cache_key] = (result, time.time() + self._cache_ttl)

        logger.info(
            f"[持续时间预测] {event_type}: {int(final_duration)}分钟 "
            f"(置信度={confidence:.2f}, 等级={duration_tier.value})"
        )

        return result

    def _get_base_duration(self, event_type: str) -> float:
        """获取基础持续时间"""
        try:
            event_enum = EventType(event_type)
            return self.BASE_DURATIONS.get(event_enum, 60)
        except ValueError:
            return 60  # 未知类型默认 60 分钟

    def _get_sentiment_multiplier(self, sentiment_score: float) -> float:
        """获取情绪调整系数"""
        if sentiment_score < -0.7:
            return self.SENTIMENT_MULTIPLIERS['extreme_negative']
        elif sentiment_score < -0.3:
            return self.SENTIMENT_MULTIPLIERS['negative']
        elif sentiment_score > 0.7:
            return self.SENTIMENT_MULTIPLIERS['extreme_positive']
        elif sentiment_score > 0.3:
            return self.SENTIMENT_MULTIPLIERS['positive']
        else:
            return self.SENTIMENT_MULTIPLIERS['neutral']

    def _get_market_cap_multiplier(self, market_cap: str) -> float:
        """获取市值调整系数"""
        multipliers = {
            'large': 1.0,   # 大市值，影响正常
            'mid': 1.2,     # 中市值，影响稍长
            'small': 1.5,   # 小市值，影响更长
        }
        return multipliers.get(market_cap.lower(), 1.0)

    def _get_duration_tier(self, duration_minutes: int) -> DurationTier:
        """确定持续时间等级"""
        if duration_minutes < 30:
            return DurationTier.VERY_SHORT
        elif duration_minutes < 60:
            return DurationTier.SHORT
        elif duration_minutes < 240:
            return DurationTier.MEDIUM
        elif duration_minutes < 1440:
            return DurationTier.LONG
        else:
            return DurationTier.VERY_LONG

    def _generate_exit_advice(self,
                             event_type: str,
                             duration_tier: DurationTier,
                             duration_minutes: int) -> Tuple[str, str]:
        """生成退出建议"""
        now = datetime.now()
        exit_time = now + timedelta(minutes=duration_minutes)

        # 基于持续时间等级的退出建议
        if duration_tier == DurationTier.VERY_SHORT:
            exit_signal = f"{exit_time.strftime('%H:%M')} 前退出"
            exit_reason = "超短期事件，建议快速了结"
        elif duration_tier == DurationTier.SHORT:
            exit_signal = f"{exit_time.strftime('%H:%M')} 前退出"
            exit_reason = "短期事件影响，1 小时内出清"
        elif duration_tier == DurationTier.MEDIUM:
            exit_signal = f"{exit_time.strftime('%H:%M')} 前退出"
            exit_reason = "中等持续时间事件，注意持仓时间风险"
        elif duration_tier == DurationTier.LONG:
            exit_signal = f"分批退出：{exit_time.strftime('%H:%M')} 退 50%, 收盘前清仓"
            exit_reason = "长期影响事件，建议分批了结"
        else:  # VERY_LONG
            exit_signal = "持仓过夜，设置追踪止损"
            exit_reason = "重大事件影响，可能需要数日消化"

        return exit_signal, exit_reason

    def _calculate_confidence(self,
                             event_type: str,
                             sentiment_score: float,
                             news_count: int) -> float:
        """计算置信度"""
        # 基础置信度
        base_confidence = 0.5

        # 历史数据数量加成
        similar_count = self._count_similar_events(event_type, sentiment_score)
        history_bonus = min(0.3, similar_count * 0.03)

        # 新闻数量加成 (更多新闻 = 更高置信度)
        news_bonus = min(0.1, news_count * 0.02)

        # 情绪强度加成 (极端情绪更容易预测)
        sentiment_bonus = 0.1 if abs(sentiment_score) > 0.5 else 0.0

        confidence = base_confidence + history_bonus + news_bonus + sentiment_bonus
        return min(1.0, confidence)

    def _collect_prediction_factors(self,
                                   event_type: str,
                                   sentiment_score: float,
                                   news_count: int,
                                   market_cap: str,
                                   volatility: float) -> List[str]:
        """收集预测因子"""
        factors = []

        # 事件类型
        factors.append(f"事件类型：{event_type}")

        # 情绪方向
        if sentiment_score > 0.3:
            factors.append(f"正面情绪 ({sentiment_score:.2f})")
        elif sentiment_score < -0.3:
            factors.append(f"负面情绪 ({sentiment_score:.2f})")

        # 新闻热度
        if news_count > 10:
            factors.append(f"高关注度 ({news_count}篇新闻)")

        # 市值规模
        factors.append(f"市值规模：{market_cap}")

        # 波动率
        if volatility > 0.3:
            factors.append(f"高波动环境 ({volatility:.1%})")

        return factors

    def _count_similar_events(self, event_type: str, sentiment_score: float) -> int:
        """计算相似事件数量"""
        count = 0
        for event in self._historical_events:
            if event.event_type == event_type:
                # 情绪方向相同算相似
                if (sentiment_score > 0 and event.sentiment_score > 0) or \
                   (sentiment_score < 0 and event.sentiment_score < 0) or \
                   (abs(sentiment_score) < 0.3 and abs(event.sentiment_score) < 0.3):
                    count += 1
        return count

    def _get_avg_historical_duration(self, event_type: str) -> int:
        """获取历史平均持续时间"""
        durations = [
            e.actual_duration_minutes
            for e in self._historical_events
            if e.event_type == event_type
        ]
        if durations:
            return int(sum(durations) / len(durations))
        return 0

    def _calibrate_with_history(self,
                               event_type: str,
                               sentiment_score: float,
                               predicted_duration: float) -> float:
        """使用历史数据校准预测"""
        similar_events = [
            e for e in self._historical_events
            if e.event_type == event_type and
               ((sentiment_score > 0 and e.sentiment_score > 0) or
                (sentiment_score < 0 and e.sentiment_score < 0))
        ]

        if len(similar_events) < 3:
            return predicted_duration

        # 计算历史平均
        avg_duration = sum(e.actual_duration_minutes for e in similar_events) / len(similar_events)

        # 加权平均：预测值和历史值
        calibrated = predicted_duration * 0.6 + avg_duration * 0.4

        return calibrated

    def record_event_outcome(self,
                            event_id: str,
                            event_type: str,
                            sentiment_score: float,
                            news_count: int,
                            actual_duration_minutes: int,
                            max_price_impact_pct: float = 0.0,
                            price_recovery_time_minutes: int = 0):
        """
        记录事件结果 (用于自我学习)

        Args:
            event_id: 事件 ID
            event_type: 事件类型
            sentiment_score: 情绪评分
            news_count: 新闻数量
            actual_duration_minutes: 实际持续时间
            max_price_impact_pct: 最大价格影响
            price_recovery_time_minutes: 价格恢复时间
        """
        event = HistoricalEvent(
            event_id=event_id,
            event_type=event_type,
            sentiment_score=sentiment_score,
            news_count=news_count,
            actual_duration_minutes=actual_duration_minutes,
            max_price_impact_pct=max_price_impact_pct,
            price_recovery_time_minutes=price_recovery_time_minutes,
            recorded_at=time.time()
        )

        # 添加到内存
        self._historical_events.insert(0, event)

        # 持久化到数据库
        try:
            with get_db(self._db_path) as conn:
                pass
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO historical_events
                (event_id, event_type, sentiment_score, news_count,
                 actual_duration_minutes, max_price_impact_pct,
                 price_recovery_time_minutes, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id, event.event_type, event.sentiment_score,
                event.news_count, event.actual_duration_minutes,
                event.max_price_impact_pct, event.price_recovery_time_minutes,
                event.recorded_at
            ))
            conn.commit()
            conn.close()
            logger.info(f"已记录事件结果：{event_id} (持续={actual_duration_minutes}分钟)")
        except Exception as e:
            logger.warning(f"记录事件结果失败：{e}")


# 全局单例
_predictor: Optional[ImpactDurationPredictor] = None


def get_duration_predictor() -> ImpactDurationPredictor:
    """获取全局 ImpactDurationPredictor 单例"""
    global _predictor
    if _predictor is None:
        _predictor = ImpactDurationPredictor()
    return _predictor


def reset_predictor():
    """重置预测器 (用于测试)"""
    global _predictor
    _predictor = None
