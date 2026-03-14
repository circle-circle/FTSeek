#!/usr/bin/env python3
"""
AI 新闻源获取 + 推送完整链路测试

测试流程:
1. 新闻采集器获取新闻
2. AI 情绪分析
3. 策略融合
4. 钉钉推送测试
"""
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

# 设置环境变量
os.environ['API_TOKEN'] = 'test_token'
os.environ['BAILIAN_API_KEY'] = 'sk-8f8a02822c1b46b4bd43b422b9196ec6'
os.environ['FTSEEK_ENABLE_AI'] = 'true'

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s: %(message)s'
)
logger = logging.getLogger("ftseek.test.news_push")

print("="*70)
print(" " * 20 + "AI 新闻源获取 + 推送完整链路测试")
print("="*70)
print()

# ============================================================================
# Step 1: 测试新闻采集器
# ============================================================================
print("Step 1: 测试新闻采集器")
print("-"*70)

try:
    from app.news_push.collector import get_news_collector
    
    collector = get_news_collector()
    print(f"✅ 新闻采集器初始化成功")
    
    # 获取所有新闻
    all_news = collector.fetch_all()
    print(f"✅ 获取新闻总数：{len(all_news)} 条")
    
    if all_news:
        # 显示前 5 条新闻
        print(f"\n最新新闻 (Top 5):")
        for i, news in enumerate(all_news[:5], 1):
            # NewsItem 对象使用属性访问
            source = getattr(news, 'source', '未知')
            title = getattr(news, 'title', '无标题')[:60]
            priority = getattr(news, 'priority', 5)
            print(f"  {i}. [{source}] {title}... (优先级：{priority})")
    
except Exception as e:
    print(f"❌ 新闻采集器测试失败：{e}")
    all_news = []

print()

# ============================================================================
# Step 2: 测试 AI 情绪分析
# ============================================================================
print("Step 2: 测试 AI 情绪分析")
print("-"*70)

try:
    from app.factors.news_integration import get_news_sentiment_analyzer
    
    analyzer = get_news_sentiment_analyzer()
    print(f"✅ 新闻情绪分析器初始化成功")
    
    # 测试分析腾讯控股
    if all_news:
        print(f"\n分析股票：HK.00700 (腾讯控股)")
        result = analyzer.analyze_stock('HK.00700', lookback_hours=24)
        
        print(f"✅ 情绪得分：{result['sentiment_score']:.2f}")
        print(f"✅ 新闻数量：{result['news_count']}")
        print(f"✅ 正面：{result['positive_count']} | 负面：{result['negative_count']} | 中性：{result['neutral_count']}")
        print(f"✅ 缓存命中：{result['cache_hit']}")
        
        if result['top_news']:
            print(f"\nTop 新闻情绪:")
            for i, news in enumerate(result['top_news'][:3], 1):
                sentiment = '🟢' if news['sentiment_score'] > 0.2 else '🔴' if news['sentiment_score'] < -0.2 else '⚪'
                print(f"  {i}. {sentiment} {news['title'][:60]}...")
                print(f"     得分：{news['sentiment_score']:.2f} | 来源：{news['source']}")
    else:
        print(f"⚠️ 无新闻数据，跳过 AI 分析")
        result = None

except Exception as e:
    print(f"❌ AI 情绪分析测试失败：{e}")
    result = None

print()

# ============================================================================
# Step 3: 测试 AI 情绪因子
# ============================================================================
print("Step 3: 测试 AI 情绪因子")
print("-"*70)

try:
    from app.factors import AISentimentFactor
    
    factor = AISentimentFactor({
        'auto_fetch_news': True,
        'lookback_hours': 24,
        'max_news': 10
    })
    print(f"✅ AI 情绪因子初始化成功")
    
    # 测试计算
    if all_news:
        print(f"\n计算情绪因子：HK.00700")
        factor_result = factor.calculate('HK.00700', {})
        
        if factor_result:
            print(f"✅ 情绪因子值：{factor_result.value:.2f}")
            print(f"✅ 自动获取：{factor_result.metadata.get('auto_fetched', False)}")
            print(f"✅ 新闻数量：{factor_result.metadata.get('news_count', 0)}")
    else:
        print(f"⚠️ 无新闻数据，使用备用方案")
        factor_result = factor.calculate('HK.00700', {
            'news': [
                {'title': '腾讯业绩超预期', 'content': '净利润增长 50%'},
                {'title': '游戏业务强劲增长', 'content': '新游戏表现优异'}
            ]
        })
        print(f"✅ 情绪因子值：{factor_result.value:.2f} (备用方案)")

