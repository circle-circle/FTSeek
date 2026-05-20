#!/usr/bin/env python3
"""
跨市场新闻消化度计算器

集成已有新闻情绪系统（SentimentFactors + NewsFreshnessManager），
为跨市场套利评分框架提供动态的新闻消化度评分（替代硬编码0.7）。

消化度含义：
  0.0: 重大新闻刚出，市场尚未反应（套利机会大）
  0.5: 新闻已部分消化
  1.0: 新闻已充分消化（套利窗口关闭）

注意：对于跨市场套利，新闻未充分消化反而是机会，
所以评分逻辑与直觉相反 — 重大未消化新闻应该降低消化度评分，
从而让外部的 news_digestion 维度贡献较低的分值，
避免低分窗口分在已充分消化时虚高。
换言之，消化度越低 → 套利机会越大 → window_score 应该越高，
这一转换已在调用方完成（高消化度 = 满分，低消化度 = 低分）。

创建时间: 2026-04-14（Task #7）
"""
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek.strategy.cross_market_news_digest")
logger.propagate = False


# ============================================================
#  新闻数据获取适配器（懒加载，避免循环依赖）
# ============================================================

def _try_import_news_collector():
    """懒加载新闻采集器，失败返回 None"""
    try:
        from app.news_push.collector import get_news_collector
        return get_news_collector()
    except Exception:
        return None


def _try_import_freshness_manager():
    """懒加载时效性管理器，失败返回 None"""
    try:
        from app.factors.news_freshness import get_freshness_manager
        return get_freshness_manager()
    except Exception:
        return None


def _try_import_sentiment_factors():
    """懒加载情绪因子，失败返回 None"""
    try:
        from app.factors.sentiment import SentimentFactors
        return SentimentFactors()
    except Exception:
        return None


# ============================================================
#  CrossMarketNewsDigest
# ============================================================

