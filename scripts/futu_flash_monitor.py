#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
富途快讯优雅获取方案 - 多方式集成

最优雅的方式:
1. WebSocket 实时推送 (最佳)
2. akshare 快讯接口 (最简单)
3. 富途 APP 抓包 API (最直接)
4. Playwright 动态抓取 (最稳定)

作者: FTSeek Team
日期: 2026-04-12
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger('futu_flash_monitor')

# ============================================================
# 方案 1: akshare 快讯接口 (最简单)
# ============================================================

async def fetch_flash_akshare() -> List[dict]:
    """
    使用 akshare 获取快讯
    
    优点: 一行代码，已封装
    缺点: 数据源可能是东方财富，非富途原生
    """
    try:
        import akshare as ak
        
        # 东方财富快讯
        df = ak.stock_news_em(symbol="快讯")
        
        if df is not None and len(df) > 0:
            news_list = []
            for _, row in df.head(20).iterrows():
                news = {
                    'id': f"akshare-{hash(row['标题']) % 1000000}",
                    'title': row['标题'],
                    'content': row.get('内容', row['标题']),
                    'source': 'eastmoney',  # akshare 数据源是东方财富
                    'publish_time': row.get('发布时间', datetime.now().isoformat()),
                    'url': row.get('链接', ''),
                    'importance': 'normal',
                }
                news_list.append(news)
            
            logger.info(f"akshare 获取 {len(news_list)} 条快讯")
            return news_list
            
    except ImportError:
        logger.warning("akshare 未安装，请运行: pip install akshare")
    except Exception as e:
        logger.warning(f"akshare 获取失败: {e}")
    
    return []


# ============================================================
# 方案 2: WebSocket 实时推送 (最优雅)
# ============================================================

class FutuWebSocketMonitor:
    """
    富途 WebSocket 快讯监听
    
    优点: 实时推送，无需轮询
    缺点: 需要找到正确的 WebSocket 端点
    
    可能的端点 (需验证):
    - wss://push.futunn.com/v2/ws
    - wss://realtime.futunn.com/ws
    - wss://news.futunn.com/ws/live
    """
    
    WS_URLS = [
        'wss://push.futunn.com/v2/ws',
        'wss://realtime.futunn.com/ws',
    ]
    
    def __init__(self):
        self.ws = None
        self.running = False
        self.news_buffer = []
        
    async def connect(self, url: str):
        """连接 WebSocket"""
        try:
            import websockets
            
            self.ws = await websockets.connect(
                url,
                extra_headers={
                    'User-Agent': 'FutuNN-WS/1.0',
                    'Origin': 'https://www.futunn.com',
                }
            )
            logger.info(f"WebSocket 连接成功: {url}")
            self.running = True
            
            # 发送订阅消息
            subscribe_msg = {
                'action': 'subscribe',
                'channel': 'news_flash',
                'lang': 'zh-cn',
            }
            await self.ws.send(json.dumps(subscribe_msg))
            
            return True
            
        except ImportError:
            logger.warning("websockets 未安装，请运行: pip install websockets")
            return False
        except Exception as e:
            logger.warning(f"WebSocket 连接失败: {e}")
            return False
    
    async def listen(self):
        """监听推送消息"""
        if not self.ws:
            return
        
        try:
            while self.running:
                msg = await self.ws.recv()
                data = json.loads(msg)
                
                # 处理快讯推送
                if data.get('type') == 'flash_news':
                    news = {
                        'id': data.get('id'),
                        'title': data.get('title'),
                        'content': data.get('content'),
                        'source': 'futu_ws',
                        'publish_time': data.get('time'),
                        'importance': data.get('importance', 'normal'),
                    }
                    self.news_buffer.append(news)
                    logger.info(f"收到快讯: {news['title'][:50]}")
                    
        except Exception as e:
            logger.warning(f"WebSocket 监听异常: {e}")
    
    async def close(self):
        """关闭连接"""
        self.running = False
        if self.ws:
            await self.ws.close()
    
    def get_news(self) -> List[dict]:
        """获取缓存的快讯"""
        news = self.news_buffer[:]
        self.news_buffer.clear()
        return news


async def test_websocket():
    """测试 WebSocket 连接"""
    monitor = FutuWebSocketMonitor()
    
    for url in FutuWebSocketMonitor.WS_URLS:
        if await monitor.connect(url):
            # 监听 30 秒
            await asyncio.wait_for(monitor.listen(), timeout=30)
            await monitor.close()
            return monitor.get_news()
    
    return []


# ============================================================
# 方案 3: 富途 APP 抓包 API (最直接)
# ============================================================

