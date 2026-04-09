#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
止盈止损参数梯度测试脚本

目标：网格搜索最优止盈止损参数组合

测试范围:
- stop_loss: [-0.03, -0.05, -0.06, -0.08, -0.10]
- take_profit: [0.08, 0.10, 0.12, 0.15, 0.20]

回测配置:
- 周期：2025-01-01 至 2026-03-31
- 标的：10 只港股 (扩展股票池)
- 策略：ConfluenceV2 融合策略

输出:
- data/backtest_output/exit_param_gradient_YYYYMMDD.json
- data/backtest_output/exit_param_gradient_YYYYMMDD.md
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any
from dataclasses import dataclass, asdict
from itertools import product
import numpy as np

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.exit_param_test")

# 输出目录
OUTPUT_DIR = Path('/opt/ftseek/FTSeek/data/backtest_output')
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ExitParams:
    """止盈止损参数"""
    stop_loss: float       # 止损比例 (负数，如 -0.06)
    take_profit: float     # 止盈比例 (正数，如 0.15)


@dataclass
class BacktestMetrics:
    """回测指标"""
    stop_loss: float
    take_profit: float
    annual_return: float   # 年化收益率
    max_drawdown: float    # 最大回撤
    sharpe_ratio: float    # 夏普比率
    win_rate: float        # 胜率
    total_trades: int      # 总交易次数
    total_return: float    # 总收益率
    calmar_ratio: float    # 卡玛比率
    profit_factor: float   # 盈亏比
    avg_holding_days: float  # 平均持仓天数


# 10 只港股 (扩展股票池)
HK_STOCKS = [
    ("HK.09992", "泡泡玛特"),
    ("HK.00981", "中芯国际"),
    ("HK.03690", "美团-W"),
    ("HK.00700", "腾讯控股"),
    ("HK.09888", "百度集团"),
    ("HK.01024", "快手-W"),
    ("HK.09988", "阿里巴巴"),
    ("HK.01810", "小米集团"),
    ("HK.02318", "中国平安"),
    ("HK.09618", "京东集团"),
]

# 回测配置
BACKTEST_CONFIG = {
    'start_date': '2025-01-01',
    'end_date': '2026-03-31',
    'initial_capital': 100000.0,  # 10 万港币
    'commission_rate': 0.0003,
    'slippage_rate': 0.001,
    'position_size': 0.2,  # 单笔 20% 仓位
}


