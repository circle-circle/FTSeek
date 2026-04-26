#!/usr/bin/env python3
"""
FTSeek 统一新闻管线 V2 — 主入口 (混合模式)
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

sys.path.insert(0, "/opt/ftseek/FTSeek")

from app.news_v2.pipeline import NewsPipelineV2
from app.news_v2.sources.rsshub_source import RSSHubSource
from app.news_v2.integration import NewsV2Integration
from app.news_v2.collector import V2DataCollector

logger = logging.getLogger("ftseek.news_v2.main")


def setup_logging(level: str = "INFO"):
    log_dir = "/opt/ftseek/FTSeek/logs"
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(log_dir, "news_pipeline_v2.log")),
        ],
    )


def create_pipeline(watchlist_codes: set = None,
                    watchlist_names: set = None,
                    dedup_db_path: str = None) -> NewsPipelineV2:
    if not watchlist_codes:
        watchlist_codes = {
            "HK.00700", "HK.09988", "HK.03690", "HK.01810",
            "US.NVDA", "US.TSLA", "US.AAPL", "US.MSFT",
        }
    if not watchlist_names:
        watchlist_names = {
            "腾讯", "阿里巴巴", "美团", "小米",
            "英伟达", "特斯拉", "苹果", "微软",
        }

    pipeline = NewsPipelineV2(
        watchlist_codes=watchlist_codes,
        watchlist_names=watchlist_names,
        dedup_db_path=dedup_db_path,
    )
    rsshub = RSSHubSource(interval_seconds=180)
    pipeline.add_source(rsshub)
    logger.info("[V2 Main] 管线创建 | 源=%d | watchlist=%d", len(pipeline.sources), len(watchlist_codes))
    return pipeline


async def main_loop(pipeline, integration, collector, interval: int):
    stop_event = asyncio.Event()

    def handler(sig, frame):
        logger.info("收到退出信号")
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    logger.info("启动循环 (interval=%ds, mode=%s)", interval, integration.mode)

    while not stop_event.is_set():
        try:
            start = time.time()
            passed = await pipeline.run_once()
            elapsed_ms = (time.time() - start) * 1000
            if collector:
                collector.record_processing_time(elapsed_ms)

            stats = pipeline.get_stats()
            istats = integration.get_stats()
            logger.info(
                "[V2] 采集=%d | 通过=%d | 拒绝=%d | 耗时=%.0fms | 推送=%d | 错误=%d",
                stats["total_collected"],
                stats["total_passed"],
                stats["total_rejected"],
                elapsed_ms,
                istats["total_pushed"],
                istats["errors"],
            )

            # 每次运行都保存数据 (不是每 10 次)
            if collector and stats["total_collected"] > 0:
                collector.flush()
                report_path = collector.save_report()
                logger.info("[V2] 数据已保存: %d 条, 报告: %s", 
                           collector._stats["total_received"], report_path)

        except Exception as e:
            logger.error("[V2] 异常: %s", e, exc_info=True)
            if collector:
                collector.record_error(str(e))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    # 最终保存
    if collector:
        collector.flush()
        report_path = collector.save_report()
        logger.info("[V2] 最终报告: %s", report_path)


def main():
    parser = argparse.ArgumentParser(description="FTSeek 统一新闻管线 V2")
    parser.add_argument("--mode", choices=["active", "observe", "off"], default="observe")
    parser.add_argument("--interval", type=int, default=180)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--dedup-db", default=None)
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger.info("=" * 60)
    logger.info("FTSeek 统一新闻管线 V2 — 启动 (mode=%s)", args.mode)
    logger.info("=" * 60)

    pipeline = create_pipeline(dedup_db_path=args.dedup_db)
    collector = V2DataCollector()
    integration = NewsV2Integration(mode=args.mode, collector=collector)
    integration.attach_to_pipeline(pipeline)
    pipeline.attach_collector(collector)

    asyncio.run(main_loop(pipeline, integration, collector, args.interval))


if __name__ == "__main__":
    main()