async def fetch_flash_futu_api(token: str = None) -> List[dict]:
    """
    使用富途内部 API (需抓包获取)
    
    优点: 富途原生数据，最快
    缺点: 需要认证 token
    
    抓包步骤:
    1. 安装 Charles/Fiddler
    2. 配置 HTTPS 抓包
    3. 打开富途牛牛 APP
    4. 查看快讯请求
    5. 提取 API 端点和 token
    
    常见端点:
    - https://api.futunn.com/v2/flash/list
    - https://www.futuhkapp.com/api/news/live
    """
    
    # 示例 API 配置 (需实际抓包填充)
    api_config = {
        'url': 'https://api.futunn.com/v2/flash/list',
        'headers': {
            'User-Agent': 'FutuNN/...',
            'Authorization': f'Bearer {token}',
            'X-Device-ID': '...',
            'X-Version': '...',
        },
        'params': {
            'lang': 'zh-cn',
            'limit': 20,
        }
    }
    
    # 如果没有 token，返回空
    if not token:
        logger.warning("富途 API 需要认证 token，请抓包获取")
        logger.info("""
抓包步骤:
  1. 安装 Charles: https://www.charlesproxy.com/
  2. 配置 HTTPS 代理
  3. 手机连接代理
  4. 打开富途牛牛 APP
  5. 查看快讯请求
  6. 提取以下信息:
     - API URL
     - Authorization header
     - X-Device-ID
     - 请求参数
""")
        return []
    
    try:
        import aiohttp
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_config['url'],
                headers=api_config['headers'],
                params=api_config['params']
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if 'data' in data and 'list' in data['data']:
                        news_list = []
                        for item in data['data']['list']:
                            news = {
                                'id': f"futu-{item.get('id')}",
                                'title': item.get('title'),
                                'content': item.get('content'),
                                'source': 'futu_api',
                                'publish_time': item.get('time'),
                                'importance': item.get('importance', 'normal'),
                            }
                            news_list.append(news)
                        
                        logger.info(f"富途 API 获取 {len(news_list)} 条快讯")
                        return news_list
                        
    except Exception as e:
        logger.warning(f"富途 API 获取失败: {e}")
    
    return []


# ============================================================
# 方案 4: Playwright 动态抓取 (最稳定)
# ============================================================

async def fetch_flash_playwright() -> List[dict]:
    """
    使用 Playwright 动态抓取
    
    优点: 稳定可靠，完整数据
    缺点: 需要 playwright 和浏览器
    """
    try:
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # 设置中文
            await page.set_extra_http_headers({'Accept-Language': 'zh-CN,zh'})
            
            # 访问快讯页面
            await page.goto('https://news.futunn.com/main/live?lang=zh-cn')
            await page.wait_for_timeout(5000)  # 等待加载
            
            # 提取快讯
            news_elements = await page.query_selector_all('a[href^="/post/"]')
            
            news_list = []
            for elem in news_elements[:20]:
                href = await elem.get_attribute('href')
                text = await elem.inner_text()
                
                if text and len(text) > 10:
                    news = {
                        'id': f"playwright-{href.split('/post/')[-1].split('/')[0]}",
                        'title': text.strip(),
                        'content': text.strip(),
                        'source': 'futu_page',
                        'publish_time': datetime.now().isoformat(),
                        'url': f"https://news.futunn.com{href}",
                        'importance': 'normal',
                    }
                    news_list.append(news)
            
            await browser.close()
            
            logger.info(f"Playwright 获取 {len(news_list)} 条快讯")
            return news_list
            
    except ImportError:
        logger.warning("playwright 未安装，请运行: pip install playwright && playwright install chromium")
    except Exception as e:
        logger.warning(f"Playwright 获取失败: {e}")
    
    return []


# ============================================================
# 综合方案: 自动选择最佳方式
# ============================================================

async def fetch_futu_flash_best() -> List[dict]:
    """
    自动选择最佳方式获取富途快讯
    
    优先级:
    1. akshare (最简单，无需额外配置)
    2. playwright (最稳定)
    3. WebSocket (最优雅，需端点)
    4. 富途 API (最快，需认证)
    """
    
    # 方式 1: akshare
    news = await fetch_flash_akshare()
    if news:
        return news
    
    # 方式 2: playwright
    news = await fetch_flash_playwright()
    if news:
        return news
    
    # 方式 3: WebSocket (需要找到正确端点)
    # news = await test_websocket()
    # if news:
    #     return news
    
    # 方式 4: 富途 API (需要 token)
    # news = await fetch_flash_futu_api(token)
    # if news:
    #     return news
    
    logger.warning("所有获取方式均失败")
    return []


# ============================================================
# 测试
# ============================================================

async def test_all_methods():
    """测试所有获取方式"""
    print("=" * 70)
    print("🔍 富途快讯获取方式测试")
    print("=" * 70)
    
    print("\n【方式 1】akshare:")
    news = await fetch_flash_akshare()
    print(f"  结果: {len(news)} 条")
    if news:
        print(f"  示例: {news[0]['title'][:50]}...")
    
    print("\n【方式 2】playwright:")
    news = await fetch_flash_playwright()
    print(f"  结果: {len(news)} 条")
    if news:
        print(f"  示例: {news[0]['title'][:50]}...")
    
    print("\n【方式 3】WebSocket:")
    print(f"  需要: 找到正确的 WebSocket 端点")
    print(f"  建议: 抓包富途 APP 或网页")
    
    print("\n【方式 4】富途 API:")
    print(f"  需要: 抓包获取认证 token")
    print(f"  步骤: 见上方说明")
    
    print("\n【最佳方案】自动选择:")
    news = await fetch_futu_flash_best()
    print(f"  结果: {len(news)} 条快讯")
    
    print("\n" + "-" * 70)
    print("✅ 测试完成")
    print("-" * 70)


if __name__ == "__main__":
    asyncio.run(test_all_methods())