except Exception as e:
    print(f"❌ AI 情绪因子测试失败：{e}")
    factor_result = None

print()

# ============================================================================
# Step 4: 测试策略融合
# ============================================================================
print("Step 4: 测试策略融合 (FusionV3)")
print("-"*70)

try:
    from app.strategy.fusion_v3 import FusionV3Strategy
    
    strategy = FusionV3Strategy({
        'use_ai': True,
        'ai_weight': 0.3
    })
    print(f"✅ FusionV3 策略初始化成功")
    
    # 模拟报价
    quote = {
        'last_done': 390.0,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    print(f"\n生成交易信号：HK.00700 @ 390.0")
    signal = strategy.on_quote('HK.00700', quote)
    
    if signal:
        print(f"✅ 信号方向：{signal.action}")
        print(f"✅ 置信度：{signal.confidence:.2f}")
        print(f"✅ AI 融合：{signal.metadata.get('ai_fused', False)}")
        if signal.metadata.get('ai_direction'):
            print(f"✅ AI 预测：{signal.metadata['ai_direction']}")
    else:
        print(f"⚠️ 无交易信号")

except Exception as e:
    print(f"❌ 策略融合测试失败：{e}")
    signal = None

print()

# ============================================================================
# Step 5: 测试钉钉推送
# ============================================================================
print("Step 5: 测试钉钉推送")
print("-"*70)

try:
    from app.ai import AIService
    import requests
    import hmac
    import hashlib
    import base64
    import urllib.parse
    import time
    
    # 钉钉配置
    webhook = "https://oapi.dingtalk.com/robot/send?access_token=cddea8919223640d4f3bd59e507373cae0df476df5f541fcca5c8921eab73121"
    secret = "SEC177f5321e91e689a1a2134e4414ae49e7b7f2cddc63775613effbf371f388f78"
    
    print(f"✅ 钉钉推送配置加载成功")
    
    # 生成测试消息
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 构建 Markdown 消息
    if signal and result:
        sentiment_emoji = '🟢' if result['sentiment_score'] > 0.2 else '🔴' if result['sentiment_score'] < -0.2 else '⚪'
        signal_emoji = '🟢' if signal.action == 'BUY' else '🔴' if signal.action == 'SELL' else '⚪'
        
        markdown_text = f"""## {signal_emoji} FTSeek AI 新闻推送测试

**时间**: {timestamp}  
**股票**: HK.00700 (腾讯控股)  
**信号**: {signal.action}  
**置信度**: {signal.confidence:.0%}

---

### 📰 新闻情绪分析

**情绪得分**: {result['sentiment_score']:.2f} {sentiment_emoji}  
**新闻数量**: {result['news_count']} 条  
**正面**: {result['positive_count']} | **负面**: {result['negative_count']} | **中性**: {result['neutral_count']}

### 🤖 AI 预测

**方向**: {signal.metadata.get('ai_direction', 'N/A')}  
**置信度**: {signal.metadata.get('ai_confidence', 0)}%

---

**测试状态**: ✅ 完整链路测试成功

*FTSeek 自动化交易系统*
"""
    else:
        markdown_text = f"""## ⚠️ FTSeek AI 新闻推送测试

**时间**: {timestamp}  

**测试状态**: ⚠️ 部分功能测试失败

*请检查新闻采集器和 AI 服务配置*
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
            "title": "FTSeek AI 新闻推送测试",
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
    ("新闻采集器", len(all_news) > 0),
    ("AI 情绪分析", result is not None),
    ("AI 情绪因子", factor_result is not None),
    ("策略融合", signal is not None),
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
    print("🎉 所有测试通过！完整链路验证成功！")
    print()
    print("链路流程:")
    print("  1. 新闻采集器获取新闻 (财联社/华尔街见闻等)")
    print("  2. AI 情绪分析 (Bailian API)")
    print("  3. AI 情绪因子计算")
    print("  4. FusionV3 策略融合")
    print("  5. 钉钉推送")
    print()
    sys.exit(0)
else:
    print(f"⚠️ {total - passed} 个测试失败，请检查系统配置！")
    print()
    sys.exit(1)
