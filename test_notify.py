#!/usr/bin/env python3.8
"""
FTSeek 手动测试工具
用法:
    python3.8 test_notify.py text           # 发送纯文本测试
    python3.8 test_notify.py signal         # 发送模拟交易信号 (带确认按钮)
    python3.8 test_notify.py signal sell    # 发送卖出信号
    python3.8 test_notify.py price          # 查看当前实时股价
    python3.8 test_notify.py account        # 查看模拟账户信息
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 确保环境变量存在
if not os.getenv("DINGTALK_WEBHOOK_URL"):
    os.environ["DINGTALK_WEBHOOK_URL"] = "https://oapi.dingtalk.com/robot/send?access_token=cddea8919223640d4f3bd59e507373cae0df476df5f541fcca5c8921eab73121"
if not os.getenv("DINGTALK_SECRET"):
    os.environ["DINGTALK_SECRET"] = "SEC177f5321e91e689a1a2134e4414ae49e7b7f2cddc63775613effbf371f388f78"

from app import config
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.base import Signal, SignalAction
from app.signal_store import signal_store


def test_text():
    """发送纯文本消息"""
    notifier = DingTalkNotifier()
    ok = notifier.send_text(
        "[FTSeek] 手动测试消息\n"
        f"时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"监控标的: {', '.join(config.WATCH_LIST)}\n"
        f"交易环境: {'模拟' if config.TRADE_ENV == 1 else '真实'}"
    )
    print(f"✅ 文本消息发送{'成功' if ok else '失败'}")


def test_signal(action="buy"):
    """发送模拟交易信号 (带确认/忽略按钮)"""
    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
    ret, data = ctx.get_market_snapshot(["HK.00700"])
    ctx.close()

    if ret == 0:
        price = data.iloc[0]["last_price"]
        name = data.iloc[0]["name"]
    else:
        price, name = 520.0, "腾讯控股"

    sig_action = SignalAction.SELL if action.lower() == "sell" else SignalAction.BUY
    sig = Signal(
        code="HK.00700",
        name=name,
        action=sig_action,
        price=price,
        quantity=100,
        reason=f"手动测试信号 - 当前价 HK${price:.2f}",
        strategy="manual_test",
    )
    signal_store.add(sig)

    notifier = DingTalkNotifier()
    ok = notifier.send_signal(sig)
    print(f"✅ 信号推送{'成功' if ok else '失败'}")
    print(f"   信号ID: {sig.id}")
    print(f"   方向: {sig.action.value}")
    print(f"   标的: {sig.code} {sig.name}")
    print(f"   价格: HK${sig.price:.2f}")
    print(f"   确认链接: {config.CALLBACK_EXTERNAL_URL}/api/confirm?signal_id={sig.id}&token={config.API_TOKEN}")


def show_price():
    """查看实时股价"""
    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
    ret, data = ctx.get_market_snapshot(config.WATCH_LIST)
    ctx.close()
    if ret == 0:
        print(f"{'代码':<12} {'名称':<8} {'现价':>10} {'涨跌%':>8} {'今高':>10} {'今低':>10} {'成交量':>12}")
        print("-" * 75)
        for _, row in data.iterrows():
            chg = row.get("price_spread", 0)
            pct = (chg / (row["last_price"] - chg) * 100) if (row["last_price"] - chg) != 0 else 0
            print(f"{row['code']:<12} {row['name']:<8} {row['last_price']:>10.2f} {pct:>+7.2f}% {row['high_price']:>10.2f} {row['low_price']:>10.2f} {row['volume']:>12,.0f}")
    else:
        print(f"获取行情失败: {data}")


def show_account():
    """查看模拟账户"""
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket
    ctx = OpenSecTradeContext(host=config.OPEND_HOST, port=config.OPEND_PORT, filter_trdmarket=TrdMarket.HK)
    ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
    ctx.close()
    if ret == 0 and len(data) > 0:
        info = data.iloc[0]
        print(f"模拟账户信息:")
        print(f"  总资产:   HK${info.get('total_assets', 0):,.2f}")
        print(f"  现金:     HK${info.get('cash', 0):,.2f}")
        print(f"  持仓市值: HK${info.get('market_val', 0):,.2f}")
    else:
        print(f"查询失败: {data}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    arg2 = sys.argv[2] if len(sys.argv) > 2 else "buy"

    if cmd == "text":
        test_text()
    elif cmd == "signal":
        test_signal(arg2)
    elif cmd == "price":
        show_price()
    elif cmd == "account":
        show_account()
    else:
        print(__doc__)
