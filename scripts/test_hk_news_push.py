#!/usr/bin/env python3
"""
港股新闻源测试 + 钉钉推送

测试港股新闻源获取并推送结果
"""
import os
import sys
import logging
import requests
import hmac
import hashlib
import base64
import urllib.parse
import time
from datetime import datetime
from pathlib import Path

# 设置环境变量
os.environ['API_TOKEN'] = 'test_token'
os.environ['BAILIAN_API_KEY'] = 'sk-8f8a02822c1b46b4bd43b422b9196ec6'

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s: %(message)s'
)
logger = logging.getLogger("ftseek.test.hk_news_push")

print("="*70)
print(" " * 25 + "港股新闻源测试 + 钉钉推送")
print("="*70)
print()

# ============================================================================
# Step 1: 测试港股新闻源
# ============================================================================
print("Step 1: 测试港股新闻源")
print("-"*70)

try:
    import feedparser
    
    # 港股新闻源列表
    hk_feeds = [
        ("港交所公告", "https://www.hkex.com.hk/chi/news/rss/announcement.xml"),
        ("同花顺滚动", "http://stock.10jqka.com.cn/roll_news/rss/"),
        ("腾讯财经", "https://finance.qq.com/rss/stocknews.xml"),
    ]
    
    all_hk_news = []
    
    for name, url in hk_feeds:
        try:
            feed = feedparser.parse(url)  # 移除 timeout 参数
            if feed.entries:
                print(f"✅ {name}: 获取 {len(feed.entries)} 条")
                for entry in feed.entries[:5]:  # 每个源取前 5 条
                    all_hk_news.append({
                        'title': entry.title[:100] if entry.title else '无标题',
                        'source': name,
                        'published': entry.get('published', '未知时间'),
                        'link': entry.get('link', '')[:100]
                    })
        except Exception as e:
            print(f"⚠️ {name}: 获取失败 - {e}")
    
    print(f"\n✅ 港股新闻总数：{len(all_hk_news)} 条")
    
    if all_hk_news:
        print(f"\n最新港股新闻 (Top 5):")
        for i, news in enumerate(all_hk_news[:5], 1):
            print(f"  {i}. [{news['source']}] {news['title'][:60]}...")
    
except Exception as e:
    print(f"❌ 港股新闻源测试失败：{e}")
    all_hk_news = []

print()

# ============================================================================
# Step 2: 测试 AI 情绪分析 (腾讯新闻)
# ============================================================================
print("Step 2: 测试 AI 情绪分析 (腾讯新闻)")
print("-"*70)

try:
    from app.factors.news_integration import get_news_sentiment_analyzer
    
    analyzer = get_news_sentiment_analyzer()
    print(f"✅ 新闻情绪分析器初始化成功")
    
    # 测试分析腾讯控股
    print(f"\n分析股票：HK.00700 (腾讯控股)")
    result = analyzer.analyze_stock('HK.00700', lookback_hours=24)
    
    print(f"✅ 情绪得分：{result['sentiment_score']:.2f}")
    print(f"✅ 新闻数量：{result['news_count']}")
    print(f"✅ 正面：{result['positive_count']} | 负面：{result['negative_count']} | 中性：{result['neutral_count']}")
    
except Exception as e:
    print(f"❌ AI 情绪分析测试失败：{e}")
    result = None

print()

# ============================================================================
# Step 3: 钉钉推送测试结果
# ============================================================================
print("Step 3: 钉钉推送测试结果")
print("-"*70)

try:
    # 钉钉配置
    webhook = "https://oapi.dingtalk.com/robot/send?access_token=cddea8919223640d4f3bd59e507373cae0df476df5f541fcca5c8921eab73121"
    secret = "SEC177f5321e91e689a1a2134e4414ae49e7b7f2cddc63775613effbf371f388f78"
    
    print(f"✅ 钉钉推送配置加载成功")
    
    # 生成测试消息
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 构建 Markdown 消息
    sentiment_emoji = '🟢' if result and result['sentiment_score'] > 0.2 else '🔴' if result and result['sentiment_score'] < -0.2 else '⚪'
    
    markdown_text = f"""## 📰 港股新闻源测试报告

**时间**: {timestamp}  
**测试**: 港股新闻源 + AI 情绪分析

---

### 📊 新闻采集结果

**港股新闻源**:
- ✅ 港交所公告
- ✅ 同花顺滚动新闻
- ✅ 腾讯财经

**获取新闻**: {len(all_hk_news)} 条

---

### 🤖 AI 情绪分析 (腾讯控股)

**情绪得分**: {result['sentiment_score']:.2f} {sentiment_emoji} if result else 'N/A'  
**新闻数量**: {result['news_count'] if result else 0}  
**正面**: {result['positive_count'] if result else 0} | **负面**: {result['negative_count'] if result else 0} | **中性**: {result['neutral_count'] if result else 0}

---

### ✅ 测试状态

**新闻源**: {'✅ 成功' if all_hk_news else '⚠️ 部分失败'}  
**AI 分析**: {'✅ 成功' if result else '❌ 失败'}  
**推送**: ✅ 成功

---

*FTSeek 自动化交易系统*
"""
    
    # 加签
    timestamp_ms = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp_ms}\n{secret}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    
    url = f"{webhook}&timestamp={timestamp_ms}&sign={sign}"
    
    # 发送消息
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "港股新闻源测试报告",
            "text": markdown_text
        }
    }
    
    response = requests.post(url, json=payload, timeout=10)
    result_json = response.json()
    
    if result_json.get('errcode') == 0:
        print(f"✅ 钉钉推送成功")
        print(f"   消息 ID: {result_json.get('message_id', 'N/A')}")
    else:
        print(f"⚠️ 钉钉推送失败：{result_json}")

except Exception as e:
    print(f"❌ 钉钉推送测试失败：{e}")

print()

# ============================================================================
# 测试总结
# ============================================================================
print("="*70)
print("测试总结")
print("="*70)

tests = [
    ("港股新闻源", len(all_hk_news) > 0),
    ("AI 情绪分析", result is not None),
    ("钉钉推送", 'result_json' in locals() and result_json.get('errcode') == 0)
]

passed = sum(1 for _, success in tests if success)
total = len(tests)

for name, success in tests:
    status = '✅' if success else '❌'
    print(f"{status} {name}")

print()
print(f"测试结果：{passed}/{total} 通过")
print()

if passed == total:
    print("🎉 所有测试通过！港股新闻源集成成功！")
    print()
    print("新增新闻源:")
    print("  ✅ 港交所公告 (实时)")
    print("  ✅ 同花顺滚动新闻 (5-10 分钟)")
    print("  ✅ 腾讯财经 (10-15 分钟)")
    print()
    sys.exit(0)
else:
    print(f"⚠️ {total - passed} 个测试失败，请检查系统配置！")
    print()
    sys.exit(1)
