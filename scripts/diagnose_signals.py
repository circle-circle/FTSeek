#!/usr/bin/env python3
"""
信号触发诊断脚本
分析为什么没有信号产生，检查策略条件和共识引擎

Usage:
    python3 scripts/diagnose_signals.py
"""

import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import json
import numpy as np
import pandas as pd
from datetime import datetime

def get_kline_data(code, count=100):
    """获取K线数据"""
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    ret, data = ctx.get_history_kline(code, count=count)
    ctx.close()
    if ret == RET_OK:
        return data
    return None

def analyze_rsi(closes, period=14, oversold=30, overbought=65):
    """分析RSI指标"""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    signals = []
    
    for i in range(period, len(closes)):
        avg_gain = np.mean(gains[i-period:i])
        avg_loss = np.mean(losses[i-period:i])
        
        if avg_loss == 0:
            rsi_val = 100
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100 - (100 / (1 + rs))
        
        # 检查信号
        if i > period:
            prev_rsi = rsi_vals[-1] if rsi_vals else 50
            if prev_rsi <= oversold and rsi_val > oversold:
                signals.append({
                    'type': 'BUY',
                    'idx': i,
                    'rsi': rsi_val,
                    'reason': f'RSI超卖反转 {prev_rsi:.1f}->{rsi_val:.1f}'
                })
            elif prev_rsi >= overbought and rsi_val < overbought:
                signals.append({
                    'type': 'SELL',
                    'idx': i,
                    'rsi': rsi_val,
                    'reason': f'RSI超买回落 {prev_rsi:.1f}->{rsi_val:.1f}'
                })
        
        rsi_vals = locals().get('rsi_vals', [])
        rsi_vals.append(rsi_val)
        locals()['rsi_vals'] = rsi_vals
    
    # 当前RSI
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    rs = avg_gain / avg_loss if avg_loss > 0 else 0
    current_rsi = 100 - (100 / (1 + rs))
    
    return current_rsi, rsi_vals[-5:] if rsi_vals else [], signals[-3:] if signals else []

def analyze_macd(closes, fast=12, slow=26, signal=9):
    """分析MACD指标"""
    s = pd.Series(closes)
    ema12 = s.ewm(span=fast).mean()
    ema26 = s.ewm(span=slow).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=signal).mean()
    histogram = macd_line - signal_line
    
    signals = []
    for i in range(1, len(histogram)):
        prev_hist = histogram.iloc[i-1]
        curr_hist = histogram.iloc[i]
        
        if prev_hist < 0 and curr_hist >= 0:
            signals.append({
                'type': 'BUY',
                'idx': i,
                'hist': curr_hist,
                'reason': f'MACD金叉 {prev_hist:.4f}->{curr_hist:.4f}'
            })
        elif prev_hist > 0 and curr_hist <= 0:
            signals.append({
                'type': 'SELL',
                'idx': i,
                'hist': curr_hist,
                'reason': f'MACD死叉 {prev_hist:.4f}->{curr_hist:.4f}'
            })
    
    return {
        'macd': macd_line.iloc[-1],
        'signal': signal_line.iloc[-1],
        'histogram': histogram.iloc[-1],
        'prev_histogram': histogram.iloc[-2] if len(histogram) > 1 else 0
    }, signals[-3:] if signals else []

def analyze_consensus(rsi_signal, macd_signal, bollinger_signal=None):
    """模拟共识引擎评分"""
    # 策略权重
    weights = {
        'rsi': 0.8,
        'macd': 0.8,
        'bollinger': 0.8,
        'supertrend': 1.0,
        'confluence': 1.2
    }
    
    score = 0
    agree_strategies = []
    
    if rsi_signal:
        score += weights['rsi'] * (1 if rsi_signal == 'BUY' else -1)
        agree_strategies.append('rsi')
    
    if macd_signal:
        score += weights['macd'] * (1 if macd_signal == 'BUY' else -1)
        agree_strategies.append('macd')
    
    if bollinger_signal:
        score += weights['bollinger'] * (1 if bollinger_signal == 'BUY' else -1)
        agree_strategies.append('bollinger')
    
    return score, agree_strategies

