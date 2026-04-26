#!/usr/bin/env python3
"""
新闻定时推送脚本 - FTSeek (增强版 2026-04-12)

功能:
- 每小时推送一次事件聚类摘要
- 使用 Pipeline 组件: NewsCleaner → EventClusterer → 事件包推送
- 替代旧版逐条推送模式

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

from app.news_push.collector import NewsCollector, get_collector
from app.news_push.rule_engine import RuleEngine
from app.news_push.notifier import NewsNotifier, get_notifier
from app.news_push.config import (
    WATCH_STOCKS,
    PUSH_TIME_WINDOW,
    RATE_LIMIT,
)

# Pipeline 组件
from app.pipeline.news_cleaner import NewsCleaner as PipelineNewsCleaner
from app.pipeline.event_clusterer import EventClusterer as PipelineEventClusterer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek.news.hourly_push")
logger.propagate = False


class HourlyNewsPush:
    """
    每小时新闻推送服务 (增强版: 清洗→聚类→事件包)
    """

    def __init__(self):
        """初始化新闻推送服务"""
        self.collector = get_collector()
        self.rule_engine = RuleEngine()

        # Pipeline 组件
        self.news_cleaner = PipelineNewsCleaner(config={
            "dedup_threshold": 0.85,
            "min_content_length": 8,
            "filter_clickbait": True,
        })
        self.event_clusterer = PipelineEventClusterer(config={
            "time_window_minutes": 120,
            "max_clusters": 8,
            "theme_generation": "rule",
        })

        # 钉钉通知器
        self.notifier = get_notifier()

        # 已推送的事件包 ID (防重复)
        self._pushed_clusters: set = set()

        # 推送限制
        self.max_per_hour = RATE_LIMIT.get("max_per_hour", 5)
        self.pushed_count = 0

        logger.info("每小时新闻推送服务已初始化 (增强版: 清洗+聚类)")

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

    def collect_and_push(self):
        """执行一次采集和推送 (增强版流程)"""
        logger.info("开始执行新闻推送任务")

        # Stage 1: 采集新闻
        news_list = self.collector.fetch_all()
        if not news_list:
            logger.info("📭 无新新闻")
            return

        logger.info(f"📥 采集到 {len(news_list)} 条新闻")

        # Stage 2: 清洗 (模糊去重 + 质量评分)
        cleaned = self.news_cleaner.clean(news_list)
        logger.info(f"🧹 清洗完成: {len(news_list)} → {len(cleaned)} 条")

        if not cleaned:
            logger.info("🚫 清洗后无有效新闻")
            return

        # Stage 3: 规则引擎过滤
        news_dicts = []
        for n in cleaned:
            nd = {
                'id': n.id,
                'title': n.title,
                'content': n.content,
                'source': n.source,
                'published_at': n.published_at,
                'related_stocks': n.related_stocks,
            }
            news_dicts.append(nd)

        filtered_pairs = self.rule_engine.batch_evaluate(news_dicts)
        filtered_news = []
        news_by_id = {n.id: n for n in cleaned}

        for nd, rule_result in filtered_pairs:
            news_id = nd.get('id', '') if isinstance(nd, dict) else getattr(nd, 'id', '')
            if news_id in news_by_id:
                news_by_id[news_id]._rule_result = rule_result
                filtered_news.append(news_by_id[news_id])

        logger.info(f"✅ 通过过滤：{len(filtered_news)} 条")

        if not filtered_news:
            logger.info("🚫 所有新闻被规则过滤")
            return

        # Stage 4: 事件聚类
        clusters = self.event_clusterer.cluster(filtered_news)
        logger.info(f"🔗 聚类完成: {len(clusters)} 个事件")

        # Stage 5: 事件包推送
        pushed_count = 0
        for cluster in clusters:
            if cluster.id in self._pushed_clusters:
                logger.debug(f"⏭️ 事件已推送过，跳过: {cluster.theme[:30]}")
                continue

            if not self.notifier._can_send():
                logger.debug("⏸️ 限流，跳过事件包")
                continue

            markdown = self.notifier._build_event_pack_markdown(cluster)
            title = f"📦 {cluster.theme[:30]}"

            ok = self._send_markdown(title, markdown)
            if ok:
                self._pushed_clusters.add(cluster.id)
                pushed_count += 1
                self.pushed_count += 1
                logger.info(f"✅ 事件包已推送: {cluster.theme[:40]} ({len(cluster.articles)} 条聚合)")

        logger.info(f"📊 本次周期：采集={len(news_list)}, 清洗={len(cleaned)}, 聚类={len(clusters)}, 推送={pushed_count}")
        logger.info("新闻推送任务完成")

    def _send_markdown(self, title: str, text: str) -> bool:
        """发送 Markdown 消息到钉钉"""
        if not self.notifier.webhook_url:
            return False
        if not self.notifier._can_send():
            return False

        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': title,
                'text': text
            }
        }
        return self.notifier._do_post(payload)

    def run(self):
        """执行一次新闻推送"""
        if not self.is_within_push_window():
            return

        self.collect_and_push()


def main():
    """主函数"""
    push_service = HourlyNewsPush()
    push_service.run()


if __name__ == "__main__":
    main()
