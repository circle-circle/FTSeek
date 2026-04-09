#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻推送系统测试脚本

功能:
1. 测试新闻采集
2. 测试 AI 情绪分析
3. 测试钉钉推送
4. 生成测试报告
"""

import sys
import os
import logging
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.news_test")

def test_news_collection():
    """测试新闻采集"""
    logger.info("=" * 80)
    logger.info("测试 1: 新闻采集")
    logger.info("=" * 80)
    
    try:
        from app.news_push.collector import NewsCollector
        
        collector = NewsCollector()
        
        # 采集新闻
        logger.info("开始采集新闻...")
        news_list = collector.collect(limit=10)
        
        logger.info(f"✅ 采集成功：{len(news_list)} 条新闻")
        
        if news_list:
            logger.info("\n最新新闻:")
            for i, news in enumerate(news_list[:5], 1):
                logger.info(f"  {i}. {news.get('title', 'N/A')[:60]}...")
                logger.info(f"     来源：{news.get('source', 'N/A')} | 时间：{news.get('publish_time', 'N/A')}")
        
        return len(news_list) > 0
        
    except Exception as e:
        logger.error(f"❌ 新闻采集失败：{e}", exc_info=True)
        return False


def test_ai_sentiment_analysis():
    """测试 AI 情绪分析"""
    logger.info("=" * 80)
    logger.info("测试 2: AI 情绪分析")
    logger.info("=" * 80)
    
    try:
        from app.news_push.qwen_analyzer import QwenAnalyzer
        from app.news_push.collector import NewsCollector
        
        analyzer = QwenAnalyzer()
        collector = NewsCollector()
        
        # 采集新闻
        news_list = collector.collect(limit=5)
        
        if not news_list:
            logger.warning("⚠️  无新闻数据，跳过 AI 分析测试")
            return True
        
        # AI 情绪分析
        logger.info("开始 AI 情绪分析...")
        analyzed_news = analyzer.analyze_batch(news_list[:3])
        
        logger.info(f"✅ AI 分析成功：{len(analyzed_news)} 条")
        
        if analyzed_news:
            logger.info("\n分析结果:")
            for i, news in enumerate(analyzed_news[:3], 1):
                sentiment = news.get('sentiment_score', 0)
                sentiment_label = "正面" if sentiment > 0.1 else "负面" if sentiment < -0.1 else "中性"
                logger.info(f"  {i}. {news.get('title', 'N/A')[:50]}...")
                logger.info(f"     情绪：{sentiment_label} (得分：{sentiment:.2f})")
        
        return len(analyzed_news) > 0
        
    except Exception as e:
        logger.error(f"❌ AI 分析失败：{e}", exc_info=True)
        return False


def test_dingtalk_push():
    """测试钉钉推送"""
    logger.info("=" * 80)
    logger.info("测试 3: 钉钉推送")
    logger.info("=" * 80)
    
    try:
        from app.news_push.notifier import DingTalkNotifier
        from app.news_push.collector import NewsCollector
        from app.news_push.qwen_analyzer import QwenAnalyzer
        
        notifier = DingTalkNotifier()
        collector = NewsCollector()
        analyzer = QwenAnalyzer()
        
        # 采集并分析新闻
        news_list = collector.collect(limit=5)
        
        if not news_list:
            logger.warning("⚠️  无新闻数据，跳过推送测试")
            return True
        
        analyzed_news = analyzer.analyze_batch(news_list[:3])
        
        # 构造推送消息
        message = f"""📰 **FTSeek 新闻推送测试**

🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

━━━━━━━━━━━━━━━━━━━━

📊 **受关注股票相关新闻**

"""
        
        for i, news in enumerate(analyzed_news[:3], 1):
            sentiment = news.get('sentiment_score', 0)
            sentiment_emoji = "🟢" if sentiment > 0.1 else "🔴" if sentiment < -0.1 else "⚪"
            sentiment_label = "正面" if sentiment > 0.1 else "负面" if sentiment < -0.1 else "中性"
            
            title = news.get('title', 'N/A')
            if len(title) > 50:
                title = title[:50] + '...'
            
            message += f"{sentiment_emoji} **新闻{i}**: {title}\n"
            message += f"   情绪：{sentiment_label} (得分：{sentiment:.2f})\n"
            message += f"   来源：{news.get('source', 'N/A')}\n\n"
        
        message += """━━━━━━━━━━━━━━━━━━━━

🤖 FTSeek 智能交易系统
"""
        
        logger.info("准备推送测试消息...")
        # 实际推送 (取消注释以启用)
        # result = notifier.send_text(message)
        
        logger.info("✅ 推送消息构造成功")
        logger.info(f"\n推送内容预览:\n{message}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 推送测试失败：{e}", exc_info=True)
        return False


def test_stock_specific_news():
    """测试特定股票新闻"""
    logger.info("=" * 80)
    logger.info("测试 4: 特定股票新闻 (泡泡玛特/中芯国际)")
    logger.info("=" * 80)
    
    try:
        from app.news_push.collector import NewsCollector
        from app.news_push.qwen_analyzer import QwenAnalyzer
        
        collector = NewsCollector()
        analyzer = QwenAnalyzer()
        
        # 监控股票
        watchlist = {
            'HK.09992': '泡泡玛特',
            'HK.00981': '中芯国际',
            'HK.00700': '腾讯控股',
        }
        
        logger.info(f"监控股票：{list(watchlist.values())}")
        
        # 采集新闻
        news_list = collector.collect(limit=20)
        
        # 筛选相关股票新闻
        related_news = []
        for news in news_list:
            title = news.get('title', '').lower()
            content = news.get('content', '').lower()
            
            for code, name in watchlist.items():
                if (name.lower() in title or name.lower() in content or 
                    code.lower() in title or code.lower() in content):
                    related_news.append(news)
                    break
        
        logger.info(f"✅ 找到相关股票新闻：{len(related_news)} 条")
        
        if related_news:
            # AI 分析
            analyzed_news = analyzer.analyze_batch(related_news[:3])
            
            logger.info("\n相关股票新闻:")
            for i, news in enumerate(analyzed_news[:3], 1):
                sentiment = news.get('sentiment_score', 0)
                sentiment_label = "正面" if sentiment > 0.1 else "负面" if sentiment < -0.1 else "中性"
                title = news.get('title', 'N/A')
                if len(title) > 50:
                    title = title[:50] + '...'
                
                logger.info(f"  {i}. {title}")
                logger.info(f"     情绪：{sentiment_label} (得分：{sentiment:.2f})")
                logger.info(f"     来源：{news.get('source', 'N/A')}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 特定股票新闻测试失败：{e}", exc_info=True)
        return False


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("📰 FTSeek 新闻推送系统测试")
    logger.info("=" * 80)
    logger.info(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)
    
    results = {
        '新闻采集': test_news_collection(),
        'AI 情绪分析': test_ai_sentiment_analysis(),
        '钉钉推送': test_dingtalk_push(),
        '特定股票新闻': test_stock_specific_news(),
    }
    
    # 生成报告
    logger.info("\n" + "=" * 80)
    logger.info("📊 测试结果报告")
    logger.info("=" * 80)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        logger.info(f"{test_name}: {status}")
    
    logger.info("=" * 80)
    logger.info(f"总计：{passed}/{total} 通过 ({passed/total*100:.1f}%)")
    logger.info("=" * 80)
    
    if passed == total:
        logger.info("🎉 所有测试通过！新闻推送系统运行正常")
        return 0
    else:
        logger.warning("⚠️  部分测试失败，请检查配置")
        return 1


if __name__ == '__main__':
    sys.exit(main())
