#!/usr/bin/env python3
"""
使用 AKShare 获取港股新闻

安装: pip install akshare
"""
import akshare as ak
import json
from datetime import datetime

def fetch_hk_news(stock_code: str = "美团-W") -> list:
    """
    获取港股新闻
    
    Args:
        stock_code: 股票名称 (如 "美团-W", "腾讯控股")
    
    Returns:
        新闻列表
    """
    try:
        df = ak.stock_news_em(symbol=stock_code)
        
        news_list = []
        for _, row in df.iterrows():
            news_list.append({
                'title': row.get('标题', ''),
                'content': row.get('内容', ''),
                'source': row.get('来源', '东方财富'),
                'url': row.get('链接', ''),
                'published_at': row.get('发布时间', ''),
                'fetched_at': datetime.now().isoformat(),
            })
        
        return news_list
    except Exception as e:
        print(f"获取新闻失败: {e}")
        return []

def fetch_global_news() -> list:
    """获取全球财经新闻"""
    try:
        # 金十数据快讯
        df = ak.fx_js_news()
        
        news_list = []
        for _, row in df.iterrows():
            news_list.append({
                'title': row.get('content', ''),
                'content': '',
                'source': '金十数据',
                'url': '',
                'published_at': row.get('datetime', ''),
                'fetched_at': datetime.now().isoformat(),
            })
        
        return news_list
    except Exception as e:
        print(f"获取快讯失败: {e}")
        return []

if __name__ == "__main__":
    # 测试
    news = fetch_hk_news("美团-W")
    print(f"获取到 {len(news)} 条美团新闻")
    
    if news:
        for n in news[:3]:
            print(f"- {n['title'][:50]}...")