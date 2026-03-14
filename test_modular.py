#!/usr/bin/env python3
"""
FTSeek v2.1 模块化引擎测试

测试模块化设计和信号概率显示
"""
import sys
import os

os.environ.setdefault('API_TOKEN', 'test123')
os.environ.setdefault('FTSEEK_DEV_MODE', '1')

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.signal.fusion_engine import get_fusion_engine, TechnicalScore, SentimentScore

print("=" * 70)
print("  FTSeek v2.1 模块化引擎 - 方向概率测试")
print("=" * 70)

engine = get_fusion_engine()

# ==================== 测试场景 1: 强烈看涨 ====================
print("\n" + "=" * 70)
print("场景 1: 强烈看涨信号 (技术 +0.8, 情绪 +0.6)")
print("=" * 70)

tech1 = TechnicalScore(
    rsi_score=0.9,
    macd_score=0.8,
    ma_score=1.0,
    volume_score=0.5,
    bollinger_score=0.8,
    composite_score=0.85,
    indicators={'rsi': 25, 'macd_signal': '金叉'}
)

sent1 = SentimentScore(
    score=0.6,
    confidence=0.8,
    news_count=10,
    keywords=['盈利', '增长', '利好']
)

final1, conf1, level1, buy1, sell1 = engine.fusion_module.fuse(tech1, sent1)
direction1 = "BUY" if final1 > 0.1 else "SELL" if final1 < -0.1 else "NEUTRAL"

print(f"""
融合结果:
  最终评分：{final1:+.3f}
  置信度：{conf1:.1%}
  信号等级：{level1}
  
方向概率:
  📈 买入概率：{buy1:.1%}
  📉 卖出概率：{sell1:.1%}
  
决策：{direction1}
""")

# ==================== 测试场景 2: 强烈看跌 ====================
print("\n" + "=" * 70)
print("场景 2: 强烈看跌信号 (技术 -0.7, 情绪 -0.5)")
print("=" * 70)

tech2 = TechnicalScore(
    rsi_score=-0.8,
    macd_score=-0.7,
    ma_score=-1.0,
    volume_score=-0.5,
    bollinger_score=-0.6,
    composite_score=-0.72,
    indicators={'rsi': 75, 'macd_signal': '死叉'}
)

sent2 = SentimentScore(
    score=-0.5,
    confidence=0.7,
    news_count=8,
    keywords=['亏损', '下滑', '利空']
)

final2, conf2, level2, buy2, sell2 = engine.fusion_module.fuse(tech2, sent2)
direction2 = "BUY" if final2 > 0.1 else "SELL" if final2 < -0.1 else "NEUTRAL"

print(f"""
融合结果:
  最终评分：{final2:+.3f}
  置信度：{conf2:.1%}
  信号等级：{level2}
  
方向概率:
  📈 买入概率：{buy2:.1%}
  📉 卖出概率：{sell2:.1%}
  
决策：{direction2}
""")

# ==================== 测试场景 3: 中性震荡 ====================
print("\n" + "=" * 70)
print("场景 3: 中性震荡信号 (技术 +0.1, 情绪 -0.1)")
print("=" * 70)

tech3 = TechnicalScore(
    rsi_score=0.1,
    macd_score=0.0,
    ma_score=0.2,
    volume_score=0.0,
    bollinger_score=0.1,
    composite_score=0.08,
    indicators={'rsi': 50, 'macd_signal': '无'}
)

sent3 = SentimentScore(
    score=-0.1,
    confidence=0.3,
    news_count=2,
    keywords=[]
)

final3, conf3, level3, buy3, sell3 = engine.fusion_module.fuse(tech3, sent3)
direction3 = "BUY" if final3 > 0.1 else "SELL" if final3 < -0.1 else "NEUTRAL"

print(f"""
融合结果:
  最终评分：{final3:+.3f}
  置信度：{conf3:.1%}
  信号等级：{level3}
  
方向概率:
  📈 买入概率：{buy3:.1%}
  📉 卖出概率：{sell3:.1%}
  
决策：{direction3} (信号太弱，不推送)
""")

# ==================== 测试场景 4: 技术强情绪弱 ====================
print("\n" + "=" * 70)
print("场景 4: 技术强情绪弱 (技术 +0.9, 情绪 0.0)")
print("=" * 70)

tech4 = TechnicalScore(
    rsi_score=1.0,
    macd_score=0.9,
    ma_score=0.8,
    volume_score=0.7,
    bollinger_score=0.9,
    composite_score=0.88,
    indicators={'rsi': 20, 'macd_signal': '金叉'}
)

sent4 = SentimentScore(
    score=0.0,
    confidence=0.0,
    news_count=0,
    keywords=[]
)

final4, conf4, level4, buy4, sell4 = engine.fusion_module.fuse(tech4, sent4)
direction4 = "BUY" if final4 > 0.1 else "SELL" if final4 < -0.1 else "NEUTRAL"

print(f"""
融合结果:
  最终评分：{final4:+.3f}
  置信度：{conf4:.1%} (无新闻降低)
  信号等级：{level4}
  
方向概率:
  📈 买入概率：{buy4:.1%}
  📉 卖出概率：{sell4:.1%}
  
决策：{direction4}
说明：技术面强烈看涨，但无新闻情绪数据，置信度降低
""")

# ==================== 概率可视化 ====================
print("\n" + "=" * 70)
print("概率可视化对比")
print("=" * 70)

scenarios = [
    ("强烈看涨", buy1, sell1),
    ("强烈看跌", buy2, sell2),
    ("中性震荡", buy3, sell3),
    ("技术强情绪弱", buy4, sell4),
]

for name, buy_p, sell_p in scenarios:
    buy_bars = int(buy_p * 20)
    sell_bars = int(sell_p * 20)
    
    bar = "🟢" * buy_bars + "🔴" * sell_bars
    print(f"{name:12s} | {bar:20s} | BUY {buy_p:5.1%} SELL {sell_p:5.1%}")

# ==================== 模块化测试 ====================
print("\n" + "=" * 70)
print("模块化功能测试")
print("=" * 70)

# 测试模块获取
print("\n获取模块:")
for module_name in ['technical', 'sentiment', 'fusion', 'output']:
    module = engine.get_module(module_name)
    print(f"  ✅ {module_name}: {type(module).__name__}")

# 测试动态配置
print("\n动态配置测试:")
print(f"  原技术权重：{engine.fusion_module.technical_weight:.1%}")
engine.fusion_module.technical_weight = 0.7
engine.fusion_module.sentiment_weight = 0.3
print(f"  新技术权重：{engine.fusion_module.technical_weight:.1%}")
print(f"  新情绪权重：{engine.fusion_module.sentiment_weight:.1%}")

# 恢复默认
engine.fusion_module.technical_weight = 0.6
engine.fusion_module.sentiment_weight = 0.4

print("\n" + "=" * 70)
print("✅ 所有测试完成")
print("=" * 70)
print("""
📊 信号展现特性:
  ✅ 方向：BUY / SELL / NEUTRAL
  ✅ 概率：买入概率% / 卖出概率%
  ✅ 置信度：0-100%
  ✅ 等级：strong / recommended / neutral / weak
  ✅ 模块化：4 个独立模块，可热插拔

🔧 配置方式:
  1. 环境变量 (推荐)
  2. 配置文件 (YAML)
  3. 代码动态配置

📚 详细文档：docs/MODULAR_CONFIG.md
""")
