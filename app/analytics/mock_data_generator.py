#!/usr/bin/env python3
"""
模拟交易数据生成器 - 用于测试信号分析器

生成模拟的 signal_details 数据，模拟不同因子、不同时段、
不同市场状态下的交易结果，用于验证分析器逻辑。

使用方法:
    python -m app.analytics.mock_data_generator --days 30 --signals 200
"""

import sqlite3
import json
import random
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# 配置
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")


def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_signal_details_table():
    """确保表存在"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL UNIQUE,
            code TEXT NOT NULL,
            action TEXT NOT NULL,
            strategy TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_pct REAL,
            factors TEXT,
            bullish_score REAL DEFAULT 0,
            bearish_score REAL DEFAULT 0,
            market_regime TEXT DEFAULT '',
            adx_value REAL DEFAULT 0,
            mtf_alignment REAL DEFAULT 0,
            consensus_score REAL DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            confidence REAL DEFAULT 0,
            exit_reason TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            exit_time TEXT
        )
    """)
    # 创建索引
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_signal_details_code ON signal_details(code)",
        "CREATE INDEX IF NOT EXISTS idx_signal_details_strategy ON signal_details(strategy)",
        "CREATE INDEX IF NOT EXISTS idx_signal_details_regime ON signal_details(market_regime)",
        "CREATE INDEX IF NOT EXISTS idx_signal_details_created ON signal_details(created_at)",
    ]:
        cursor.execute(idx_sql)
    conn.commit()
    conn.close()


