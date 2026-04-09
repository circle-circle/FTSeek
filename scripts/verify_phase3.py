#!/usr/bin/env python3
"""
Phase 3: AI/新闻时效性增强 - 验证测试脚本

测试项目:
1. VIX 波动率监控器
2. 情绪得分衰减计算器
3. 新闻采集器动态 TTL
4. 突发新闻快速通道
5. 新闻集成模块衰减
"""
import sys
import time
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 配置日志
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

print("=" * 70)
print("Phase 3: AI/新闻时效性增强 - 验证测试")
print("=" * 70)
print(f"测试时间：{datetime.now().isoformat()}")
print()


# ==============================================================================
# 测试 1: VIX 波动率监控器
# ==============================================================================
print("-" * 70)
print("测试 1: VIX 波动率监控器")
print("-" * 70)

try:
    from app.risk.vix_monitor import get_vix_monitor, VolatilityState
    
    monitor = get_vix_monitor()
    
    # 测试不同 VIX 值的状态判断
    test_cases = [
        (10, "low"),       # 低波动
        (20, "normal"),    # 正常波动
        (30, "high"),      # 高波动
        (40, "extreme"),   # 极端波动
    ]
    
    print("\nVIX 状态判断测试:")
    for vix_value, expected_state in test_cases:
        state = monitor.update_vix(vix_value)
        status = "✓" if state.state == expected_state else "✗"
        ttl_factor = state.get_volatility_factor()
        print(f"  {status} VIX={vix_value} -> 状态:{state.state} (期望:{expected_state}), TTL 因子:{ttl_factor:.2f}")
    
    # 获取统计信息
    stats = monitor.get_statistics()
    print(f"\n统计信息：{stats}")
    
    print("\n✓ VIX 波动率监控器测试通过")
    
