#!/usr/bin/env python3
"""
金融新闻推送服务 (增强版 - 2026-04-12)

整合:
- 多源新闻采集
- 规则引擎过滤
- 频率控制
- Qwen 智能分析
- 钉钉推送

增强功能 (2026-04-12):
- NewsCleaner: 模糊去重 + 质量评分 + 垃圾过滤
- EventClusterer: 事件聚类 (同事件多来源合并)
- 事件包推送: 将相关多条新闻聚合成可读的事件摘要推送
  代替原来的逐条推送模式，大幅减少信息噪音

用法:
  python3 app/news_push/service.py
"""
import os
import sys
import time
import signal
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.news_push.config import LOG_LEVEL, LOG_FILE
from app.news_push.collector import get_collector, NewsItem
from app.news_push.rule_engine import get_rule_engine
from app.news_push.rate_limiter import get_rate_limiter
from app.news_push.qwen_analyzer import get_qwen_analyzer
from app.news_push.notifier import get_notifier

# 导入 Pipeline 组件 (2026-04-12 新增)
from app.pipeline.news_cleaner import NewsCleaner as PipelineNewsCleaner
from app.pipeline.event_clusterer import EventClusterer as PipelineEventClusterer

# 导入 trace_id 工具
try:
    from app.core.logging_config import trace_context, get_trace_id
    TRACE_AVAILABLE = True
except ImportError:
    import contextlib
    import uuid
    TRACE_AVAILABLE = False
    @contextlib.contextmanager
    def trace_context(trace_id=None):
        yield trace_id or f"NEWS-{uuid.uuid4().hex[:12].upper()}"
    def get_trace_id(): return None

# ============================================================
#  日志配置
# ============================================================

def setup_logging():
    """配置日志"""
    log_file = Path(LOG_FILE)
    log_file.parent.mkdir(exist_ok=True)
    
    # 文件格式
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, LOG_LEVEL))
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    
    # 控制台格式
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    
    # 根日志
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    return logging.getLogger("ftseek.news_push")


# ============================================================
#  新闻推送服务 (增强版)
# ============================================================

