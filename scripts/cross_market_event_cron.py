#!/usr/bin/env python3
"""
跨市场事件采集 Cron 脚本

功能:
1. 在美股交易时段 (21:30-04:00 HKT) 定期运行
2. 采集美股相关新闻和价格异动
3. 写入跨市场事件缓存
4. 次日港股盘前分析时读取

运行频率: 每 15 分钟 (美股交易时段)

Crontab 示例:
  */15 21-23 * * 1-5 cd /opt/ftseek/FTSeek && /usr/bin/python3.8 scripts/cross_market_event_cron.py >> logs/cross_market_events.log 2>&1
  */15 0-3 * * 2-6 cd /opt/ftseek/FTSeek && /usr/bin/python3.8 scripts/cross_market_event_cron.py >> logs/cross_market_events.log 2>&1

更新时间: 2026-04-13
"""

import sys
import os
import logging
import time
import uuid
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/cross_market_events.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.cross_market_cron")


def collect_price_moves():
    """采集美股价格异动"""
    from app.data.us_data_fetcher import get_us_data_fetcher
    from app.data.cross_market_event_cache import get_cross_market_event_cache, CrossMarketEvent
    from app.utils.cross_market_mapper import get_cross_market_mapper

    fetcher = get_us_data_fetcher()
    cache = get_cross_market_event_cache()
    mapper = get_cross_market_mapper()

    # 获取核心美股代码
    core_codes = mapper.get_core_us_codes()[:15]
    if not core_codes:
        from app.config import WATCH_LIST_US
        core_codes = WATCH_LIST_US[:15]

    events_collected = 0

    for us_code in core_codes:
        try:
            quote = fetcher.get_quote(us_code)
            if not quote or quote.get("price", 0) <= 0:
                continue

            change_pct = quote.get("change_pct", 0)

            # 价格异动阈值: ±2%
            if abs(change_pct) < 2.0:
                continue

            # 获取影响的港股
            hk_codes = mapper.get_hk_codes_by_us(us_code)
            if not hk_codes:
                continue

            # 确定影响分数
            impact_score = change_pct / 100.0  # 归一化到 -1.0 ~ 1.0

            # 创建事件
            event = CrossMarketEvent(
                event_id=f"price_{us_code}_{int(time.time())}",
                timestamp=time.time(),
                event_type="price_move",
                us_code=us_code,
                hk_codes=hk_codes,
                title=f"{us_code} 价格异动 {change_pct:+.2f}%",
                content=f"价格: ${quote['price']:.2f}, 涨跌幅: {change_pct:+.2f}%",
                impact_score=impact_score,
                metadata={"price": quote["price"], "change_pct": change_pct},
            )

            cache.write(event)
            events_collected += 1

            logger.info(
                f"[PriceMove] {us_code} {change_pct:+.2f}% → {hk_codes} | "
                f"impact={impact_score:+.2f}"
            )

        except Exception as e:
            logger.warning(f"[PriceMove] 采集 {us_code} 失败: {e}")

    return events_collected


def collect_market_news():
    """采集市场新闻"""
    from app.data.cross_market_event_cache import get_cross_market_event_cache, CrossMarketEvent
    from app.utils.cross_market_mapper import get_cross_market_mapper

    try:
        import requests
        import json

        # 从 .env.us 读取 Finnhub key
        api_key = ""
        with open('/opt/ftseek/FTSeek-US/.env.us') as f:
            for line in f:
                if line.startswith('FINNHUB_API_KEY='):
                    api_key = line.split('=', 1)[1].strip()
                    break

        if not api_key:
            return 0

        # 获取市场新闻
        url = f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        news_list = resp.json()[:10]  # 最近 10 条

        if not news_list:
            return 0

        cache = get_cross_market_event_cache()
        mapper = get_cross_market_mapper()
        events_collected = 0

        for news in news_list:
            headline = news.get("headline", "")
            summary = news.get("summary", "")
            source = news.get("source", "")
            related = news.get("related", "")

            # 检查是否包含美股代码
            mentioned_us_codes = []
            for code in mapper.get_us_watch_list():
                ticker = code.replace("US.", "")
                if ticker in headline or ticker in related:
                    mentioned_us_codes.append(code)

            if not mentioned_us_codes:
                continue

            # 获取影响的港股
            affected_hk = []
            for us_code in mentioned_us_codes:
                hk_codes = mapper.get_hk_codes_by_us(us_code)
                affected_hk.extend(hk_codes)

            if not affected_hk:
                continue

            # 简单情感分析
            sentiment = 0.0
            lower_headline = headline.lower()
            if any(w in lower_headline for w in ['surge', 'rally', 'jump', 'rise', 'beat', 'upgrade']):
                sentiment = 0.5
            elif any(w in lower_headline for w in ['drop', 'fall', 'plunge', 'miss', 'downgrade', 'crash']):
                sentiment = -0.5

            event = CrossMarketEvent(
                event_id=f"news_{uuid.uuid4().hex[:8]}_{int(time.time())}",
                timestamp=time.time(),
                event_type="news",
                us_code=mentioned_us_codes[0],
                hk_codes=list(set(affected_hk)),
                title=headline,
                content=summary,
                impact_score=sentiment,
                metadata={"source": source, "related": related, "all_us_codes": mentioned_us_codes},
            )

            cache.write(event)
            events_collected += 1

        return events_collected

    except Exception as e:
        logger.error(f"[NewsCollect] 采集失败: {e}")
        return 0


def cleanup_expired():
    """清理过期事件"""
    try:
        from app.data.cross_market_event_cache import get_cross_market_event_cache
        cache = get_cross_market_event_cache()
        cache.cleanup_expired(hours=48)
    except Exception as e:
        logger.error(f"[Cleanup] 清理失败: {e}")


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("跨市场事件采集开始")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    total_events = 0

    # 1. 采集价格异动
    try:
        price_events = collect_price_moves()
        total_events += price_events
        logger.info(f"[PriceMove] 采集 {price_events} 个价格异动事件")
    except Exception as e:
        logger.error(f"[PriceMove] 采集失败: {e}")

    # 2. 采集市场新闻
    try:
        news_events = collect_market_news()
        total_events += news_events
        logger.info(f"[NewsCollect] 采集 {news_events} 个新闻事件")
    except Exception as e:
        logger.error(f"[NewsCollect] 采集失败: {e}")

    # 3. 清理过期事件
    try:
        cleanup_expired()
    except Exception as e:
        logger.error(f"[Cleanup] 清理失败: {e}")

    # 4. 输出统计
    try:
        from app.data.cross_market_event_cache import get_cross_market_event_cache
        cache = get_cross_market_event_cache()
        stats = cache.get_stats()
        logger.info(f"[Stats] 总事件: {stats.get('total_events', 0)}, "
                   f"未处理: {stats.get('unprocessed', 0)}, "
                   f"24h 内: {stats.get('last_24h', 0)}")
    except Exception as e:
        logger.error(f"[Stats] 获取统计失败: {e}")

    logger.info("=" * 60)
    logger.info(f"采集完成，共 {total_events} 个事件")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("\n采集被用户中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n采集异常: {e}", exc_info=True)
        sys.exit(1)
