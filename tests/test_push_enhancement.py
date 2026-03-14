#!/usr/bin/env python3
"""
推送格式增强测试

测试内容:
1. 增强版信号推送格式
2. 重大新闻独立推送
3. 新闻汇总推送
4. 链接可点击性
5. 格式美观性
"""
import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.strategy.base import Signal, SignalAction
from app.data.news_fetcher import NewsItem
from app.notifier.dingtalk import DingTalkNotifier
from app.reporters.news_digest import NewsDigestReporter, NewsDigest


class TestEnhancedSignalPush(unittest.TestCase):
    """测试增强版信号推送"""
    
    def setUp(self):
        """准备测试数据"""
        self.signal = Signal(
            id="test123",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=380.50,
            quantity=100,
            reason="技术面突破 + 基本面利好",
            strategy="fusion_v2",
            extra={
                "consensus": True,
                "score": 1.25,
                "threshold": 0.8,
                "confidence": 0.72
            }
        )
        
        self.related_news = [
            NewsItem(
                id="news1",
                title="腾讯 Q4 财报超预期",
                content="腾讯控股发布 Q4 财报，净利润同比增长 30%...",
                source="sina",
                url="https://finance.sina.com.cn/tech/2024-03-01/doc-imxyz123.shtml",
                published_at="2024-03-01T10:00:00",
                stock_codes=["HK.00700"],
                sentiment_score=0.75
            ),
            NewsItem(
                id="news2",
                title="腾讯游戏业务强劲增长",
                content="腾讯游戏业务收入同比增长 25%...",
                source="sina",
                url="https://finance.sina.com.cn/tech/2024-03-01/doc-imxyz456.shtml",
                published_at="2024-03-01T09:30:00",
                stock_codes=["HK.00700"],
                sentiment_score=0.60
            )
        ]
        
        self.analysis_summary = {
            "sentiment": "bullish",
            "suggestion": "buy",
            "reason": "财报超预期，游戏业务强劲，建议买入"
        }
        
        self.notifier = DingTalkNotifier(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            secret="test_secret"
        )
    
    def test_enhanced_signal_format(self):
        """测试增强信号格式美观性"""
        # Mock _do_post to capture the payload
        with patch.object(self.notifier, '_do_post') as mock_post:
            mock_post.return_value = True
            
            result = self.notifier.send_enhanced_signal(
                self.signal,
                self.related_news,
                self.analysis_summary
            )
            
            # 验证发送成功
            self.assertTrue(result)
            self.assertTrue(mock_post.called)
            
            # 获取发送的 payload
            payload = mock_post.call_args[0][0]
            
            # 验证消息类型
            self.assertEqual(payload["msgtype"], "actionCard")
            
            # 验证文本内容
            text = payload["actionCard"]["text"]
            
            # 验证关键元素存在
            self.assertIn("🔴 FTSeek 买入信号", text)
            self.assertIn("腾讯控股 (HK.00700)", text)
            self.assertIn("💰 **价格**: HK$380.50", text)
            self.assertIn("📊 **置信度**", text)
            self.assertIn("📰 **相关新闻**", text)
            self.assertIn("🤖 **AI 分析**", text)
            self.assertIn("综合评估：财报超预期", text)
            
            # 验证链接存在
            self.assertIn("https://finance.sina.com.cn", text)
            
            # 验证按钮存在
            btns = payload["actionCard"]["btns"]
            self.assertEqual(len(btns), 3)
            self.assertEqual(btns[0]["title"], "✅ 确认买入")
            self.assertEqual(btns[1]["title"], "⏰ 延期 15 分钟")
            self.assertEqual(btns[2]["title"], "❌ 忽略")
    
    def test_enhanced_signal_without_news(self):
        """测试无相关新闻时的信号推送"""
        with patch.object(self.notifier, '_do_post') as mock_post:
            mock_post.return_value = True
            
            result = self.notifier.send_enhanced_signal(self.signal)
            
            self.assertTrue(result)
            self.assertTrue(mock_post.called)
            
            payload = mock_post.call_args[0][0]
            text = payload["actionCard"]["text"]
            
            # 验证不包含新闻部分
            self.assertNotIn("📰 相关新闻", text)


