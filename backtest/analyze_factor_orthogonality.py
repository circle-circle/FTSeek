#!/usr/bin/env python3
"""
策略因子正交性分析 -- Spearman 相关矩阵

目的: 识别 confluence_v2 内部 6 个因子的同源性，
验证第三方评审"5个同源策略"的论断。

输出:
  - 6x6 Spearman 相关矩阵 (RSI, MACD, BB, ST, MA, Vol)
  - 因子聚类分析
  - 独立因子数量估计

用法:
  python3 backtest/analyze_factor_orthogonality.py
"""
import os
import sys
import json
import math
import random
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

random.seed(42)

# ── 生成模拟 OHLCV 数据 (300 天日K线) ─────────────────────────────
def generate_ohlcv(n_bars=300, trend=0.0, volatility=0.02):
    """生成带趋势的模拟 OHLCV 数据"""
    price = 100.0
    data = []
    for i in range(n_bars):
        change = random.gauss(trend, volatility)
        open_p = price
        close_p = price * (1 + change)
        high_p = max(open_p, close_p) * (1 + abs(random.gauss(0, 0.005)))
        low_p = min(open_p, close_p) * (1 - abs(random.gauss(0, 0.005)))
        # 成交量: 趋势日放量
        base_vol = 10_000_000
        vol_multiplier = 1.0 + abs(change) * 10  # 大波动日放量
        volume = int(base_vol * vol_multiplier * random.uniform(0.5, 1.5))
        data.append({
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": volume,
        })
        price = close_p
    return data


# ── 技术指标计算 (复用 indicators.py 逻辑) ─────────────────────────
def sma(data, period):
    if len(data) < period:
        return None
    return sum(data[-period:]) / period

