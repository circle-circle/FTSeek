#!/usr/bin/env python3
"""
因子验证脚本 — 用真实历史数据验证 SSS/GDIAN/MFFF 因子的有效性

功能:
1. 从 backtest_cache/ 加载日线 OHLCV
2. 计算技术指标 (RSI, BB, MA, MACD, ATR)
3. 在每根 K 线上计算因子值
4. 模拟因子信号入场 → 统计虚拟交易结果
5. 与实际交易结果交叉验证

Usage:
    python3 scripts/validate_factors.py
    python3 scripts/validate_factors.py --symbols AAPL,MSFT,NVDA
    python3 scripts/validate_factors.py --timeframe daily --verbose
"""
import sys
import os
import csv
import json
import math
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.factors.sss import SSSFactor
from app.factors.gdian import GDIANFactor
from app.factors.mfff import MFFFFactor
from app.factors.base import FactorParams
from app.factors.assembly import FactorAssembly, FactorSlot, StrategyVariantLibrary

# ============================================================
#  技术指标计算 (纯 Python, 无外部依赖)
# ============================================================

def compute_rsi(closes: List[float], period: int = 14) -> List[float]:
    """计算 RSI 序列"""
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    
    rsi_values = [50.0] * period
    gains = []
    losses = []
    
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    
    # 初始平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))
    
    return rsi_values


def compute_bollinger(closes: List[float], period: int = 20, std_dev: float = 2.0
                       ) -> Tuple[List[float], List[float], List[float]]:
    """计算布林带: (upper, mid, lower)"""
    n = len(closes)
    upper = [0.0] * n
    mid = [0.0] * n
    lower = [0.0] * n
    
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        std = math.sqrt(variance)
        mid[i] = m
        upper[i] = m + std_dev * std
        lower[i] = m - std_dev * std
    
    return upper, mid, lower


def compute_ma(closes: List[float], period: int) -> List[float]:
    """计算移动平均"""
    result = [0.0] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1:i + 1]) / period
    return result


def compute_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
                  ) -> Tuple[List[float], List[float], List[float]]:
    """计算 MACD: (macd_line, signal_line, histogram)"""
    n = len(closes)
    
    def ema(values, period):
        result = [0.0] * len(values)
        if not values:
            return result
        k = 2.0 / (period + 1)
        result[0] = values[0]
        for i in range(1, len(values)):
            result[i] = values[i] * k + result[i-1] * (1 - k)
        return result
    
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(n)]
    signal_line = ema(macd_line, signal)
    histogram = [macd_line[i] - signal_line[i] for i in range(n)]
    
    return macd_line, signal_line, histogram


def compute_atr(highs: List[float], lows: List[float], closes: List[float],
                period: int = 14) -> List[float]:
    """计算 ATR"""
    n = len(closes)
    atr_values = [0.0] * n
    
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    
    if len(trs) < period:
        return [sum(trs) / max(1, len(trs))] * n if trs else [0.0] * n
    
    # 初始 ATR
    first_atr = sum(trs[:period]) / period
    atr_values[period] = first_atr
    
    for i in range(period, len(trs)):
        atr_values[i + 1] = (atr_values[i] * (period - 1) + trs[i]) / period
    
    return atr_values


# ============================================================
#  数据加载
# ============================================================

def load_csv(path: str) -> List[Dict]:
    """加载 OHLCV CSV"""
    bars = []
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append({
                'datetime': row['datetime'],
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': int(row['Volume']),
            })
    # 按日期升序
    bars.sort(key=lambda x: x['datetime'])
    return bars


