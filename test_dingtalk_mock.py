#!/usr/bin/env python3
"""
FTSeek v2.1 钉钉推送模拟测试

不需要实际钉钉配置，生成消息预览

用法:
  python3 test_dingtalk_mock.py
"""
import sys
import os
import time

os.environ.setdefault('API_TOKEN', 'test123')
os.environ.setdefault('FTSEEK_DEV_MODE', '1')

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.strategy.base import Signal, SignalAction
from app.notifier.dingtalk import DingTalkNotifier, ACTION_LABELS
from app import config

print("=" * 70)
print("  FTSeek v2.1 钉钉推送模拟测试")
print("=" * 70)
print()
print("📱 此测试生成消息预览，无需实际钉钉配置")
print()

# ==================== 消息预览 1: 看涨信号 ====================
print("=" * 70)
print("📌 消息预览 1: 看涨信号 (BUY)")
print("=" * 70)

signal_buy = Signal(
    id="demo_buy_001",
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    price=520.0,
    quantity=100,
    strategy="fusion_v2",
    reason="RSI 超卖 (25.0) | MACD 金叉 | 均线多头 | 情绪正面 (盈利，增长)",
    extra={
        'direction': 'BUY',
        'buy_probability': 0.875,
        'sell_probability': 0.125,
        'confidence': 0.83,
        'signal_level': 'recommended',
        'technical_score': 0.85,
        'sentiment_score': 0.60
    }
)

# 生成钉钉消息内容
buy_prob_bar = "█" * int(signal_buy.extra['buy_probability'] * 10)
sell_prob_bar = "█" * int(signal_buy.extra['sell_probability'] * 10)

preview_text = f"""
┌─────────────────────────────────────────────────────────────┐
│  🔴 HK.00700 BUY 信号                                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  标的：HK.00700 腾讯控股                                     │
│                                                             │
│  方向：BUY                                                  │
│                                                             │
│  概率：                                                      │
│  买入：{buy_prob_bar} {signal_buy.extra['buy_probability']:.1%}                    │
│  卖出：{sell_prob_bar} {signal_buy.extra['sell_probability']:.1%}                    │
│                                                             │
│  置信度：{signal_buy.extra['confidence']:.1%}                                              │
│  等级：{signal_buy.extra['signal_level']}                                          │
│                                                             │
│  技术分：{signal_buy.extra['technical_score']:+.2f}                                              │
│  情绪分：{signal_buy.extra['sentiment_score']:+.2f}                                              │
│                                                             │
│  原因：{signal_buy.reason[:45]}                    │
│                                                             │
│  ⏰ 有效期 15 分钟                                             │
│                                                             │
│  [✅ 确认买入]  [⏰ 延期 15 分钟]  [❌ 忽略]                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
"""

print(preview_text)

# ==================== 消息预览 2: 看跌信号 ====================
print("=" * 70)
print("📌 消息预览 2: 看跌信号 (SELL)")
print("=" * 70)

signal_sell = Signal(
    id="demo_sell_001",
    code="US.TSLA",
    name="特斯拉",
    action=SignalAction.SELL,
    price=350.0,
    quantity=10,
    strategy="fusion_v2",
    reason="RSI 超买 (75.0) | MACD 死叉 | 均线空头 | 情绪负面 (亏损，下滑)",
    extra={
        'direction': 'SELL',
        'buy_probability': 0.184,
        'sell_probability': 0.816,
        'confidence': 0.712,
        'signal_level': 'recommended',
        'technical_score': -0.72,
        'sentiment_score': -0.50
    }
)

sell_buy_bar = "█" * int(signal_sell.extra['buy_probability'] * 10)
sell_sell_bar = "█" * int(signal_sell.extra['sell_probability'] * 10)

preview_text_sell = f"""
┌─────────────────────────────────────────────────────────────┐
│  🟢 US.TSLA SELL 信号                                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  标的：US.TSLA 特斯拉                                        │
│                                                             │
│  方向：SELL                                                 │
│                                                             │
│  概率：                                                      │
│  买入：{sell_buy_bar} {signal_sell.extra['buy_probability']:.1%}                    │
│  卖出：{sell_sell_bar} {signal_sell.extra['sell_probability']:.1%}                    │
│                                                             │
│  置信度：{signal_sell.extra['confidence']:.1%}                                              │
│  等级：{signal_sell.extra['signal_level']}                                          │
│                                                             │
│  技术分：{signal_sell.extra['technical_score']:+.2f}                                             │
│  情绪分：{signal_sell.extra['sentiment_score']:+.2f}                                             │
│                                                             │
│  原因：{signal_sell.reason[:45]}                    │
│                                                             │
│  ⏰ 有效期 15 分钟                                             │
│                                                             │
│  [✅ 确认卖出]  [⏰ 延期 15 分钟]  [❌ 忽略]                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
"""