def generate_mock_data(days: int = 30, total_signals: int = 200):
    """
    生成模拟交易数据

    模拟逻辑：
    - rsi 因子在震荡市胜率更高
    - supertrend 在趋势市胜率更高
    - macd 在开盘时段胜率较低
    - 尾盘(14:00后)信号整体胜率较低
    - 强趋势市整体胜率更高
    """
    ensure_signal_details_table()

    codes = ["HK.00700", "HK.09988", "HK.03690", "HK.00981", "HK.01347",
             "HK.01810", "HK.01211", "HK.00388", "HK.02269", "HK.09992"]

    regimes = ["STRONG_TREND_UP", "STRONG_TREND_DOWN", "RANGE_BOUND",
               "HIGH_VOLATILITY", "LOW_VOLATILITY"]

    # 时段权重 (14:00后胜率低)
    time_buckets = [
        ("09:30", "10:00", 0.55),   # 开盘，胜率稍高
        ("10:00", "11:00", 0.50),   # 正常
        ("11:00", "12:00", 0.48),   # 午间前
        ("13:00", "14:00", 0.52),   # 下午
        ("14:00", "15:00", 0.40),   # 尾盘，胜率低 (模拟尾盘拥堵)
        ("15:00", "16:00", 0.35),   # 收盘前，胜率最低
    ]

    conn = get_db_connection()
    cursor = conn.cursor()

    # 清空旧数据（可选）
    # cursor.execute("DELETE FROM signal_details")
    # conn.commit()

    inserted = 0
    base_time = datetime.now() - timedelta(days=days)

    for i in range(total_signals):
        # 随机时间
        day_offset = random.randint(0, days - 1)
        bucket = random.choices(time_buckets, weights=[0.2, 0.25, 0.15, 0.2, 0.15, 0.05])[0]
        hour = random.randint(int(bucket[0].split(":")[0]), int(bucket[1].split(":")[0]) - 1)
        minute = random.randint(0, 59)
        second = random.randint(0, 59)
        created_at = base_time + timedelta(days=day_offset, hours=hour - base_time.hour,
                                             minutes=minute - base_time.minute,
                                             seconds=second)

        # 随机标的
        code = random.choice(codes)

        # 随机市场状态
        regime = random.choice(regimes)

        # 生成因子
        factors = {}
        factor_names = ["rsi", "macd", "bollinger", "supertrend", "ma_cross", "volume"]
        selected = random.sample(factor_names, random.randint(2, 4))
        for fn in selected:
            direction = random.choice([1, -1])
            factors[fn] = {
                "direction": direction,
                "weight": round(random.uniform(0.8, 1.5), 2),
                "reason": f"{fn} { 'bullish' if direction > 0 else 'bearish' }"
            }

        # 共识分数
        consensus_score = round(random.uniform(1.0, 2.5), 2)

        # 根据条件计算胜率基础值
        base_win_rate = bucket[2]  # 时段胜率

        # 市场状态调整
        if regime == "STRONG_TREND_UP" and random.random() < 0.6:
            base_win_rate += 0.10  # 强趋势市做多胜率高
        elif regime == "RANGE_BOUND":
            base_win_rate += 0.05  # 震荡市稍好
        elif regime == "HIGH_VOLATILITY":
            base_win_rate -= 0.05  # 高波动率低

        # rsi 在震荡市有加成
        if "rsi" in factors and regime == "RANGE_BOUND":
            base_win_rate += 0.05

        # supertrend 在趋势市有加成
        if "supertrend" in factors and "TREND" in regime:
            base_win_rate += 0.05

        # macd 在尾盘惩罚
        if "macd" in factors and hour >= 14:
            base_win_rate -= 0.05

        # 共识分数调整
        if consensus_score >= 2.0:
            base_win_rate += 0.08
        elif consensus_score < 1.5:
            base_win_rate -= 0.05

        # 限制范围
        base_win_rate = max(0.1, min(0.9, base_win_rate))

        # 生成结果
        is_win = random.random() < base_win_rate
        if is_win:
            pnl_pct = round(random.uniform(0.005, 0.15), 4)  # 盈利 0.5% - 15%
            exit_reason = random.choice(["take_profit", "trailing_stop", "signal_reversal"])
        else:
            pnl_pct = round(random.uniform(-0.10, -0.005), 4)  # 亏损 -10% - -0.5%
            exit_reason = random.choice(["stop_loss", "time_stop", "circuit_breaker"])

        exit_price = 100 * (1 + pnl_pct)

        # 插入数据
        signal_id = f"mock_{i:06d}_{code.replace('.', '_')}"
        action = "BUY" if random.random() > 0.3 else "SELL"  # 70%做多

        cursor.execute("""
            INSERT OR REPLACE INTO signal_details
            (signal_id, code, action, strategy, entry_price, exit_price, pnl_pct,
             factors, bullish_score, bearish_score, market_regime, adx_value,
             consensus_score, quality_score, confidence, exit_reason, created_at, exit_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            signal_id, code, action, "confluence_v2", 100.0, exit_price, pnl_pct,
            json.dumps(factors, ensure_ascii=False),
            random.uniform(1.5, 3.0), random.uniform(0, 1.5),
            regime, random.uniform(15, 55),
            consensus_score, random.choice([0.8, 1.0, 1.3]),
            random.uniform(0.5, 0.9), exit_reason,
            created_at.strftime("%Y-%m-%d %H:%M:%S")
        ))

        inserted += 1
        if inserted % 50 == 0:
            conn.commit()
            print(f"  已插入 {inserted}/{total_signals} 条...")

    conn.commit()
    conn.close()
    print(f"\n✅ 模拟数据生成完成: {inserted} 条信号")
    print(f"   数据库: {DB_PATH}")
    print(f"   分析命令: python -m app.analytics.signal_analyzer --days {days}")


def main():
    parser = argparse.ArgumentParser(description="生成模拟交易数据")
    parser.add_argument("--days", type=int, default=30, help="模拟天数")
    parser.add_argument("--signals", type=int, default=200, help="信号数量")
    args = parser.parse_args()

    print(f"正在生成 {args.signals} 条模拟信号数据 (周期: {args.days} 天)...")
    generate_mock_data(days=args.days, total_signals=args.signals)


if __name__ == "__main__":
    main()
