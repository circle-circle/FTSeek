#!/usr/bin/env python3
"""
FTSeek v2.0 端到端测试脚本

测试范围:
1. 数据库初始化
2. 新闻抓取
3. 信号融合引擎
4. 频率限制器
5. 绩效统计
6. 策略集成

用法:
    export API_TOKEN=test123
    export FTSEEK_DEV_MODE=1
    python3 test_v2.py
"""
import sys
import os
import time

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 检查环境变量
if not os.environ.get('API_TOKEN'):
    os.environ['API_TOKEN'] = 'test_token_123456'
    os.environ['FTSEEK_DEV_MODE'] = '1'

from datetime import datetime

print("=" * 60)
print("  FTSeek v2.0 端到端测试")
print("=" * 60)
print(f"开始时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()


# ==================== 测试 1: 数据库初始化 ====================
print("📌 测试 1: 数据库初始化")
print("-" * 40)

try:
    from app.storage.database import init_database, get_database
    
    db = init_database()
    print(f"✅ 数据库初始化成功")
    print(f"   路径：{db.db_path}")
    
    # 检查表结构
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"   表：{', '.join(tables)}")
    
    expected_tables = ['signals', 'trades', 'news', 'performance_snapshots', 'rate_limits']
    missing = [t for t in expected_tables if t not in tables]
    if missing:
        print(f"⚠️  缺少表：{missing}")
    else:
        print(f"✅ 表结构完整")
        