class CrossMarketNewsDigest:
    """
    跨市场新闻消化度计算器

    集成已有新闻情绪系统，为跨市场套利评分框架提供
    动态的新闻消化度评分（替代硬编码0.7）。

    消化度含义：
      - 0.0: 重大新闻刚出，市场尚未反应（套利机会大）
      - 0.5: 新闻已部分消化
      - 1.0: 新闻已充分消化（套利窗口关闭）

    注意：对于跨市场套利，新闻未充分消化反而是机会，
    所以评分逻辑与直觉相反 — 重大未消化新闻应该提高套利评分。
    具体而言，消化度越低说明市场反应还未完成，
    此时应降低 news_digestion 评分值，
    调用方以 1-消化度 形式使用此值可实现正向激励。
    """

    # 默认配置
    DEFAULT_CONFIG = {
        'cache_ttl_seconds': 300,        # 新闻缓存 TTL 5分钟
        'major_event_weight': 0.3,       # 重大事件对消化度的影响
        'sentiment_threshold': 0.6,      # 情绪极端判定阈值
        'news_lookback_hours': 12,       # 新闻回溯时间窗口（小时）
        'max_news_for_analysis': 20,     # 最多分析新闻条数
    }

    def __init__(self, config: dict = None):
        cfg = config or {}
        # 从 unified_config.yaml cross_market.news_digest 加载
        try:
            from config.config_center import get_config_center
            cc = get_config_center()
            cm_val = cc.get('cross_market')
            cm_dict = {}
            if cm_val is not None:
                if hasattr(cm_val, 'to_dict'):
                    cm_dict = cm_val.to_dict()
                elif isinstance(cm_val, dict):
                    cm_dict = cm_val
            nd_val = cm_dict.get('news_digest', {})
            if hasattr(nd_val, 'to_dict'):
                nd_val = nd_val.to_dict()
            if isinstance(nd_val, dict) and nd_val:
                cfg = {**nd_val, **cfg}
        except Exception as _e:
            logger.debug(f"[NewsDigest] 配置加载失败，使用默认值: {_e}")

        self._config = {**self.DEFAULT_CONFIG, **cfg}

        # 新闻数据缓存: key → (timestamp, data_dict)
        self._cache: Dict[str, Tuple[float, dict]] = {}

        # 延迟初始化的模块实例
        self._sentiment_factors = None
        self._freshness_manager = None
        self._collector = None

        logger.info(
            f"[CrossMarketNewsDigest] 初始化完成 | "
            f"cache_ttl={self._config['cache_ttl_seconds']}s | "
            f"major_event_weight={self._config['major_event_weight']} | "
            f"sentiment_threshold={self._config['sentiment_threshold']}"
        )

    # ----------------------------------------------------------
    #  公共接口
    # ----------------------------------------------------------

    def calculate_news_digestion(
        self,
        hk_code: str,
        us_code: str,
        session_phase: str,
        current_time: Optional[datetime] = None,
    ) -> Tuple[float, Dict]:
        """
        计算新闻消化度评分

        Args:
            hk_code: 港股代码（如 00700.HK）
            us_code: 美股代码（如 TCEHY）
            session_phase: 当前时段 (pre_market / arb_window / trend_session / after_hours)
            current_time: 当前时间（None 则取 datetime.now()）

        Returns:
            (digestion_score, detail_info)

            digestion_score (float, 0.0–1.0):
                1.0 = 新闻已充分消化（无套利空间）
                0.0 = 重大新闻尚未消化（套利机会大）

            detail_info (dict):
                news_count, sentiment_score, major_events,
                confidence, data_source, session_phase,
                hk_code, us_code
        """
        if current_time is None:
            current_time = datetime.now()

        # 先检查缓存
        cache_key = f"{hk_code}_{us_code}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            logger.debug(f"[NewsDigest] 使用缓存: {cache_key}")
            # 需要重新计算 time_factor（缓存中的 sentiment 数据不变，但时间在变）
            return self._apply_time_factor(cached, session_phase, current_time)

        # 尝试从新闻系统获取数据
        try:
            news_data = self._fetch_news_data(hk_code, us_code)
        except Exception as _e:
            logger.warning(f"[NewsDigest] 新闻数据获取失败，使用 fallback: {_e}")
            news_data = None

        if news_data and news_data.get('total_count', 0) > 0:
            # 缓存数据
            self._set_cache(cache_key, news_data)
            return self._apply_time_factor(news_data, session_phase, current_time)
        else:
            # fallback: 时间衰减模型
            logger.debug(f"[NewsDigest] 无新闻数据，使用时间衰减 fallback: {hk_code}/{us_code}")
            return self._fallback_time_decay(session_phase, current_time)

    # ----------------------------------------------------------
    #  内部：新闻数据获取
    # ----------------------------------------------------------

    def _fetch_news_data(self, hk_code: str, us_code: str) -> Optional[dict]:
        """
        从已有新闻系统获取新闻情绪数据

        整合步骤：
        1. 用 news_collector 获取新闻列表
        2. 用 NewsFreshnessManager 过滤并计算新鲜度
        3. 用 SentimentFactors 计算情绪分
        4. 用 NewsFreshnessManager.calculate_weighted_sentiment() 合并结果
        """
        # 懒加载
        if self._collector is None:
            self._collector = _try_import_news_collector()
        if self._freshness_manager is None:
            self._freshness_manager = _try_import_freshness_manager()
        if self._sentiment_factors is None:
            self._sentiment_factors = _try_import_sentiment_factors()

        if self._collector is None:
            logger.debug("[NewsDigest] news_collector 不可用")
            return None

        if self._freshness_manager is None or self._sentiment_factors is None:
            logger.debug("[NewsDigest] freshness_manager 或 sentiment_factors 不可用")
            return None

        # 收集两个市场的新闻
        raw_news: List[dict] = []

        for code in [hk_code, us_code]:
            try:
                news_items = self._collect_news_for_code(code)
                raw_news.extend(news_items)
            except Exception as _e:
                logger.debug(f"[NewsDigest] {code} 新闻采集失败: {_e}")

        if not raw_news:
            return None

        # 过滤可用新闻（新鲜度检查）
        actionable = self._freshness_manager.filter_actionable_news(raw_news)

        if not actionable:
            return None

        # 计算每条新闻的情绪分
        news_sentiments = []
        for news_dict, freshness in actionable:
            title = news_dict.get('title', '')
            content = news_dict.get('content', news_dict.get('summary', ''))
            text = f"{title} {content}"
            if text.strip():
                sentiment = self._sentiment_factors._sentiment_score(text)
                news_sentiments.append((news_dict, sentiment, freshness))

        if not news_sentiments:
            return None

        # 用加权情绪计算
        result = self._freshness_manager.calculate_weighted_sentiment(news_sentiments)
        result['hk_code'] = hk_code
        result['us_code'] = us_code
        result['data_source'] = 'news_system'
        result['fetched_at'] = time.time()

        logger.info(
            f"[NewsDigest] {hk_code}/{us_code} 新闻分析完成 | "
            f"总数={result['total_count']}, 热点={result['hot_count']}, "
            f"重大={result['major_events']}, 情绪={result['weighted_score']:.3f}, "
            f"置信度={result['confidence']:.2f}"
        )

        return result

    def _collect_news_for_code(self, code: str) -> List[dict]:
        """
        从 news_collector 获取指定股票代码的新闻，统一转为 dict 格式

        支持的 collector 接口：
          - get_news(code, hours) → list
          - collect_all([code]) → dict with 'news' key
        """
        news_items = []
        lookback_hours = self._config['news_lookback_hours']
        max_news = self._config['max_news_for_analysis']

        # 尝试 get_news 接口
        if hasattr(self._collector, 'get_news'):
            try:
                items = self._collector.get_news(code, hours=lookback_hours)
                for item in (items or [])[:max_news]:
                    news_items.append(self._news_item_to_dict(item))
            except Exception as _e:
                logger.debug(f"[NewsDigest] get_news({code}) 失败: {_e}")

        # 尝试 collect_all 接口（备选）
        if not news_items and hasattr(self._collector, 'collect_all'):
            try:
                result = self._collector.collect_all([code])
                items = result.get('news', [])
                for item in (items or [])[:max_news]:
                    news_items.append(self._news_item_to_dict(item))
            except Exception as _e:
                logger.debug(f"[NewsDigest] collect_all({code}) 失败: {_e}")

        return news_items

    @staticmethod
    def _news_item_to_dict(item) -> dict:
        """将 NewsItem 对象或 dict 统一转为 dict"""
        if isinstance(item, dict):
            return item
        # 支持 dataclass 或对象属性访问
        d = {}
        for attr in ('title', 'content', 'summary', 'published_at', 'timestamp', 'source'):
            val = getattr(item, attr, None)
            if val is not None:
                d[attr] = val
        return d

    # ----------------------------------------------------------
    #  内部：消化度计算
    # ----------------------------------------------------------

    def _apply_time_factor(
        self,
        news_data: dict,
        session_phase: str,
        current_time: datetime,
    ) -> Tuple[float, Dict]:
        """
        根据新闻数据 + 时间衰减计算最终消化度

        公式：
            digestion = base_digestion * time_factor * sentiment_factor

        - base_digestion: 基于新闻数量和新鲜度（热点越多，消化度越低）
        - time_factor: 时间衰减（盘前0.6, 套利窗口0.6→1.0线性增长, 其他1.0）
        - sentiment_factor: 情绪极端程度的调节（情绪越极端，消化度越低）
        """
        cfg = self._config
        total_count = news_data.get('total_count', 0)
        hot_count = news_data.get('hot_count', 0)
        fresh_count = news_data.get('fresh_count', 0)
        major_events = news_data.get('major_events', 0)
        weighted_score = news_data.get('weighted_score', 0.0)
        confidence = news_data.get('confidence', 0.0)

        # 1. base_digestion: 从热点新闻数量出发
        #    无新闻 → 消化度0.9（接近充分），有大量热点 → 消化度降低
        if total_count == 0:
            base_digestion = 0.9
        else:
            # 热点 + 新鲜新闻越多，消化度越低（套利机会越大）
            freshness_pressure = min(1.0, (hot_count * 2 + fresh_count) / 10.0)
            base_digestion = 1.0 - freshness_pressure * 0.5  # 范围：0.5–1.0

        # 2. time_factor: 时间衰减
        time_factor = self._calc_time_factor(session_phase, current_time)

        # 3. sentiment_factor: 情绪极端程度调节
        #    情绪越极端 (|score| > threshold)，消化度越低
        sentiment_threshold = cfg['sentiment_threshold']
        abs_sentiment = abs(weighted_score)
        if abs_sentiment > sentiment_threshold and confidence > 0.3:
            # 极端情绪降低消化度（情绪未被市场充分反应）
            sentiment_factor = 1.0 - (abs_sentiment - sentiment_threshold) * 0.5
            sentiment_factor = max(0.5, sentiment_factor)  # 不低于0.5
        else:
            sentiment_factor = 1.0

        # 4. 重大事件修正（有重大事件直接降低消化度）
        major_event_weight = cfg['major_event_weight']
        if major_events > 0:
            major_penalty = min(major_events * major_event_weight, 0.6)
            base_digestion = max(0.3, base_digestion - major_penalty)

        # 最终消化度
        digestion_score = base_digestion * time_factor * sentiment_factor
        digestion_score = max(0.0, min(1.0, digestion_score))

        detail_info = {
            'news_count': total_count,
            'hot_count': hot_count,
            'fresh_count': fresh_count,
            'major_events': major_events,
            'sentiment_score': round(weighted_score, 3),
            'confidence': round(confidence, 2),
            'base_digestion': round(base_digestion, 3),
            'time_factor': round(time_factor, 3),
            'sentiment_factor': round(sentiment_factor, 3),
            'digestion_score': round(digestion_score, 3),
            'session_phase': session_phase,
            'data_source': news_data.get('data_source', 'news_system'),
            'hk_code': news_data.get('hk_code', ''),
            'us_code': news_data.get('us_code', ''),
        }

        logger.debug(
            f"[NewsDigest] 消化度={digestion_score:.3f} | "
            f"base={base_digestion:.2f} × time={time_factor:.2f} × sentiment={sentiment_factor:.2f} | "
            f"major_events={major_events}, hot={hot_count}"
        )

        return round(digestion_score, 4), detail_info

    def _calc_time_factor(self, session_phase: str, current_time: datetime) -> float:
        """
        计算时间衰减因子

        - pre_market:    0.6（盘前，新闻影响尚未充分反应）
        - arb_window:    0.6 → 1.0（线性增长，套利窗口逐渐关闭）
        - trend_session: 1.0（已充分消化）
        - after_hours:   1.0
        """
        if session_phase == 'pre_market':
            return 0.6
        elif session_phase == 'arb_window':
            try:
                from app.strategy.cross_market import CrossMarketSessionWindow
                seconds_to_end = CrossMarketSessionWindow.get_seconds_to_arb_end(current_time)
                total_seconds = 30 * 60  # 30 分钟
                # 窗口开始时 to_end=1800 → factor=0.6
                # 窗口结束时 to_end=0   → factor=1.0
                factor = 0.6 + 0.4 * (1.0 - seconds_to_end / total_seconds)
                return round(max(0.6, min(1.0, factor)), 4)
            except Exception as _e:
                logger.debug(f"[NewsDigest] arb_window 时间计算失败: {_e}")
                return 0.8  # 保守 fallback
        else:
            return 1.0

    def _fallback_time_decay(
        self,
        session_phase: str,
        current_time: datetime,
    ) -> Tuple[float, Dict]:
        """
        无新闻数据时的 fallback：与原硬编码逻辑等效

        原逻辑：
          pre_market  → 0.7
          arb_window  → 0.7 + 0.3*(elapsed/1800)
          other       → 1.0
        """
        if session_phase == 'pre_market':
            digestion_score = 0.7
        elif session_phase == 'arb_window':
            try:
                from app.strategy.cross_market import CrossMarketSessionWindow
                seconds_elapsed = 1800 - CrossMarketSessionWindow.get_seconds_to_arb_end(current_time)
                digestion_score = min(1.0, 0.7 + 0.3 * (seconds_elapsed / 1800))
            except Exception:
                digestion_score = 0.85  # 保守 fallback
        else:
            digestion_score = 1.0

        detail_info = {
            'news_count': 0,
            'hot_count': 0,
            'fresh_count': 0,
            'major_events': 0,
            'sentiment_score': 0.0,
            'confidence': 0.0,
            'base_digestion': digestion_score,
            'time_factor': 1.0,
            'sentiment_factor': 1.0,
            'digestion_score': round(digestion_score, 3),
            'session_phase': session_phase,
            'data_source': 'fallback_time_decay',
            'hk_code': '',
            'us_code': '',
        }

        return round(digestion_score, 4), detail_info

    # ----------------------------------------------------------
    #  内部：缓存管理
    # ----------------------------------------------------------

    def _get_cache(self, key: str) -> Optional[dict]:
        """获取缓存（TTL 内有效）"""
        if key not in self._cache:
            return None
        ts, data = self._cache[key]
        ttl = self._config['cache_ttl_seconds']
        if time.time() - ts < ttl:
            return data
        # 过期
        del self._cache[key]
        return None

    def _set_cache(self, key: str, data: dict):
        """写入缓存"""
        self._cache[key] = (time.time(), data)

    def clear_cache(self):
        """清空所有缓存"""
        self._cache.clear()
        logger.info("[CrossMarketNewsDigest] 缓存已清空")

    def get_cache_stats(self) -> dict:
        """获取缓存统计"""
        return {
            'cache_size': len(self._cache),
            'cache_ttl_seconds': self._config['cache_ttl_seconds'],
        }


# ============================================================
#  全局单例
# ============================================================

_news_digest: Optional[CrossMarketNewsDigest] = None


def get_news_digest(config: dict = None) -> CrossMarketNewsDigest:
    """获取 CrossMarketNewsDigest 全局单例"""
    global _news_digest
    if _news_digest is None:
        _news_digest = CrossMarketNewsDigest(config)
    return _news_digest


def reset_news_digest():
    """重置单例（测试用）"""
    global _news_digest
    _news_digest = None