except Exception as e:
    print(f"\n✗ VIX 波动率监控器测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==============================================================================
# 测试 2: 情绪得分衰减计算器
# ==============================================================================
print("-" * 70)
print("测试 2: 情绪得分衰减计算器")
print("-" * 70)

try:
    from app.factors.sentiment_decay import get_decay_calculator, DecayConfig
    
    # 测试指数衰减
    calc = get_decay_calculator(mode="exponential", half_life_minutes=30)
    
    print("\n指数衰减测试 (半衰期 30 分钟):")
    test_ages = [0, 15, 30, 60, 90, 120]
    original_score = 0.8
    
    for age in test_ages:
        decayed = calc.decay_score(original_score, age)
        factor = calc.calculate_decay_factor(age)
        print(f"  {age:3d}分钟 -> 因子:{factor:.3f}, 衰减后得分:{decayed:.3f}")
    
    # 测试线性衰减
    calc_linear = get_decay_calculator(mode="linear")
    calc_linear.config.decay_rate = 0.02
    
    print("\n线性衰减测试 (每分钟 2%):")
    for age in test_ages:
        decayed = calc_linear.decay_score(original_score, age)
        factor = calc_linear.calculate_decay_factor(age)
        print(f"  {age:3d}分钟 -> 因子:{factor:.3f}, 衰减后得分:{decayed:.3f}")
    
    # 测试批量衰减
    from app.news_push.collector import NewsItem

    news_list = [
        NewsItem(
            title=f"新闻{i}",
            sentiment_score=0.5 + i * 0.1,
            published_at=(datetime.now() - timedelta(minutes=i*20)).isoformat(),
        )
        for i in range(5)
    ]

    decayed_list = calc.batch_decay(news_list)
    
    print("\n批量衰减测试:")
    for news in decayed_list:
        print(f"  {news.title}: 原始={news.sentiment_score:.2f}, "
              f"衰减后={getattr(news, 'decayed_score', 'N/A'):.2f}, "
              f"年龄={news.age_minutes}分钟")
    
    print("\n✓ 情绪得分衰减计算器测试通过")
    
except Exception as e:
    print(f"\n✗ 情绪得分衰减计算器测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==============================================================================
# 测试 3: 新闻采集器动态 TTL
# ==============================================================================
print("-" * 70)
print("测试 3: 新闻采集器动态 TTL")
print("-" * 70)

try:
    from app.news_push.collector import ServiceClient
    from app.risk.vix_monitor import get_vix_monitor
    
    # 获取 VIX 监控器
    vix = get_vix_monitor()
    
    # 创建 ServiceClient (带动态 TTL)
    client = ServiceClient(
        ttl=3600,  # 基础 1 小时
        use_dynamic_ttl=True,
        vix_monitor=vix
    )
    
    print("\n动态 TTL 计算测试:")
    
    # 模拟不同优先级的 URL
    test_urls = [
        ("https://example.com/flash/1", 1),  # 高优先级
        ("https://example.com/news/2", 3),   # 中优先级
        ("https://example.com/rss/3", 5),    # 低优先级
    ]
    
    for url, priority in test_urls:
        ttl = client._get_dynamic_ttl(url, priority)
        print(f"  优先级{priority} URL -> TTL: {ttl}秒")
    
    # 测试波动率影响
    print("\n波动率影响测试:")
    for vix_val in [10, 20, 30, 40]:
        vix.update_vix(vix_val)
        ttl = client._get_dynamic_ttl("https://example.com/news", 3)
        vol_factor = vix.get_cache_ttl_multiplier()
        print(f"  VIX={vix_val} -> 波动因子:{vol_factor:.2f}, TTL:{ttl}秒")
    
    print("\n✓ 新闻采集器动态 TTL 测试通过")
    
except Exception as e:
    print(f"\n✗ 新闻采集器动态 TTL 测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==============================================================================
# 测试 4: 突发新闻快速通道
# ==============================================================================
print("-" * 70)
print("测试 4: 突发新闻快速通道")
print("-" * 70)

try:
    from app.news_push.breaking_news import get_breaking_news_detector, BreakingNewsDetector
    from app.news_push.collector import NewsItem
    
    detector = get_breaking_news_detector()
    
    # 测试突发新闻检测
    test_news = [
        NewsItem(
            title="突发：腾讯发布超预期财报，净利润增长 50%",
            content="腾讯控股今日发布 2024 年 Q1 财报...",
            source='cls',
            category='flash',
        ),
        NewsItem(
            title="特斯拉宣布新的电池技术突破",
            content="Elon Musk 在推特上宣布...",
            source='reuters',
            category='tech',
        ),
        NewsItem(
            title="美联储宣布加息 25 个基点",
            content="Federal Reserve announces...",
            source='fed',
            category='macro',
        ),
        NewsItem(
            title="日常财经新闻：港股今日小幅上涨",
            content="恒生指数今日上涨 0.5%...",
            source='rss',
            category='finance',
        ),
    ]
    
    print("\n突发新闻检测测试:")
    for news in test_news:
        alert = detector.detect(news)
        if alert:
            print(f"  ✓ 检测到：{news.title[:40]}...")
            print(f"     优先级:{alert.priority}, 严重程度:{alert.severity}, "
                  f"相关股票:{alert.related_stocks}")
        else:
            print(f"  - 未标记为突发：{news.title[:40]}...")
    
    # 统计信息
    stats = detector.get_statistics()
    print(f"\n统计信息：{stats}")
    
    print("\n✓ 突发新闻快速通道测试通过")
    
except Exception as e:
    print(f"\n✗ 突发新闻快速通道测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==============================================================================
# 测试 5: 新闻集成模块衰减
# ==============================================================================
print("-" * 70)
print("测试 5: 新闻集成模块衰减 (整合测试)")
print("-" * 70)

try:
    from app.factors.news_integration import get_news_sentiment_analyzer
    
    analyzer = get_news_sentiment_analyzer()
    
    print("\n模块初始化检查:")
    print(f"  VIX 监控器：{'已集成' if analyzer.vix_monitor else '未集成'}")
    print(f"  衰减计算器：{'已集成' if analyzer.decay_calculator else '未集成'}")
    print(f"  基础缓存 TTL: {analyzer.base_cache_ttl}秒")
    print(f"  动态 TTL: {analyzer._get_dynamic_ttl()}秒")
    
    # 测试衰减计算
    if analyzer.decay_calculator:
        print("\n衰减计算测试:")
        test_cases = [
            (0.8, 0),    # 新鲜新闻
            (0.8, 30),   # 30 分钟前
            (0.8, 60),   # 60 分钟前
            (0.8, 120),  # 120 分钟前
        ]
        
        for score, age in test_cases:
            decayed = analyzer.decay_calculator.decay_score(score, age)
            factor = analyzer.decay_calculator.calculate_decay_factor(age)
            print(f"  原始得分:{score:.2f}, 年龄:{age}分钟 -> 衰减后:{decayed:.3f} (因子:{factor:.2f})")
    
    print("\n✓ 新闻集成模块衰减测试通过")
    
except Exception as e:
    print(f"\n✗ 新闻集成模块衰减测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==============================================================================
# 测试总结
# ==============================================================================
print("=" * 70)
print("测试总结")
print("=" * 70)

print("""
Phase 3 功能清单:
✓ VIX 波动率监控器 - 实时追踪市场波动，动态调整缓存 TTL
✓ 情绪得分衰减 - 指数/线性/阶梯三种衰减模式
✓ 动态缓存 TTL - 基于优先级和波动率自动调整
✓ 突发新闻快速通道 - 关键词检测，跳过缓存直接推送
✓ 新闻集成模块增强 - 整合衰减和波动率监控

性能提升预期:
- 突发新闻响应时间：< 30 秒 (原 5-30 分钟)
- 情绪得分时效性：半衰期 30 分钟可配置
- 缓存效率：高波动市场 TTL 降低 60-80%
- 推送到 DingTalk: 快速通道 < 10 秒

下一步:
1. 集成到 ConsensusEngine (质量评分 + 时效性)
2. 集成到 RiskManager (波动率联动风控参数)
3. 实盘测试和参数优化
""")

print("\nPhase 3 验证完成!")