def compute_indicators(bars: List[Dict]) -> List[Dict]:
    """为每根 K 线计算技术指标"""
    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]
    
    rsi = compute_rsi(closes, 14)
    bb_upper, bb_mid, bb_lower = compute_bollinger(closes, 20, 2.0)
    ma_5 = compute_ma(closes, 5)
    ma_20 = compute_ma(closes, 20)
    ma_50 = compute_ma(closes, 50)
    macd_line, macd_signal, macd_hist = compute_macd(closes)
    atr = compute_atr(highs, lows, closes, 14)
    avg_volume = compute_ma([float(v) for v in volumes], 20)
    
    for i, bar in enumerate(bars):
        bar['rsi_14'] = rsi[i] if i < len(rsi) else 50.0
        bar['bollinger_upper'] = bb_upper[i] if i < len(bb_upper) else 0.0
        bar['bollinger_lower'] = bb_lower[i] if i < len(bb_lower) else 0.0
        bar['bollinger_mid'] = bb_mid[i] if i < len(bb_mid) else 0.0
        bar['ma_5'] = ma_5[i] if i < len(ma_5) else 0.0
        bar['ma_20'] = ma_20[i] if i < len(ma_20) else 0.0
        bar['ma_50'] = ma_50[i] if i < len(ma_50) else 0.0
        bar['macd'] = macd_line[i] if i < len(macd_line) else 0.0
        bar['macd_signal'] = macd_signal[i] if i < len(macd_signal) else 0.0
        bar['macd_hist'] = macd_hist[i] if i < len(macd_hist) else 0.0
        bar['atr_14'] = atr[i] if i < len(atr) else 0.0
        bar['avg_volume'] = avg_volume[i] if i < len(avg_volume) else 0.0
        bar['price'] = bar['close']
        # bars 窗口 (最近 5 根)
        bar['bars'] = bars[max(0, i-4):i+1]
    
    return bars


# ============================================================
#  因子信号回测
# ============================================================

def build_market_data(bar: Dict) -> Dict[str, Any]:
    """构建因子输入的 market_data 字典"""
    return {
        'price': bar['price'],
        'close': bar['close'],
        'open': bar['open'],
        'high': bar['high'],
        'low': bar['low'],
        'volume': bar['volume'],
        'avg_volume': bar['avg_volume'],
        'ma_5': bar['ma_5'],
        'ma_20': bar['ma_20'],
        'ma_50': bar['ma_50'],
        'rsi_14': bar['rsi_14'],
        'macd': bar['macd'],
        'macd_signal': bar['macd_signal'],
        'macd_hist': bar['macd_hist'],
        'bollinger_upper': bar['bollinger_upper'],
        'bollinger_lower': bar['bollinger_lower'],
        'bollinger_mid': bar['bollinger_mid'],
        'atr_14': bar['atr_14'],
        'bars': bar['bars'],
    }


