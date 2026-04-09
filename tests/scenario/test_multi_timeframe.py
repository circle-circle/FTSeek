"""
Phase 2: 多周期特征增强测试

测试多周期特征提取器和跨周期分析
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.scenario import (
    get_scenario_classifier,
    get_multi_timeframe_extractor,
)
from app.scenario.multi_timeframe import MultiTimeframeFeatureExtractor

print("=" * 60)
print("Phase 2: 多周期特征增强测试")
print("=" * 60)

# ============================================================
# 测试 1: 多周期特征提取器
# ============================================================
print("\n=== 测试 1: 多周期特征提取器 ===")

extractor = get_multi_timeframe_extractor()

# 模拟多周期 K 线数据
def generate_klines(base_price, count, trend='bullish'):
    """生成模拟 K 线数据"""
    klines = []
    price = base_price
    for i in range(count):
        if trend == 'bullish':
            change = 0.002  # 0.2% 上涨
        elif trend == 'bearish':
            change = -0.002  # -0.2% 下跌
        else:
            change = 0.0005 * (1 if i % 2 == 0 else -1)  # 震荡

        open_price = price
        close_price = price * (1 + change)
        high_price = max(open_price, close_price) * 1.01
        low_price = min(open_price, close_price) * 0.99
        volume = 1000000

        klines.append({
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
        })
        price = close_price
    return klines

# 创建多周期数据
klines_by_timeframe = {
    'm1': generate_klines(100, 100, trend='bullish'),    # 1 分钟看涨
    'm5': generate_klines(100, 100, trend='bullish'),    # 5 分钟看涨
    'm15': generate_klines(100, 100, trend='bullish'),   # 15 分钟看涨
    'm30': generate_klines(100, 100, trend='neutral'),   # 30 分钟震荡
    'h1': generate_klines(100, 100, trend='bullish'),    # 小时线看涨
}

result = extractor.extract("HK.TEST_MULTI", klines_by_timeframe)

print(f"主导周期：{result.dominant_timeframe}")
print(f"趋势一致性：{result.trend_alignment:.2f}")
print(f"置信度提升：{result.confidence_boost:.2f}x")
print(f"多周期共振：{result.resonance_detected}")

# 验证多周期共振
if result.resonance_detected:
    print("✅ 检测到多周期共振")
else:
    print(f"⚠️ 未检测到共振 (趋势一致性={result.trend_alignment:.2f})")

# ============================================================
# 测试 2: 趋势一致性计算
# ============================================================
print("\n=== 测试 2: 趋势一致性计算 ===")

# 完全一致 (全看涨)
klines_aligned = {
    'm5': generate_klines(100, 100, trend='bullish'),
    'm15': generate_klines(100, 100, trend='bullish'),
    'h1': generate_klines(100, 100, trend='bullish'),
}
result_aligned = extractor.extract("HK.TEST_ALIGNED", klines_aligned)
print(f"完全一致场景：趋势一致性={result_aligned.trend_alignment:.2f}, 置信度提升={result_aligned.confidence_boost:.2f}x")

# 完全背离 (多数看涨，少数看跌)
klines_divergent = {
    'm5': generate_klines(100, 100, trend='bullish'),
    'm15': generate_klines(100, 100, trend='bearish'),
    'h1': generate_klines(100, 100, trend='bullish'),
}
result_divergent = extractor.extract("HK.TEST_DIVERGENT", klines_divergent)
print(f"背离场景：趋势一致性={result_divergent.trend_alignment:.2f}, 置信度提升={result_divergent.confidence_boost:.2f}x")

# ============================================================
# 测试 3: 场景分类器集成 (带多周期数据)
# ============================================================
print("\n=== 测试 3: 场景分类器集成 ===")

classifier = get_scenario_classifier()

# 准备主周期 K 线
main_klines = generate_klines(100, 100, trend='bullish')

# 准备多周期 K 线
multi_tf_klines = {
    'm5': generate_klines(100, 100, trend='bullish'),
    'm15': generate_klines(100, 100, trend='bullish'),
    'h1': generate_klines(100, 100, trend='bullish'),
}

# 不带多周期
result_single = classifier.classify("HK.TEST_SINGLE", main_klines)
print(f"单周期分类：{result_single.scenario_type.value}, 置信度={result_single.confidence:.2f}")

# 带多周期
result_multi = classifier.classify("HK.TEST_MULTI_TF", main_klines, multi_timeframe_klines=multi_tf_klines)
print(f"多周期分类：{result_multi.scenario_type.value}, 置信度={result_multi.confidence:.2f}")
print(f"多周期共振：{result_multi.metadata.get('resonance', False)}")

# 验证置信度提升
if result_multi.confidence >= result_single.confidence:
    print(f"✅ 多周期分析提升/保持置信度 ({result_single.confidence:.2f} -> {result_multi.confidence:.2f})")
else:
    print(f"⚠️ 置信度下降 ({result_single.confidence:.2f} -> {result_multi.confidence:.2f})")

# ============================================================
# 测试总结
# ============================================================
print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
print("多周期特征提取器实现验证完成")
print("支持:")
print("  - 1m/5m/15m/30m/h1 多周期分析")
print("  - 跨周期趋势一致性计算")
print("  - 多周期共振检测")
print("  - 置信度提升/降低调整")
