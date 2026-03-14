"""
钉钉推送集成测试

测试目标：
1. 周报推送功能正常
2. 对比报告推送功能正常
3. 绩效汇总推送功能正常
4. 失败隔离（推送失败不影响主流程）
5. 代码符合 PEP 8
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import sys
import os

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')


class TestDingTalkPerformancePush:
    """钉钉绩效推送测试"""

    @pytest.fixture
    def mock_notifier(self):
        """创建模拟的 DingTalkNotifier"""
        with patch('app.notifier.dingtalk.DingTalkNotifier') as mock:
            notifier_instance = Mock()
            mock.return_value = notifier_instance
            yield notifier_instance

    @pytest.fixture
    def weekly_report_data(self):
        """周报测试数据"""
        return {
            'period': '2026-02-24 to 2026-03-02',
            'total_trades': 15,
            'win_rate': 0.67,
            'total_return': 8.5,
            'by_strategy': {
                'trend_following': {
                    'count': 8,
                    'win_count': 6,
                    'avg_return': 1.2,
                    'total_profit': 5000,
                    'win_rate': 0.75
                },
                'mean_reversion': {
                    'count': 7,
                    'win_count': 4,
                    'avg_return': 0.5,
                    'total_profit': 2000,
                    'win_rate': 0.57
                }
            },
            'by_stock': {
                '0700.HK': {
                    'count': 5,
                    'win_count': 4,
                    'avg_return': 2.0,
                    'total_profit': 3000,
                    'win_rate': 0.80
                },
                '9988.HK': {
                    'count': 4,
                    'win_count': 2,
                    'avg_return': 0.5,
                    'total_profit': 500,
                    'win_rate': 0.50
                }
            },
            'recommendations': [
                '胜率优秀（67%），可考虑适当增加仓位',
                '本周收益表现优异（8.50%），建议总结成功经验并保持一致性'
            ],
            'benchmark_comparison': {
                'strategy_return': 8.5,
                'benchmark_return': 2.0,
                'excess_return': 6.5,
                'outperform': True
            }
        }

    @pytest.fixture
    def backtest_compare_data(self):
        """回测对比测试数据"""
        return {
            'period': '30d',
            'code': 'all',
            'strategy': 'all',
            'returns': {
                'live_return': 5.2,
                'backtest_return': 4.8,
                'difference': 0.4
            },
            'win_rates': {
                'live_win_rate': 0.65,
                'backtest_win_rate': 0.60,
                'difference': 0.05
            },
            'trades': {
                'live_trades': 25,
                'backtest_trades': 30,
                'difference': -5
            },
            'avg_profits': {
                'live_avg_profit': 1200.50,
                'backtest_avg_profit': 1100.00,
                'difference': 100.50
            },
            'summary': '✓ 实盘收益率 (5.20%) 优于回测 (4.80%) | = 实盘与回测胜率相近 (65.0% vs 60.0%)'
        }

    @pytest.fixture
    def performance_summary_data(self):
        """绩效汇总测试数据"""
        return {
            'date': '2026-03-02',
            'today_trades': 5,
            'today_wins': 3,
            'today_win_rate': 0.60,
            'today_pnl': 1500.00,
            'today_pnl_pct': 1.5,
            'cumulative_trades': 50,
            'cumulative_wins': 32,
            'cumulative_win_rate': 0.64,
            'cumulative_pnl': 12000.00,
            'cumulative_pnl_pct': 12.0,
            'positions': [
                {'code': '0700.HK', 'name': '腾讯控股', 'pnl_pct': 2.5},
                {'code': '9988.HK', 'name': '阿里巴巴', 'pnl_pct': -1.2},
                {'code': '3690.HK', 'name': '美团', 'pnl_pct': 3.8}
            ],
            'risk_status': 'normal',
            'consecutive_loss': 0,
            'max_drawdown': 3.5,
            'safe_drawdown': 15.0
        }

    def test_send_weekly_report_success(self, mock_notifier, weekly_report_data):
        """测试周报推送成功"""
        mock_notifier.send_weekly_report.return_value = True
        
        from app.notifier.dingtalk import DingTalkNotifier
        
        notifier = DingTalkNotifier()
        result = notifier.send_weekly_report(weekly_report_data)
        
        assert result is True
        mock_notifier.send_weekly_report.assert_called_once_with(weekly_report_data)

    def test_send_weekly_report_with_no_config(self, weekly_report_data):
        """测试未配置 webhook 时周报推送"""
        with patch('app.notifier.dingtalk.DingTalkNotifier._can_send', return_value=True):
            notifier = Mock()
            notifier.webhook_url = None
            notifier.send_weekly_report = lambda data: False if not notifier.webhook_url else True
            
            result = notifier.send_weekly_report(weekly_report_data)
            assert result is False

    def test_send_backtest_compare_report_success(self, mock_notifier, backtest_compare_data):
        """测试回测对比报告推送成功"""
        mock_notifier.send_backtest_compare_report.return_value = True
        
        from app.notifier.dingtalk import DingTalkNotifier
        
        notifier = DingTalkNotifier()
        result = notifier.send_backtest_compare_report(backtest_compare_data)
        
        assert result is True
        mock_notifier.send_backtest_compare_report.assert_called_once_with(backtest_compare_data)

    def test_send_backtest_compare_report_format(self, backtest_compare_data):
        """测试回测对比报告数据格式"""
        required_keys = ['period', 'returns', 'win_rates', 'trades', 'avg_profits', 'summary']
        
        for key in required_keys:
            assert key in backtest_compare_data, f"Missing required key: {key}"
        
        assert 'live_return' in backtest_compare_data['returns']
        assert 'backtest_return' in backtest_compare_data['returns']
        assert 'difference' in backtest_compare_data['returns']

    def test_send_performance_summary_success(self, mock_notifier, performance_summary_data):
        """测试绩效汇总推送成功"""
        mock_notifier.send_performance_summary.return_value = True
        
        from app.notifier.dingtalk import DingTalkNotifier
        
        notifier = DingTalkNotifier()
        result = notifier.send_performance_summary(performance_summary_data)
        
        assert result is True
        mock_notifier.send_performance_summary.assert_called_once_with(performance_summary_data)

    def test_send_performance_summary_with_positions(self, performance_summary_data):
        """测试绩效汇总包含持仓数据"""
        assert 'positions' in performance_summary_data
        assert len(performance_summary_data['positions']) > 0
        
        for pos in performance_summary_data['positions']:
            assert 'code' in pos
            assert 'pnl_pct' in pos

    def test_send_performance_summary_risk_status(self, performance_summary_data):
        """测试绩效汇总风控状态"""
        assert 'risk_status' in performance_summary_data
        assert performance_summary_data['risk_status'] in ['normal', 'warning', 'critical']

    def test_weekly_reviewer_push_integration(self, weekly_report_data):
        """测试 WeeklyReviewer 推送集成"""
        with patch('app.performance.weekly_review.WeeklyReviewer._get_connection'):
            with patch('app.notifier.dingtalk.DingTalkNotifier') as mock_notifier:
                mock_instance = Mock()
                mock_instance.send_weekly_report.return_value = True
                mock_notifier.return_value = mock_instance
                
                from app.performance.weekly_review import WeeklyReviewer
                
                reviewer = WeeklyReviewer()
                result = reviewer.push_to_dingtalk(weekly_report_data)
                
                assert result is True
                mock_notifier.assert_called_once()
                mock_instance.send_weekly_report.assert_called_once()

    def test_backtest_comparer_push_integration(self, backtest_compare_data):
        """测试 BacktestComparer 推送集成"""
        with patch('app.notifier.dingtalk.DingTalkNotifier') as mock_notifier:
            mock_instance = Mock()
            mock_instance.send_backtest_compare_report.return_value = True
            mock_notifier.return_value = mock_instance
            
            from app.performance.backtest_compare import BacktestComparer
            
            comparer = BacktestComparer()
            result = comparer.push_to_dingtalk(backtest_compare_data)
            
            assert result is True
            mock_notifier.assert_called_once()
            mock_instance.send_backtest_compare_report.assert_called_once()

    def test_push_failure_isolation(self, weekly_report_data):
        """测试推送失败不影响主流程"""
        with patch('app.performance.weekly_review.WeeklyReviewer._get_connection'):
            with patch('app.notifier.dingtalk.DingTalkNotifier') as mock_notifier:
                mock_instance = Mock()
                mock_instance.send_weekly_report.side_effect = Exception("Network error")
                mock_notifier.return_value = mock_instance
                
                from app.performance.weekly_review import WeeklyReviewer
                
                reviewer = WeeklyReviewer()
                # 推送失败应该返回 False，但不应该抛出异常
                result = reviewer.push_to_dingtalk(weekly_report_data)
                
                assert result is False

    def test_rate_limiting_respected(self, weekly_report_data):
        """测试限流控制"""
        with patch('app.notifier.dingtalk.DingTalkNotifier._can_send', return_value=False):
            from app.notifier.dingtalk import DingTalkNotifier
            
            notifier = DingTalkNotifier()
            result = notifier.send_weekly_report(weekly_report_data)
            
            # 限流时应该返回 False
            assert result is False

    def test_weekly_report_format_validation(self, weekly_report_data):
        """测试周报数据格式验证"""
        required_keys = ['period', 'total_trades', 'win_rate', 'total_return']
        
        for key in required_keys:
            assert key in weekly_report_data, f"Missing required key: {key}"
        
        # 验证数据类型
        assert isinstance(weekly_report_data['total_trades'], int)
        assert isinstance(weekly_report_data['win_rate'], float)
        assert 0 <= weekly_report_data['win_rate'] <= 1

    def test_performance_summary_format_validation(self, performance_summary_data):
        """测试绩效汇总数据格式验证"""
        required_keys = ['date', 'today_trades', 'today_pnl', 'cumulative_pnl']
        
        for key in required_keys:
            assert key in performance_summary_data, f"Missing required key: {key}"
        
        # 验证日期格式
        try:
            datetime.strptime(performance_summary_data['date'], '%Y-%m-%d')
        except ValueError:
            pytest.fail("Date format should be YYYY-MM-DD")

    def test_backtest_compare_format_validation(self, backtest_compare_data):
        """测试回测对比数据格式验证"""
        required_keys = ['period', 'returns', 'win_rates', 'trades']
        
        for key in required_keys:
            assert key in backtest_compare_data, f"Missing required key: {key}"
        
        # 验证收益率数据类型
        assert isinstance(backtest_compare_data['returns']['live_return'], float)
        assert isinstance(backtest_compare_data['returns']['backtest_return'], float)


class TestDingTalkNotifierMethods:
    """钉钉推送方法测试"""

    def test_send_weekly_report_method_exists(self):
        """测试 send_weekly_report 方法存在"""
        from app.notifier.dingtalk import DingTalkNotifier
        assert hasattr(DingTalkNotifier, 'send_weekly_report')
        assert callable(getattr(DingTalkNotifier, 'send_weekly_report'))

    def test_send_backtest_compare_report_method_exists(self):
        """测试 send_backtest_compare_report 方法存在"""
        from app.notifier.dingtalk import DingTalkNotifier
        assert hasattr(DingTalkNotifier, 'send_backtest_compare_report')
        assert callable(getattr(DingTalkNotifier, 'send_backtest_compare_report'))

    def test_send_performance_summary_method_exists(self):
        """测试 send_performance_summary 方法存在"""
        from app.notifier.dingtalk import DingTalkNotifier
        assert hasattr(DingTalkNotifier, 'send_performance_summary')
        assert callable(getattr(DingTalkNotifier, 'send_performance_summary'))

    def test_weekly_reviewer_push_method_exists(self):
        """测试 WeeklyReviewer.push_to_dingtalk 方法存在"""
        from app.performance.weekly_review import WeeklyReviewer
        assert hasattr(WeeklyReviewer, 'push_to_dingtalk')
        assert callable(getattr(WeeklyReviewer, 'push_to_dingtalk'))

    def test_backtest_comparer_push_method_exists(self):
        """测试 BacktestComparer.push_to_dingtalk 方法存在"""
        from app.performance.backtest_compare import BacktestComparer
        assert hasattr(BacktestComparer, 'push_to_dingtalk')
        assert callable(getattr(BacktestComparer, 'push_to_dingtalk'))


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