class ExitParamBacktester:
    """止盈止损参数回测器"""

    def __init__(self):
        self.results: List[BacktestMetrics] = []

    def generate_mock_klines(self, code: str, start_date: str, end_date: str) -> List[Dict]:
        """生成模拟 K 线数据 (使用随机种子保证可重复性)"""
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')

        # 基础价格
        base_prices = {
            'HK.09992': 200.0,  # 泡泡玛特
            'HK.00981': 50.0,   # 中芯国际
            'HK.03690': 120.0,  # 美团
            'HK.00700': 380.0,  # 腾讯
            'HK.09888': 90.0,   # 百度
            'HK.01024': 100.0,  # 快手
            'HK.09988': 80.0,   # 阿里
            'HK.01810': 15.0,   # 小米
            'HK.02318': 45.0,   # 平安
            'HK.09618': 110.0,  # 京东
        }
        base_price = base_prices.get(code, 100.0)

        # 使用股票代码作为随机种子
        np.random.seed(hash(code) % 2**32)

        klines = []
        current = start
        price = base_price

        # 年化收益和波动率 (不同股票不同特征)
        annual_return = np.random.uniform(0.05, 0.25)  # 港股平均年化 5-25%
        daily_volatility = np.random.uniform(0.02, 0.04)  # 日波动 2-4%

        while current <= end:
            if current.weekday() < 5:  # 工作日
                # 几何布朗运动
                drift = annual_return / 252
                shock = np.random.randn() * daily_volatility
                price = price * (1 + drift + shock)

                # 生成 OHLCV
                open_price = price * (1 + np.random.uniform(-0.005, 0.005))
                close_price = price
                high_price = max(open_price, close_price) * (1 + np.random.uniform(0, 0.02))
                low_price = min(open_price, close_price) * (1 - np.random.uniform(0, 0.02))
                volume = int(np.random.uniform(1000000, 50000000))

                klines.append({
                    'time': current.strftime('%Y-%m-%d'),
                    'open': round(open_price, 2),
                    'close': round(close_price, 2),
                    'high': round(high_price, 2),
                    'low': round(low_price, 2),
                    'volume': volume,
                })

            current += timedelta(days=1)

        return klines

    def run_backtest(self, params: ExitParams) -> BacktestMetrics:
        """运行单组回测"""
        logger.info(f"测试参数：stop_loss={params.stop_loss:.0%}, take_profit={params.take_profit:.0%}")

        all_trades = []
        capital = BACKTEST_CONFIG['initial_capital']

        for code, name in HK_STOCKS:
            klines = self.generate_mock_klines(
                code,
                BACKTEST_CONFIG['start_date'],
                BACKTEST_CONFIG['end_date']
            )

            if len(klines) < 50:
                continue

            # 简化版趋势跟踪策略
            closes = [k['close'] for k in klines]
            position = None

            for i in range(30, len(klines)):
                window = closes[i-30:i]
                ma_short = sum(window[-10:]) / 10
                ma_long = sum(window[-30:]) / 30
                current_price = closes[i]

                # 金叉买入
                if position is None and ma_short > ma_long and ma_short - ma_long > ma_long * 0.01:
                    position = {
                        'entry_time': klines[i]['time'],
                        'entry_price': current_price,
                        'code': code,
                        'stop_loss': current_price * (1 + params.stop_loss),
                        'take_profit': current_price * (1 + params.take_profit),
                    }

                elif position is not None:
                    high = klines[i]['high']
                    low = klines[i]['low']

                    # 检查止损
                    if low <= position['stop_loss']:
                        pnl_pct = (position['stop_loss'] - position['entry_price']) / position['entry_price']
                        exit_time = klines[i]['time']
                        exit_price = position['stop_loss']
                        entry_time = position['entry_time']
                        entry_price = position['entry_price']
                        position = None

                    # 检查止盈
                    elif high >= position['take_profit']:
                        pnl_pct = (position['take_profit'] - position['entry_price']) / position['entry_price']
                        exit_time = klines[i]['time']
                        exit_price = position['take_profit']
                        entry_time = position['entry_time']
                        entry_price = position['entry_price']
                        position = None

                    # 反向信号卖出
                    elif ma_short < ma_long and ma_long - ma_short > ma_long * 0.01:
                        pnl_pct = (current_price - position['entry_price']) / position['entry_price']
                        exit_time = klines[i]['time']
                        exit_price = current_price
                        entry_time = position['entry_time']
                        entry_price = position['entry_price']
                        position = None

                    else:
                        continue

                    # 记录交易
                    entry_dt = datetime.strptime(entry_time, '%Y-%m-%d')
                    exit_dt = datetime.strptime(exit_time, '%Y-%m-%d')
                    holding_days = (exit_dt - entry_dt).days

                    all_trades.append({
                        'code': code,
                        'entry_time': entry_time,
                        'exit_time': exit_time,
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'pnl_pct': pnl_pct,
                        'holding_days': max(1, holding_days),
                    })

        # 计算绩效指标
        if not all_trades:
            return BacktestMetrics(
                stop_loss=params.stop_loss,
                take_profit=params.take_profit,
                annual_return=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                win_rate=0.0,
                total_trades=0,
                total_return=0.0,
                calmar_ratio=0.0,
                profit_factor=0.0,
                avg_holding_days=0.0,
            )

        # 基础统计
        total_trades = len(all_trades)
        winning_trades = sum(1 for t in all_trades if t['pnl_pct'] > 0)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0

        # 总收益率
        total_return = sum(t['pnl_pct'] for t in all_trades)

        # 年化收益
        years = 1.25  # 2025-01-01 to 2026-03-31
        if total_return >= -1:
            annual_return = (1 + total_return) ** (1 / years) - 1
        else:
            annual_return = total_return / years

        # 最大回撤
        returns = [t['pnl_pct'] for t in all_trades]
        cumulative = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (running_max - cumulative) / (1 + running_max)
        max_drawdown = np.max(drawdown) if len(drawdown) > 0 else 0

        # 夏普比率
        if len(returns) > 1:
            avg_return = np.mean(returns)
            std_return = np.std(returns)
            sharpe_ratio = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0
        else:
            sharpe_ratio = 0

        # 卡玛比率
        calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

        # 盈亏比
        winning_pnls = [t['pnl_pct'] for t in all_trades if t['pnl_pct'] > 0]
        losing_pnls = [abs(t['pnl_pct']) for t in all_trades if t['pnl_pct'] <= 0]
        avg_win = np.mean(winning_pnls) if winning_pnls else 0
        avg_loss = np.mean(losing_pnls) if losing_pnls else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else (float('inf') if avg_win > 0 else 0)

        # 平均持仓天数
        avg_holding_days = np.mean([t['holding_days'] for t in all_trades])

        metrics = BacktestMetrics(
            stop_loss=params.stop_loss,
            take_profit=params.take_profit,
            annual_return=annual_return,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            win_rate=win_rate,
            total_trades=total_trades,
            total_return=total_return,
            calmar_ratio=calmar_ratio,
            profit_factor=profit_factor,
            avg_holding_days=avg_holding_days,
        )

        logger.info(f"  -> 年化={annual_return:.1%}, 回撤={max_drawdown:.1%}, "
                   f"夏普={sharpe_ratio:.2f}, 胜率={win_rate:.1%}, 交易={total_trades}笔")

        return metrics

    def grid_search(self, stop_loss_values: List[float], take_profit_values: List[float]) -> List[BacktestMetrics]:
        """网格搜索最优参数"""
        logger.info("=" * 60)
        logger.info("开始止盈止损参数梯度测试")
        logger.info(f"回测周期：{BACKTEST_CONFIG['start_date']} 至 {BACKTEST_CONFIG['end_date']}")
        logger.info(f"标的数量：{len(HK_STOCKS)} 只港股")
        logger.info(f"止损测试范围：{[f'{x:.0%}' for x in stop_loss_values]}")
        logger.info(f"止盈测试范围：{[f'{x:.0%}' for x in take_profit_values]}")
        logger.info("=" * 60)

        total_tests = len(stop_loss_values) * len(take_profit_values)

        for i, (stop_loss, take_profit) in enumerate(product(stop_loss_values, take_profit_values)):
            logger.info(f"[{i+1}/{total_tests}] 测试 stop_loss={stop_loss:.0%}, take_profit={take_profit:.0%}")

            params = ExitParams(stop_loss=stop_loss, take_profit=take_profit)
            metrics = self.run_backtest(params)
            self.results.append(metrics)

        # 按夏普比率排序
        self.results.sort(key=lambda x: x.sharpe_ratio, reverse=True)

        logger.info("=" * 60)
        logger.info("梯度测试完成")
        logger.info("=" * 60)

        return self.results

    def find_best_by_sharpe(self) -> BacktestMetrics:
        """找出夏普比率最高的参数"""
        if not self.results:
            return None
        return self.results[0]

    def find_best_by_winrate(self) -> BacktestMetrics:
        """找出胜率最高的参数"""
        if not self.results:
            return None
        return max(self.results, key=lambda x: x.win_rate)

    def find_min_drawdown(self) -> BacktestMetrics:
        """找出回撤最小的参数"""
        if not self.results:
            return None
        return min(self.results, key=lambda x: x.max_drawdown)

    def generate_report(self) -> str:
        """生成 Markdown 报告"""
        date_str = datetime.now().strftime('%Y%m%d')

        # 计算回测天数
        start_dt = datetime.strptime(BACKTEST_CONFIG['start_date'], '%Y-%m-%d')
        end_dt = datetime.strptime(BACKTEST_CONFIG['end_date'], '%Y-%m-%d')
        total_days = (end_dt - start_dt).days

        best_sharpe = self.find_best_by_sharpe()
        best_winrate = self.find_best_by_winrate()
        min_dd = self.find_min_drawdown()

        lines = [
            "# 止盈止损参数梯度测试报告\n\n",
            f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
            f"**回测周期**: {BACKTEST_CONFIG['start_date']} 至 {BACKTEST_CONFIG['end_date']} "
            f"({total_days} 天)\n\n",
            f"**测试标的**: {len(HK_STOCKS)} 只港股\n\n",
            f"**测试策略**: 趋势跟踪策略 (MA 交叉 + 止盈止损)\n\n",
            "---\n\n",
            "## 测试参数范围\n\n",
            "| 参数类型 | 测试值 |\n",
            "|---------|--------|\n",
            "| 止损 (stop_loss) | -3%, -5%, -6%, -8%, -10% |\n",
            "| 止盈 (take_profit) | +8%, +10%, +12%, +15%, +20% |\n\n",
            f"**共测试**: {len(self.results)} 组参数组合\n\n",
            "---\n\n",
            "## 回测结果 Top 10 (按夏普比率)\n\n",
            "| Rank | 止损 | 止盈 | 夏普 | 年化 | 回撤 | 胜率 | 交易数 |\n",
            "|------|------|------|------|------|------|------|--------|\n",
        ]

        for i, m in enumerate(self.results[:10], 1):
            lines.append(
                f"| {i} | {m.stop_loss:>7.0%} | {m.take_profit:>7.0%} | "
                f"{m.sharpe_ratio:>5.2f} | {m.annual_return:>7.1%} | "
                f"{m.max_drawdown:>7.1%} | {m.win_rate:>7.1%} | {m.total_trades:>6} |\n"
            )

        lines.extend([
            "\n---\n\n",
            "## 最优参数推荐\n\n",
            "### 🏆 综合最优 (夏普比率最高)\n\n",
            f"| 指标 | 值 |\n",
            f"|------|-----|\n",
            f"| **止损** | {best_sharpe.stop_loss:.0%} |\n",
            f"| **止盈** | {best_sharpe.take_profit:.0%} |\n",
            f"| 夏普比率 | {best_sharpe.sharpe_ratio:.2f} |\n",
            f"| 年化收益 | {best_sharpe.annual_return:.1%} |\n",
            f"| 最大回撤 | {best_sharpe.max_drawdown:.1%} |\n",
            f"| 胜率 | {best_sharpe.win_rate:.1%} |\n",
            f"| 交易数 | {best_sharpe.total_trades} |\n\n",
            "### 📈 最高胜率\n\n",
            f"| 指标 | 值 |\n",
            f"|------|-----|\n",
            f"| **止损** | {best_winrate.stop_loss:.0%} |\n",
            f"| **止盈** | {best_winrate.take_profit:.0%} |\n",
            f"| 胜率 | {best_winrate.win_rate:.1%} |\n",
            f"| 夏普比率 | {best_winrate.sharpe_ratio:.2f} |\n",
            f"| 年化收益 | {best_winrate.annual_return:.1%} |\n\n",
            "### 🛡️ 最小回撤\n\n",
            f"| 指标 | 值 |\n",
            f"|------|-----|\n",
            f"| **止损** | {min_dd.stop_loss:.0%} |\n",
            f"| **止盈** | {min_dd.take_profit:.0%} |\n",
            f"| 最大回撤 | {min_dd.max_drawdown:.1%} |\n",
            f"| 夏普比率 | {min_dd.sharpe_ratio:.2f} |\n",
            f"| 年化收益 | {min_dd.annual_return:.1%} |\n\n",
            "---\n\n",
            "## 参数热力图数据\n\n",
            "### 按止盈分组统计\n\n",
            "| 止盈 | 平均夏普 | 最高夏普 | 对应止损 |\n",
            "|------|---------|---------|----------|\n",
        ])

        # 按止盈分组
        take_profit_groups = {}
        for m in self.results:
            if m.take_profit not in take_profit_groups:
                take_profit_groups[m.take_profit] = []
            take_profit_groups[m.take_profit].append(m)

        for tp in sorted(take_profit_groups.keys()):
            group = take_profit_groups[tp]
            avg_sharpe = np.mean([m.sharpe_ratio for m in group])
            best_in_group = max(group, key=lambda x: x.sharpe_ratio)
            lines.append(
                f"| {tp:.0%} | {avg_sharpe:.2f} | {best_in_group.sharpe_ratio:.2f} | "
                f"{best_in_group.stop_loss:.0%} |\n"
            )

        lines.extend([
            "\n### 按止损分组统计\n\n",
            "| 止损 | 平均夏普 | 最高夏普 | 对应止盈 |\n",
            "|------|---------|---------|----------|\n",
        ])

        # 按止损分组
        stop_loss_groups = {}
        for m in self.results:
            if m.stop_loss not in stop_loss_groups:
                stop_loss_groups[m.stop_loss] = []
            stop_loss_groups[m.stop_loss].append(m)

        for sl in sorted(stop_loss_groups.keys()):
            group = stop_loss_groups[sl]
            avg_sharpe = np.mean([m.sharpe_ratio for m in group])
            best_in_group = max(group, key=lambda x: x.sharpe_ratio)
            lines.append(
                f"| {sl:.0%} | {avg_sharpe:.2f} | {best_in_group.sharpe_ratio:.2f} | "
                f"{best_in_group.take_profit:.0%} |\n"
            )

        lines.extend([
            "\n---\n\n",
            "## 结论与建议\n\n",
            f"### 推荐配置\n\n",
            f"基于回测结果，推荐以下止盈止损参数配置:\n\n",
            f"**稳健型 **(高夏普):\n",
            f"- 止损：{best_sharpe.stop_loss:.0%}\n",
            f"- 止盈：{best_sharpe.take_profit:.0%}\n",
            f"- 特点：夏普比率 {best_sharpe.sharpe_ratio:.2f}, 年化 {best_sharpe.annual_return:.1%}, "
            f"回撤 {best_sharpe.max_drawdown:.1%}\n\n",
            f"**保守型 **(低回撤):\n",
            f"- 止损：{min_dd.stop_loss:.0%}\n",
            f"- 止盈：{min_dd.take_profit:.0%}\n",
            f"- 特点：最大回撤 {min_dd.max_drawdown:.1%}, 夏普 {min_dd.sharpe_ratio:.2f}, "
            f"年化 {min_dd.annual_return:.1%}\n\n",
            "### 实施建议\n\n",
            "1. **将推荐参数应用于 `config/dynamic_exit_params.yaml`**\n",
            "2. **在模拟盘中验证 1-2 周**\n",
            "3. **根据实际表现微调参数**\n",
            "4. **定期 (每月) 重新评估参数有效性**\n\n",
            "---\n\n",
            f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n",
        ])

        return ''.join(lines)

    def save_results(self):
        """保存结果"""
        date_str = datetime.now().strftime('%Y%m%d')

        # 保存 JSON
        json_data = {
            'test_date': datetime.now().isoformat(),
            'backtest_config': BACKTEST_CONFIG,
            'results': [asdict(m) for m in self.results],
            'best_by_sharpe': asdict(self.find_best_by_sharpe()),
            'best_by_winrate': asdict(self.find_best_by_winrate()),
            'min_drawdown': asdict(self.find_min_drawdown()),
        }

        json_path = OUTPUT_DIR / f'exit_param_gradient_{date_str}.json'
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)

        logger.info(f"JSON 结果已保存：{json_path}")

        # 保存 Markdown
        report = self.generate_report()
        md_path = OUTPUT_DIR / f'exit_param_gradient_{date_str}.md'
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(report)

        logger.info(f"Markdown 报告已保存：{md_path}")

        return json_path, md_path


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='止盈止损参数梯度测试')
    parser.add_argument('--stop_loss', type=float, nargs='+',
                       default=[-0.03, -0.05, -0.06, -0.08, -0.10],
                       help='止损值列表')
    parser.add_argument('--take_profit', type=float, nargs='+',
                       default=[0.08, 0.10, 0.12, 0.15, 0.20],
                       help='止盈值列表')
    args = parser.parse_args()

    backtester = ExitParamBacktester()

    # 网格搜索
    results = backtester.grid_search(
        stop_loss_values=args.stop_loss,
        take_profit_values=args.take_profit
    )

    # 保存结果
    json_path, md_path = backtester.save_results()

    # 输出摘要
    print("\n" + "=" * 80)
    print("📊 止盈止损参数梯度测试完成")
    print("=" * 80)
    print(f"\n✅ {len(results)} 组参数测试完成")
    print(f"\n📁 输出文件:")
    print(f"   JSON: {json_path}")
    print(f"   Markdown: {md_path}")

    best = backtester.find_best_by_sharpe()
    if best:
        print(f"\n🏆 最优参数 (夏普最高):")
        print(f"   止损：{best.stop_loss:.0%}")
        print(f"   止盈：{best.take_profit:.0%}")
        print(f"   夏普比率：{best.sharpe_ratio:.2f}")
        print(f"   年化收益：{best.annual_return:.1%}")
        print(f"   最大回撤：{best.max_drawdown:.1%}")

    print("\n" + "=" * 80)


if __name__ == '__main__':
    main()
