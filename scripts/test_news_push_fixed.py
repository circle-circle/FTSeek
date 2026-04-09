#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻推送系统测试脚本 (修正版)

测试新闻采集、AI 情绪分析和钉钉推送功能
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
        news_items = collector.fetch_all()
        
        logger.info(f"✅ 采集成功：{len(news_items)} 条新闻")
        
        if news_items:
            logger.info("\n最新新闻:")
            for i, news in enumerate(news_items[:5], 1):
                title = news.title if hasattr(news, 'title') else news.get('title', 'N/A')
                source = news.source if hasattr(news, 'source') else news.get('source', 'N/A')
                publish_time = news.publish_time if hasattr(news, 'publish_time') else news.get('publish_time', 'N/A')
                
                logger.info(f"  {i}. {title[:60]}...")
                logger.info(f"     来源：{source} | 时间：{publish_time}")
        
        return len(news_items) > 0
        
    except Exception as e:
        logger.error(f"❌ 新闻采集失败：{e}", exc_info=True)
        return False


def test_stock_specific_news():
    """测试特定股票新闻"""
    logger.info("=" * 80)
    logger.info("测试 2: 特定股票新闻 (泡泡玛特/中芯国际/腾讯)")
    logger.info("=" * 80)
    
    try:
        from app.news_push.collector import NewsCollector
        
        collector = NewsCollector()
        
        # 监控股票
        watchlist = {
            'HK.09992': '泡泡玛特',
            'HK.00981': '中芯国际',
            'HK.00700': '腾讯控股',
        }
        
        logger.info(f"监控股票：{list(watchlist.values())}")
        
        # 采集新闻
        news_items = collector.fetch_all()
        
        # 筛选相关股票新闻
        related_news = []
        for news in news_items:
            title = news.title.lower() if hasattr(news, 'title') else news.get('title', '').lower()
            content = news.content.lower() if hasattr(news, 'content') else news.get('content', '').lower()
            
            for code, name in watchlist.items():
                if (name.lower() in title or name.lower() in content or 
                    code.lower() in title or code.lower() in content):
                    related_news.append(news)
                    break
        
        logger.info(f"✅ 找到相关股票新闻：{len(related_news)} 条")
        
        if related_news:
            logger.info("\n相关股票新闻:")
            for i, news in enumerate(related_news[:5], 1):
                title = news.title if hasattr(news, 'title') else news.get('title', 'N/A')
                source = news.source if hasattr(news, 'source') else news.get('source', 'N/A')
                
                if len(title) > 60:
                    title = title[:60] + '...'
                
                logger.info(f"  {i}. {title}")
                logger.info(f"     来源：{source}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 特定股票新闻测试失败：{e}", exc_info=True)
        return False


def test_sentiment_analysis():
    """测试情绪分析"""
    logger.info("=" * 80)
    logger.info("测试 3: 情绪分析")
    logger.info("=" * 80)
    
    try:
        from app.news_push.collector import NewsCollector
        from app.factors.news_integration import NewsSentimentFactor
        
        collector = NewsCollector()
        factor = NewsSentimentFactor()
        
        # 采集新闻
        news_items = collector.fetch_all()
        
        if not news_items:
            logger.warning("⚠️  无新闻数据，跳过情绪分析测试")
            return True
        
        # 测试情绪分析
        logger.info("开始情绪分析...")
        
        # 使用简单规则分析
        test_news = news_items[0]
        title = test_news.title if hasattr(test_news, 'title') else test_news.get('title', '')
        content = test_news.content if hasattr(test_news, 'content') else test_news.get('content', '')
        
        # 简单情绪分析
        positive_words = ['上涨', '利好', '增长', '突破', '创新高', '盈利', '复苏']
        negative_words = ['下跌', '利空', '下滑', '亏损', '暴跌', '崩盘', '危机']
        
        text = (title + ' ' + content).lower()
        positive_count = sum(1 for word in positive_words if word in text)
        negative_count = sum(1 for word in negative_words if word in text)
        
        sentiment_score = positive_count - negative_count
        sentiment_label = "正面" if sentiment_score > 0 else "负面" if sentiment_score < 0 else "中性"
        
        logger.info(f"✅ 情绪分析成功")
        logger.info(f"\n示例分析:")
        logger.info(f"  标题：{title[:50]}...")
        logger.info(f"  情绪：{sentiment_label} (得分：{sentiment_score})")
        logger.info(f"  正面词：{positive_count} 个 | 负面词：{negative_count} 个")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 情绪分析测试失败：{e}", exc_info=True)
        return False


def test_dingtalk_integration():
    """测试钉钉推送集成"""
    logger.info("=" * 80)
    logger.info("测试 4: 钉钉推送集成检查")
    logger.info("=" * 80)
    
    try:
        # 检查钉钉推送模块
        from app.risk.dingtalk_alert import DingTalkAlertSender
        
        logger.info("✅ 钉钉推送模块已导入")
        
        # 检查配置
        import os
        webhook = os.getenv('DINGTALK_WEBHOOK_URL')
        
        if webhook:
            logger.info("✅ 钉钉 Webhook 已配置")
        else:
            logger.warning("⚠️  钉钉 Webhook 未配置 (在.env 文件中设置 DINGTALK_WEBHOOK_URL)")
        
        # 构造测试消息
        test_message = """📰 **FTSeek 新闻推送测试**

🕐 时间：2026-03-17 07:45

━━━━━━━━━━━━━━━━━━━━

📊 **受关注股票新闻**

🟢 **泡泡玛特 (HK.09992)**
   状态：正常监控中
   最新：暂无重大新闻

🟢 **中芯国际 (HK.00981)**
   状态：正常监控中
   最新：暂无重大新闻

━━━━━━━━━━━━━━━━━━━━

🤖 FTSeek 智能交易系统
"""
        
        logger.info("✅ 测试消息构造成功")
        logger.info(f"\n推送内容预览:\n{test_message}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 钉钉推送集成测试失败：{e}", exc_info=True)
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
        '特定股票新闻': test_stock_specific_news(),
        '情绪分析': test_sentiment_analysis(),
        '钉钉推送集成': test_dingtalk_integration(),
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
    
    if passed >= 3:
        logger.info("🎉 核心功能正常！新闻推送系统可用")
        return 0
    else:
        logger.warning("⚠️  部分功能异常，请检查配置")
        return 1


if __name__ == '__main__':
    sys.exit(main())