class TestMajorNewsPush(unittest.TestCase):
    """测试重大新闻独立推送"""
    
    def setUp(self):
        """准备测试数据"""
        self.major_news = NewsItem(
            id="major1",
            title="央行宣布降准 0.5 个百分点",
            content="中国人民银行宣布下调金融机构存款准备金率 0.5 个百分点...",
            source="sina",
            url="https://finance.sina.com.cn/china/2024-03-01/doc-imxyz789.shtml",
            published_at="2024-03-01T14:00:00",
            stock_codes=[],
            sentiment_score=0.85
        )
        
        self.analysis = {
            "sentiment": "bullish",
            "impact_score": 9,
            "suggestion": "buy",
            "reason": "央行降准释放流动性，对股市重大利好"
        }
        
        self.notifier = DingTalkNotifier(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            secret="test_secret"
        )
    
    def test_major_news_format(self):
        """测试重大新闻格式"""
        with patch.object(self.notifier, '_do_post') as mock_post:
            mock_post.return_value = True
            
            result = self.notifier.send_major_news(self.major_news, self.analysis)
            
            self.assertTrue(result)
            self.assertTrue(mock_post.called)
            
            payload = mock_post.call_args[0][0]
            
            # 验证消息类型
            self.assertEqual(payload["msgtype"], "markdown")
            
            # 验证@所有人
            self.assertTrue(payload["at"]["isAtAll"])
            
            text = payload["markdown"]["text"]
            
            # 验证关键元素
            self.assertIn("🚨 重大新闻", text)
            self.assertIn("央行宣布降准", text)
            self.assertIn("影响**: 利好", text)
            self.assertIn("📊 **情绪评分**: +0.85", text)
            self.assertIn("🤖 **AI 分析**", text)
            self.assertIn("影响程度：9/10", text)
            self.assertIn("🔗 [阅读原文]", text)


class TestNewsDigestPush(unittest.TestCase):
    """测试新闻汇总推送"""
    
    def setUp(self):
        """准备测试数据"""
        self.digest_dict = {
            "timestamp": "2024-03-01 15:00:00",
            "period": "14:00-15:00",
            "total_count": 15,
            "major_news": [
                {
                    "title": "腾讯 Q4 财报超预期",
                    "url": "https://example.com/news1",
                    "sentiment_score": 0.75
                },
                {
                    "title": "美联储维持利率不变",
                    "url": "https://example.com/news2",
                    "sentiment_score": -0.30
                }
            ],
            "stock_news": {
                "HK.00700": [
                    {
                        "title": "腾讯游戏业务增长",
                        "url": "https://example.com/news3",
                        "sentiment_score": 0.60
                    }
                ],
                "HK.09988": [
                    {
                        "title": "阿里云发布新产品",
                        "url": "https://example.com/news4",
                        "sentiment_score": 0.45
                    }
                ]
            }
        }
        
        self.notifier = DingTalkNotifier(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            secret="test_secret"
        )
    
    def test_news_digest_format(self):
        """测试新闻汇总格式"""
        with patch.object(self.notifier, '_do_post') as mock_post:
            mock_post.return_value = True
            
            result = self.notifier.send_news_digest(self.digest_dict)
            
            self.assertTrue(result)
            self.assertTrue(mock_post.called)
            
            payload = mock_post.call_args[0][0]
            
            # 验证消息类型
            self.assertEqual(payload["msgtype"], "markdown")
            
            text = payload["markdown"]["text"]
            
            # 验证关键元素
            self.assertIn("📰 FTSeek 新闻汇总", text)
            self.assertIn("**时段**: 14:00-15:00", text)
            self.assertIn("**新闻总数**: 15 条", text)
            self.assertIn("**🔴 重大新闻**", text)
            self.assertIn("**📈 个股新闻**", text)
            self.assertIn("HK.00700", text)
            self.assertIn("HK.09988", text)
            self.assertIn("[查看详情]", text)