def simulate_factor_signals(
    bars: List[Dict],
    assembly: FactorAssembly,
    symbol: str,
    stop_loss_pct: float = -0.03,
    take_profit_pct: float = 0.06,
    max_hold_bars: int = 10,
) -> List[Dict]:
    """
    模拟因子信号入场 → 虚拟交易

    对每根 K 线:
    1. 计算因子 → 评估 assembly
    2. 如果通过 → 开仓
    3. 跟踪持仓直到止损/止盈/超时

    Returns:
        虚拟交易列表
    """
    sss = SSSFactor()
    gdian = GDIANFactor()
    mfff = MFFFFactor()
    
    trades = []
    position = None  # {entry_price, direction, entry_bar, entry_date}
    cooldown = 0  # 平仓后冷却期
    
    for i in range(50, len(bars)):  # 前 50 根用于指标预热
        bar = bars[i]
        
        # 如果有持仓 → 检查退出
        if position is not None:
            bars_held = i - position['entry_bar']
            current_price = bar['close']
            entry_price = position['entry_price']
            direction = position['direction']
            
            if direction == 'LONG':
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - current_price) / entry_price
            
            # 退出条件
            exit_reason = None
            if pnl_pct <= stop_loss_pct:
                exit_reason = 'SL_HIT'
            elif pnl_pct >= take_profit_pct:
                exit_reason = 'TP_HIT'
            elif bars_held >= max_hold_bars:
                exit_reason = 'TIME_STOP'
            
            if exit_reason:
                trades.append({
                    'symbol': symbol,
                    'direction': direction,
                    'entry_date': position['entry_date'],
                    'exit_date': bar['datetime'],
                    'entry_price': entry_price,
                    'exit_price': current_price,
                    'pnl': pnl_pct * entry_price * 100,  # 假设 100 股
                    'pnl_pct': pnl_pct,
                    'exit_reason': exit_reason,
                    'bars_held': bars_held,
                    'entry_sss': position['entry_sss'],
                    'entry_gdian': position['entry_gdian'],
                    'entry_mfff': position['entry_mfff'],
                })
                position = None
                cooldown = 3  # 冷却 3 根 K 线
            continue  # 持仓期间不开新仓
        
        # 冷却期
        if cooldown > 0:
            cooldown -= 1
            continue
        
        # 无持仓 → 评估因子信号
        md = build_market_data(bar)
        result = assembly.evaluate(symbol, md)
        
        if result.passed and result.direction in ('LONG', 'SHORT'):
            # 开仓
            position = {
                'entry_price': bar['close'],
                'direction': result.direction,
                'entry_bar': i,
                'entry_date': bar['datetime'],
                'entry_sss': result.factor_values.get('SSS', None),
                'entry_gdian': result.factor_values.get('GDIAN', None),
                'entry_mfff': result.factor_values.get('MFFF', None),
            }
    
    # 如果最后还有持仓，强制平仓
    if position is not None:
        bar = bars[-1]
        current_price = bar['close']
        entry_price = position['entry_price']
        direction = position['direction']
        if direction == 'LONG':
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
        trades.append({
            'symbol': symbol,
            'direction': direction,
            'entry_date': position['entry_date'],
            'exit_date': bar['datetime'],
            'entry_price': entry_price,
            'exit_price': current_price,
            'pnl': pnl_pct * entry_price * 100,
            'pnl_pct': pnl_pct,
            'exit_reason': 'END_OF_DATA',
            'bars_held': len(bars) - 1 - position['entry_bar'],
            'entry_sss': position.get('entry_sss'),
            'entry_gdian': position.get('entry_gdian'),
            'entry_mfff': position.get('entry_mfff'),
        })
    
    return trades


# ============================================================
#  实际交易数据加载
# ============================================================

