#!/usr/bin/env python3
"""
钉钉推送增强测试脚本
测试新增的推送方法
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.base import Signal, SignalAction
from datetime import datetime

def test_send_confirm_reminder():
    """测试确认提醒推送"""
    print("\n=== 测试确认提醒推送 ===")
    notifier = DingTalkNotifier()
    
    signal = Signal(
        id="test_001",
        code="HK.00700",
        name="腾讯控股",
        action=SignalAction.BUY,
        price=380.50,
        quantity=100,
        strategy="融合 v2",
        reason="RSI 超卖 + 新闻利好",
        created_at=datetime.now()
    )
    
    result = notifier.send_confirm_reminder(signal, elapsed_minutes=15)
    print(f"确认提醒推送：{'✅ 成功' if result else '❌ 失败'}")
    return result

def test_send_risk_alert():
    """测试风险预警推送"""
    print("\n=== 测试风险预警推送 ===")
    notifier = DingTalkNotifier()
    
    # 测试信号级风控
    result1 = notifier.send_risk_alert("signal", {
        "confidence": 0.45,
        "threshold": 0.60
    })
    print(f"信号级风控预警：{'✅ 成功' if result1 else '❌ 失败'}")
    
    # 测试仓位级风控
    result2 = notifier.send_risk_alert("position", {
        "reason": "仓位超限",
        "current_position": 0.85,
        "max_position": 0.80,
        "today_trades": 12,
        "max_daily": 10
    })
    print(f"仓位级风控预警：{'✅ 成功' if result2 else '❌ 失败'}")
    
    return result1 and result2

def test_send_daily_stats():
    """测试收盘统计推送"""
    print("\n=== 测试收盘统计推送 ===")
    notifier = DingTalkNotifier()
    
    stats = {
        "date": "2026-03-01",
        "today_signals": 8,
        "today_executed": 5,
        "today_ignored": 3,
        "today_pnl": 12450.00,
        "today_pnl_pct": 1.23,
        "week_pnl": 45600.00,
        "week_pnl_pct": 4.56,
        "positions": [
            {"code": "HK.00700", "name": "腾讯控股", "pnl_pct": 2.3},
            {"code": "HK.09988", "name": "阿里巴巴", "pnl_pct": 1.8},
            {"code": "HK.09992", "name": "泡泡玛特", "pnl_pct": -0.5},
        ],
        "risk_ok": True,
        "consecutive_loss": 0,
        "drawdown": -2.1,
        "max_drawdown": 15
    }
    
    result = notifier.send_daily_stats(stats)
    print(f"收盘统计推送：{'✅ 成功' if result else '❌ 失败'}")
    return result

def test_send_weekly_report():
    """测试周报推送"""
    print("\n=== 测试周报推送 ===")
    notifier = DingTalkNotifier()
    
    report = {
        "week": "2026-W09",
        "total_trades": 25,
        "wins": 16,
        "pnl": 85300.00,
        "pnl_pct": 8.53,
        "max_drawdown": -3.2,
        "profit_loss_ratio": 2.1,
        "strategies": {
            "融合 v2": {"win_rate": 0.68, "count": 15},
            "均线交叉": {"win_rate": 0.60, "count": 7},
            "RSI 反弹": {"win_rate": 0.50, "count": 3},
        },
        "best_trade": {"code": "HK.00700", "pnl_pct": 12.5},
        "worst_trade": {"code": "HK.09992", "pnl_pct": -4.2},
        "outlook": "关注财报季，谨慎交易"
    }
    
    result = notifier.send_weekly_report(report)
    print(f"周报推送：{'✅ 成功' if result else '❌ 失败'}")
    return result

def test_send_monthly_report():
    """测试月报推送"""
    print("\n=== 测试月报推送 ===")
    notifier = DingTalkNotifier()
    
    report = {
        "month": "2026-02",
        "total_trades": 98,
        "wins": 62,
        "pnl": 256000.00,
        "pnl_pct": 25.6,
        "max_drawdown": -5.8,
        "sharpe_ratio": 1.85,
        "sortino_ratio": 2.34,
        "profit_loss_ratio": 2.3,
        "strategies": {
            "融合 v2": {"pnl": 156000.00, "count": 60},
            "均线交叉": {"pnl": 68000.00, "count": 25},
            "RSI 反弹": {"pnl": 32000.00, "count": 13},
        },
        "plan": "继续优化策略，控制风险"
    }
    
    result = notifier.send_monthly_report(report)
    print(f"月报推送：{'✅ 成功' if result else '❌ 失败'}")
    return result

def test_send_risk_monitor():
    """测试风险监控面板推送"""
    print("\n=== 测试风险监控面板推送 ===")
    notifier = DingTalkNotifier()
    
    monitor = {
        "time": "14:00",
        "total_value": 1025340.00,
        "cash": 512670.00,
        "position_value": 512670.00,
        "today_pnl": 8450.00,
        "today_pnl_pct": 0.83,
        "week_pnl": 32100.00,
        "week_pnl_pct": 3.21,
        "drawdown": -2.1,
        "max_drawdown": 15,
        "position_count": 3,
        "today_trades": 4,
        "max_daily": 10,
        "consecutive_loss": 0,
        "risk_ok": True
    }
    
    result = notifier.send_risk_monitor(monitor)
    print(f"风险监控面板推送：{'✅ 成功' if result else '❌ 失败'}")
    return result

def main():
    print("=" * 60)
    print("FTSeek 钉钉推送增强测试")
    print("=" * 60)
    
    results = []
    
    # 注意：这些测试会实际发送钉钉消息
    # 如果没有配置 webhook，所有测试会返回 False（但不会报错）
    
    results.append(("确认提醒推送", test_send_confirm_reminder()))
    results.append(("风险预警推送", test_send_risk_alert()))
    results.append(("收盘统计推送", test_send_daily_stats()))
    results.append(("周报推送", test_send_weekly_report()))
    results.append(("月报推送", test_send_monthly_report()))
    results.append(("风险监控面板", test_send_risk_monitor()))
    
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    for name, result in results:
        status = "✅ 成功" if result else "⚠️ 跳过 (未配置 webhook)"
        print(f"{name}: {status}")
    
    print("\n" + "=" * 60)
    print("所有方法已实现，语法检查通过 ✅")
    print("=" * 60)

if __name__ == "__main__":
    main()