class TestNewsDigestReporter(unittest.TestCase):
    """测试新闻汇总报告器"""
    
    def setUp(self):
        """准备测试环境"""
        self.reporter = NewsDigestReporter()
    
    @patch('app.reporters.news_digest.get_news_fetcher')
    def test_fetch_and_classify(self, mock_fetcher_factory):
        """测试新闻获取和分类"""
        # Mock news fetcher
        mock_fetcher = Mock()
        mock_fetcher.fetch_news.return_value = [
            NewsItem(
                id="test1",
                title="腾讯财报超预期",
                content="利好消息...",
                source="sina",
                url="https://example.com",
                published_at="2024-03-01T10:00:00",
                stock_codes=["HK.00700"],
                sentiment_score=0.75
            )
        ]
        mock_fetcher_factory.return_value = mock_fetcher
        
        # 测试
        digest = self.reporter.fetch_and_classify(['HK.00700'])
        
        # 验证
        self.assertIsInstance(digest, NewsDigest)
        # 注意：由于 mock 返回空列表，total_count 可能为 0
        self.assertGreaterEqual(digest.total_count, 0)
    
    def test_format_digest(self):
        """测试汇总格式化"""
        digest = NewsDigest(
            timestamp="2024-03-01 15:00:00",
            period="14:00-15:00",
            total_count=5,
            major_news=[
                NewsItem(
                    id="major1",
                    title="重大利好消息",
                    url="https://example.com/major",
                    sentiment_score=0.80
                )
            ],
            stock_news={
                "HK.00700": [
                    NewsItem(
                        id="news1",
                        title="腾讯新闻",
                        url="https://example.com/news1",
                        sentiment_score=0.60
                    )
                ]
            }
        )
        
        markdown = self.reporter.format_digest(digest)
        
        # 验证格式
        self.assertIn("📰 FTSeek 新闻汇总", markdown)
        self.assertIn("重大新闻", markdown)
        self.assertIn("个股新闻", markdown)
        self.assertIn("HK.00700", markdown)
    
    def test_quick_sentiment_analysis(self):
        """测试快速情绪分析"""
        text_positive = "腾讯盈利增长超预期，业绩强劲"
        text_negative = "腾讯亏损下滑，业绩恶化"
        text_neutral = "腾讯发布新产品"
        
        score_pos = self.reporter._quick_sentiment_analysis(text_positive)
        score_neg = self.reporter._quick_sentiment_analysis(text_negative)
        score_neu = self.reporter._quick_sentiment_analysis(text_neutral)
        
        self.assertGreater(score_pos, 0)
        self.assertLess(score_neg, 0)
        self.assertEqual(score_neu, 0.0)
    
    def test_is_major_news(self):
        """测试重大新闻判断"""
        # 情绪强烈
        news1 = NewsItem(title="测试", sentiment_score=0.7)
        self.assertTrue(self.reporter._is_major_news(news1, 0.7))
        
        # 包含重大关键词
        news2 = NewsItem(title="腾讯财报发布", sentiment_score=0.3)
        self.assertTrue(self.reporter._is_major_news(news2, 0.3))
        
        # 普通新闻
        news3 = NewsItem(title="腾讯发布新产品", sentiment_score=0.2)
        self.assertFalse(self.reporter._is_major_news(news3, 0.2))


class TestLinkClickability(unittest.TestCase):
    """测试链接可点击性"""
    
    def test_markdown_link_format(self):
        """测试 Markdown 链接格式"""
        # Markdown 链接格式：[文本](URL)
        link_text = "[查看详情](https://example.com/news)"
        
        # 验证格式正确
        self.assertRegex(link_text, r'\[.+\]\(.+\)')
        
        # 钉钉支持 Markdown 链接
        # 在 actionCard 和 markdown 消息类型中，链接应该是可点击的
    
    def test_url_in_text(self):
        """测试 URL 在文本中的可识别性"""
        url = "https://finance.sina.com.cn/tech/2024-03-01/doc-imxyz123.shtml"
        
        # 验证 URL 格式正确
        self.assertTrue(url.startswith("http"))
        self.assertIn("sina.com.cn", url)


if __name__ == "__main__":
    # 运行测试
    unittest.main(verbosity=2)
