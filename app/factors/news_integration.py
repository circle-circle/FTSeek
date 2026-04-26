"""
新闻数据集成模块 (Phase 3 增强版)

功能增强:
- 动态缓存 TTL (基于市场波动率)
- 情绪得分衰减
- 突发新闻快速通道
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek.factors.news_integration")
logger.propagate = False  # 防止日志重复

# 导入 trace_id 工具
try:
    from app.core.logging_config import get_trace_id
    TRACE_AVAILABLE = True
except ImportError:
    TRACE_AVAILABLE = False
    def get_trace_id(): return None

# 导入新闻采集器
try:
    from app.news_push.collector import get_news_collector, NewsItem
    NEWS_COLLECTOR_AVAILABLE = True
except ImportError:
    NEWS_COLLECTOR_AVAILABLE = False
    logger.warning("新闻采集器未安装，使用模拟模式")

# 导入 AI 服务
from app.ai import AIService

# 导入衰减计算器
try:
    from app.factors.sentiment_decay import get_decay_calculator, SentimentDecay
    DECAY_AVAILABLE = True
except ImportError:
    DECAY_AVAILABLE = False
    logger.warning("情绪衰减模块未安装，使用固定 TTL")

# 导入 VIX 监控器
try:
    from app.risk.vix_monitor import get_vix_monitor
    VIX_AVAILABLE = True
except ImportError:
    VIX_AVAILABLE = False
    logger.warning("VIX 监控器未安装，使用固定 TTL")


class NewsSentimentAnalyzer:
    """新闻情绪分析器 (Phase 3 增强版)"""
    
    def __init__(self):
        # 初始化 VIX 监控器
        self.vix_monitor = None
        if VIX_AVAILABLE:
            try:
                self.vix_monitor = get_vix_monitor()
                logger.info("VIX 监控器已集成")
            except Exception as e:
                logger.warning(f"VIX 监控器初始化失败：{e}")
        
        # 初始化新闻采集器 (传入 VIX 监控器)
        if NEWS_COLLECTOR_AVAILABLE:
            self.collector = get_news_collector(vix_monitor=self.vix_monitor)
        else:
            self.collector = None
        
        self.ai = AIService()
        
        # 缓存配置 (动态 TTL)
        self.cache = {}
        self.base_cache_ttl = 1800  # 基础 30 分钟
        
        # 情绪衰减计算器
        self.decay_calculator = None
        if DECAY_AVAILABLE:
            try:
                self.decay_calculator = get_decay_calculator(
                    mode="exponential",
                    half_life_minutes=30  # 半衰期 30 分钟
                )
                logger.info("情绪衰减计算器已集成")
            except Exception as e:
                logger.warning(f"情绪衰减计算器初始化失败：{e}")
        
        # LLM 深度分析增强（延迟注入）
        self._llm_analyst = None
        self._llm_news_enabled = False

        logger.info("新闻情绪分析器已初始化 (Phase 3 增强版)")
    
    def init_llm_enhancement(self, llm_analyst) -> None:
        """注入 LLM 市场研判引擎（由外部调用）

        Args:
            llm_analyst: LLMMarketAnalyst 实例
        """
        try:
            from app.ai.llm_market_analyst import LLMMarketAnalyst
            if isinstance(llm_analyst, LLMMarketAnalyst) and llm_analyst.enabled:
                self._llm_analyst = llm_analyst
                self._llm_news_enabled = True
                logger.info("新闻分析LLM增强已启用")
            else:
                logger.info("LLM研判引擎未启用或类型不匹配，新闻LLM增强保持关闭")
        except Exception as e:
            logger.warning(f"init_llm_enhancement 失败: {e}")

    def _get_dynamic_ttl(self) -> int:
        """
        获取动态缓存 TTL

        Returns:
            TTL (秒)
        """
        if self.vix_monitor:
            multiplier = self.vix_monitor.get_cache_ttl_multiplier()
            return int(self.base_cache_ttl * multiplier)
        return self.base_cache_ttl
    
    def analyze_stock(self, 
                     code: str, 
                     lookback_hours: int = 24,
                     max_news: int = 20,
                     trace_id: str = None) -> Dict:
        """
        分析股票情绪

        Args:
            code: 股票代码 (HK.00700)
            lookback_hours: 回溯时间 (小时)
            max_news: 最多分析新闻数
            trace_id: 上游传入的 trace_id（若空则尝试从上下文获取或自动生成）

        Returns:
            {
                'sentiment_score': 0.75,  # -1 到 1
                'decayed_score': 0.65,    # 衰减后得分
                'news_count': 10,
                'positive_count': 8,
                'negative_count': 2,
                'neutral_count': 0,
                'top_news': [...],
                'ai_provider': 'bailian',
                'cache_hit': False,
                'avg_age_minutes': 45,     # 新闻平均年龄
                'volatility_state': 'normal'  # 市场波动状态
            }
        """
        # 处理 trace_id：优先使用传入的，其次从线程上下文获取，最后自动生成
        import uuid
        effective_trace_id = trace_id or (get_trace_id() if TRACE_AVAILABLE else None)
        if not effective_trace_id:
            effective_trace_id = f"NEWS-ANA-{uuid.uuid4().hex[:10].upper()}"
            logger.warning(
                f"[{effective_trace_id}] analyze_stock({code}) 未收到上游 trace_id，已自动生成"
            )
        # 检查缓存 (使用动态 TTL)
        cache_key = f"{code}_{lookback_hours}"
        current_ttl = self._get_dynamic_ttl()
        
        if cache_key in self.cache:
            cached_result, timestamp = self.cache[cache_key]
            if (datetime.now() - timestamp).total_seconds() < current_ttl:
                logger.debug(f"使用缓存情绪分析：{code} (TTL={current_ttl}s)")
                cached_result['cache_hit'] = True
                return cached_result
        
        # 1. 获取新闻
        if self.collector:
            news_list = self._fetch_news(code, lookback_hours)
        else:
            news_list = []
            logger.warning("新闻采集器不可用，使用空新闻列表")
        
        if not news_list:
            logger.warning(f"[{effective_trace_id}] 未获取到新闻：{code}")
            result = {
                'sentiment_score': 0.0,
                'decayed_score': 0.0,
                'news_count': 0,
                'positive_count': 0,
                'negative_count': 0,
                'neutral_count': 0,
                'top_news': [],
                'ai_provider': 'bailian',
                'cache_hit': False,
                'avg_age_minutes': 0,
                'volatility_state': self._get_volatility_state(),
                'trace_id': effective_trace_id,
            }
            self.cache[cache_key] = (result, datetime.now())
            return result
        
        # 2. AI 情绪分析 + 衰减计算
        scores = []
        decayed_scores = []
        analyzed_news = []
        ages = []
        
        for news in news_list[:max_news]:
            # 构建分析文本
            title = getattr(news, 'title', '') or getattr(news, 'title', '')
            summary = getattr(news, 'summary', '') or getattr(news, 'content', '')
            content = getattr(news, 'content', '')
            text = f"{title} {summary} {content}"
            
            if not text.strip():
                continue
            
            # 检查新闻缓存
            news_cache_key = f"news_sentiment_{hash(text)}"
            if news_cache_key in self.cache:
                score, _ = self.cache[news_cache_key]
            else:
                # 调用 AI API 分析
                response = self.ai.analyze_sentiment(text)
                
                if response.success:
                    score = response.data.get('sentiment_score', 0.0)
                    # 缓存结果
                    self.cache[news_cache_key] = (score, datetime.now())
                else:
                    logger.warning(f"AI 分析失败：{title[:50]}")
                    score = 0.0
            
            scores.append(score)
            
            # 计算衰减后得分
            news_age = getattr(news, 'age_minutes', 0)
            ages.append(news_age)
            
            if self.decay_calculator:
                decayed_score = self.decay_calculator.decay_score(score, news_age)
            else:
                # 简化衰减：每分钟 2% 衰减
                decay_factor = max(0.1, 1.0 - (news_age * 0.02))
                decayed_score = score * decay_factor
            
            decayed_scores.append(decayed_score)
            
            # 记录分析结果
            analyzed_news.append({
                'title': title[:100],
                'summary': summary[:200],
                'source': getattr(news, 'source', ''),
                'published_at': getattr(news, 'published_at', ''),
                'sentiment_score': score,
                'decayed_score': decayed_score,
                'age_minutes': news_age,
                'is_breaking': getattr(news, 'is_breaking', False),
                'url': getattr(news, 'url', '')
            })
        
        # 3. 统计结果
        positive_count = sum(1 for s in scores if s > 0.2)
        negative_count = sum(1 for s in scores if s < -0.2)
        neutral_count = len(scores) - positive_count - negative_count
        
        # 计算平均分 (原始和衰减后)
        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_decayed_score = sum(decayed_scores) / len(decayed_scores) if decayed_scores else 0.0
        avg_age = sum(ages) / len(ages) if ages else 0
        
        # 按衰减后得分排序，取 top 5
        analyzed_news.sort(key=lambda x: x['decayed_score'], reverse=True)
        
        result = {
            'sentiment_score': round(avg_score, 4),
            'decayed_score': round(avg_decayed_score, 4),
            'news_count': len(scores),
            'positive_count': positive_count,
            'negative_count': negative_count,
            'neutral_count': neutral_count,
            'top_news': analyzed_news[:5],
            'ai_provider': 'bailian',
            'cache_hit': False,
            'lookback_hours': lookback_hours,
            'analyzed_at': datetime.now().isoformat(),
            'avg_age_minutes': avg_age,
            'volatility_state': self._get_volatility_state(),
            'cache_ttl': current_ttl,
            'trace_id': effective_trace_id,
        }
        
        # LLM 新闻深度分析增强（同步降级模式）
        if self._llm_news_enabled and self._llm_analyst:
            try:
                llm_result = self._call_llm_news_enhancement_sync(
                    code=code,
                    news_list=news_list[:max_news],
                    sentiment_score=avg_score,
                    trace_id=effective_trace_id,
                )
                if llm_result:
                    result['llm_enhancement'] = llm_result
            except Exception as e:
                logger.warning(
                    f"[{effective_trace_id}] LLM新闻增强失败(降级跳过): {e}"
                )

        # 缓存结果
        self.cache[cache_key] = (result, datetime.now())
        
        logger.info(
            f"[{effective_trace_id}] 新闻情绪分析完成：{code} - {len(scores)}条新闻，"
            f"原始得分 {avg_score:.2f}, 衰减后 {avg_decayed_score:.2f}"
        )
        
        return result
    
    def _get_volatility_state(self) -> str:
        """获取市场波动状态"""
        if self.vix_monitor:
            state = self.vix_monitor.get_state()
            return state.state
        return "unknown"

    def _call_llm_news_enhancement_sync(self, code, news_list, sentiment_score, trace_id):
        """同步包装器 - 在同步上下文中调用异步 LLM 分析（真正超时控制）"""
        import concurrent.futures
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                asyncio.run,
                self._call_llm_news_enhancement(code, news_list, sentiment_score, trace_id)
            )
            try:
                return future.result(timeout=15)
            except concurrent.futures.TimeoutError:
                logger.warning(f"[{trace_id}] LLM新闻增强超时(>=15s)，降级跳过")
                future.cancel()
                return None
        except Exception as e:
            logger.warning(f"[{trace_id}] LLM同步调用失败: {e}")
            return None
        finally:
            executor.shutdown(wait=False)

    async def _call_llm_news_enhancement(
        self,
        code: str,
        news_list: list,
        sentiment_score: float,
        trace_id: str,
    ):
        """调用 LLM 进行新闻深度分析

        Args:
            code: 股票代码
            news_list: 新闻列表（dict 或 NewsItem dataclass 均可）
            sentiment_score: 已计算的情绪分值
            trace_id: 链路追踪 ID

        Returns:
            dict 或 None
        """
        from app.ai.llm_market_analyst import NewsContext

        headlines = []
        summary_parts = []
        has_breaking = False
        ages = []

        for news in news_list[:10]:
            if isinstance(news, dict):
                title = news.get('title', '')
                content = news.get('content', '') or news.get('summary', '')
                importance = news.get('importance', '')
                age = news.get('age_minutes', 30)
                is_breaking = news.get('is_breaking', False)
            else:
                title = getattr(news, 'title', '')
                content = getattr(news, 'content', '') or getattr(news, 'summary', '')
                importance = getattr(news, 'importance', '')
                age = getattr(news, 'age_minutes', 30)
                is_breaking = getattr(news, 'is_breaking', False)

            if not title:
                continue

            headlines.append(title)
            snippet = str(content)[:200] if content else ''
            if snippet:
                summary_parts.append(f"{title}: {snippet}")
            if importance in ('BREAKING', 'breaking', 'critical') or is_breaking:
                has_breaking = True
            ages.append(age)

        if not headlines:
            return None

        news_context = NewsContext(
            headlines=headlines,
            sentiment_score=sentiment_score,
            news_count=len(news_list),
            top_news_summary="\n".join(summary_parts[:5]),
            avg_age_minutes=sum(ages) / len(ages) if ages else 30,
            has_breaking=has_breaking,
        )

        assessment = await self._llm_analyst.analyze(
            code=code,
            news_context=news_context,
            trigger_type="NEWS_ONLY",
        )

        result = {
            'llm_direction': assessment.direction,
            'llm_confidence': assessment.confidence_level,
            'llm_confidence_value': assessment.confidence_value,
            'llm_timing': assessment.recommended_timing,
            'llm_key_factors': assessment.key_factors,
            'llm_risk_flags': assessment.risk_flags,
            'llm_reasoning': assessment.reasoning,
        }

        if assessment.news_impact:
            result['llm_news_severity'] = assessment.news_impact.severity
            result['llm_news_duration_minutes'] = assessment.news_impact.duration_minutes
            result['llm_affected_codes'] = assessment.news_impact.affected_codes

        logger.info(
            f"[{trace_id}] LLM新闻增强: {code} direction={assessment.direction} "
            f"confidence={assessment.confidence_level}"
        )
        return result
    
    def _parse_published_at(self, published_at_str: str) -> 'datetime':
        """解析新闻发布时间，统一转换为本地 naive datetime"""
        if not published_at_str:
            return None
        try:
            s = published_at_str.strip()
            # RSS GMT 格式
            if 'GMT' in s or (', ' in s and len(s) > 20):
                from email.utils import parsedate_to_datetime
                import calendar
                try:
                    dt_aware = parsedate_to_datetime(s)
                    utc_ts = calendar.timegm(dt_aware.utctimetuple())
                    return datetime.fromtimestamp(utc_ts)
                except Exception:
                    pass
            # 带时区 ISO 格式
            if '+' in s or s.endswith('Z'):
                s2 = s.replace('Z', '+00:00')
                try:
                    from dateutil.parser import parse as _parse
                    import calendar
                    dt_aware = _parse(s2)
                    utc_ts = calendar.timegm(dt_aware.utctimetuple())
                    return datetime.fromtimestamp(utc_ts)
                except Exception:
                    return datetime.fromisoformat(s2.split('+')[0].rstrip('Z'))
            # 纯 ISO
            return datetime.fromisoformat(s)
        except Exception as e:
            logger.debug(f"时间解析失败：{published_at_str!r} - {e}")
            return None

    def _fetch_news(self, code: str, lookback_hours: int) -> List[Dict]:
        """获取新闻"""
        if not self.collector:
            return []
        
        try:
            cutoff_time = datetime.now() - timedelta(hours=lookback_hours)
            
            # 获取所有新闻
            all_news = self.collector.fetch_all()
            
            # 过滤时间
            filtered_news = []
            for news in all_news:
                try:
                    published_at_str = getattr(news, 'published_at', '')
                    if published_at_str:
                        published_at = self._parse_published_at(published_at_str)
                        if published_at is None or published_at >= cutoff_time:
                            filtered_news.append(news)
                    else:
                        # 无时间信息，保留新闻
                        filtered_news.append(news)
                except Exception as e:
                    logger.debug(f"时间过滤失败：{e}")
                    filtered_news.append(news)
            
            # 按优先级排序
            filtered_news.sort(key=lambda x: getattr(x, 'priority', 5))
            
            # 突发新闻快速通道：优先返回
            breaking_news = [n for n in filtered_news if getattr(n, 'is_breaking', False)]
            normal_news = [n for n in filtered_news if not getattr(n, 'is_breaking', False)]
            
            # 突发新闻排在前面
            result_news = breaking_news[:5] + normal_news[:15]
            
            logger.info(
                f"获取新闻：{code} - {len(result_news)} 条 "
                f"(突发{len(breaking_news)}条，最近{lookback_hours}小时)"
            )
            
            return result_news
        
        except Exception as e:
            logger.error(f"获取新闻失败：{e}")
            return []
    
    def get_sentiment_trend(self, code: str, days: int = 7) -> Dict:
        """
        获取情绪趋势

        Args:
            code: 股票代码
            days: 天数

        Returns:
            {
                'trend': 'improving' | 'worsening' | 'stable',
                'daily_scores': [...],
                'avg_score': 0.5,
                'daily_decayed_scores': [...]  # 新增：衰减后得分趋势
            }
        """
        daily_scores = []
        daily_decayed_scores = []
        
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            result = self.analyze_stock(code, lookback_hours=24)
            daily_scores.append({
                'date': date.strftime('%Y-%m-%d'),
                'score': result['sentiment_score']
            })
            daily_decayed_scores.append({
                'date': date.strftime('%Y-%m-%d'),
                'decayed_score': result.get('decayed_score', result['sentiment_score'])
            })
        
        # 计算趋势 (使用衰减后得分)
        if len(daily_decayed_scores) >= 2:
            recent_avg = sum(s['decayed_score'] for s in daily_decayed_scores[:3]) / 3
            older_avg = sum(s['decayed_score'] for s in daily_decayed_scores[3:]) / max(1, len(daily_decayed_scores) - 3)
            
            if recent_avg > older_avg + 0.1:
                trend = 'improving'
            elif recent_avg < older_avg - 0.1:
                trend = 'worsening'
            else:
                trend = 'stable'
        else:
            trend = 'stable'
        
        return {
            'trend': trend,
            'daily_scores': daily_scores,
            'daily_decayed_scores': daily_decayed_scores,
            'avg_score': sum(s['score'] for s in daily_scores) / len(daily_scores) if daily_scores else 0,
        }
    
    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()
        logger.info("新闻情绪分析缓存已清空")


# 全局单例
_analyzer = None

def get_news_sentiment_analyzer() -> NewsSentimentAnalyzer:
    """获取新闻情绪分析器单例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = NewsSentimentAnalyzer()
    return _analyzer


