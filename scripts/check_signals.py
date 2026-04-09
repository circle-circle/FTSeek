#!/usr/bin/env python3
"""
信号检查脚本
实时检查策略信号状态，帮助诊断信号触发情况

Usage:
    python3 scripts/check_signals.py
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import json
from datetime import datetime, timedelta

def check_signal_records():
    """检查信号记录"""
    print("=" * 60)
    print("📊 信号记录检查")
    print("=" * 60)
    
    signal_dir = PROJECT_ROOT / "data" / "signal_records"
    total_signals = 0
    
    for file in sorted(signal_dir.glob("signals_*.json")):
        date_str = file.stem.replace("signals_", "")
        with open(file, 'r') as f:
            signals = json.load(f)
        
        print(f"\n📅 {date_str}: {len(signals)} 个信号")
        for sig in signals:
            print(f"   - {sig['code']} {sig['action']} @ {sig.get('signal_price', 'N/A')}")
            print(f"     置信度: {sig.get('confidence', 'N/A')}")
            print(f"     共识分: {sig.get('consensus_score', 'N/A')}")
            print(f"     策略: {sig.get('strategies', [])}")
            print(f"     执行: {'✅' if sig.get('executed') else '❌'}")
        total_signals += len(signals)
    
    print(f"\n📈 总信号数: {total_signals}")
    return total_signals


def check_trade_records():
    """检查交易记录"""
    print("\n" + "=" * 60)
    print("💰 交易记录检查")
    print("=" * 60)
    
    trade_dir = PROJECT_ROOT / "data" / "trade_records"
    total_trades = 0
    
    for file in sorted(trade_dir.glob("*.json")):
        date_str = file.stem
        with open(file, 'r') as f:
            trades = json.load(f)
        
        print(f"\n📅 {date_str}: {len(trades)} 笔交易")
        for trade in trades:
            print(f"   - {trade['code']} {trade['action']} @ {trade['entry_price']}")
            print(f"     止损: {trade.get('stop_loss', 'N/A')}")
            print(f"     止盈: {trade.get('take_profit', 'N/A')}")
            if trade.get('exit_price'):
                print(f"     平仓: {trade['exit_price']} ({trade.get('exit_reason', 'N/A')})")
                print(f"     盈亏: {trade.get('pnl', 'N/A')}")
        total_trades += len(trades)
    
    print(f"\n📈 总交易数: {total_trades}")
    return total_trades


def check_consensus_config():
    """检查共识引擎配置"""
    print("\n" + "=" * 60)
    print("⚙️ 共识引擎配置")
    print("=" * 60)
    
    config_file = PROJECT_ROOT / "config" / "strategy_params.json"
    if config_file.exists():
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        consensus = config.get('consensus', {})
        print(f"共识阈值: {consensus.get('threshold', 1.5)}")
        print(f"ADX 趋势: > {consensus.get('adx_trend', 25)}")
        print(f"ADX 震荡: < {consensus.get('adx_range', 20)}")
        
        fusion = config.get('fusion', {})
        print(f"\n融合权重:")
        print(f"  技术: {fusion.get('technical_weight', 0.55) * 100}%")
        print(f"  情绪: {fusion.get('sentiment_weight', 0.25) * 100}%")
        print(f"  AI: {fusion.get('ai_weight', 0.20) * 100}%")
        
        print(f"\n置信度阈值:")
        print(f"  最低: {config.get('min_signal_confidence', 0.4)}")
        print(f"  做多: {config.get('long_signals', {}).get('min_confidence', 0.6)}")
        print(f"  做空: {config.get('short_signals', {}).get('min_confidence', 0.55)}")


def check_watchlist():
    """检查监控列表"""
    print("\n" + "=" * 60)
    print("👁️ 监控标的")
    print("=" * 60)
    
    watchlist_file = PROJECT_ROOT / "config" / "watchlist.json"
    if watchlist_file.exists():
        with open(watchlist_file, 'r') as f:
            watchlist = json.load(f)
        
        print("港股:")
        for stock in watchlist.get('hk_stocks', []):
            status = "✅" if stock.get('enabled') else "⏸️"
            print(f"  {status} {stock['code']} {stock['name']}")
        
        print("\n美股:")
        us_stocks = watchlist.get('us_stocks', [])
        if us_stocks:
            for stock in us_stocks:
                status = "✅" if stock.get('enabled') else "⏸️"
                print(f"  {status} {stock['code']} {stock['name']}")
        else:
            print("  无")


def check_realtime_quotes():
    """检查实时行情"""
    print("\n" + "=" * 60)
    print("📈 实时行情")
    print("=" * 60)
    
    try:
        from futu import OpenQuoteContext, RET_OK
        
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        
        codes = ['HK.09992', 'HK.00981', 'HK.03690']
        ret, data = ctx.get_market_snapshot(codes)
        
        if ret == RET_OK and len(data) > 0:
            for _, row in data.iterrows():
                code = row['code']
                price = row.get('last_price', 0)
                change = row.get('change_rate', 0) * 100
                volume = row.get('volume', 0)
                print(f"  {code}: {price} ({change:+.2f}%) 成交量: {volume:,}")
        else:
            print(f"  行情获取失败: {data}")
        
        ctx.close()
    except Exception as e:
        print(f"  连接失败: {e}")


def check_strategy_status():
    """检查策略状态"""
    print("\n" + "=" * 60)
    print("🎯 策略状态")
    print("=" * 60)
    
    try:
        from app import config
        
        print(f"监控标的: {config.WATCH_LIST}")
        print(f"共识窗口: {config.CONSENSUS_WINDOW}s")
        print(f"共识阈值: {config.CONSENSUS_THRESHOLD}")
        print(f"信号冷却: {config.SIGNAL_COOLDOWN}s")
        
        print("\n启用的策略:")
        from app.strategy import STRATEGY_REGISTRY
        for name, cls in STRATEGY_REGISTRY.items():
            print(f"  - {name}")
    except Exception as e:
        print(f"  检查失败: {e}")


def main():
    """主检查流程"""
    print("\n🔍 FTSeek 信号检查报告")
    print(f"   生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    check_watchlist()
    check_consensus_config()
    check_strategy_status()
    check_realtime_quotes()
    check_signal_records()
    check_trade_records()
    
    print("\n" + "=" * 60)
    print("✅ 检查完成")
    print("=" * 60)


if __name__ == "__main__":
    main()