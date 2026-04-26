#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
富途快讯定时采集服务

功能:
1. 每 3 分钟采集富途快讯 (RSSHub)
2. 自动保存到数据库
3. 重要快讯钉钉推送
4. 股票关联标记
5. 情感分析

定时任务配置:
- crontab: */3 * * * * cd /opt/ftseek/FTSeek && python3 scripts/futu_flash_cron.py

作者: FTSeek Team
日期: 2026-04-12
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# 配置路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.utils.dedup import get_dedup

BASE_DIR = Path('/opt/ftseek/FTSeek')
DATA_DIR = BASE_DIR / 'data' / 'futu_flash'
LOG_DIR = BASE_DIR / 'logs'

# 创建目录
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / 'futu_flash_cron.log'),
    ]
)
logger = logging.getLogger('futu_flash_cron')


class FutuFlashCronService:
    """
    富途快讯定时采集服务
    """
    
    # P0 修复 (2026-04-16): 重要新闻关键词补充宏观/地缘事件
    MAJOR_KEYWORDS = [
        # 个股事件
        '财报', '业绩', '盈利', '亏损', '并购', '收购',
        '监管', '处罚', 'IPO', '上市', '重大合同',
        '涨停', '跌停', '暴涨', '暴跌', '熔断',
        # 宏观/地缘事件 (P0 新增)
        '美联储', 'FED', '利率', '加息', '降息', 'CPI', 'PPI', '非农',
        '纳指', '道指', '标普', 'S&P', 'Nasdaq', 'Dow', '恒生',
        '制裁', '关税', '贸易战', '地缘', '战争', '停火', '中东',
        '原油', '油价', '黄金', 'bitcoin', 'crypto',
        '芯片', '半导体', 'AI', '电动车', 'EV',
    ]
    
    # P0 修复 (2026-04-16): 从 config.yaml 动态加载 watchlist，不再硬编码
    @classmethod
    def _load_watchlist(cls):
        """从配置中心加载 watchlist + 股票名称映射"""
        try:
            sys.path.insert(0, '/opt/ftseek/FTSeek')
            from config.config_center import get_config_center
            center = get_config_center()
            hk = center.get('trading.watch_list_hk', [])
            us = center.get('trading.watch_list_us', [])
            watchlist = list(hk) + list(us)
            
            # 构造名称映射
            names = {}
            name_map = {
                '腾讯': 'HK.00700', '阿里巴巴': 'HK.09988', '阿里': 'HK.09988',
                '美团': 'HK.03690', '小米': 'HK.01810', '中芯国际': 'HK.00981',
                '泡泡玛特': 'HK.09992', '快手': 'HK.01024', '京东': 'HK.09618',
                '百度': 'HK.09888', '平安': 'HK.02318', '友邦': 'HK.01299',
                '港交所': 'HK.00388', '建行': 'HK.00939', '信达': 'HK.01801',
                '药明': 'HK.02269', '京东健康': 'HK.06618', '华虹': 'HK.01347',
                '舜宇': 'HK.02382', '比亚迪': 'HK.01211', '理想': 'HK.02015',
                '小鹏': 'HK.09868',
                '英伟达': 'US.NVDA', 'NVDA': 'US.NVDA',
                '特斯拉': 'US.TSLA', 'TSLA': 'US.TSLA',
                '苹果': 'US.AAPL', 'AAPL': 'US.AAPL',
                '微软': 'US.MSFT', 'MSFT': 'US.MSFT',
                '谷歌': 'US.GOOG', 'GOOGL': 'US.GOOG',
                '亚马逊': 'US.AMZN', 'AMZN': 'US.AMZN',
                'META': 'US.META', '拼多多': 'US.PDD', 'PDD': 'US.PDD',
            }
            for code in watchlist:
                for name, mcode in name_map.items():
                    if mcode == code:
                        names[name] = code
            
            if watchlist:
                return watchlist, names
        except Exception as e:
            logger.debug(f"加载 watchlist 失败，使用 fallback: {e}")
        
        # Fallback: 至少包含核心股票
        fallback_list = ['HK.00700', 'HK.09988', 'HK.03690', 'HK.01810', 'HK.00981']
        fallback_names = {'腾讯': 'HK.00700', '阿里巴巴': 'HK.09988', '美团': 'HK.03690'}
        return fallback_list, fallback_names
    
    # 延迟初始化 (在 __init__ 中调用 _load_watchlist)
    WATCH_LIST = []
    STOCK_NAMES = {}
    
    def __init__(self):
        """初始化"""
        # P0 修复 (2026-04-16): 动态加载 watchlist
        self.WATCH_LIST, self.STOCK_NAMES = self._load_watchlist()
        
        self.data_file = DATA_DIR / 'futu_flash_history.json'
        self.today_file = DATA_DIR / f'futu_flash_{datetime.now().strftime("%Y%m%d")}.json'
        
        # 加载历史数据
        self.history = self._load_history()
        
        # 统计
        self.stats = {
            'total_runs': 0,
            'total_news': 0,
            'major_news': 0,
            'pushed_news': 0,
            'last_run': None,
        }
        
        # 加载已有统计
        self._load_stats()
    
    def _load_stats(self):
        """加载已有统计数据"""
        stats_file = DATA_DIR / 'futu_flash_stats.json'
        if stats_file.exists():
            try:
                with open(stats_file, 'r') as f:
                    existing = json.load(f)
                    self.stats.update(existing)
            except Exception:
                logger.error("Failed to load futu_flash_stats.json", exc_info=True)
    
    def _load_history(self) -> dict:
        """加载历史数据"""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r') as f:
                    return json.load(f)
            except Exception:
                logger.error("Failed to load history data file", exc_info=True)
                return {}
        return {}
    
    def _save_history(self):
        """保存历史数据"""
        with open(self.data_file, 'w') as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)
    
    def _is_major_news(self, title: str, content: str) -> bool:
        """判断是否为重要新闻"""
        text = f"{title} {content}"
        
        for keyword in self.MAJOR_KEYWORDS:
            if keyword in text:
                return True
        
        return False
    
    def _extract_stocks(self, text: str) -> list:
        """提取相关股票"""
        stocks = []
        
        for name, code in self.STOCK_NAMES.items():
            if name in text:
                stocks.append(code)
        
        # 检查是否在监控列表中
        for code in self.WATCH_LIST:
            code_short = code.replace('HK.', '').replace('US.', '')
            if code_short in text:
                stocks.append(code)
        
        return list(set(stocks))
    
    async def fetch_flash(self) -> list:
        """获取富途快讯"""
        from scripts.futu_rsshub_fetcher import fetch_futu_flash
        
        try:
            news_list = await fetch_futu_flash()
            logger.info(f"获取 {len(news_list)} 条富途快讯")
            return news_list
        except Exception as e:
            logger.error(f"获取快讯失败: {e}")
            return []
    
    def save_news(self, news_list: list):
        """保存快讯数据"""
        today_news = []
        
        for news in news_list:
            news_id = news['id']
            
            # 检查是否已存在 (避免重复)
            if news_id in self.history.get('ids', []):
                continue
            
            # 判断重要性
            is_major = self._is_major_news(news['title'], news.get('content', news['title']))
            news['is_major'] = is_major
            
            # 提取股票
            stocks = self._extract_stocks(f"{news['title']} {news.get('content', '')}")
            news['stocks'] = stocks
            
            # 添加到历史
            if 'ids' not in self.history:
                self.history['ids'] = []
            self.history['ids'].append(news_id)
            
            today_news.append(news)
            
            # 更新统计
            self.stats['total_news'] += 1
            if is_major:
                self.stats['major_news'] += 1
        
        # 保存今日快讯
        if today_news:
            # 读取已有数据
            existing = []
            if self.today_file.exists():
                try:
                    with open(self.today_file, 'r') as f:
                        existing = json.load(f)
                except Exception:
                    logger.error("Failed to load today's flash news file", exc_info=True)
            
            # 合并新数据
            all_today = existing + today_news
            
            with open(self.today_file, 'w') as f:
                json.dump(all_today, f, indent=2, ensure_ascii=False)
            
            logger.info(f"保存 {len(today_news)} 条新快讯到 {self.today_file}")
        
        # 保存历史 ID
        self._save_history()
    
    async def push_major_news(self, news_list: list):
        """推送重要快讯到钉钉（带全局去重）"""
        # 初始化全局去重器
        dedup = get_dedup(ttl_hours=2)
        
        # 过滤: 重要快讯 + 未在全局去重库中
        major_news = []
        for n in news_list:
            if not n.get('is_major'):
                continue
            title = n.get('title', '')
            source = n.get('source_name', '富途')
            if dedup.is_duplicate(title, source):
                logger.debug(f"[去重] 跳过已推送: {title[:50]}")
                continue
            major_news.append(n)
        
        if not major_news:
            logger.info("[富途快讯] 无重要快讯需推送（全部已推送或去重）")
            return
        
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            
            notifier = DingTalkNotifier()
            
            for news in major_news[:3]:  # 最多推送 3 条
                title = news['title'][:100]
                stocks = news.get('stocks', [])
                sentiment = news.get('sentiment', 'neutral')
                source = news.get('source_name', '富途')
                
                # 全局去重检查（原子操作）
                if not dedup.check_and_mark(title, source, channel='futu_flash'):
                    logger.debug(f"[去重] 跳过: {title[:50]}")
                    continue
                
                # 构建推送内容
                text = f"""
### 🔴 富途重要快讯

**标题**: {title}

**时间**: {news.get('publish_time', '')[:19]}

**情感**: {sentiment}

**相关股票**: {', '.join(stocks) if stocks else '未识别'}

**链接**: {news.get('url', '')}

---
💡 此快讯包含重要关键词，请关注
"""
                
                # 发送文本消息
                result = notifier.send_text(text)
                
                if result:
                    self.stats['pushed_news'] += 1
                    logger.info(f"推送重要快讯: {title[:50]}")
                else:
                    logger.warning(f"推送失败: {title[:50]}")
                
        except Exception as e:
            logger.error(f"钉钉推送失败: {e}")
    
    async def _trigger_event_pipeline(self, news_list: list):
        """
        P0 修复 (2026-04-16): 将富途快讯注入事件驱动管线
        
        链路: futu_flash → DataSubscriber.on_news_event() → 分层策略 → 共识引擎
        """
        try:
            # 构造新闻事件格式 (与 news_push/collector 一致)
            news_events = []
            for n in news_list[:20]:  # 最多传递最近 20 条，避免过载
                # 提取相关股票
                text = f"{n.get('title', '')} {n.get('content', '')}"
                related = []
                for name, code in self.STOCK_NAMES.items():
                    if name in text:
                        related.append(code)
                
                news_events.append({
                    'id': n.get('id', ''),
                    'title': n.get('title', ''),
                    'content': n.get('content', ''),
                    'source': 'futu_flash',
                    'publish_time': n.get('publish_time', ''),
                    'related_stocks': related,
                    'url': n.get('url', ''),
                })
            
            if not news_events:
                return
            
            # 调用 DataSubscriber.on_news_event()
            # 注意: 如果 FTSeek 主进程正在运行，on_news_event 会通过 HTTP 回调触发
            # 如果主进程未运行，此调用会静默失败 (不影响采集)
            try:
                import requests
                # 通过 FTSeek API 注入新闻事件
                resp = requests.post(
                    'http://localhost:8899/api/news/event',
                    json={'news_list': news_events},
                    timeout=5
                )
                if resp.status_code == 200:
                    logger.info(f"[事件驱动] {len(news_events)} 条快讯已注入管线")
                else:
                    logger.debug(f"[事件驱动] API 返回 {resp.status_code}，主进程可能未运行")
            except requests.exceptions.ConnectionError:
                logger.debug("[事件驱动] FTSeek 主进程未运行 (localhost:8899 不可达)，跳过注入")
            except Exception as e:
                logger.debug(f"[事件驱动] 注入失败: {e}")
                
        except Exception as e:
            logger.warning(f"[事件驱动] 管线触发异常: {e}")
    
    async def run(self):
        """执行定时采集"""
        logger.info("=" * 60)
        logger.info(f"富途快讯定时采集开始: {datetime.now()}")
        logger.info("=" * 60)
        
        self.stats['total_runs'] += 1
        self.stats['last_run'] = datetime.now().isoformat()
        
        # 1. 获取快讯
        news_list = await self.fetch_flash()
        
        if not news_list:
            logger.warning("未获取到快讯")
            return
        
        # 2. 保存快讯
        self.save_news(news_list)
        
        # 3. 推送重要快讯
        await self.push_major_news(news_list)
        
        # 3.5 事件驱动管线 (P0 修复 2026-04-16: 打通 futu_flash → 分层策略 → 共识引擎)
        await self._trigger_event_pipeline(news_list)
        
        # 4. 输出统计
        logger.info(f"\n本次统计:")
        logger.info(f"  获取快讯: {len(news_list)} 条")
        logger.info(f"  重要快讯: {self.stats['major_news']} 条")
        logger.info(f"  推送数量: {self.stats['pushed_news']} 条")
        logger.info(f"  累计快讯: {self.stats['total_news']} 条")
        
        # 5. 保存统计
        stats_file = DATA_DIR / 'futu_flash_stats.json'
        with open(stats_file, 'w') as f:
            json.dump(self.stats, f, indent=2)
        
        logger.info("-" * 60)
        logger.info(f"富途快讯定时采集完成")
        logger.info("-" * 60)


async def main():
    """主函数"""
    service = FutuFlashCronService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())