def load_actual_trades(db_path: str = "data/ftseek_us.db") -> List[Dict]:
    """从 trade_records 加载实际交易"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT symbol, direction, entry_time, exit_time, 
               entry_price, exit_price, realized_pnl, pnl_pct,
               exit_reason, signal_strength, owner_strategy, market_regime
        FROM trade_records
        WHERE realized_pnl IS NOT NULL AND direction IN ('LONG','SHORT')
        ORDER BY entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  分析与报告
# ============================================================

def analyze_trades(trades: List[Dict], label: str = "") -> Dict:
    """分析交易统计"""
    if not trades:
        return {"label": label, "n": 0}
    
    n = len(trades)
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    
    total_pnl = sum(t['pnl'] for t in trades)
    win_rate = len(wins) / n if n > 0 else 0
    avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl_pct'] for t in losses) / len(losses) if losses else 0
    profit_factor = (
        abs(sum(t['pnl'] for t in wins)) / abs(sum(t['pnl'] for t in losses))
        if losses and sum(t['pnl'] for t in losses) != 0 else float('inf')
    )
    
    # 按方向
    longs = [t for t in trades if t['direction'] == 'LONG']
    shorts = [t for t in trades if t['direction'] == 'SHORT']
    
    # 按 exit_reason
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t['exit_reason']].append(t)
    
    return {
        "label": label,
        "n": n,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "longs": len(longs),
        "shorts": len(shorts),
        "long_wr": len([t for t in longs if t['pnl_pct'] > 0]) / len(longs) if longs else 0,
        "short_wr": len([t for t in shorts if t['pnl_pct'] > 0]) / len(shorts) if shorts else 0,
        "by_reason": {k: {"n": len(v), "pnl": sum(t['pnl'] for t in v)} 
                      for k, v in by_reason.items()},
        "avg_bars_held": sum(t['bars_held'] for t in trades) / n,
    }


def print_report(results: Dict[str, Dict]):
    """打印验证报告"""
    print("\n" + "=" * 80)
    print("  因子验证报告 — 真实历史数据 vs 因子信号模拟")
    print("=" * 80)
    
    for variant_name, stats in results.items():
        if stats.get("n", 0) == 0:
            print(f"\n📊 {variant_name}: 无交易信号")
            continue
        
        print(f"\n📊 {variant_name}")
        print(f"   交易数: {stats['n']} | 胜率: {stats['win_rate']:.1%} | "
              f"利润因子: {stats['profit_factor']:.2f}")
        print(f"   总PnL: ${stats['total_pnl']:.0f} | "
              f"平均赢: {stats['avg_win_pct']:.2%} | 平均亏: {stats['avg_loss_pct']:.2%}")
        print(f"   LONG: {stats['longs']}笔 (WR {stats['long_wr']:.0%}) | "
              f"SHORT: {stats['shorts']}笔 (WR {stats['short_wr']:.0%})")
        print(f"   平均持仓: {stats['avg_bars_held']:.1f} 根K线")
        
        # 按退出原因
        if stats.get('by_reason'):
            print(f"   退出原因:")
            for reason, data in sorted(stats['by_reason'].items(), 
                                        key=lambda x: x[1]['pnl'], reverse=True):
                print(f"     {reason}: {data['n']}笔, ${data['pnl']:.0f}")


def cross_validate(factor_trades: List[Dict], actual_trades: List[Dict]) -> Dict:
    """
    交叉验证: 因子信号与实际交易的吻合度

    检查:
    1. 实际盈利的交易，因子是否也给出正确方向?
    2. 实际亏损的交易，因子是否给出警告信号?
    """
    # 按 symbol + date 匹配
    actual_by_key = {}
    for t in actual_trades:
        # 简化匹配: symbol + entry_date
        date_str = t['entry_time'][:10] if t.get('entry_time') else ''
        symbol_short = t['symbol'].replace('US.', '')
        key = f"{symbol_short}_{date_str}"
        actual_by_key[key] = t
    
    matches = 0
    factor_agreed_with_win = 0
    factor_agreed_with_loss = 0
    
    for ft in factor_trades:
        symbol_short = ft['symbol'].replace('US.', '')
        date_str = ft['entry_date'][:10]
        key = f"{symbol_short}_{date_str}"
        
        if key in actual_by_key:
            matches += 1
            at = actual_by_key[key]
            actual_win = at['realized_pnl'] > 0
            factor_win = ft['pnl_pct'] > 0
            
            if actual_win and factor_win:
                factor_agreed_with_win += 1
            elif not actual_win and not factor_win:
                factor_agreed_with_loss += 1
    
    return {
        "matched_trades": matches,
        "factor_agreed_with_win": factor_agreed_with_win,
        "factor_agreed_with_loss": factor_agreed_with_loss,
        "agreement_rate": (
            (factor_agreed_with_win + factor_agreed_with_loss) / matches 
            if matches > 0 else 0
        ),
    }


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="因子验证: 历史数据 vs 因子信号")
    parser.add_argument("--symbols", type=str, default="",
                        help="逗号分隔的symbols (默认: 全部可用)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", type=str, default="",
                        help="输出 JSON 路径")
    args = parser.parse_args()
    
    # 加载策略变体
    lib = StrategyVariantLibrary()
    lib.load("config/strategy_variants.yaml")
    
    # 要测试的变体 (选取代表性的 3 个)
    test_variants = [
        "stable_15m_relaxed_oversold_tight_trend",  # 最佳 15min
        "radical_daily_deep_oversold",               # 日线深度超卖
        "aggressive_5m_high_momentum",               # 5min 高动量
    ]
    
    # 发现可用数据
    cache_dir = Path("data/backtest_cache")
    available_symbols = []
    for f in cache_dir.glob("*_1day.csv"):
        sym = f.name.split("_")[0]
        available_symbols.append(sym)
    
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = available_symbols
    
    print(f"📁 可用数据: {len(available_symbols)} 个标的")
    print(f"🎯 测试标的: {symbols}")
    print(f"📋 测试变体: {test_variants}")
    print()
    
    all_results = {}
    all_factor_trades = []
    
    for variant_name in test_variants:
        assembly = lib.get(variant_name)
        if assembly is None:
            print(f"⚠️ 变体 {variant_name} 不存在，跳过")
            continue
        
        variant_trades = []
        
        for symbol in symbols:
            csv_path = cache_dir / f"{symbol}_2025-03-27_2026-03-27_1day.csv"
            if not csv_path.exists():
                continue
            
            bars = load_csv(str(csv_path))
            bars = compute_indicators(bars)
            
            # 日线策略参数
            if "daily" in variant_name:
                sl_pct, tp_pct, max_hold = -0.07, 0.14, 10
            elif "5m" in variant_name:
                sl_pct, tp_pct, max_hold = -0.015, 0.03, 3  # 日线模拟 3 根≈短线
            else:  # 15min
                sl_pct, tp_pct, max_hold = -0.03, 0.06, 5
            
            trades = simulate_factor_signals(
                bars, assembly, f"US.{symbol}",
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                max_hold_bars=max_hold,
            )
            variant_trades.extend(trades)
            
            if args.verbose and trades:
                print(f"  {symbol}: {len(trades)} signals, "
                      f"WR={sum(1 for t in trades if t['pnl_pct']>0)/len(trades):.0%}")
        
        # 分析
        stats = analyze_trades(variant_trades, variant_name)
        all_results[variant_name] = stats
        all_factor_trades.extend(variant_trades)
    
    # 打印报告
    print_report(all_results)
    
    # 交叉验证
    print("\n" + "=" * 80)
    print("  交叉验证: 因子信号 vs 实际交易")
    print("=" * 80)
    
    try:
        actual_trades = load_actual_trades()
        cv = cross_validate(all_factor_trades, actual_trades)
        print(f"\n  匹配交易: {cv['matched_trades']}")
        print(f"  因子与盈利一致: {cv['factor_agreed_with_win']}")
        print(f"  因子与亏损一致: {cv['factor_agreed_with_loss']}")
        print(f"  总体一致率: {cv['agreement_rate']:.1%}")
    except Exception as e:
        print(f"\n  ⚠️ 交叉验证失败: {e}")
        cv = {}
    
    # 实际交易对比 (main 策略)
    print("\n" + "=" * 80)
    print("  实际交易基线 (main策略, 相同标的)")
    print("=" * 80)
    
    try:
        actual_trades = load_actual_trades()
        # 筛选相同标的
        symbol_set = {f"US.{s}" for s in symbols}
        filtered_actual = [t for t in actual_trades 
                           if t['symbol'] in symbol_set and t['owner_strategy'] == 'main']
        actual_stats = analyze_trades(
            [{"pnl": t['realized_pnl'], "pnl_pct": t['pnl_pct'] or 0,
              "direction": t['direction'], "exit_reason": t['exit_reason'] or 'unknown',
              "bars_held": 0, "symbol": t['symbol']}
             for t in filtered_actual],
            "actual_main"
        )
        if actual_stats['n'] > 0:
            print(f"\n  实际交易 (main):")
            print(f"    交易数: {actual_stats['n']} | 胜率: {actual_stats['win_rate']:.1%}")
            print(f"    总PnL: ${actual_stats['total_pnl']:.0f}")
            print(f"    LONG: {actual_stats['longs']}笔 WR {actual_stats['long_wr']:.0%} | "
                  f"SHORT: {actual_stats['shorts']}笔 WR {actual_stats['short_wr']:.0%}")
        else:
            print(f"\n  ⚠️ 这些标的在 main 策略中无交易记录")
    except Exception as e:
        print(f"\n  ⚠️ 实际交易加载失败: {e}")
    
    # 保存 JSON
    if args.output:
        output = {
            "timestamp": datetime.now().isoformat(),
            "symbols": symbols,
            "variants": {k: v for k, v in all_results.items()},
            "cross_validation": cv,
        }
        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n📄 结果已保存: {args.output}")
    
    print("\n✅ 验证完成")


if __name__ == "__main__":
    main()