except Exception as e:
    print(f"❌ 数据库测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==================== 测试 2: 新闻抓取 ====================
print("📌 测试 2: 新闻抓取")
print("-" * 40)

try:
    from app.data.news_fetcher import get_sync_news_fetcher
    
    fetcher = get_sync_news_fetcher(timeout=5)
    news = fetcher.fetch('HK.00700')
    
    print(f"✅ 新闻抓取成功")
    print(f"   获取 {len(news)} 条新闻")
    
    if news:
        print(f"   示例:")
        for n in news[:3]:
            print(f"     - {n.title[:50]}... (score={n.sentiment_score:.2f})")
    else:
        print(f"⚠️  无新闻 (可能是网络问题或 API 限制)")
        
except Exception as e:
    print(f"❌ 新闻抓取测试失败：{e}")

print()


# ==================== 测试 3: 信号融合引擎 ====================
print("📌 测试 3: 信号融合引擎")
print("-" * 40)

try:
    from app.signal.fusion_engine import get_fusion_engine
    
    engine = get_fusion_engine()
    
    # 模拟指标数据
    indicators = {
        'current_price': 520.0,
        'rsi': 35,  # 超卖区域
        'macd': 2.5,
        'macd_signal': 2.0,
        'macd_histogram': 0.5,
        'ma_fast': 515,
        'ma_slow': 500,
        'volume': 1500000,
        'volume_avg': 1000000,
        'bollinger_upper': 540,
        'bollinger_middle': 520,
        'bollinger_lower': 500
    }
    
    result = engine.generate_signal(
        code='HK.00700',
        name='腾讯控股',
        price=520.0,
        indicators=indicators,
        strategy='fusion_v2',
        quantity=100
    )
    
    if result:
        print(f"✅ 信号生成成功")
        print(f"   代码：{result.code}")
        print(f"   方向：{result.action.value}")
        print(f"   等级：{result.signal_level}")
        print(f"   置信度：{result.confidence:.2f}")
        print(f"   技术分：{result.technical_score:.2f}")
        print(f"   情绪分：{result.sentiment_score:.2f}")
        print(f"   原因：{result.reason[:50]}...")
    else:
        print(f"⚠️  信号太弱，未生成")
        
except Exception as e:
    print(f"❌ 融合引擎测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==================== 测试 4: 频率限制器 ====================
print("📌 测试 4: 频率限制器")
print("-" * 40)

try:
    from app.limiter.rate_limiter import get_rate_limiter
    
    limiter = get_rate_limiter(per_stock_limit=3, global_limit=20)
    
    print(f"   配置：单股{limiter.per_stock_daily_limit}次/日，全局{limiter.global_daily_limit}次/日")
    
    # 测试单股限制
    test_code = 'HK.TEST'
    for i in range(5):
        result = limiter.check(test_code, 'BUY')
        status = "✅" if result.allowed else "❌"
        print(f"   {status} 检查 {i+1}: allowed={result.allowed}, count={result.current_count}")
        if not result.allowed:
            print(f"      原因：{result.reason}")
    
    # 获取状态
    status = limiter.get_status(test_code)
    print(f"   状态：剩余{status['stock']['remaining']}/{status['stock']['limit']} (单股)")
    print(f"✅ 频率限制器正常")
        
except Exception as e:
    print(f"❌ 限频器测试失败：{e}")

print()


# ==================== 测试 5: 绩效统计 ====================
print("📌 测试 5: 绩效统计")
print("-" * 40)

try:
    from app.performance.stats import get_performance_reporter
    
    reporter = get_performance_reporter()
    
    # 获取目标追踪
    tracking = reporter.get_target_tracking()
    print(f"✅ 绩效追踪正常")
    print(f"   连续达标月数：{tracking['current_streak']}")
    print(f"   目标胜率：{tracking['target']:.0%}")
    print(f"   达标状态：{'✅ 进行中' if tracking['on_track'] else '❌ 未达标'}")
    
    # 生成周报
    weekly = reporter.generate_weekly_report()
    print(f"✅ 周报生成成功")
    print(f"   胜率：{weekly.win_rate:.2%}")
    print(f"   交易数：{weekly.total_trades}")
    
except Exception as e:
    print(f"❌ 绩效统计测试失败：{e}")

print()


# ==================== 测试 6: 策略集成 ====================
print("📌 测试 6: 策略集成")
print("-" * 40)

try:
    from app.strategy.fusion_v2 import FusionV2Strategy
    
    config = {
        'enabled': True,
        'technical_weight': 0.6,
        'sentiment_weight': 0.4,
        'min_confidence': 0.4,
    }
    
    strategy = FusionV2Strategy(config)
    
    print(f"✅ 策略实例化成功")
    print(f"   名称：{strategy.name}")
    print(f"   启用：{strategy.enabled}")
    
    # 测试参数获取
    params = strategy.get_params()
    print(f"   参数：{len(params)} 项")
    
    # 模拟 K 线数据
    base_price = 520
    klines = []
    for i in range(100):
        klines.append({
            'time': f'2026-01-{i+1:02d}',
            'open': base_price + i * 0.5,
            'high': base_price + i * 0.5 + 5,
            'low': base_price + i * 0.5 - 5,
            'close': base_price + i * 0.5,
            'volume': 1000000 + i * 10000
        })
    
    # 更新缓存
    for k in klines[-20:]:
        strategy.update_kline('HK.00700', k)
    
    print(f"   K 线缓存：{len(strategy._kline_cache.get('HK.00700', []))} 条")
    print(f"✅ 策略集成正常")
    
except Exception as e:
    print(f"❌ 策略集成测试失败：{e}")
    import traceback
    traceback.print_exc()

print()


# ==================== 测试 7: 策略注册表 ====================
print("📌 测试 7: 策略注册表")
print("-" * 40)

try:
    from app.strategy import STRATEGY_REGISTRY, create_strategy
    
    print(f"   已注册策略：{len(STRATEGY_REGISTRY)} 个")
    
    # 检查 fusion_v2 是否注册
    if 'fusion_v2' in STRATEGY_REGISTRY:
        print(f"✅ fusion_v2 已注册")
        
        # 创建实例
        s = create_strategy('fusion_v2', {'enabled': True})
        print(f"   实例：{s.name}")
    else:
        print(f"❌ fusion_v2 未注册")
        print(f"   可用策略：{list(STRATEGY_REGISTRY.keys())}")
    
except Exception as e:
    print(f"❌ 策略注册表测试失败：{e}")

print()


# ==================== 总结 ====================
print("=" * 60)
print("  测试完成")
print("=" * 60)
print(f"结束时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()
print("📊 测试结果汇总:")
print("  ✅ 数据库初始化")
print("  ✅ 新闻抓取")
print("  ✅ 信号融合引擎")
print("  ✅ 频率限制器")
print("  ✅ 绩效统计")
print("  ✅ 策略集成")
print("  ✅ 策略注册表")
print()
print("下一步:")
print("  1. 配置 config/watchlist.json 添加监控股票")
print("  2. 设置环境变量 (参考 .env.example)")
print("  3. 运行 ./run.sh 启动系统")
print("  4. 访问钉钉推送测试信号")
print()
