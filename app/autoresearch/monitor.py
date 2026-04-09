"""
AutoResearch 监控模块

监控优化后策略的实盘/模拟盘表现。
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger("ftseek.autoresearch.monitor")
logger.propagate = False


class StrategyMonitor:
    """
    策略监控器
    
    监控优化后策略的实盘/模拟盘表现。
    
    使用示例:
        monitor = StrategyMonitor()
        performance = monitor.get_performance('confluence_v2', days=7)
        report = monitor.generate_report('confluence_v2')
    """
    
    def __init__(self):
        """初始化监控器"""
        self.data_dir = Path("/opt/ftseek/FTSeek/data/autoresearch")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("ftseek.autoresearch.monitor")
    
    def get_performance(self, strategy_name: str, days: int = 7) -> Dict[str, Any]:
        """
        获取策略表现
        
        Args:
            strategy_name: 策略名称
            days: 天数
        
        Returns:
            表现数据
        """
        # TODO: 从数据库或日志中获取实际表现数据
        # 这里使用模拟数据
        
        return {
            'strategy_name': strategy_name,
            'period_days': days,
            'start_date': (datetime.now() - timedelta(days=days)).isoformat(),
            'end_date': datetime.now().isoformat(),
            'total_trades': 15,
            'winning_trades': 10,
            'losing_trades': 5,
            'win_rate': 0.667,
            'total_pnl': 12500.0,
            'total_return': 0.125,
            'sharpe_ratio': 1.65,
            'max_drawdown': 0.085,
            'avg_win': 1800.0,
            'avg_loss': 900.0,
            'profit_factor': 2.0,
        }
    
    def compare_with_backtest(self, strategy_name: str, days: int = 7) -> Dict[str, Any]:
        """
        对比实盘表现与回测结果
        
        Args:
            strategy_name: 策略名称
            days: 天数
        
        Returns:
            对比数据
        """
        # 获取实盘表现
        live_performance = self.get_performance(strategy_name, days)
        
        # 获取回测结果
        backtest_result = self._load_backtest_result(strategy_name)
        
        # 计算差异
        win_rate_diff = live_performance['win_rate'] - backtest_result.get('win_rate', 0)
        sharpe_diff = live_performance['sharpe_ratio'] - backtest_result.get('sharpe_ratio', 0)
        drawdown_diff = live_performance['max_drawdown'] - backtest_result.get('max_drawdown', 0)
        
        return {
            'strategy_name': strategy_name,
            'period_days': days,
            'live_performance': live_performance,
            'backtest_performance': backtest_result,
            'differences': {
                'win_rate_diff': win_rate_diff,
                'sharpe_diff': sharpe_diff,
                'drawdown_diff': drawdown_diff,
            },
            'assessment': self._assess_performance(win_rate_diff, sharpe_diff, drawdown_diff),
        }
    
    def _load_backtest_result(self, strategy_name: str) -> Dict[str, Any]:
        """
        加载回测结果
        
        Args:
            strategy_name: 策略名称
        
        Returns:
            回测结果
        """
        # 查找最新的回测结果文件
        result_files = list(self.data_dir.glob(f"{strategy_name}_*.json"))
        
        if not result_files:
            return {}
        
        # 取最新的文件
        latest_file = max(result_files, key=lambda f: f.stat().st_mtime)
        
        with open(latest_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _assess_performance(self, win_rate_diff: float, sharpe_diff: float, drawdown_diff: float) -> str:
        """
        评估表现
        
        Args:
            win_rate_diff: 胜率差异
            sharpe_diff: 夏普差异
            drawdown_diff: 回撤差异
        
        Returns:
            评估结果
        """
        # 如果实盘表现接近回测 (差异 < 10%)
        if abs(win_rate_diff) < 0.1 and abs(sharpe_diff) < 0.2 and abs(drawdown_diff) < 0.05:
            return "✅ 实盘表现与回测一致"
        # 如果实盘表现优于回测
        elif win_rate_diff > 0.05 and sharpe_diff > 0.2:
            return "🎉 实盘表现优于回测"
        # 如果实盘表现差于回测
        elif win_rate_diff < -0.1 or sharpe_diff < -0.3:
            return "⚠️ 实盘表现差于回测，需关注"
        else:
            return "📊 实盘表现正常"
    
    def generate_report(self, strategy_name: str, days: int = 7) -> str:
        """
        生成监控报告
        
        Args:
            strategy_name: 策略名称
            days: 天数
        
        Returns:
            报告文本
        """
        comparison = self.compare_with_backtest(strategy_name, days)
        
        lines = [
            "=" * 70,
            f"AutoResearch 监控报告 - {strategy_name}",
            "=" * 70,
            "",
            f"监控周期：{days}天",
            f"报告时间：{datetime.now().isoformat()}",
            "",
            "实盘表现:",
            "-" * 70,
        ]
        
        live = comparison['live_performance']
        lines.extend([
            f"  交易次数：{live['total_trades']}",
            f"  胜率：{live['win_rate']:.1%}",
            f"  总收益：{live['total_return']:.1%}",
            f"  夏普比率：{live['sharpe_ratio']:.2f}",
            f"  最大回撤：{live['max_drawdown']:.1%}",
            f"  盈亏比：{live['profit_factor']:.2f}",
        ])
        
        lines.extend([
            "",
            "回测表现:",
            "-" * 70,
        ])
        
        backtest = comparison['backtest_performance']
        if backtest:
            lines.extend([
                f"  胜率：{backtest.get('win_rate', 0):.1%}",
                f"  夏普比率：{backtest.get('sharpe_ratio', 0):.2f}",
                f"  最大回撤：{backtest.get('max_drawdown', 0):.1%}",
            ])
        else:
            lines.append("  无回测数据")
        
        lines.extend([
            "",
            "差异分析:",
            "-" * 70,
        ])
        
        diffs = comparison['differences']
        lines.extend([
            f"  胜率差异：{diffs['win_rate_diff']:+.1%}",
            f"  夏普差异：{diffs['sharpe_diff']:+.2f}",
            f"  回撤差异：{diffs['drawdown_diff']:+.1%}",
        ])
        
        lines.extend([
            "",
            "评估:",
            "-" * 70,
            f"  {comparison['assessment']}",
            "",
            "=" * 70,
        ])
        
        report = "\n".join(lines)
        
        # 保存报告
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{strategy_name}_monitor_{timestamp}.md"
        filepath = self.data_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)
        
        self.logger.info(f"Report saved to: {filepath}")
        
        return report
    
    def check_alerts(self, strategy_name: str) -> List[str]:
        """
        检查告警
        
        Args:
            strategy_name: 策略名称
        
        Returns:
            告警列表
        """
        alerts = []
        
        # 获取实盘表现
        performance = self.get_performance(strategy_name, days=1)
        
        # 检查胜率
        if performance['win_rate'] < 0.4:
            alerts.append(f"⚠️ {strategy_name} 胜率过低：{performance['win_rate']:.1%}")
        
        # 检查回撤
        if performance['max_drawdown'] > 0.15:
            alerts.append(f"⚠️ {strategy_name} 回撤过大：{performance['max_drawdown']:.1%}")
        
        # 检查交易次数
        if performance['total_trades'] < 5:
            alerts.append(f"ℹ️ {strategy_name} 交易次数较少：{performance['total_trades']}")
        
        return alerts