# 命令行工具
if __name__ == '__main__':
    import sys
    
    analyzer = get_news_sentiment_analyzer()
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 news_integration.py HK.00700 [hours]")
        sys.exit(1)
    
    code = sys.argv[1]
    hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    
    print(f"分析股票：{code}")
    print(f"回溯时间：{hours}小时")
    print()
    
    result = analyzer.analyze_stock(code, lookback_hours=hours)
    
    print(f"原始情绪得分：{result['sentiment_score']:.2f}")
    print(f"衰减后得分：{result.get('decayed_score', 'N/A')}")
    print(f"新闻数量：{result['news_count']}")
    print(f"正面：{result['positive_count']} | 负面：{result['negative_count']} | 中性：{result['neutral_count']}")
    print(f"平均年龄：{result['avg_age_minutes']}分钟")
    print(f"市场波动状态：{result['volatility_state']}")
    print(f"缓存 TTL: {result['cache_ttl']}秒")
    print()
    
    if result['top_news']:
        print("Top 新闻 (按衰减后得分排序):")
        for i, news in enumerate(result['top_news'], 1):
            sentiment_emoji = '🟢' if news['decayed_score'] > 0.2 else '🔴' if news['decayed_score'] < -0.2 else '⚪'
            print(f"  {i}. {sentiment_emoji} {news['title'][:80]}...")
            print(f"     原始得分：{news['sentiment_score']:.2f} | 衰减后：{news['decayed_score']:.2f}")
            print(f"     来源：{news['source']} | 年龄：{news['age_minutes']}分钟 | 突发：{'是' if news['is_breaking'] else '否'}")
