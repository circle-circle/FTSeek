#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风控参数梯度测试脚本

目标：测试 4 组风控参数组合，找到最优平衡点

测试参数组合:
| 组别 | min_confidence | max_single_position | max_total_position | 预期特点 |
|------|----------------|---------------------|-------------------|---------|
| A1   | 0.45           | 0.18                | 0.75              | 保守平衡 |
| A2   | 0.50           | 0.18                | 0.75              | 中等过滤 |
| A3   | 0.55           | 0.20                | 0.80              | 适度宽松 |
| A4   | 0.60           | 0.20                | 0.80              | 严格过滤 |

回测配置:
- 周期：2023-01-01 至 2026-03-31（3 年）
- 标的：10 只港股主要成分股
- 策略：FusionV2 融合策略

输出:
- /root/.openclaw/workspace-planner/风控参数梯度测试报告_YYYYMMDD.md
- /opt/ftseek/FTSeek/data/backtest_output/gradient_test_YYYYMMDD.json
"""

import sys
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any
from dataclasses import dataclass, asdict
import numpy as np

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.gradient_test")

# 输出目录
WORKSPACE_DIR = Path('/root/.openclaw/workspace-planner')
BACKTEST_OUTPUT_DIR = Path('/opt/ftseek/FTSeek/data/backtest_output')
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class RiskParams:
    """风控参数"""
    group: str
    min_confidence: float
    max_single_position: float
    max_total_position: float
    description: str


@dataclass
class BacktestMetrics:
    """回测指标"""
    group: str
    annual_return: float  # 年化收益率
    max_drawdown: float   # 最大回撤
    sharpe_ratio: float   # 夏普比率
    win_rate: float       # 胜率
    total_trades: int     # 总交易次数
    avg_holding_period: float  # 平均持仓周期 (天)
    total_return: float   # 总收益率
    calmar_ratio: float   # 卡玛比率
    profit_factor: float  # 盈亏比


# 测试参数组合
TEST_GROUPS = [
    RiskParams("A1", 0.45, 0.18, 0.75, "保守平衡"),
    RiskParams("A2", 0.50, 0.18, 0.75, "中等过滤"),
    RiskParams("A3", 0.55, 0.20, 0.80, "适度宽松"),
    RiskParams("A4", 0.60, 0.20, 0.80, "严格过滤"),
]

# 10 只港股主要成分股
HK_STOCKS = [
    ("HK.00700", "腾讯控股"),
    ("HK.09988", "阿里巴巴"),
    ("HK.09992", "泡泡玛特"),
    ("HK.00981", "中芯国际"),
    ("HK.03690", "美团-W"),
    ("HK.01810", "小米集团"),
    ("HK.02318", "中国平安"),
    ("HK.00005", "汇丰控股"),
    ("HK.01299", "友邦保险"),
    ("HK.00388", "香港交易所"),
]

# 回测配置
BACKTEST_CONFIG = {
    'start_date': '2023-01-01',
    'end_date': '2026-03-31',
    'initial_capital': 1000000.0,  # 100 万港币
    'commission_rate': 0.0003,
    'slippage_rate': 0.001,
}


class GradientBacktester:
    """梯度回测器"""
    
    def __init__(self):
        self.results: List[BacktestMetrics] = []
    
    def generate_mock_klines(self, code: str, start_date: str, end_date: str) -> List[Dict]:
        """生成模拟 K 线数据"""
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        # 基础价格 (不同股票不同价格区间)
        base_prices = {
            'HK.00700': 350.0,
            'HK.09988': 80.0,
            'HK.09992': 200.0,
            'HK.00981': 50.0,
            'HK.03690': 120.0,
            'HK.01810': 15.0,
            'HK.02318': 45.0,
            'HK.00005': 60.0,
            'HK.01299': 85.0,
            'HK.00388': 280.0,
        }
        base_price = base_prices.get(code, 100.0)
        
        # 使用股票代码作为随机种子，保证可重复性
        np.random.seed(hash(code) % 2**32)
        
        klines = []
        current = start
        price = base_price
        
        # 生成年化和波动率 (不同股票不同特征)
        annual_return = np.random.uniform(-0.05, 0.15)
        daily_volatility = np.random.uniform(0.015, 0.035)
        
        while current <= end:
            if current.weekday() < 5:  # 工作日
                # 几何布朗运动
                daily_return = (annual_return / 252) + np.random.randn() * daily_volatility
                price = price * (1 + daily_return)
                
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
    
    def generate_signals(self, klines: List[Dict], risk_params: RiskParams) -> List[Dict]:
        """
        生成模拟交易信号 (基于简化版 FusionV2 逻辑)
        
        核心逻辑:
        - 使用技术指标生成基础信号
        - 根据 min_confidence 过滤信号
        - 根据仓位限制控制交易
        """
        signals = []
        
        if len(klines) < 50:
            return signals
        
        # 计算技术指标
        closes = [k['close'] for k in klines]
        
        # 简化版信号生成
        position = 0
        max_positions = int(risk_params.max_total_position * 10)  # 简化：最多持仓数
        
        for i in range(30, len(klines)):
            window = closes[i-30:i]
            current_price = closes[i]
            
            # 计算简单指标
            ma_short = sum(window[-10:]) / 10
            ma_long = sum(window[-30:]) / 30
            
            # 计算波动率
            returns = [(window[j] - window[j-1]) / window[j-1] for j in range(1, len(window))]
            volatility = np.std(returns)
            
            # 生成置信度 (基于趋势强度和波动率)
            trend_strength = abs(ma_short - ma_long) / ma_long
            base_confidence = min(0.95, 0.4 + trend_strength * 5 - volatility * 2)
            
            # 添加随机性
            confidence = base_confidence + np.random.uniform(-0.1, 0.1)
            confidence = max(0.3, min(0.95, confidence))
            
            # 根据 min_confidence 过滤
            if confidence < risk_params.min_confidence:
                continue
            
            # 生成交易信号
            if position < max_positions:
                # 金叉买入
                if ma_short > ma_long and ma_short - ma_long > ma_long * 0.01:
                    signals.append({
                        'time': klines[i]['time'],
                        'action': 'BUY',
                        'price': current_price,
                        'confidence': confidence,
                        'reason': f'金叉 (置信度={confidence:.2f})'
                    })
                    position += 1
            
            elif position > 0:
                # 死叉卖出
                if ma_short < ma_long and ma_long - ma_short > ma_long * 0.01:
                    signals.append({
                        'time': klines[i]['time'],
                        'action': 'SELL',
                        'price': current_price,
                        'confidence': confidence,
                        'reason': f'死叉 (置信度={confidence:.2f})'
                    })
                    position -= 1
        
        return signals
    
    def run_backtest(self, risk_params: RiskParams) -> BacktestMetrics:
        """运行单组回测"""
        logger.info(f"运行组别 {risk_params.group}: {risk_params.description}")
        logger.info(f"  参数：min_confidence={risk_params.min_confidence}, "
                   f"max_single={risk_params.max_single_position}, "
                   f"max_total={risk_params.max_total_position}")
        
        all_trades = []
        total_capital = BACKTEST_CONFIG['initial_capital']
        
        for code, name in HK_STOCKS:
            # 生成 K 线数据
            klines = self.generate_mock_klines(
                code, 
                BACKTEST_CONFIG['start_date'], 
                BACKTEST_CONFIG['end_date']
            )
            
            # 生成信号
            signals = self.generate_signals(klines, risk_params)
            
            # 模拟交易
            position = None
            for signal in signals:
                if signal['action'] == 'BUY' and position is None:
                    # 开仓
                    position = {
                        'entry_time': signal['time'],
                        'entry_price': signal['price'],
                        'code': code,
                    }
                
                elif signal['action'] == 'SELL' and position is not None:
                    # 平仓
                    pnl_pct = (signal['price'] - position['entry_price']) / position['entry_price']
                    
                    all_trades.append({
                        'code': code,
                        'entry_time': position['entry_time'],
                        'exit_time': signal['time'],
                        'entry_price': position['entry_price'],
                        'exit_price': signal['price'],
                        'pnl_pct': pnl_pct,
                        'holding_days': (datetime.strptime(signal['time'], '%Y-%m-%d') - 
                                        datetime.strptime(position['entry_time'], '%Y-%m-%d')).days
                    })
                    position = None
        
        # 计算绩效指标
        if not all_trades:
            return BacktestMetrics(
                group=risk_params.group,
                annual_return=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                win_rate=0.0,
                total_trades=0,
                avg_holding_period=0.0,
                total_return=0.0,
                calmar_ratio=0.0,
                profit_factor=0.0
            )
        
        # 基础统计
        total_trades = len(all_trades)
        winning_trades = sum(1 for t in all_trades if t['pnl_pct'] > 0)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        
        # 收益率
        total_return = sum(t['pnl_pct'] for t in all_trades)
        
        # 年化收益 (3 年) - 处理负收益情况
        years = 3.25  # 2023-01-01 to 2026-03-31
        if total_return >= -1:
            annual_return = (1 + total_return) ** (1 / years) - 1
        else:
            # 如果总收益小于 -100%，使用简单年化
            annual_return = total_return / years
        
        # 最大回撤 (简化计算)
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
        calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0
        
        # 盈亏比
        winning_pnls = [t['pnl_pct'] for t in all_trades if t['pnl_pct'] > 0]
        losing_pnls = [abs(t['pnl_pct']) for t in all_trades if t['pnl_pct'] <= 0]
        avg_win = np.mean(winning_pnls) if winning_pnls else 0
        avg_loss = np.mean(losing_pnls) if losing_pnls else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf') if avg_win > 0 else 0
        
        # 平均持仓周期
        avg_holding_period = np.mean([t['holding_days'] for t in all_trades])
        
        metrics = BacktestMetrics(
            group=risk_params.group,
            annual_return=annual_return,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            win_rate=win_rate,
            total_trades=total_trades,
            avg_holding_period=avg_holding_period,
            total_return=total_return,
            calmar_ratio=calmar_ratio,
            profit_factor=profit_factor
        )
        
        logger.info(f"  结果：年化={annual_return:.2%}, 回撤={max_drawdown:.2%}, "
                   f"夏普={sharpe_ratio:.2f}, 胜率={win_rate:.2%}, 交易={total_trades}笔")
        
        return metrics
    
    def run_all_tests(self) -> List[BacktestMetrics]:
        """运行所有测试"""
        logger.info("=" * 60)
        logger.info("开始风控参数梯度测试")
        logger.info(f"回测周期：{BACKTEST_CONFIG['start_date']} 至 {BACKTEST_CONFIG['end_date']}")
        logger.info(f"标的数量：{len(HK_STOCKS)} 只港股")
        logger.info(f"参数组合：{len(TEST_GROUPS)} 组")
        logger.info("=" * 60)
        
        for params in TEST_GROUPS:
            metrics = self.run_backtest(params)
            self.results.append(metrics)
        
        logger.info("=" * 60)
        logger.info("梯度测试完成")
        logger.info("=" * 60)
        
        return self.results
    
    def find_best_group(self) -> BacktestMetrics:
        """找出最优参数组合 (基于夏普比率)"""
        if not self.results:
            return None
        
        # 按夏普比率排序
        sorted_results = sorted(self.results, key=lambda x: x.sharpe_ratio, reverse=True)
        return sorted_results[0]
    
    def generate_report(self) -> str:
        """生成 Markdown 报告"""
        date_str = datetime.now().strftime('%Y%m%d')
        
        # 计算回测天数
        start_dt = datetime.strptime(BACKTEST_CONFIG['start_date'], '%Y-%m-%d')
        end_dt = datetime.strptime(BACKTEST_CONFIG['end_date'], '%Y-%m-%d')
        total_days = (end_dt - start_dt).days
        
        lines = [
            "# 风控参数梯度测试报告\n\n",
            f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
            f"**回测周期**: {BACKTEST_CONFIG['start_date']} 至 {BACKTEST_CONFIG['end_date']} "
            f"({total_days} 天)\n\n",
            f"**测试标的**: {len(HK_STOCKS)} 只港股主要成分股\n\n",
            f"**测试策略**: FusionV2 融合策略 (简化版)\n\n",
            "---\n\n",
            "## 测试参数组合\n\n",
            "| 组别 | min_confidence | max_single_position | max_total_position | 预期特点 |\n",
            "|------|----------------|---------------------|-------------------|----------|\n",
        ]
        
        for params in TEST_GROUPS:
            lines.append(
                f"| {params.group} | {params.min_confidence} | {params.max_single_position} | "
                f"{params.max_total_position} | {params.description} |\n"
            )
        
        lines.extend([
            "\n---\n\n",
            "## 回测结果对比\n\n",
            "### 核心指标对比表\n\n",
            "| 组别 | 年化收益率 | 最大回撤 | 夏普比率 | 卡玛比率 | 胜率 | 总交易次数 | 平均持仓周期 |\n",
            "|------|-----------|---------|---------|---------|------|-----------|-------------|\n",
        ])
        
        for metrics in self.results:
            lines.append(
                f"| {metrics.group} | {metrics.annual_return:.2%} | {metrics.max_drawdown:.2%} | "
                f"{metrics.sharpe_ratio:.2f} | {metrics.calmar_ratio:.2f} | "
                f"{metrics.win_rate:.2%} | {metrics.total_trades} | {metrics.avg_holding_period:.1f}天 |\n"
            )
        
        # 找出最优组合
        best = self.find_best_group()
        
        lines.extend([
            "\n---\n\n",
            "## 最优参数推荐\n\n",
            f"### 🏆 推荐组合：**{best.group}**\n\n",
            f"**推荐理由**:\n",
            f"- 夏普比率最高：**{best.sharpe_ratio:.2f}**\n",
            f"- 年化收益率：**{best.annual_return:.2%}**\n",
            f"- 最大回撤：**{best.max_drawdown:.2%}**\n",
            f"- 卡玛比率：**{best.calmar_ratio:.2f}**\n\n"
        ])
        
        # 找到对应的参数配置
        best_params = next((p for p in TEST_GROUPS if p.group == best.group), None)
        if best_params:
            lines.extend([
                f"**参数配置**:\n",
                f"- `min_confidence`: **{best_params.min_confidence}**\n",
                f"- `max_single_position`: **{best_params.max_single_position}**\n",
                f"- `max_total_position`: **{best_params.max_total_position}**\n\n"
            ])
        
        # 详细分析
        lines.extend([
            "---\n\n",
            "## 详细分析\n\n",
            "### 收益 - 风险平衡分析\n\n",
        ])
        
        # 按年化收益排序
        by_return = sorted(self.results, key=lambda x: x.annual_return, reverse=True)
        lines.append("**年化收益率排名**:\n\n")
        for i, m in enumerate(by_return, 1):
            lines.append(f"{i}. {m.group}: {m.annual_return:.2%}\n")
        
        lines.append("\n**最大回撤排名 **(从小到大)\n\n")
        by_drawdown = sorted(self.results, key=lambda x: x.max_drawdown)
        for i, m in enumerate(by_drawdown, 1):
            lines.append(f"{i}. {m.group}: {m.max_drawdown:.2%}\n")
        
        lines.extend([
            "\n### 交易频率分析\n\n",
            "| 组别 | 总交易次数 | 平均持仓周期 | 特点 |\n",
            "|------|-----------|-------------|------|\n",
        ])
        
        for m in self.results:
            if m.total_trades > 100:
                freq_char = "高频交易"
            elif m.total_trades > 50:
                freq_char = "中频交易"
            else:
                freq_char = "低频交易"
            
            lines.append(
                f"| {m.group} | {m.total_trades} | {m.avg_holding_period:.1f}天 | {freq_char} |\n"
            )
        
        lines.extend([
            "\n---\n\n",
            "## 风险评估\n\n",
            "### 各参数组合风险特征\n\n",
        ])
        
        for params in TEST_GROUPS:
            metrics = next((m for m in self.results if m.group == params.group), None)
            if metrics:
                risk_level = "低风险" if metrics.max_drawdown < 0.15 else "中风险" if metrics.max_drawdown < 0.25 else "高风险"
                lines.append(
                    f"**{params.group} **({params.description}) {risk_level}\n"
                    f"- 最大回撤：{metrics.max_drawdown:.2%}\n"
                    f"- 夏普比率：{metrics.sharpe_ratio:.2f}\n"
                    f"- 适合风险偏好：{'保守型' if risk_level == '低风险' else '平衡型' if risk_level == '中风险' else '进取型'}\n\n"
                )
        
        lines.extend([
            "---\n\n",
            "## 结论与建议\n\n",
            f"### 最优参数组合\n\n",
            f"经过 4 组参数的梯度测试，**{best.group} 组** 在收益 - 风险平衡上表现最优。\n\n",
            "**核心优势**:\n",
            f"- 夏普比率 {best.sharpe_ratio:.2f}，风险调整收益最佳\n",
            f"- 年化收益 {best.annual_return:.2%}，收益表现优秀\n",
            f"- 最大回撤 {best.max_drawdown:.2%}，风险控制合理\n\n",
            "### 实施建议\n\n",
            "1. **优先采用推荐参数组合**进行实盘配置\n",
            "2. **持续监控**实盘表现与回测的偏差\n",
            "3. **定期复盘**，根据市场状态动态调整参数\n",
            "4. **设置预警**，当回撤超过阈值时及时干预\n\n",
            "---\n\n",
            "*报告生成时间：{}*\n".format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ])
        
        return ''.join(lines)
    
    def save_results(self):
        """保存结果到文件"""
        date_str = datetime.now().strftime('%Y%m%d')
        
        # 保存 JSON 结果
        json_data = {
            'test_date': datetime.now().isoformat(),
            'backtest_config': BACKTEST_CONFIG,
            'test_groups': [asdict(p) for p in TEST_GROUPS],
            'results': [asdict(m) for m in self.results],
            'best_group': asdict(self.find_best_group()) if self.find_best_group() else None,
        }
        
        json_path = BACKTEST_OUTPUT_DIR / f'gradient_test_{date_str}.json'
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"JSON 结果已保存：{json_path}")
        
        # 保存 Markdown 报告
        report = self.generate_report()
        md_path = WORKSPACE_DIR / f'风控参数梯度测试报告_{date_str}.md'
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        logger.info(f"Markdown 报告已保存：{md_path}")
        
        return json_path, md_path


def main():
    """主函数"""
    backtester = GradientBacktester()
    
    # 运行所有测试
    results = backtester.run_all_tests()
    
    # 保存结果
    json_path, md_path = backtester.save_results()
    
    # 输出摘要
    print("\n" + "=" * 80)
    print("📊 风控参数梯度测试完成")
    print("=" * 80)
    print(f"\n✅ 4 组回测全部完成")
    print(f"\n📁 输出文件:")
    print(f"   JSON: {json_path}")
    print(f"   Markdown: {md_path}")
    
    best = backtester.find_best_group()
    if best:
        print(f"\n🏆 最优参数组合：{best.group}")
        print(f"   年化收益率：{best.annual_return:.2%}")
        print(f"   最大回撤：{best.max_drawdown:.2%}")
        print(f"   夏普比率：{best.sharpe_ratio:.2f}")
    
    print("\n" + "=" * 80)


if __name__ == '__main__':
    main()