def main():
    print("\n" + "="*70)
    print("🔍 FTSeek 信号触发诊断报告")
    print(f"   生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    codes = ['HK.09992', 'HK.00981', 'HK.03690']
    names = {'HK.09992': '泡泡玛特', 'HK.00981': '中芯国际', 'HK.03690': '美团-W'}
    
    # 加载配置
    config_file = PROJECT_ROOT / "config" / "strategy_params.json"
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    print("\n📋 共识引擎配置:")
    print(f"   触发阈值: {config.get('consensus', {}).get('threshold', 1.5)}")
    print(f"   最低置信度: {config.get('min_signal_confidence', 0.45)}")
    print(f"   做多阈值: {config.get('long_signals', {}).get('min_confidence', 0.55)}")
    print(f"   做空阈值: {config.get('short_signals', {}).get('min_confidence', 0.65)}")
    
    for code in codes:
        print(f"\n{'='*70}")
        print(f"📊 {code} {names.get(code, '')}")
        print("="*70)
        
        # 获取K线
        kline = get_kline_data(code)
        if kline is None:
            print("   ❌ 无法获取K线数据")
            continue
        
        closes = kline['close'].values
        
        # RSI分析
        print("\n[RSI 策略分析]")
        current_rsi, recent_rsi, rsi_signals = analyze_rsi(closes)
        print(f"   当前RSI(14): {current_rsi:.2f}")
        print(f"   近5个RSI: {[f'{r:.1f}' for r in recent_rsi]}")
        
        if current_rsi <= 30:
            print(f"   ⬇️ 超卖区 - 可能触发买入 (需反转上穿)")
        elif current_rsi >= 65:
            print(f"   ⬆️ 超买区 - 可能触发卖出 (需反转下穿)")
        else:
            print(f"   - 中性区间 (30-65)")
        
        if rsi_signals:
            print(f"   近期信号: {rsi_signals}")
        else:
            print(f"   近期无RSI信号")
        
        # MACD分析
        print("\n[MACD 策略分析]")
        macd_data, macd_signals = analyze_macd(closes)
        print(f"   MACD: {macd_data['macd']:.4f}")
        print(f"   信号线: {macd_data['signal']:.4f}")
        print(f"   柱状图: {macd_data['histogram']:.4f} (前值: {macd_data['prev_histogram']:.4f})")
        
        if macd_data['histogram'] > 0:
            if macd_data['prev_histogram'] <= 0:
                print(f"   ⬆️ 刚金叉 - 可能触发买入")
            else:
                print(f"   ⬆️ 多头 - 柱状图正")
        elif macd_data['histogram'] < 0:
            if macd_data['prev_histogram'] >= 0:
                print(f"   ⬇️ 刚死叉 - 可能触发卖出")
            else:
                print(f"   ⬇️ 空头 - 柱状图负")
        
        if macd_signals:
            print(f"   近期信号: {macd_signals}")
        else:
            print(f"   近期无MACD信号")
        
        # 共识模拟
        print("\n[共识引擎模拟]")
        rsi_signal = None
        if current_rsi <= 30:
            rsi_signal = 'BUY'
        elif current_rsi >= 65:
            rsi_signal = 'SELL'
        
        macd_signal = None
        if macd_data['histogram'] > 0 and macd_data['prev_histogram'] <= 0:
            macd_signal = 'BUY'
        elif macd_data['histogram'] < 0 and macd_data['prev_histogram'] >= 0:
            macd_signal = 'SELL'
        
        score, agree = analyze_consensus(rsi_signal, macd_signal)
        threshold = config.get('consensus', {}).get('threshold', 1.5)
        
        print(f"   RSI信号: {rsi_signal or '无'}")
        print(f"   MACD信号: {macd_signal or '无'}")
        print(f"   共识分数: {score:+.2f} (阈值: {threshold})")
        print(f"   参与策略: {agree if agree else '无'}")
        
        if abs(score) >= threshold:
            print(f"   ✅ 会触发共识信号")
        else:
            print(f"   ❌ 未达阈值，无信号")
        
        # 建议
        print("\n[诊断结论]")
        if abs(score) < threshold:
            needed = threshold - abs(score)
            print(f"   距离触发还需: {needed:.2f} 分")
            print(f"   建议: 等待更多策略同向，或调整阈值")
        else:
            print(f"   应有信号但未记录，检查日志")

if __name__ == "__main__":
    main()