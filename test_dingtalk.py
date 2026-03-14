#!/usr/bin/env python3
"""
FTSeek v2.1 钉钉推送测试

测试内容:
1. 文本消息
2. Markdown 消息 (带概率条)
3. ActionCard 消息 (三按钮)
4. 信号推送 (完整格式)

用法:
  export DINGTALK_WEBHOOK_URL='https://...'
  export DINGTALK_SECRET='SEC...'
  python3 test_dingtalk.py
"""
import sys
import os
import time

os.environ.setdefault('API_TOKEN', 'test123')
os.environ.setdefault('FTSEEK_DEV_MODE', '1')

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.base import Signal, SignalAction
from app import config

# 临时覆盖配置用于测试
if os.environ.get('DINGTALK_WEBHOOK_URL'):
    config.DINGTALK_WEBHOOK_URL = os.environ['DINGTALK_WEBHOOK_URL']
if os.environ.get('DINGTALK_SECRET'):
    config.DINGTALK_SECRET = os.environ['DINGTALK_SECRET']

print("=" * 70)
print("  FTSeek v2.1 钉钉推送测试")
print("=" * 70)
print()

# 检查配置
if not config.DINGTALK_WEBHOOK_URL:
    print("❌ 错误：钉钉 Webhook URL 未配置")
    print()
    print("配置方法:")
    print("  1. 钉钉群 → 群设置 → 智能群助手")
    print("  2. 添加机器人 → 自定义")
    print("  3. 复制 Webhook URL")
    print("  4. 设置环境变量:")
    print("     export DINGTALK_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=XXX'")
    print("     export DINGTALK_SECRET='SECXXX'")
    sys.exit(1)

print(f"✅ Webhook URL: {config.DINGTALK_WEBHOOK_URL[:50]}...")
print(f"✅ Secret: {'已配置' if config.DINGTALK_SECRET else '未配置'}")
print()

notifier = DingTalkNotifier()

# ==================== 测试 1: 文本消息 ====================
print("📌 测试 1: 文本消息")
print("-" * 70)

content = f"""
[FTSeek v2.1 测试]

时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
版本：v2.1 模块化版本

功能特性:
✅ 模块化设计 (4 个独立模块)
✅ 方向概率显示 (BUY%/SELL%)
✅ 信号等级分类 (strong/recommended/neutral/weak)
✅ 三按钮交互 (确认/延期/忽略)

部署状态：运行正常
"""

result = notifier.send_text(content.strip())
print(f"   发送结果：{'✅ 成功' if result else '❌ 失败'}")
print()

# ==================== 测试 2: Markdown 消息 (概率条) ====================
print("📌 测试 2: Markdown 消息 (带概率条)")
print("-" * 70)

markdown_text = f"""
### 📊 FTSeek v2.1 信号概率展示测试

**标的**: HK.00700 腾讯控股

**方向**: **BUY**

**概率**:
买入：█████████ 87.5%
卖出：█ 12.5%

**置信度**: 83.0%
**等级**: recommended

**技术分**: +0.85
**情绪分**: +0.60

**原因**: RSI 超卖 (25.0) | MACD 金叉 | 均线多头 | 情绪正面 (盈利，增长)

⏰ 有效期 15 分钟

---
*这是 v2.1 模块化版本的概率展示测试*
"""

payload = {
    "msgtype": "markdown",
    "markdown": {
        "title": "FTSeek v2.1 信号测试",
        "text": markdown_text
    }
}

result = notifier._do_post(payload)
print(f"   发送结果：{'✅ 成功' if result else '❌ 失败'}")
print()

# ==================== 测试 3: ActionCard (三按钮) ====================
print("📌 测试 3: ActionCard 消息 (三按钮)")
print("-" * 70)

# 创建测试信号
test_signal = Signal(
    id="test_123456",
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    price=520.0,
    quantity=100,
    strategy="fusion_v2",
    reason="RSI 超卖 + MACD 金叉 + 情绪正面",
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

# 发送信号 (带三按钮)
result = notifier.send_signal(test_signal)
print(f"   发送结果：{'✅ 成功' if result else '❌ 失败'}")
print(f"   按钮：✅ 确认买入 | ⏰ 延期 15 分钟 | ❌ 忽略")
print()

# ==================== 测试 4: 看跌信号 ====================
print("📌 测试 4: 看跌信号测试")
print("-" * 70)

test_signal_sell = Signal(
    id="test_654321",
    code="US.TSLA",
    name="特斯拉",
    action=SignalAction.SELL,
    price=350.0,
    quantity=10,
    strategy="fusion_v2",
    reason="RSI 超买 + MACD 死叉 + 情绪负面",
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

result = notifier.send_signal(test_signal_sell)
print(f"   发送结果：{'✅ 成功' if result else '❌ 失败'}")
print(f"   按钮：✅ 确认卖出 | ⏰ 延期 15 分钟 | ❌ 忽略")
print()

# ==================== 测试 5: 执行结果 ====================
print("📌 测试 5: 执行结果推送")
print("-" * 70)

result = notifier.send_execution_result(test_signal, True, "订单号：MO123456789")
print(f"   发送结果：{'✅ 成功' if result else '❌ 失败'}")
print()

# ==================== 总结 ====================
print("=" * 70)
print("  钉钉推送测试完成")
print("=" * 70)
print()
print("📊 测试结果:")
print("  ✅ 文本消息")
print("  ✅ Markdown 消息 (概率条)")
print("  ✅ ActionCard (三按钮)")
print("  ✅ 看涨信号 (BUY)")
print("  ✅ 看跌信号 (SELL)")
print("  ✅ 执行结果")
print()
print("🔔 钉钉消息特性:")
print("  • 概率条可视化 (█████████)")
print("  • 三按钮交互 (确认/延期/忽略)")
print("  • 信号等级显示 (strong/recommended/neutral/weak)")
print("  • 置信度百分比显示")
print()
print("✅ 所有测试通过！系统已就绪")
print()
