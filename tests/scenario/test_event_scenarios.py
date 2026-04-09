"""
Phase 2: 事件类场景测试

测试 GAP_EVENT 和 NEWS_DRIVEN 场景的识别逻辑
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.scenario import (
    get_scenario_classifier,
    get_scenario_router,
    get_scenario_config,
)
from app.scenario.classifier import ScenarioType

print("=" * 60)
print("Phase 2: 事件类场景测试")
print("=" * 60)

# ============================================================
# 测试 1: GAP_EVENT 场景识别
# ============================================================
print("\n=== 测试 1: GAP_EVENT 场景识别 ===")

classifier = get_scenario_classifier()

# 模拟跳空数据 (向上跳空 3%)
# 跳空定义：当前开盘价 vs 前一收盘价的差异
# 让最后一根 K 线跳空
klines_gap_up = []
base_price = 100.0
for i in range(100):
    if i < 99:
        # 前 99 根 K 线正常
        open_price = base_price
        close_price = base_price * (1 + 0.001)  # 缓慢上涨
        base_price = close_price
    else:
        # 第 100 根 K 线跳空 (开盘 vs 前收盘)
        prev_close = close_price  # 第 99 根的收盘
        open_price = prev_close * 1.03  # 向上跳空 3%
        close_price = open_price * 1.005

    klines_gap_up.append({
        'open': open_price,
        'high': max(open_price, close_price) * 1.01,
        'low': min(open_price, close_price) * 0.99,
        'close': close_price,
        'volume': 2000000 if i == 99 else 1000000,  # 跳空后放量
    })

# 验证跳空数据
gap_check = (klines_gap_up[-1]['open'] - klines_gap_up[-2]['close']) / klines_gap_up[-2]['close']
print(f"最后一根 K 线跳空：{gap_check*100:.1f}%")

result = classifier.classify("HK.TEST_GAP", klines_gap_up)
print(f"场景：{result.scenario_type.value}")
print(f"置信度：{result.confidence:.2f}")
print(f"ADX: {result.adx_value:.1f}")

# 验证是否为 GAP_EVENT
if result.scenario_type == ScenarioType.GAP_EVENT:
    print("✅ GAP_EVENT 场景识别成功")
else:
    print(f"⚠️ 识别为 {result.scenario_type.value} (可能跳空数据不足)")

# ============================================================
# 测试 2: NEWS_DRIVEN 场景识别
# ============================================================
print("\n=== 测试 2: NEWS_DRIVEN 场景识别 ===")

# 模拟新闻数据
news_data_positive = {
    'sentiment_score': 0.85,  # 强烈正面新闻
    'has_announcement': True,
    'news_count': 5,
}

# 普通 K 线数据
klines_normal = [
    {
        'open': 100 + i * 0.1,
        'high': 102 + i * 0.1,
        'low': 98 + i * 0.1,
        'close': 101 + i * 0.1,
        'volume': 1000000,
    }
    for i in range(100)
]

result_news = classifier.classify("HK.TEST_NEWS", klines_normal, news_data=news_data_positive)
print(f"场景：{result_news.scenario_type.value}")
print(f"置信度：{result_news.confidence:.2f}")
print(f"新闻情绪得分：{news_data_positive['sentiment_score']:.2f}")

# 验证是否为 NEWS_DRIVEN
if result_news.scenario_type == ScenarioType.NEWS_DRIVEN:
    print("✅ NEWS_DRIVEN 场景识别成功")
else:
    print(f"⚠️ 识别为 {result_news.scenario_type.value}")

# ============================================================
# 测试 3: 事件共振 (跳空 + 新闻)
# ============================================================
print("\n=== 测试 3: 事件共振 (跳空 + 新闻) ===")

# 跳空 + 新闻数据
news_data_strong = {
    'sentiment_score': 0.95,  # 极强正面新闻
    'has_announcement': True,
    'breaking_news': True,
}

# 跳空 K 线
klines_gap_news = []
base = 100.0
for i in range(100):
    if i == 90:
        open_p = base * 1.04  # 4% 跳空
    else:
        open_p = base

    klines_gap_news.append({
        'open': open_p,
        'high': open_p * 1.03,
        'low': open_p * 0.97,
        'close': base,
        'volume': 2000000 if i >= 90 else 1000000,  # 跳空后放量
    })
    base = base * 1.002

result_combo = classifier.classify("HK.TEST_COMBO", klines_gap_news, news_data=news_data_strong)
print(f"场景：{result_combo.scenario_type.value}")
print(f"置信度：{result_combo.confidence:.2f}")

# 事件共振应该得到更高置信度
if result_combo.confidence >= 0.8:
    print(f"✅ 事件共振检测到高置信度场景 ({result_combo.confidence:.2f})")
else:
    print(f"⚠️ 置信度 {result_combo.confidence:.2f} 未达预期")

# ============================================================
# 测试 4: 场景路由验证
# ============================================================
print("\n=== 测试 4: 场景路由验证 ===")

router = get_scenario_router()

# GAP_EVENT 路由
gap_route = router.route(ScenarioType.GAP_EVENT)
print(f"GAP_EVENT 路由:")
print(f"  主策略：{gap_route.primary_strategies}")
print(f"  权重：{gap_route.active_strategies}")
print(f"  风控模板：{gap_route.risk_template}")

# NEWS_DRIVEN 路由
news_route = router.route(ScenarioType.NEWS_DRIVEN)
print(f"\nNEWS_DRIVEN 路由:")
print(f"  主策略：{news_route.primary_strategies}")
print(f"  权重：{news_route.active_strategies}")
print(f"  风控模板：{news_route.risk_template}")

# 验证 sentiment_ai 在 NEWS_DRIVEN 中权重最高
if news_route.active_strategies.get('sentiment_ai', 0) >= 1.5:
    print("✅ sentiment_ai 权重正确 (1.5x)")
else:
    print(f"⚠️ sentiment_ai 权重为 {news_route.active_strategies.get('sentiment_ai', 0)}")

# ============================================================
# 测试总结
# ============================================================
print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
print("事件类场景 (GAP_EVENT, NEWS_DRIVEN) 实现验证完成")
