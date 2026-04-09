#!/usr/bin/env python3
"""
新闻定时推送脚本 - FTSeek-US

功能:
- 每小时推送一次精选新闻
- 过滤持仓股相关新闻
- 集成到 crontab 定时任务

使用方法:
  1. 配置环境变量 DINGTALK_WEBHOOK_URL
  2. 添加到 crontab: 0 * * * * cd /opt/ftseek/FTSeek && python3 -m app.news_push.hourly_push
"""
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any
import requests

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.news_push.collector import NewsCollector
from app.news_push.rule_engine import RuleEngine
from app.news_push.notifier import DingTalkNotifier
from app.news_push.config import (
    WATCH_STOCKS,
    PUSH_TIME_WINDOW,
    RATE_LIMIT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek.news.hourly_push")
logger.propagate = False  # 防止日志重复


class HourlyNewsPush:
    """
    每小时新闻推送服务
    """

    def __init__(self):
        """初始化新闻推送服务"""
        self.collector = NewsCollector()
        self.rule_engine = RuleEngine()
        self.dingtalk_webhook = os.getenv("DINGTALK_WEBHOOK_URL", "")
        
        # 推送限制
        self.max_per_hour = RATE_LIMIT.get("max_per_hour", 5)
        self.pushed_count = 0
        
        logger.info("每小时新闻推送服务已初始化")

    def is_within_push_window(self) -> bool:
        """检查是否在推送时间窗口内"""
        now = datetime.now()
        start_hour = PUSH_TIME_WINDOW.get("start_hour", 9)
        end_hour = PUSH_TIME_WINDOW.get("end_hour", 22)
        
        # 周末不推送 (除非配置允许)
        if not PUSH_TIME_WINDOW.get("weekend_push", False):
            if now.weekday() >= 5:  # 5=周六，6=周日
                logger.info("周末，跳过推送")
                return False
        
        # 检查小时
        if start_hour <= now.hour < end_hour:
            return True
        else:
            logger.info(f"当前时间 {now.hour}:00 不在推送窗口 [{start_hour}:00-{end_hour}:00]")
            return False

    def collect_news(self, limit: int = 20) -> List[Dict]:
        """
        收集新闻
        
        Args:
            limit: 最多收集的新闻数量
        
        Returns:
            新闻列表
        """
        try:
            news_list = self.collector.collect_all(limit=limit)
            logger.info(f"收集到 {len(news_list)} 条新闻")
            return news_list
        except Exception as e:
            logger.error(f"收集新闻失败：{e}")
            return []

    def filter_news(self, news_list: List[Dict]) -> List[Dict]:
        """
        过滤新闻 (规则引擎 + 持仓股过滤)
        
        Args:
            news_list: 原始新闻列表
        
        Returns:
            过滤后的新闻列表
        """
        filtered = []
        
        for news in news_list:
            # 规则引擎过滤
            if not self.rule_engine.should_push(news):
                continue
            
            # 持仓股关键词匹配 (优先推送)
            title = news.get("title", "")
            content = news.get("content", "")
            text = f"{title} {content}"
            
            is_stock_news = False
            for stock in WATCH_STOCKS:
                if not stock.get("enabled", False):
                    continue
                aliases = [stock["name"]]
                # 简单匹配
                for alias in aliases:
                    if alias in text:
                        news["matched_stock"] = stock
                        is_stock_news = True
                        break
                if is_stock_news:
                    break
            
            filtered.append(news)
        
        logger.info(f"过滤后剩余 {len(filtered)} 条新闻")
        return filtered

    def push_news(self, news_list: List[Dict]):
        """
        推送新闻到钉钉
        
        Args:
            news_list: 新闻列表
        """
        if not self.dingtalk_webhook:
            logger.warning("未配置钉钉 Webhook，跳过推送")
            return
        
        if not news_list:
            logger.info("无新闻需要推送")
            return
        
        # 限制推送数量
        news_to_push = news_list[:self.max_per_hour]
        
        # 构建 Markdown 消息
        markdown_lines = [f"## 📰 财经快讯 - {datetime.now().strftime('%m-%d %H:00')}"]
        markdown_lines.append("")
        
        for i, news in enumerate(news_to_push, 1):
            title = news.get("title", "")
            summary = news.get("summary", news.get("content", ""))[:100]
            source = news.get("source", "")
            url = news.get("url", "")
            
            # 持仓股新闻标记
            stock_marker = ""
            if "matched_stock" in news:
                stock = news["matched_stock"]
                stock_marker = f" 🎯 **[{stock['name']}]**"
            
            markdown_lines.append(f"**{i}. {title}**{stock_marker}")
            markdown_lines.append(f"   {summary}...")
            markdown_lines.append(f"   _来源：{source}_")
            markdown_lines.append("")
        
        markdown_lines.append("---")
        markdown_lines.append(f"*FTSeek-US 新闻推送系统*")
        markdown_lines.append(f"*推送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        
        markdown_text = "\n".join(markdown_lines)
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"财经快讯 - {datetime.now().strftime('%m-%d %H:00')}",
                "text": markdown_text,
            },
        }
        
        try:
            response = requests.post(
                self.dingtalk_webhook,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            logger.info(f"新闻推送成功 | 数量={len(news_to_push)}")
        except Exception as e:
            logger.error(f"新闻推送失败：{e}")

    def run(self):
        """执行一次新闻推送"""
        logger.info("开始执行新闻推送任务")
        
        # 检查时间窗口
        if not self.is_within_push_window():
            return
        
        # 收集新闻
        news_list = self.collect_news(limit=30)
        
        # 过滤新闻
        filtered_news = self.filter_news(news_list)
        
        # 推送新闻
        self.push_news(filtered_news)
        
        logger.info("新闻推送任务完成")


def main():
    """主函数"""
    push_service = HourlyNewsPush()
    push_service.run()


if __name__ == "__main__":
    main()
