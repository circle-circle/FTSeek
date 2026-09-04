"""
FTSeek-US RSSHub 美股新闻源

数据源:
- RSSHub WSJ (华尔街日报)
- RSSHub Reuters (路透社)
- RSSHub Bloomberg (彭博社)
- RSSHub Fed (美联储)

注意: RSSHub 实例地址需可访问，否则自动跳过
"""

import hashlib
import time
import logging
from typing import List
from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, NewsSource
from app.news_v2.sources.base import SourceAdapter

logger = logging.getLogger("ftseek_us.news_v2.sources.rsshub_us")

# RSSHub 实例配置 (主 → 备)
# 2026-04-29: rsshub.feeded.xyz DNS 解析全面失败，已移除
RSSHUB_INSTANCES = [
    "https://rsshub.rssforever.com",
]

# 美股新闻路由
RSSHUB_ROUTES = {
    "wsj": {
        "path": "/wsj",
        "name": "华尔街日报",
        "source_id": NewsSource.RSSHUB_WSJ,
    },
    "reuters": {
        "path": "/reuters",
        "name": "路透社",
        "source_id": NewsSource.RSSHUB_REUTERS,
    },
    "fed": {
        "path": "/federalreserve/press",
        "name": "美联储",
        "source_id": NewsSource.RSSHUB_FED,
        "urgency_override": NewsUrgency.HIGH,
    },
}


class RSSHubUSSource(SourceAdapter):
    """
    RSSHub 美股新闻源适配器
    
    自动尝试多个 RSSHub 实例，失败后切换备用
    """
    
    def __init__(self, interval_seconds: int = 180):
        super().__init__(interval_seconds=interval_seconds)
        self._active_instance = 0
        self._routes = list(RSSHUB_ROUTES.values())
    
    @property
    def source_id(self) -> str:
        return "rsshub_us"
    
    @property
    def source_name(self) -> str:
        return "RSSHub 美股新闻聚合"
    
    async def fetch(self) -> List[UnifiedNewsItemV2]:
        """
        采集 RSSHub 美股新闻
        
        由于 RSSHub 是同步 API，使用 run_in_executor 避免阻塞
        
        P4 修复 (2026-05-06):
        - 单路由超时从15s降至5s (防止一只慢路由阻塞全部)
        - 连续失败3次的路由自动跳过24h
        - 增加重试1次机制
        """
        import requests
        
        # P4 修复: 初始化失败计数器
        if not hasattr(self, '_route_failures'):
            self._route_failures = {}  # route_name -> (fail_count, last_fail_time)
        
        items = []
        base_url = RSSHUB_INSTANCES[self._active_instance]
        
        for route in self._routes:
            name = route["name"]
            
            # P4 修复: 检查路由是否被冷却 (连续失败跳过)
            now = time.time()
            fail_info = self._route_failures.get(name, (0, 0))
            fail_count, last_fail = fail_info[0] if isinstance(fail_info, tuple) and len(fail_info) >= 2 else 0, fail_info[1] if isinstance(fail_info, tuple) and len(fail_info) >= 2 else 0
            if fail_count >= 3 and (now - last_fail) < 86400:
                logger.debug("[%s] 连续失败%d次，冷却中 (剩余%.0fh)", name, fail_count, (86400 - (now - last_fail)) / 3600)
                continue
            
            url = f"{base_url}{route['path']}"
            success = False
            
            # P4 修复: 单次超时5s + 重试1次
            for attempt in range(2):
                try:
                    loop = __import__('asyncio').get_running_loop()
                    resp = await loop.run_in_executor(
                        None, lambda u=url: requests.get(u, timeout=5, headers={
                            "User-Agent": "FTSeek-US/2.0 News Pipeline"
                        })
                    )
                    
                    if resp.status_code != 200:
                        logger.debug("[%s] HTTP %d, 跳过", route["name"], resp.status_code)
                        break
                    
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(resp.content)
                    
                    for channel_item in root.findall(".//item"):
                        title = channel_item.findtext("title", "").strip()
                        desc = channel_item.findtext("description", "").strip()
                        link = channel_item.findtext("link", "").strip()
                        pub_date = channel_item.findtext("pubDate", "")
                        
                        if not title:
                            continue
                        
                        # 清理描述
                        desc = "".join(c for c in desc if ord(c) < 128 or ord(c) > 255 or c in '，。！？；：""''（）【】')[:1500]
                        
                        # 解析时间
                        # DEB-20260903 #64: pubDate 解析失败时丢弃该条 —
                        # 原实现解析失败保持 time.time() (当前时间), 过期新闻被误作
                        # 即时新闻, 可能触发基于时效的信号生成
                        # DEB-20260904 #163: pubDate 缺失同理 — `if pub_date:` 为假时
                        # timestamp 保持当前时间, 旧闻被当即时新闻。缺失一并丢弃。
                        if not pub_date:
                            logger.debug("[%s] pubDate 缺失, 丢弃该条: %s", route["name"], title[:50])
                            continue
                        timestamp = time.time()
                        if pub_date:
                            try:
                                from email.utils import parsedate_to_datetime
                                dt = parsedate_to_datetime(pub_date)
                                timestamp = dt.timestamp()
                            except Exception:
                                logger.debug("[%s] pubDate 解析失败, 丢弃该条: %r", route["name"], pub_date)
                                continue
                        
                        # 计算 ID
                        item_id = f"{route['source_id']}_{hashlib.md5(title.encode()).hexdigest()[:8]}"
                        
                        urgency = route.get("urgency_override", NewsUrgency.NORMAL)
                        if any(kw in title.lower() for kw in ["breaking", "urgent", "alert"]):
                            urgency = NewsUrgency.IMMEDIATE
                        
                        items.append(UnifiedNewsItemV2(
                            id=item_id,
                            title=title,
                            content=desc,
                            source=route["source_id"],
                            source_name=route["name"],
                            timestamp=timestamp,
                            urgency=urgency,
                            raw_url=link,
                            market_tags=["US", "MACRO"],
                        ))
                    
                    logger.info("[%s] 采集 %d 条", route["name"], len([i for i in items if i.source == route["source_id"]]))
                    # P4 修复: 成功则重置失败计数
                    self._route_failures[name] = (0, 0)
                    success = True
                    break
                    
                except Exception as e:
                    if attempt == 0:
                        logger.debug("[%s] 第1次尝试失败，重试: %s", name, e)
                        continue
                    logger.warning("[%s] 采集失败: %s", name, e)
                    # P4 修复: 记录失败
                    prev = self._route_failures.get(name, (0, 0))
                    prev_count = prev[0] if isinstance(prev, tuple) and len(prev) >= 2 else 0
                    self._route_failures[name] = (prev_count + 1, now)
        
        # DEB-20260903 #65: RSSHUB_INSTANCES 仅剩 1 个实例, 原备用实例切换分支
        # (self._active_instance < len(RSSHUB_INSTANCES)-1 恒 False) 为死代码, 已移除。
        # 单实例风险: 该实例不可用时本数据源整体返回空, 无自动切换。
        return items
