#!/usr/bin/env python3
"""
钉钉新闻推送器

功能:
- Markdown 格式消息
- 来源标识和优先级
- 股票关联显示
- 推送限流集成
"""
import time
import hmac
import hashlib
import base64
import json
import logging
import urllib.parse
import threading
from datetime import datetime
from typing import List, Dict, Optional
from collections import deque

from app.news_push.config import DINGTALK_WEBHOOK_URL, DINGTALK_SECRET
from app.news_push.collector import NewsItem
from app.news_push.qwen_analyzer import AnalysisResult

logger = logging.getLogger("ftseek.news_push.notifier")


class NewsNotifier:
    """
    钉钉新闻推送器
    """
    
    # 来源图标映射
    SOURCE_ICONS = {
        'cls': '🔴',           # 财联社 (快讯)
        'blockbeats': '🟠',    # BlockBeats
        'wallstreetcn': '🔵',  # 华尔街见闻
        'sina': '🟢',          # 新浪财经
        'cnbc': '🟣',          # CNBC
        'sec': '📄',           # SEC
        'fed': '🏛️',           # 美联储
        'hkex': '📈',          # 港交所
        'rss': '📰',           # 其他 RSS
    }
    
    # 情绪图标
    SENTIMENT_ICONS = {
        'positive': '🟢',
        'neutral': '🟡',
        'negative': '🔴'
    }
    
    # 影响程度图标
    IMPACT_ICONS = {
        'high': '🔥',
        'medium': '⚡',
        'low': '💧'
    }
    
    def __init__(self, webhook_url: str = None, secret: str = None):
        self.webhook_url = webhook_url or DINGTALK_WEBHOOK_URL
        self.secret = secret or DINGTALK_SECRET
        
        # 限流
        self._send_times: deque = deque(maxlen=60)
        self._lock = threading.Lock()
        
        # 钉钉限制：20 条/分钟
        self.max_per_minute = 18
        
        if self.webhook_url:
            logger.info(f"✅ 新闻推送器已初始化")
        else:
            logger.warning("⚠️  钉钉 Webhook URL 未配置")
    
    def _sign_url(self) -> str:
        """加签"""
        if not self.secret:
            return self.webhook_url
        
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        
        return f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"
    
    def _can_send(self) -> bool:
        """检查限流"""
        now = time.time()
        with self._lock:
            while self._send_times and now - self._send_times[0] > 60:
                self._send_times.popleft()
            return len(self._send_times) < self.max_per_minute
    
    def _record_send(self):
        """记录发送"""
        with self._lock:
            self._send_times.append(time.time())
    
    def _do_post(self, payload: dict) -> bool:
        """执行 POST"""
        try:
            import requests
            url = self._sign_url()
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            
            if result.get('errcode') == 0:
                self._record_send()
                return True
            else:
                logger.error(f"钉钉推送失败：{result}")
                return False
        except ImportError:
            logger.error("缺少 requests 库")
            return False
        except Exception as e:
            logger.error(f"钉钉推送异常：{e}")
            return False
    
    def _build_news_markdown(self, news: NewsItem, 
                            analysis: AnalysisResult = None) -> str:
        """
        构建新闻 Markdown 消息
        
        Args:
            news: 新闻条目
            analysis: 分析结果 (可选)
            
        Returns:
            Markdown 文本
        """
        # 来源图标
        source_icon = self.SOURCE_ICONS.get(news.source, '📰')
        source_name = {
            'cls': '财联社',
            'blockbeats': 'BlockBeats',
            'wallstreetcn': '华尔街见闻',
            'sina': '新浪财经',
            'cnbc': 'CNBC',
            'sec': 'SEC',
            'fed': '美联储',
            'hkex': '港交所'
        }.get(news.source, news.source)
        
        # 时间格式化
        pub_time = news.published_at
        if pub_time:
            try:
                dt = datetime.fromisoformat(pub_time.replace('Z', '+00:00'))
                time_str = dt.strftime('%H:%M')
            except:
                time_str = pub_time[:16] if len(pub_time) > 16 else pub_time
        else:
            time_str = datetime.now().strftime('%H:%M')
        
        # 构建消息
        lines = [
            f"### {source_icon} [{source_name}] {time_str}",
            "",
            f"**{news.title}**",
            ""
        ]
        
        # 摘要或内容
        if analysis and analysis.summary:
            lines.append(f"📝 {analysis.summary}")
            lines.append("")
        elif news.summary:
            lines.append(f"📝 {news.summary}")
            lines.append("")
        elif news.content:
            content = news.content[:200]
            if len(news.content) > 200:
                content += "..."
            lines.append(f"📝 {content}")
            lines.append("")
        
        # 分析结果
        if analysis:
            sentiment_icon = self.SENTIMENT_ICONS.get(analysis.sentiment, '🟡')
            impact_icon = self.IMPACT_ICONS.get(analysis.impact, '💧')
            
            analysis_lines = [
                "---",
                "",
                f"{sentiment_icon} **情绪**: {analysis.sentiment} ({analysis.sentiment_score:+.2f})",
                f"{impact_icon} **影响**: {analysis.impact}",
            ]
            
            if analysis.action_suggestion:
                action_icon = {
                    '关注': '👀',
                    '观望': '⏸️',
                    '回避': '⚠️'
                }.get(analysis.action_suggestion, '📌')
                analysis_lines.append(f"{action_icon} **建议**: {analysis.action_suggestion}")
            
            if analysis.key_points:
                analysis_lines.append("")
                analysis_lines.append("**关键点**:")
                for i, point in enumerate(analysis.key_points[:3], 1):
                    analysis_lines.append(f"  {i}. {point}")
            
            lines.extend(analysis_lines)
        
        # 相关股票
        if news.related_stocks or (analysis and analysis.related_stocks):
            stocks = list(set(news.related_stocks + (analysis.related_stocks if analysis else [])))
            if stocks:
                lines.append("")
                lines.append("---")
                lines.append("")
                lines.append(f"📈 **相关股票**: {', '.join(stocks)}")
        
        # 链接
        if news.url:
            lines.append("")
            lines.append(f"[阅读原文]({news.url})")
        
        return "\n".join(lines)
    
    def _build_merge_markdown(self, news_list: List[NewsItem]) -> str:
        """
        构建合并摘要消息
        
        Args:
            news_list: 新闻列表
            
        Returns:
            Markdown 文本
        """
        lines = [
            "### 📊 新闻摘要",
            "",
            f"**过去 1 分钟内 {len(news_list)} 条重要新闻**",
            ""
        ]
        
        for i, news in enumerate(news_list[:5], 1):  # 最多显示 5 条
            source_icon = self.SOURCE_ICONS.get(news.source, '📰')
            lines.append(f"{i}. {source_icon} **{news.title[:60]}**")
        
        if len(news_list) > 5:
            lines.append(f"... 还有 {len(news_list) - 5} 条")
        
        return "\n".join(lines)
    
    def send_news(self, news: NewsItem, 
                 analysis: AnalysisResult = None) -> bool:
        """
        发送单条新闻
        
        Args:
            news: 新闻条目
            analysis: 分析结果 (可选)
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("钉钉 Webhook URL 未配置")
            return False
        
        if not self._can_send():
            logger.warning(f"限流跳过：{news.title[:30]}...")
            return False
        
        markdown = self._build_news_markdown(news, analysis)
        
        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': news.title[:50],
                'text': markdown
            }
        }
        
        ok = self._do_post(payload)
        
        if ok:
            logger.info(f"✅ 新闻推送成功：{news.title[:30]}...")
        else:
            logger.error(f"❌ 新闻推送失败：{news.title[:30]}...")
        
        return ok
    
    def send_merge(self, news_list: List[NewsItem]) -> bool:
        """
        发送合并摘要
        
        Args:
            news_list: 新闻列表
            
        Returns:
            是否发送成功
        """
        if not self.webhook_url or not news_list:
            return False
        
        if not self._can_send():
            return False
        
        markdown = self._build_merge_markdown(news_list)
        
        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': '新闻摘要',
                'text': markdown
            }
        }
        
        return self._do_post(payload)
    
    def send_text(self, text: str) -> bool:
        """发送纯文本消息"""
        if not self.webhook_url:
            return False
        
        if not self._can_send():
            return False
        
        payload = {
            'msgtype': 'text',
            'text': {
                'content': text
            }
        }
        
        return self._do_post(payload)


# 全局单例
_notifier: Optional[NewsNotifier] = None


def get_notifier(webhook_url: str = None, secret: str = None) -> NewsNotifier:
    """获取推送器单例"""
    global _notifier
    if _notifier is None:
        _notifier = NewsNotifier(webhook_url, secret)
    return _notifier


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 测试
    notifier = get_notifier()
    
    test_news = NewsItem(
        id='test_001',
        title='腾讯控股发布 Q4 财报，净利润同比增长 30% 超预期',
        content='腾讯控股今日发布 2025 年第四季度财报...',
        source='sina',
        category='finance',
        url='https://example.com/news/001',
        published_at=datetime.now().isoformat(),
        related_stocks=['HK.00700']
    )
    
    ok = notifier.send_news(test_news)
    print(f"推送结果：{'成功' if ok else '失败'}")