class NewsPushService:
    """
    新闻推送服务 (增强版)

    推送流程 (2026-04-12 更新):
      1. 采集新闻 (多源)
      2. NewsCleaner 清洗 (模糊去重 + 质量评分 + 垃圾过滤)
      3. RuleEngine 规则过滤 (保留原有规则)
      4. EventClusterer 事件聚类 (同事件合并)
      5. 事件包推送 (可读摘要，代替逐条推送)
    """

    def __init__(self):
        self.logger = logging.getLogger("ftseek.news_push.service")
        self.logger.propagate = False  # 防止日志重复

        # 初始化组件
        self.collector = get_collector()
        self.rule_engine = get_rule_engine()
        self.rate_limiter = get_rate_limiter()
        self.qwen_analyzer = get_qwen_analyzer()
        self.notifier = get_notifier()

        # Pipeline 组件 (2026-04-12 新增)
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

        # 运行状态
        self._running = False
        self._interval = 60  # 采集间隔 (秒)
        self._thread: Optional[threading.Thread] = None

        # 已推送的事件包 ID 集合 (防止重复推送)
        self._pushed_clusters: set = set()

        # 统计
        self._stats = {
            'total_collected': 0,
            'total_cleaned': 0,
            'total_clusters': 0,
            'total_pushed': 0,
            'total_filtered': 0,
            'last_run': None,
            'last_push': None
        }

        self.logger.info("✅ 新闻推送服务已初始化 (增强版: 清洗+聚类)")

    def _collect_and_push(self):
        """执行一次采集和推送 (增强版: 清洗→聚类→事件包推送)"""
        import uuid
        cycle_trace_id = f"CYCLE-{uuid.uuid4().hex[:10].upper()}"
        with trace_context(cycle_trace_id) as tid:
            try:
                self.logger.info("\n" + "=" * 60)
                self.logger.info(
                    f"[{tid}] 🕐 开始采集周期 | "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.logger.info("=" * 60)

                # ── Stage 1: 采集新闻 ──────────────────────────────
                news_list = self.collector.fetch_all()
                self._stats['total_collected'] += len(news_list)

                if not news_list:
                    self.logger.info(f"[{tid}] 📭 无新新闻")
                    return

                # ── Stage 2: 清洗 (模糊去重 + 质量评分) ────────────
                cleaned = self.news_cleaner.clean(news_list)
                self._stats['total_cleaned'] += len(cleaned)
                self.logger.info(
                    f"[{tid}] 🧹 清洗完成: {len(news_list)} → {len(cleaned)} 条"
                )

                if not cleaned:
                    self.logger.info(f"[{tid}] 🚫 清洗后无有效新闻")
                    return

                # ── Stage 2.5: 规则引擎过滤 (保留原有规则) ─────────
                # 将清洗后的新闻转换为 dict 格式供 rule_engine 使用
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

                self._stats['total_filtered'] += len(cleaned) - len(filtered_news)

                if not filtered_news:
                    self.logger.info(f"[{tid}] 🚫 所有新闻被规则过滤")
                    return

                self.logger.info(f"[{tid}] ✅ 通过过滤：{len(filtered_news)} 条")

                # ── Stage 3: 事件聚类 ──────────────────────────────
                clusters = self.event_clusterer.cluster(filtered_news)
                self._stats['total_clusters'] += len(clusters)
                self.logger.info(f"[{tid}] 🔗 聚类完成: {len(clusters)} 个事件")

                # ── Stage 4: 事件包推送 ─────────────────────────────
                pushed_count = 0
                for cluster in clusters:
                    # 防止重复推送同一聚类
                    cluster_key = cluster.id
                    if cluster_key in self._pushed_clusters:
                        self.logger.debug(
                            f"[{tid}] ⏭️ 事件已推送过，跳过: {cluster.theme[:30]}"
                        )
                        continue

                    # 检查是否可以发送
                    if not self.notifier._can_send():
                        self.logger.debug(f"[{tid}] ⏸️ 限流，跳过事件包")
                        continue

                    # 构建事件包 Markdown
                    markdown = self.notifier._build_event_pack_markdown(cluster)
                    title = f"📦 {cluster.theme[:30]}"

                    ok = self._send_markdown(title, markdown)
                    if ok:
                        self._pushed_clusters.add(cluster_key)
                        pushed_count += 1
                        self._stats['total_pushed'] += 1
                        self._stats['last_push'] = datetime.now().isoformat()
                        self.logger.info(
                            f"[{tid}] ✅ 事件包已推送: {cluster.theme[:40]} "
                            f"({len(cluster.articles)} 条新闻聚合)"
                        )
                    else:
                        self.logger.warning(f"[{tid}] ❌ 事件包推送失败: {cluster.theme[:40]}")

                # ── Stage 5: 统计 ──────────────────────────────────
                self._stats['last_run'] = datetime.now().isoformat()
                self.logger.info("=" * 60)
                self.logger.info(
                    f"[{tid}] 📊 本次周期：采集={len(news_list)}, 清洗={len(cleaned)}, "
                    f"聚类={len(clusters)}, 推送={pushed_count}"
                )

            except Exception as e:
                self.logger.error(f"[{tid}] 采集推送异常：{e}", exc_info=True)

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

    def _run_loop(self):
        """运行循环"""
        while self._running:
            self._collect_and_push()

            # 等待下一次
            for _ in range(self._interval):
                if not self._running:
                    break
                time.sleep(1)

    def start(self, interval: int = 60):
        """
        启动服务

        Args:
            interval: 采集间隔 (秒)
        """
        if self._running:
            self.logger.warning("服务已在运行中")
            return

        self._interval = interval
        self._running = True

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self.logger.info(f"🚀 新闻推送服务已启动 (增强版) | 间隔={interval}秒")

    def stop(self):
        """停止服务"""
        self._running = False

        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

        self.logger.info("🛑 新闻推送服务已停止")

    def run_once(self):
        """手动执行一次"""
        self._collect_and_push()

    def get_stats(self) -> dict:
        """获取统计信息"""
        limiter_stats = self.rate_limiter.get_stats()
        analyzer_stats = self.qwen_analyzer.get_stats()

        return {
            **self._stats,
            'rate_limiter': limiter_stats,
            'analyzer': analyzer_stats,
            'running': self._running,
            'interval': self._interval
        }


# ============================================================
#  全局服务实例
# ============================================================

_service: Optional[NewsPushService] = None


def get_service() -> NewsPushService:
    """获取服务单例"""
    global _service
    if _service is None:
        _service = NewsPushService()
    return _service


# ============================================================
#  主程序
# ============================================================

def main():
    """主程序"""
    logger = setup_logging()
    logger.info("\n" + "=" * 60)
    logger.info("🚀 金融新闻推送服务启动")
    logger.info("=" * 60)
    
    # 创建服务
    service = get_service()
    
    # 信号处理
    def signal_handler(sig, frame):
        logger.info(f"\n收到信号 {sig}, 正在停止...")
        service.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 获取采集间隔 (环境变量)
    interval = int(os.getenv('NEWS_PUSH_INTERVAL', '60'))
    
    # 启动服务
    service.start(interval=interval)
    
    # 保持运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