def ema(data, period):
    if len(data) < period:
        return None
    k = 2.0 / (period + 1)
    result = sum(data[:period]) / period
    for val in data[period:]:
        result = val * k + result * (1 - k)
    return result

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def macd_indicator(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    if fast_ema is None or slow_ema is None:
        return None, None, None
    macd_line = fast_ema - slow_ema
    # 简化: 计算 histogram 近似值
    return macd_line, 0, macd_line  # (macd, signal, histogram)

def bollinger(closes, period=20, std_mult=2.0):
    if len(closes) < period:
        return None, None, None
    mean = sum(closes[-period:]) / period
    variance = sum((x - mean) ** 2 for x in closes[-period:]) / period
    std = math.sqrt(variance)
    upper = mean + std_mult * std
    lower = mean - std_mult * std
    return upper, mean, lower

def bollinger_pctb(close, upper, lower):
    if upper == lower:
        return 0.5
    return (close - lower) / (upper - lower)

def supertrend(highs, lows, closes, period=10, multiplier=3.0):
    """简化 SuperTrend: 返回方向 (1=up, -1=down)"""
    if len(closes) < period:
        return None, None
    atr = _calc_atr(highs, lows, closes, period)
    if atr is None:
        return None, None
    mid = (highs[-1] + lows[-1]) / 2
    upper_band = mid + multiplier * atr
    lower_band = mid - multiplier * atr
    if closes[-1] > upper_band:
        return lower_band, "up"
    elif closes[-1] < lower_band:
        return upper_band, "down"
    return None, None

def _calc_atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def ma_cross(closes, fast=10, slow=30):
    fast_ma = sma(closes, fast)
    slow_ma = sma(closes, slow)
    if fast_ma is None or slow_ma is None:
        return None
    return fast_ma, slow_ma


# ── 因子信号提取 ────────────────────────────────────────────────────
def extract_factor_signals(data):
    """
    从 OHLCV 数据提取每个因子的方向信号
    返回 {factor_name: [+1, -1, 0, ...]} 方向序列
    """
    closes = [d["close"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    volumes = [d["volume"] for d in data]

    signals = {
        "rsi": [],
        "macd": [],
        "bollinger": [],
        "supertrend": [],
        "ma_cross": [],
        "volume": [],
    }

    for i in range(30, len(data)):
        window_closes = closes[:i+1]
        window_highs = highs[:i+1]
        window_lows = lows[:i+1]
        window_volumes = volumes[:i+1]

        # RSI
        rsi_val = rsi(window_closes, 14)
        rsi_prev = rsi(window_closes[:-1], 14) if len(window_closes) > 15 else None
        if rsi_val is not None and rsi_prev is not None:
            if rsi_prev <= 30 and rsi_val > 30:
                signals["rsi"].append(1)  # 超卖反转
            elif rsi_prev >= 70 and rsi_val < 70:
                signals["rsi"].append(-1)  # 超买回落
            elif rsi_val < 30:
                signals["rsi"].append(1)
            elif rsi_val > 70:
                signals["rsi"].append(-1)
            else:
                signals["rsi"].append(0)
        else:
            signals["rsi"].append(0)

        # MACD
        macd_val, _, hist = macd_indicator(window_closes)
        macd_prev, _, _ = macd_indicator(window_closes[:-1]) if len(window_closes) > 27 else (None, None, None)
        if macd_val is not None:
            if hist > 0:
                signals["macd"].append(1)
            elif hist < 0:
                signals["macd"].append(-1)
            else:
                signals["macd"].append(0)
        else:
            signals["macd"].append(0)

        # Bollinger
        upper, mid, lower = bollinger(window_closes, 20, 2.0)
        if upper is not None:
            pctb = bollinger_pctb(window_closes[-1], upper, lower)
            if pctb <= 0.1:
                signals["bollinger"].append(1)
            elif pctb >= 0.9:
                signals["bollinger"].append(-1)
            else:
                signals["bollinger"].append(0)
        else:
            signals["bollinger"].append(0)

        # SuperTrend
        st_val, st_dir = supertrend(window_highs, window_lows, window_closes, 10, 3.0)
        if st_dir == "up":
            signals["supertrend"].append(1)
        elif st_dir == "down":
            signals["supertrend"].append(-1)
        else:
            signals["supertrend"].append(0)

        # MA Cross
        fast_ma, slow_ma = ma_cross(window_closes, 10, 30)
        if fast_ma is not None and slow_ma is not None:
            if fast_ma > slow_ma:
                signals["ma_cross"].append(1)
            else:
                signals["ma_cross"].append(-1)
        else:
            signals["ma_cross"].append(0)

        # Volume
        if len(window_volumes) > 20:
            avg_vol = sum(window_volumes[-21:-1]) / 20
            vol_ratio = window_volumes[-1] / avg_vol if avg_vol > 0 else 0
            if vol_ratio > 1.5:
                # 放量 + 价格上涨 = 1, 放量 + 价格下跌 = -1
                if len(window_closes) > 1:
                    if window_closes[-1] > window_closes[-2]:
                        signals["volume"].append(1)
                    else:
                        signals["volume"].append(-1)
                else:
                    signals["volume"].append(0)
            else:
                signals["volume"].append(0)
        else:
            signals["volume"].append(0)

    return signals


# ── Spearman 相关系数计算 ───────────────────────────────────────────
def spearman_correlation(x, y):
    """计算 Spearman 秩相关系数 (纯 Python)"""
    n = len(x)
    if n < 3:
        return 0.0

    def rank(data):
        sorted_indices = sorted(range(len(data)), key=lambda i: data[i])
        ranks = [0.0] * len(data)
        i = 0
        while i < len(sorted_indices):
            j = i
            while j < len(sorted_indices) - 1 and data[sorted_indices[j]] == data[sorted_indices[j+1]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[sorted_indices[k]] = avg_rank
            i = j + 1
        return ranks

    rank_x = rank(x)
    rank_y = rank(y)

    mean_x = sum(rank_x) / n
    mean_y = sum(rank_y) / n

    cov = sum((rank_x[i] - mean_x) * (rank_y[i] - mean_y) for i in range(n)) / n
    std_x = math.sqrt(sum((r - mean_x)**2 for r in rank_x) / n)
    std_y = math.sqrt(sum((r - mean_y)**2 for r in rank_y) / n)

    if std_x == 0 or std_y == 0:
        return 0.0

    return cov / (std_x * std_y)


# ── 主分析 ──────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("  FTSeek 港股策略因子正交性分析")
    print("  Spearman 秩相关矩阵")
    print("=" * 70)

    # 生成 300 天模拟日K线 (带轻微上升趋势)
    print("\n→ 生成 300 根模拟日K线 (trend=0.0003, vol=0.02)...")
    data = generate_ohlcv(n_bars=300, trend=0.0003, volatility=0.02)

    # 提取因子信号
    print("→ 提取 6 个因子方向信号...")
    signals = extract_factor_signals(data)

    for factor, s in signals.items():
        non_zero = sum(1 for x in s if x != 0)
        print(f"   {factor:12s}: {len(s):4d} 信号, 其中 {non_zero:4d} 非零 ({non_zero/len(s)*100:.1f}%)")

    # 计算 Spearman 相关矩阵
    factors = ["rsi", "macd", "bollinger", "supertrend", "ma_cross", "volume"]
    n = len(factors)

    print(f"\n{'=' * 70}")
    print("  Spearman 相关矩阵")
    print(f"{'=' * 70}")

    header = f"{'':>12s}"
    for f in factors:
        header += f" {f:>12s}"
    print(header)
    print("-" * 85)

    matrix = {}
    high_corr_pairs = []

    for i, f1 in enumerate(factors):
        row = f"{f1:>12s}"
        matrix[f1] = {}
        for j, f2 in enumerate(factors):
            corr = spearman_correlation(signals[f1], signals[f2])
            matrix[f1][f2] = corr
            row += f" {corr:>+11.4f}"
            if i < j and abs(corr) > 0.3:
                high_corr_pairs.append((f1, f2, corr))
        print(row)

    # 分析
    print(f"\n{'=' * 70}")
    print("  高度相关因子对 (|r| > 0.30)")
    print(f"{'=' * 70}")

    if not high_corr_pairs:
        print("  无高度相关因子对 ✅")
    else:
        for f1, f2, corr in sorted(high_corr_pairs, key=lambda x: -abs(x[2])):
            severity = "⚠️  严重同源" if abs(corr) > 0.6 else "⚡ 中等相关"
            print(f"  {severity}: {f1} ↔ {f2}  (r = {corr:+.4f})")

    # 因子聚类
    print(f"\n{'=' * 70}")
    print("  因子聚类分析")
    print(f"{'=' * 70}")

    # 按平均绝对相关度分组
    factor_clusters = {}
    for f1 in factors:
        avg_abs_corr = sum(abs(matrix[f1][f2]) for f2 in factors if f1 != f2) / (n - 1)
        factor_clusters[f1] = avg_abs_corr

    sorted_factors = sorted(factor_clusters.items(), key=lambda x: -x[1])
    print("  按平均相关性排序 (越高越不独立):")
    for f, avg in sorted_factors:
        bar = "█" * int(avg * 20)
        print(f"    {f:12s} |{bar:20s}| {avg:.3f}")

    # 独立因子估计
    print(f"\n{'=' * 70}")
    print("  独立因子数量估计")
    print(f"{'=' * 70}")

    # 使用特征值法 (简化 PCA)
    # 构建相关矩阵
    corr_matrix = [[matrix[f1][f2] for f2 in factors] for f1 in factors]

    # 计算特征值 (幂迭代法)
    def power_iteration(matrix, n_iterations=100):
        n = len(matrix)
        v = [1.0 / math.sqrt(n)] * n
        for _ in range(n_iterations):
            w = [sum(matrix[i][j] * v[j] for j in range(n)) for i in range(n)]
            norm = math.sqrt(sum(x**2 for x in w))
            if norm == 0:
                break
            v = [x / norm for x in w]
        eigenvalue = sum(matrix[i][j] * v[j] for i in range(n) for j in range(n))
        return eigenvalue / sum(x**2 for x in v)

    # 简化: 用 Kaiser 准则 (特征值 > 1 的因子数)
    # 对于相关矩阵，trace = n，平均特征值 = 1
    # 特征值 > 1 表示该成分解释了超过单个变量的方差
    eigenvalues = []
    residual = [row[:] for row in corr_matrix]

    for _ in range(n):
        ev = power_iteration(residual)
        if ev > 0.01:
            eigenvalues.append(ev)
            #  deflate
            for i in range(n):
                for j in range(n):
                    residual[i][j] -= ev * (1.0 / math.sqrt(n)) * (1.0 / math.sqrt(n))
        else:
            eigenvalues.append(0)

    independent_factors = sum(1 for ev in eigenvalues if ev > 1.0)

    print(f"  总因子数:       {n}")
    print(f"  独立因子数估计: {independent_factors} (Kaiser 准则, λ > 1)")
    print(f"  特征值:         {[round(ev, 3) for ev in eigenvalues]}")
    print(f"  累计解释方差:   {sum(eigenvalues):.2f} / {n} ({sum(eigenvalues)/n*100:.1f}%)")

    # 结论
    print(f"\n{'=' * 70}")
    print("  结论与建议")
    print(f"{'=' * 70}")

    if independent_factors <= 2:
        print("  ❌ 因子池严重冗余，实际独立因子 ≤ 2 个")
        print("     → 建议: 仅保留 MACD (趋势) + RSI (均值回归)")
    elif independent_factors <= 3:
        print("  ⚠️  因子池存在中度冗余，实际独立因子 ≤ 3 个")
        print("     → 建议: 正交化后保留 3 个独立因子")
    else:
        print("  ✅ 因子池正交性良好")

    # 输出 JSON
    output = {
        "spearman_matrix": {f1: {f2: round(matrix[f1][f2], 4) for f2 in factors} for f1 in factors},
        "high_corr_pairs": [{"f1": f1, "f2": f2, "corr": round(c, 4)} for f1, f2, c in high_corr_pairs],
        "factor_independence": {f: round(v, 4) for f, v in sorted_factors},
        "independent_factor_count": independent_factors,
        "eigenvalues": [round(ev, 4) for ev in eigenvalues],
    }

    output_path = os.path.join(os.path.dirname(__file__), "factor_orthogonality_result.json")
    with open(output_path, "w") as fp:
        json.dump(output, fp, indent=2)
    print(f"\n→ 结果已保存: {output_path}")


if __name__ == "__main__":
    main()
