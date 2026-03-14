#!/usr/bin/env python3
"""
金融新闻推送服务

整合:
- 多源新闻采集
- 规则引擎过滤
- 频率控制
- Qwen 智能分析
- 钉钉推送

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
#  新闻推送服务
# ============================================================

class NewsPushService:
    """
    新闻推送服务
    """
    
    def __init__(self):
        self.logger = logging.getLogger("ftseek.news_push.service")
        
        # 初始化组件
        self.collector = get_collector()
        self.rule_engine = get_rule_engine()
        self.rate_limiter = get_rate_limiter()
        self.qwen_analyzer = get_qwen_analyzer()
        self.notifier = get_notifier()
        
        # 运行状态
        self._running = False
        self._interval = 60  # 采集间隔 (秒)
        self._thread: Optional[threading.Thread] = None
        
        # 统计
        self._stats = {
            'total_collected': 0,
            'total_pushed': 0,
            'total_filtered': 0,
            'last_run': None,
            'last_push': None
        }
        
        self.logger.info("✅ 新闻推送服务已初始化")
    
    def _collect_and_push(self):
        """执行一次采集和推送"""
        try:
            self.logger.info("\n" + "=" * 60)
            self.logger.info(f"🕐 开始采集周期 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self.logger.info("=" * 60)
            
            # 1. 采集新闻
            news_list = self.collector.fetch_all()
            self._stats['total_collected'] += len(news_list)
            
            if not news_list:
                self.logger.info("📭 无新新闻")
                return
            
            # 2. 规则过滤
            filtered = self.rule_engine.batch_evaluate(news_list)
            self._stats['total_filtered'] += len(news_list) - len(filtered)
            
            if not filtered:
                self.logger.info("🚫 所有新闻被规则过滤")
                return
            
            self.logger.info(f"✅ 通过过滤：{len(filtered)} 条")
            
            # 3. 推送 (带限流)
            pushed_count = 0
            merged_buffer = []
            
            for news, rule_result in filtered:
                # 检查限流
                can_push, reason = self.rate_limiter.can_push(news.id)
                
                if can_push:
                    # Qwen 分析
                    analysis = self.qwen_analyzer.analyze(
                        news_id=news.id,
                        title=news.title,
                        content=news.content,
                        source=news.source
                    )
                    
                    # 推送
                    ok = self.notifier.send_news(news, analysis)
                    
                    if ok:
                        self.rate_limiter.record_push(
                            news_id=news.id,
                            title=news.title,
                            source=news.source,
                            score=rule_result.score
                        )
                        pushed_count += 1
                        self._stats['total_pushed'] += 1
                        self._stats['last_push'] = datetime.now().isoformat()
                        
                        # 冷却时间
                        time.sleep(2)
                else:
                    # 加入合并缓冲
                    self.logger.debug(f"限流：{news.title[:30]}... ({reason})")
                    merged_buffer.append(news)
            
            # 4. 发送合并摘要
            if len(merged_buffer) >= 3:  # 至少 3 条才合并
                self.logger.info(f"📦 发送合并摘要：{len(merged_buffer)} 条")
                self.notifier.send_merge(merged_buffer)
            
            # 5. 统计
            self._stats['last_run'] = datetime.now().isoformat()
            self.logger.info("=" * 60)
            self.logger.info(f"📊 本次周期：采集={len(news_list)}, 推送={pushed_count}, 过滤={len(news_list) - len(filtered)}")
            
        except Exception as e:
            self.logger.error(f"采集推送异常：{e}", exc_info=True)
    
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
        
        self.logger.info(f"🚀 新闻推送服务已启动 | 间隔={interval}秒")
    
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
