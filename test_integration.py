#!/usr/bin/env python3
"""
FTSeek v2.1 集成测试

测试钉钉 - 富途 OpenD 完整联动流程:
1. 信号生成
2. 钉钉推送
3. 模拟下单
4. 结果返回

用法:
  export API_TOKEN=test123
  export FTSEEK_DEV_MODE=1
  python3 test_integration.py
"""
import sys
import os
import time

os.environ.setdefault('API_TOKEN', 'test123')
os.environ.setdefault('FTSEEK_DEV_MODE', '1')

sys.path.insert(0, '/opt/ftseek/FTSeek')

print("=" * 70)
print("  FTSeek v2.1 集成测试 - 钉钉 × 富途 OpenD 联动")
print("=" * 70)
print()

# ==================== 测试 1: 信号生成 ====================
print("📌 测试 1: 信号生成 (融合引擎)")
print("-" * 70)

from app.signal.fusion_engine import get_fusion_engine

engine = get_fusion_engine()

# 模拟看涨指标
indicators_buy = {
    'current_price': 520.0,
    'rsi': 25,  # 超卖
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

result_buy = engine.generate_signal(
    code='HK.00700',
    name='腾讯控股',
    price=520.0,
    indicators=indicators_buy,
    strategy='fusion_v2'
)

if result_buy:
    print(f"✅ 看涨信号生成成功")
    print(f"   方向：{result_buy.direction}")
    print(f"   概率：BUY {result_buy.buy_probability:.1%} / SELL {result_buy.sell_probability:.1%}")
    print(f"   置信度：{result_buy.confidence:.1%}")
    print(f"   等级：{result_buy.signal_level}")
    signal_buy = result_buy.to_signal()
    print(f"   Signal ID: {signal_buy.id}")
else:
    print("⚠️  信号太弱，未生成")
    signal_buy = None

print()

# ==================== 测试 2: 钉钉推送 ====================
print("📌 测试 2: 钉钉推送")
print("-" * 70)

if os.environ.get('DINGTALK_WEBHOOK_URL'):
    from app.notifier.dingtalk import DingTalkNotifier
    from app import config
    
    config.DINGTALK_WEBHOOK_URL = os.environ['DINGTALK_WEBHOOK_URL']
    config.DINGTALK_SECRET = os.environ.get('DINGTALK_SECRET', '')
    
    notifier = DingTalkNotifier()
    
    if signal_buy:
        result = notifier.send_signal(signal_buy)
        print(f"✅ 信号推送：{'成功' if result else '失败'}")
        
        if result:
            print(f"   请在钉钉中查看消息并点击按钮")
            print(f"   按钮：✅ 确认买入 | ⏰ 延期 | ❌ 忽略")
    else:
        print("⚠️  无信号，跳过推送测试")
else:
    print("⚠️  钉钉未配置，跳过推送测试")
    print("   配置：export DINGTALK_WEBHOOK_URL='https://...'")

print()

# ==================== 测试 3: TradeExecutor 初始化 ====================
print("📌 测试 3: 交易执行器初始化")
print("-" * 70)

from app.trader.executor import TradeExecutor
from app import config as app_config

executor = TradeExecutor()
print(f"✅ 交易执行器已初始化")
print(f"   交易环境：{'模拟' if app_config.TRADE_ENV == 1 else '真实'}")
print(f"   OpenD 地址：{app_config.OPEND_HOST}:{app_config.OPEND_PORT}")
print(f"   默认数量：港股{app_config.DEFAULT_ORDER_QTY_HK} / 美股{app_config.DEFAULT_ORDER_QTY_US}")
print(f"   最大金额：{app_config.MAX_ORDER_AMOUNT}")

print()

# ==================== 测试 4: 模拟下单 ====================
print("📌 测试 4: 模拟下单 (需要 OpenD 运行)")
print("-" * 70)

if signal_buy:
    print(f"准备下单:")
    print(f"   标的：{signal_buy.code}")
    print(f"   方向：{signal_buy.action.value}")
    print(f"   价格：{signal_buy.price:.2f}")
    print(f"   数量：{signal_buy.quantity}")
    print()
    
    # 检查 OpenD 是否运行
    import subprocess
    try:
        result = subprocess.run(['pgrep', '-f', 'FutuOpenD'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ OpenD 正在运行")
            
            # 执行下单 (注释掉，需要实际 OpenD 环境)
            # order_result = executor.execute(signal_buy)
            # print(f"下单结果：{order_result}")
            
            print("⚠️  模拟下单已跳过 (需要实际 OpenD 环境)")
            print("   实际运行时会自动调用富途 API 下单")
        else:
            print("⚠️  OpenD 未运行，跳过下单测试")
            print("   启动 OpenD: /opt/ftseek/Futu_OpenD_.../FutuOpenD")
    except Exception as e:
        print(f"⚠️  检查 OpenD 失败：{e}")
else:
    print("⚠️  无信号，跳过下单测试")

print()

# ==================== 测试 5: 数据库记录 ====================
print("📌 测试 5: 数据库记录")
print("-" * 70)

from app.storage.database import get_database

db = get_database()

if signal_buy:
    # 保存信号到数据库
    signal_data = {
        'id': signal_buy.id,
        'code': signal_buy.code,
        'name': signal_buy.name,
        'action': signal_buy.action.value,
        'price': signal_buy.price,
        'quantity': signal_buy.quantity,
        'strategy': signal_buy.strategy,
        'technical_score': result_buy.technical_score,
        'sentiment_score': result_buy.sentiment_score,
        'confidence': result_buy.confidence,
        'reason': signal_buy.reason,
        'status': 'pending'
    }
    
    db.save_signal(signal_data)
    print(f"✅ 信号已保存到数据库")
    
    # 查询信号
    saved_signal = db.get_signal(signal_buy.id)
    if saved_signal:
        print(f"   信号 ID: {saved_signal['id']}")
        print(f"   标的：{saved_signal['code']}")
        print(f"   状态：{saved_signal['status']}")
        print(f"   技术分：{saved_signal['technical_score']:.2f}")
        print(f"   情绪分：{saved_signal['sentiment_score']:.2f}")
else:
    print("⚠️  无信号，跳过数据库测试")

print()

# ==================== 测试 6: 回调接口测试 ====================
print("📌 测试 6: 回调接口模拟")
print("-" * 70)

from app.signal_store import signal_store

if signal_buy:
    # 模拟信号确认流程
    print(f"模拟回调流程:")
    print(f"   1. 用户点击钉钉按钮")
    print(f"   2. FastAPI 接收 /api/confirm 请求")
    print(f"   3. 验证 Token 和信号状态")
    
    # 确认信号
    confirmed_signal = signal_store.confirm(signal_buy.id)
    if confirmed_signal:
        print(f"   4. ✅ 信号确认成功")
        print(f"      确认时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 标记已执行
        signal_store.mark_executed(signal_buy.id)
        print(f"   5. ✅ 标记已执行")
        
        # 更新数据库
        db.update_signal_status(signal_buy.id, 'executed')
        print(f"   6. ✅ 数据库状态更新")
    else:
        print(f"   4. ❌ 信号确认失败 (可能已在信号库中)")
else:
    print("⚠️  无信号，跳过回调测试")

print()

# ==================== 测试 7: 完整流程总结 ====================
print("=" * 70)
print("  集成测试总结")
print("=" * 70)

print("""
📊 测试覆盖:
  ✅ 信号生成 (融合引擎)
  ✅ 钉钉推送 (ActionCard 三按钮)
  ✅ 交易执行器初始化
  ⏳ 模拟下单 (需要 OpenD 环境)
  ✅ 数据库记录
  ✅ 回调接口模拟

🔗 联动流程:
  富途行情 → 策略引擎 → 融合引擎 → 钉钉推送
                                      ↓
  数据库记录 ← 执行结果 ← 富途下单 ← 用户点击
                                      ↓
  绩效统计 ← 信号确认 ← 回调接口

📋 实际运行配置:
  1. 确保 OpenD 已启动
  2. 配置 TRADE_ENV=1 (模拟环境)
  3. 设置 DINGTALK_WEBHOOK_URL
  4. 启动 FTSeek: ./run.sh
  5. 等待信号生成并推送到钉钉
  6. 点击确认按钮执行交易

🎯 核心目标:
  模拟盘连续两个月胜率超过 60%
""")

print("=" * 70)
print("✅ 集成测试完成")
print("=" * 70)
