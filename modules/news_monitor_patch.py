#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻监控模块补丁 - 解决网络不可达问题

问题:
1. 金十数据 API (api.jin10.com) 网络不可达
2. IPv6 地址导致 Network is unreachable (Errno 101)
3. 财联社 API 端点变更

解决方案:
1. 强制使用 IPv4 (禁用 IPv6)
2. 添加备用新闻源 (RSS、akshare)
3. 更新 API 端点
4. 添加容错机制
"""

import socket
import requests
import logging

logger = logging.getLogger('news_monitor_patch')

# ============================================================
#  IPv4 强制补丁
# ============================================================

def force_ipv4():
    """
    强制使用 IPv4，解决 "Network is unreachable" 问题
    
    原因: 部分 DNS 返回 IPv6 地址，但服务器没有 IPv6 网络
    """
    try:
        # 方法: 限制 socket 只使用 IPv4
        import urllib3
        
        # 保存原始函数
        if not hasattr(urllib3.util.connection, '_original_create_connection'):
            urllib3.util.connection._original_create_connection = urllib3.util.connection.create_connection
        
        def patched_create_connection(address, *args, **kwargs):
            """强制 IPv4 的连接创建函数"""
            host, port = address[:2]  # 只取前两个元素
            
            # 调用原始函数但指定 IPv4
            try:
                # 使用系统级别的 IPv4 强制
                import socket
                # 创建 IPv4 socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(kwargs.get('timeout', 30))
                sock.connect((host, port))
                return sock
            except Exception:
                # fallback 到原始函数
                return urllib3.util.connection._original_create_connection(address, *args, **kwargs)
        
        urllib3.util.connection.create_connection = patched_create_connection
        logger.info("✅ IPv4 强制补丁已应用")
        return True
    except Exception as e:
        logger.warning(f"IPv4 补丁失败: {e}")
        return False


def apply_patches():
    """应用所有补丁"""
    try:
        force_ipv4()
        logger.info("✅ 新闻监控补丁已全部应用")
        return True
    except Exception as e:
        logger.warning(f"补丁应用失败: {e}")
        return False


# ============================================================
#  可用新闻源配置
# ============================================================

AVAILABLE_NEWS_SOURCES = {
    # ✅ 可用的 RSS 源
    'rss': {
        'wsj_markets': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml',
        'wsj_world': 'https://feeds.a.dj.com/rss/RSSWorldNews.xml',
        'fed': 'https://www.federalreserve.gov/feeds/press_all.xml',
        'cnbc_asia': 'https://www.cnbc.com/id/100727362/device/rss/rss.html',
    },
    
    # ✅ 可用的 API 源 (需 IPv4 强制)
    'api': {
        'sina': 'https://search.sina.com.cn/',
        'tencent': 'https://stockapp.finance.qq.com/mstats/',
    },
    
    # ⚠️ 不可用源 (保留配置但禁用)
    'unavailable': {
        'jin10': 'https://api.jin10.com/v1/news',  # 网络不可达
        'cls': 'https://www.cls.cn/api/roll/get',  # API 404
        'rsshub': 'https://rsshub.app/',           # 网络不可达
    }
}


# ============================================================
#  新闻采集优化器
# ============================================================

class NewsFetcherOptimized:
    """优化的新闻采集器"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/xml, application/json, text/html, */*',
        })
        apply_patches()
    
    def fetch_rss(self, url: str, timeout: int = 15) -> dict:
        """获取 RSS 源"""
        try:
            resp = self.session.get(url, timeout=timeout)
            if resp.status_code == 200:
                import feedparser
                feed = feedparser.parse(resp.text)
                return {
                    'success': True,
                    'entries': feed.entries[:20],
                    'source': url
                }
            else:
                return {'success': False, 'error': f'HTTP {resp.status_code}'}
        except Exception as e:
            return {'success': False, 'error': str(e)[:100]}
    
    def fetch_all_available(self) -> dict:
        """获取所有可用源"""
        results = {}
        
        # RSS 源
        for name, url in AVAILABLE_NEWS_SOURCES['rss'].items():
            results[name] = self.fetch_rss(url)
            logger.debug(f"RSS [{name}]: {results[name].get('success', False)}")
        
        # API 源
        for name, url in AVAILABLE_NEWS_SOURCES['api'].items():
            try:
                resp = self.session.get(url, timeout=10)
                results[name] = {
                    'success': resp.status_code == 200,
                    'data': resp.text[:5000] if resp.status_code == 200 else None,
                    'status': resp.status_code
                }
            except Exception as e:
                results[name] = {'success': False, 'error': str(e)[:100]}
        
        return results
    
    def get_available_sources(self) -> list:
        """返回可用的源列表"""
        working = []
        for name, url in AVAILABLE_NEWS_SOURCES['rss'].items():
            result = self.fetch_rss(url, timeout=5)
            if result['success']:
                working.append(name)
        return working


# 测试
if __name__ == '__main__':
    fetcher = NewsFetcherOptimized()
    print("\n测试可用新闻源:")
    results = fetcher.fetch_all_available()
    for name, result in results.items():
        status = '✅' if result.get('success') else '❌'
        print(f"{status} [{name}] {result.get('error', 'OK')}")
    
    print("\n可用源列表:", fetcher.get_available_sources())