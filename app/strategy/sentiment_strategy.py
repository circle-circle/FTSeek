"""
情绪策略 - 基于 AI 新闻情绪分析生成交易信号

功能:
- 自动获取股票相关新闻
- 使用 AI 分析新闻情绪
- 根据情绪得分生成买入/卖出信号
- 集成到共识引擎参与决策
- 时效性管理：优先使用新鲜新闻 (2026-03-17 更新)

权重: 0.9 (较高，因为 AI 分析质量好)
"""
import logging
import time
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.consensus_types import SignalSource

logger = logging.getLogger("ftseek.strategy.sentiment")
logger.propagate = False  # 防止日志重复


class SentimentStrategy(BaseStrategy):
    """AI 情绪策略"""
    
    name = "sentiment_ai"
    
    # 情绪阈值配置
    POSITIVE_THRESHOLD = 0.4   # 正面情绪阈值
    NEGATIVE_THRESHOLD = -0.4  # 负面情绪阈值
    
    # 冷却时间 (秒)
    COOLDOWN_SECONDS = 7200  # 2 小时
    
    # 时效性配置 (新增 2026-03-17)
    MAX_NEWS_AGE_HOURS = 3    # 只使用最近 3 小时的新闻 (原 6 小时)
    MIN_FRESH_NEWS = 3        # 最少需要 3 条新鲜新闻
    MIN_HOT_NEWS = 1          # 最少需要 1 条热点新闻 (30分钟内)
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        
        if config:
            self.POSITIVE_THRESHOLD = config.get('positive_threshold', self.POSITIVE_THRESHOLD)
            self.NEGATIVE_THRESHOLD = config.get('negative_threshold', self.NEGATIVE_THRESHOLD)
            self.COOLDOWN_SECONDS = config.get('cooldown_seconds', self.COOLDOWN_SECONDS)
            self.MAX_NEWS_AGE_HOURS = config.get('max_news_age_hours', self.MAX_NEWS_AGE_HOURS)
        
        self._analyzer = None
        self._freshness_manager = None
        
        self._last_signal_time: Dict[str, float] = {}
        self._sentiment_cache: Dict[str, dict] = {}
        self._cache_ttl = 600  # 10 分钟缓存 (缩短以提升时效性)
        
        logger.info(
            f"情绪策略初始化: 正面阈值={self.POSITIVE_THRESHOLD}, "
            f"负面阈值={self.NEGATIVE_THRESHOLD}, 冷却={self.COOLDOWN_SECONDS}s, "
            f"最大新闻年龄={self.MAX_NEWS_AGE_HOURS}h"
        )
    
    def _get_analyzer(self):
        """获取情绪分析器 (延迟加载)"""
        if self._analyzer is None:
            try:
                from app.factors.news_integration import get_news_sentiment_analyzer
                self._analyzer = get_news_sentiment_analyzer()
            except Exception as e:
                logger.warning(f"情绪分析器加载失败: {e}")
        return self._analyzer
    
    def _get_freshness_manager(self):
        """获取时效性管理器 (延迟加载)"""
        if self._freshness_manager is None:
            try:
                from app.factors.news_freshness import get_freshness_manager
                self._freshness_manager = get_freshness_manager()
            except Exception as e:
                logger.warning(f"时效性管理器加载失败: {e}")
        return self._freshness_manager
    
    def _is_cooled_down(self, code: str) -> bool:
        """检查是否冷却完成"""
        last_time = self._last_signal_time.get(code, 0)
        return time.time() - last_time >= self.COOLDOWN_SECONDS
    
    def analyze_sentiment(self, code: str) -> Optional[dict]:
        """分析股票情绪 (带时效性过滤)"""
        analyzer = self._get_analyzer()
        if analyzer is None:
            return None
        
        # 检查缓存
        cache_key = code
        if cache_key in self._sentiment_cache:
            cached_result, timestamp = self._sentiment_cache[cache_key]
            if (datetime.now() - timestamp).total_seconds() < self._cache_ttl:
                logger.debug(f"使用缓存情绪: {code}")
                return cached_result
        
        try:
            # 获取新闻 (使用更短的时间窗口)
            result = analyzer.analyze_stock(
                code=code,
                lookback_hours=self.MAX_NEWS_AGE_HOURS,  # 只取最近几小时
                max_news=30
            )
            
            # 应用时效性过滤
            freshness_mgr = self._get_freshness_manager()
            if freshness_mgr and result.get('top_news'):
                # 过滤并加权
                news_list = []
                for news in result['top_news']:
                    news_item = {
                        'title': news.get('title', ''),
                        'content': news.get('summary', ''),
                        'published_at': news.get('published_at', ''),
                        'sentiment_score': news.get('sentiment_score', 0)
                    }
                    news_list.append(news_item)
                
                # 过滤可用新闻
                actionable = freshness_mgr.filter_actionable_news(
                    news_list, 
                    max_age_minutes=self.MAX_NEWS_AGE_HOURS * 60
                )
                
                if actionable:
                    # 计算加权情绪
                    sentiments = [
                        (news, news.get('sentiment_score', 0), freshness)
                        for news, freshness in actionable
                    ]
                    weighted_result = freshness_mgr.calculate_weighted_sentiment(sentiments)
                    
                    # 更新结果
                    result['weighted_score'] = weighted_result['weighted_score']
                    result['hot_count'] = weighted_result['hot_count']
                    result['fresh_count'] = weighted_result['fresh_count']
                    result['major_events'] = weighted_result['major_events']
                    result['freshness_confidence'] = weighted_result['confidence']
                    
                    # 检查是否应该交易
                    should_trade, reason = freshness_mgr.should_trade_on_news(
                        weighted_result,
                        min_confidence=0.5,
                        min_hot_count=self.MIN_HOT_NEWS
                    )
                    result['should_trade'] = should_trade
                    result['trade_reason'] = reason
                else:
                    result['should_trade'] = False
                    result['trade_reason'] = '无可用新鲜新闻'
            else:
                result['should_trade'] = True  # 无时效性管理器时默认可用
                result['trade_reason'] = '时效性检查跳过'
            
            # 缓存结果
            self._sentiment_cache[cache_key] = (result, datetime.now())
            
            return result
            
        except Exception as e:
            logger.error(f"情绪分析失败: {code} - {e}")
            return None
    
    def on_quote(self, code: str, quote: dict) -> Optional[Signal]:
        """
        行情回调 - 仅使用缓存情绪数据，不触发 LLM 调用
        
        P0 修复 (2026-04-14): 之前在缓存未命中时调用 analyze_sentiment()
        触发 LLM API 调用，导致 17 秒超时，阻塞线程池。
        情绪分析应只在 on_kline (低频 K 线事件) 中触发。
        """
        if not self.enabled:
            return None
        
        # P0 修复: 只使用缓存，不触发新分析
        cache_key = code
        if cache_key in self._sentiment_cache:
            cached_result, timestamp = self._sentiment_cache[cache_key]
            if (datetime.now() - timestamp).total_seconds() < self._cache_ttl:
                # 缓存有效，继续检查
                pass
            else:
                # 缓存过期，不触发新分析，直接跳过
                return None
        else:
            # 无缓存，不触发分析，直接跳过
            return None
        
        # 检查冷却
        if not self._is_cooled_down(code):
            return None
        
        # 使用缓存的情绪数据
        sentiment_result = self._sentiment_cache[cache_key][0]
        if sentiment_result is None or sentiment_result.get('news_count', 0) == 0:
            return None
        
        # 检查时效性
        if not sentiment_result.get('should_trade', True):
            logger.debug(f"时效性检查未通过: {sentiment_result.get('trade_reason', '')}")
            return None
        
        # 使用加权得分 (如果有)
        score = sentiment_result.get('weighted_score', sentiment_result.get('sentiment_score', 0))
        news_count = sentiment_result['news_count']
        hot_count = sentiment_result.get('hot_count', 0)
        fresh_count = sentiment_result.get('fresh_count', 0)
        
        # 判断信号方向
        action = None
        reason = ""
        
        if score >= self.POSITIVE_THRESHOLD:
            action = SignalAction.BUY
            reason = f"AI情绪正面 (得分={score:.2f}, 热点={hot_count}, 新鲜={fresh_count}, 总={news_count})"
            
        elif score <= self.NEGATIVE_THRESHOLD:
            action = SignalAction.SELL
            reason = f"AI情绪负面 (得分={score:.2f}, 热点={hot_count}, 新鲜={fresh_count}, 总={news_count})"
        
        if action is None:
            return None
        
        # 更新冷却时间
        self._last_signal_time[code] = time.time()
        
        # 获取价格
        price = quote.get('last_price', quote.get('last_done', 0))
        if price <= 0:
            return None
        
        # 生成信号
        signal = Signal(
            code=code,
            name=quote.get('stock_name', code),
            action=action,
            price=price,
            quantity=200,
            reason=reason,
            strategy=self.name,
            source=SignalSource.EVENT,  # Phase 2: 标记为事件驱动
            extra={
                'sentiment_score': score,
                'weighted_score': score,
                'news_count': news_count,
                'hot_count': hot_count,
                'fresh_count': fresh_count,
                'major_events': sentiment_result.get('major_events', 0),
                'freshness_confidence': sentiment_result.get('freshness_confidence', 0),
                'ai_provider': sentiment_result.get('ai_provider', 'qwen'),
            }
        )
        
        logger.info(
            f"[情绪信号] {code} {action.value} | 加权得分={score:.2f} | "
            f"热点={hot_count} | 新鲜={fresh_count} | 总={news_count}"
        )
        
        return signal
    
    def on_kline(self, code: str, kline: dict, k_type: str = "K_1M") -> Optional[Signal]:
        """K线回调 - 不处理"""
        return None
    
    def get_sentiment_summary(self, code: str) -> dict:
        """获取情绪摘要"""
        result = self.analyze_sentiment(code)
        if result is None:
            return {
                'code': code,
                'available': False,
                'message': '无法获取情绪数据'
            }
        
        score = result.get('weighted_score', result.get('sentiment_score', 0))
        if score >= 0.4:
            sentiment_label = '正面'
            emoji = '🟢'
        elif score <= -0.4:
            sentiment_label = '负面'
            emoji = '🔴'
        else:
            sentiment_label = '中性'
            emoji = '⚪'
        
        return {
            'code': code,
            'available': True,
            'score': score,
            'label': sentiment_label,
            'emoji': emoji,
            'news_count': result.get('news_count', 0),
            'hot_count': result.get('hot_count', 0),
            'fresh_count': result.get('fresh_count', 0),
            'major_events': result.get('major_events', 0),
            'should_trade': result.get('should_trade', True),
            'trade_reason': result.get('trade_reason', ''),
        }


# 策略注册在 __init__.py 中完成，避免循环导入