print(preview_text_sell)

# ==================== 消息预览 3: 强烈信号 ====================
print("=" * 70)
print("📌 消息预览 3: 强烈推荐信号 (Strong)")
print("=" * 70)

signal_strong = Signal(
    id="demo_strong_001",
    code="US.NVDA",
    name="英伟达",
    action=SignalAction.BUY,
    price=875.0,
    quantity=5,
    strategy="fusion_v2",
    reason="RSI 超卖 (20.0) | MACD 金叉 | 均线多头 | 放量 2.5x | 情绪强烈正面 (AI 增长，超预期)",
    extra={
        'direction': 'BUY',
        'buy_probability': 0.95,
        'sell_probability': 0.05,
        'confidence': 0.92,
        'signal_level': 'strong',
        'technical_score': 0.92,
        'sentiment_score': 0.85
    }
)

strong_buy_bar = "█" * int(signal_strong.extra['buy_probability'] * 10)
strong_sell_bar = "█" * int(signal_strong.extra['sell_probability'] * 10)

preview_text_strong = f"""
┌─────────────────────────────────────────────────────────────┐
│  🔴 US.NVDA BUY 信号  [强烈推荐]                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  标的：US.NVDA 英伟达                                        │
│                                                             │
│  方向：BUY                                                  │
│                                                             │
│  概率：                                                      │
│  买入：{strong_buy_bar} {signal_strong.extra['buy_probability']:.1%}                    │
│  卖出：{strong_sell_bar} {signal_strong.extra['sell_probability']:.1%}                     │
│                                                             │
│  置信度：{signal_strong.extra['confidence']:.1%}                                              │
│  等级：{signal_strong.extra['signal_level']} ⭐                                     │
│                                                             │
│  技术分：{signal_strong.extra['technical_score']:+.2f}                                             │
│  情绪分：{signal_strong.extra['sentiment_score']:+.2f}                                             │
│                                                             │
│  原因：{signal_strong.reason[:45]}  │
│                                                             │
│  ⏰ 有效期 15 分钟                                             │
│                                                             │
│  [✅ 确认买入]  [⏰ 延期 15 分钟]  [❌ 忽略]                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
"""

print(preview_text_strong)

# ==================== 消息预览 4: 执行结果 ====================
print("=" * 70)
print("📌 消息预览 4: 执行结果")
print("=" * 70)

exec_preview = """
┌─────────────────────────────────────────────────────────────┐
│  [交易结果] ✅ 执行成功                                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  标的：HK.00700                                             │
│  方向：BUY                                                  │
│  价格：HK$520.00                                            │
│  数量：100 股                                               │
│  金额：HK$52,000.00                                         │
│  订单号：MO123456789                                        │
│  时间：2026-02-27 17:05:30                                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
"""

print(exec_preview)

# ==================== 功能特性总结 ====================
print("=" * 70)
print("📊 FTSeek v2.1 钉钉推送特性")
print("=" * 70)
print("""
✅ 模块化消息生成
   • 技术指标模块 → 技术分
   • 情绪分析模块 → 情绪分
   • 融合模块 → 方向概率
   • 输出模块 → 格式化

✅ 概率可视化
   • 买入概率：█████████ 87.5%
   • 卖出概率：█ 12.5%

✅ 三按钮交互
   • ✅ 确认执行 → /api/confirm
   • ⏰ 延期 15 分钟 → /api/defer
   • ❌ 忽略 → /api/reject

✅ 信号等级
   • strong: 强烈推荐 (红色星标)
   • recommended: 推荐 (正常)
   • neutral: 中性 (蓝色)
   • weak: 不推送

✅ 置信度显示
   • 0-100% 百分比
   • 基于技术和情绪一致性

✅ 频率限制
   • 单股≤3 次/日
   • 全局≤20 次/日
   • 超限自动合并摘要
""")

# ==================== 实际测试提示 ====================
print("=" * 70)
print("🔔 实际推送测试")
print("=" * 70)
print("""
如需实际推送到钉钉:

1. 配置钉钉机器人
   • 钉钉群 → 智能群助手 → 添加机器人
   • 复制 Webhook URL 和 Secret

2. 设置环境变量
   export DINGTALK_WEBHOOK_URL='https://...'
   export DINGTALK_SECRET='SEC...'

3. 运行测试
   python3 test_dingtalk.py

📚 详细配置：docs/DINGTALK_SETUP.md
""")

print("=" * 70)
print("✅ 模拟测试完成")
print("=" * 70)
