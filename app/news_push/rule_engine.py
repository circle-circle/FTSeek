#!/usr/bin/env python3
"""
推送规则引擎

功能:
- 关键词匹配 (必须包含/必须排除)
- 来源优先级过滤
- 分类开关控制
- 股票代码匹配
"""
import logging
import re
from typing import List, Optional, Tuple
from dataclasses import dataclass

from app.news_push.config import (
    PUSH_KEYWORDS, SOURCE_PRIORITY, CATEGORY_ENABLED,
    WATCH_STOCKS, STOCK_ALIASES
)
from app.news_push.collector import NewsItem

logger = logging.getLogger("ftseek.news_push.rule_engine")


@dataclass
class RuleResult:
    """规则匹配结果"""
    passed: bool
    reason: str
    score: float = 0.0
    matched_keywords: List[str] = None
    matched_stocks: List[str] = None
    
    def __post_init__(self):
        if self.matched_keywords is None:
            self.matched_keywords = []
        if self.matched_stocks is None:
            self.matched_stocks = []


class RuleEngine:
    """
    推送规则引擎
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        
        # 加载监控股票
        self.watch_stocks = {
            item['code']: item for item in WATCH_STOCKS 
            if item.get('enabled', True)
        }
        
        logger.info(f"✅ 规则引擎已初始化 | 监控股票：{len(self.watch_stocks)} 只")
    
    def check_category(self, news: NewsItem) -> RuleResult:
        """
        检查分类开关
        
        Returns:
            RuleResult
        """
        category = news.category
        
        if not CATEGORY_ENABLED.get(category, True):
            return RuleResult(
                passed=False,
                reason=f"分类未启用：{category}"
            )
        
        return RuleResult(
            passed=True,
            reason=f"分类已启用：{category}"
        )
    
    def check_source_priority(self, news: NewsItem) -> RuleResult:
        """
        检查来源优先级
        
        Returns:
            RuleResult (包含优先级分数)
        """
        source = news.source
        priority = SOURCE_PRIORITY.get(source, 5)
        
        # 优先级 1-3 为高优先级，直接通过
        if priority <= 3:
            return RuleResult(
                passed=True,
                reason=f"高优先级来源：{source}",
                score=1.0 - (priority / 10.0)
            )
        
        # 优先级 4-5 需要进一步过滤
        return RuleResult(
            passed=True,
            reason=f"普通优先级来源：{source}",
            score=0.5 - (priority / 20.0)
        )
    
    def check_keywords(self, news: NewsItem) -> RuleResult:
        """
        检查关键词匹配
        
        Returns:
            RuleResult
        """
        text = f"{news.title} {news.content} {news.summary}".lower()
        
        # 检查必须排除的关键词
        exclude_keywords = PUSH_KEYWORDS.get('must_exclude', [])
        for kw in exclude_keywords:
            if kw.lower() in text:
                return RuleResult(
                    passed=False,
                    reason=f"包含排除关键词：{kw}",
                    matched_keywords=[kw]
                )
        
        # 检查必须包含的关键词
        include_keywords = PUSH_KEYWORDS.get('must_include', [])
        matched = []
        for kw in include_keywords:
            if kw.lower() in text:
                matched.append(kw)
        
        if matched:
            return RuleResult(
                passed=True,
                reason=f"匹配关键词：{', '.join(matched[:5])}",
                score=min(1.0, len(matched) / 5.0),
                matched_keywords=matched
            )
        
        # 没有匹配关键词，但也不在排除列表中
        return RuleResult(
            passed=True,
            reason="无关键词匹配 (但未被排除)",
            score=0.3
        )
    
    def check_stock_match(self, news: NewsItem) -> RuleResult:
        """
        检查股票代码匹配
        
        Returns:
            RuleResult
        """
        text = f"{news.title} {news.content} {news.summary}".lower()
        
        # 使用已有的 related_stocks
        matched_stocks = news.related_stocks
        
        # 如果没有预匹配，再尝试一次
        if not matched_stocks:
            for code, aliases in STOCK_ALIASES.items():
                if code in self.watch_stocks:
                    for alias in aliases:
                        if alias.lower() in text:
                            matched_stocks.append(code)
                            break
        
        matched_stocks = list(set(matched_stocks))
        
        if matched_stocks:
            # 检查这些股票是否在监控列表中
            watched = [s for s in matched_stocks if s in self.watch_stocks]
            
            if watched:
                stock_names = [self.watch_stocks[s]['name'] for s in watched]
                return RuleResult(
                    passed=True,
                    reason=f"匹配监控股票：{', '.join(stock_names)}",
                    score=1.0,
                    matched_stocks=watched
                )
            else:
                return RuleResult(
                    passed=True,
                    reason=f"匹配股票 (非监控): {', '.join(matched_stocks)}",
                    score=0.5,
                    matched_stocks=matched_stocks
                )
        
        return RuleResult(
            passed=True,
            reason="无股票匹配",
            score=0.2
        )
    
    def evaluate(self, news: NewsItem) -> Tuple[bool, RuleResult]:
        """
        综合评估新闻是否应该推送
        
        评估流程:
        1. 分类开关检查
        2. 关键词过滤
        3. 股票匹配 (加分项)
        4. 来源优先级 (加分项)
        
        Returns:
            (是否推送，评估结果)
        """
        results = []
        total_score = 0.0
        
        # 1. 分类开关 (硬性条件)
        category_result = self.check_category(news)
        results.append(category_result)
        if not category_result.passed:
            return False, category_result
        
        # 2. 关键词过滤 (硬性条件)
        keyword_result = self.check_keywords(news)
        results.append(keyword_result)
        if not keyword_result.passed:
            return False, keyword_result
        
        # 3. 股票匹配 (加分项)
        stock_result = self.check_stock_match(news)
        results.append(stock_result)
        total_score += stock_result.score * 0.5  # 50% 权重
        
        # 4. 来源优先级 (加分项)
        source_result = self.check_source_priority(news)
        results.append(source_result)
        total_score += source_result.score * 0.3  # 30% 权重
        
        # 5. 情绪分析 (加分项)
        sentiment_score = abs(news.sentiment_score) * news.sentiment_confidence
        total_score += sentiment_score * 0.2  # 20% 权重
        
        # 综合评分
        final_score = min(1.0, total_score)
        
        # 判断是否推送 (阈值 0.4)
        threshold = 0.4
        
        # 如果是快讯类或匹配监控股票，降低阈值
        if news.category == 'flash' or stock_result.matched_stocks:
            threshold = 0.3
        
        passed = final_score >= threshold
        
        reason_parts = [r.reason for r in results if r.passed]
        
        result = RuleResult(
            passed=passed,
            reason="; ".join(reason_parts),
            score=final_score,
            matched_keywords=keyword_result.matched_keywords,
            matched_stocks=stock_result.matched_stocks
        )
        
        if passed:
            logger.debug(f"✅ 推送：{news.title[:50]}... (分数={final_score:.2f})")
        else:
            logger.debug(f"❌ 过滤：{news.title[:50]}... (分数={final_score:.2f} < {threshold})")
        
        return passed, result
    
    def batch_evaluate(self, news_list: List[NewsItem]) -> List[Tuple[NewsItem, RuleResult]]:
        """
        批量评估新闻列表
        
        Returns:
            [(新闻，评估结果), ...]
        """
        results = []
        
        for news in news_list:
            passed, result = self.evaluate(news)
            if passed:
                results.append((news, result))
        
        # 按分数排序
        results.sort(key=lambda x: x[1].score, reverse=True)
        
        logger.info(f"📊 批量评估：{len(news_list)} 条 -> {len(results)} 条符合推送条件")
        
        return results


# 全局单例
_engine: Optional[RuleEngine] = None


def get_rule_engine(config: dict = None) -> RuleEngine:
    """获取规则引擎单例"""
    global _engine
    if _engine is None:
        _engine = RuleEngine(config)
    return _engine


if __name__ == "__main__":
    # 测试
    from app.news_push.collector import get_collector
    
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    collector = get_collector()
    news_list = collector.fetch_all()
    
    engine = get_rule_engine()
    results = engine.batch_evaluate(news_list)
    
    print(f"\n📋 符合推送条件的新闻 ({len(results)} 条):\n")
    for news, result in results[:10]:
        print(f"📰 {news.title}")
        print(f"   来源：{news.source} | 分数：{result.score:.2f}")
        print(f"   原因：{result.reason}")
        if result.matched_stocks:
            print(f"   股票：{result.matched_stocks}")
        print()
    
    collector.close()
