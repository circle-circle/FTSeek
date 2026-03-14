"""StrategyGraduator 单元测试

测试策略晋级器的核心功能：
1. 绩效追踪：记录每日收益率
2. 晋级条件：连续 3 天收益>5% → 发送通知
3. 降级条件：连续 2 天亏损 → 发送通知
4. 人工切换：提供 API 供用户手动切换模式
5. 钉钉通知：达到条件时推送建议

约束:
- 不自动切换模式，仅发送通知
- 钉钉通知包含建议和操作指引
- 用户通过 API 或钉钉指令手动确认
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from typing import Optional, List
import tempfile
import os

from app.v4.core.graduator import (
    StrategyGraduator,
    GraduationSuggestion,
    SuggestionType,
    PerformanceRecord
)
from app.v4.models.trading_models import TradingMode


class TestGraduationSuggestion:
    """测试 GraduationSuggestion 数据类"""
    
    def test_graduation_suggestion_creation(self):
        """测试建议创建"""
        suggestion = GraduationSuggestion(
            code='0700.HK',
            suggestion_type=SuggestionType.GRADUATE,
            current_mode=TradingMode.PAPER,
            suggested_mode=TradingMode.HYBRID,
            reason='连续 3 天收益率>5%',
            consecutive_days=3,
            avg_return=0.06
        )
        assert suggestion.code == '0700.HK'
        assert suggestion.suggestion_type == SuggestionType.GRADUATE
        assert suggestion.current_mode == TradingMode.PAPER
        assert suggestion.suggested_mode == TradingMode.HYBRID
        assert suggestion.consecutive_days == 3
        assert suggestion.avg_return == 0.06
    
    def test_graduation_suggestion_demote(self):
        """测试降级建议"""
        suggestion = GraduationSuggestion(
            code='MU',
            suggestion_type=SuggestionType.DEMOTE,
            current_mode=TradingMode.HYBRID,
            suggested_mode=TradingMode.PAPER,
            reason='连续 2 天亏损',
            consecutive_days=2,
            avg_return=-0.03
        )
        assert suggestion.suggestion_type == SuggestionType.DEMOTE
        assert suggestion.consecutive_days == 2


class TestPerformanceRecord:
    """测试 PerformanceRecord 数据类"""
    
    def test_performance_record_creation(self):
        """测试绩效记录创建"""
        record = PerformanceRecord(
            code='0700.HK',
            date='2025-03-10',
            daily_return=0.06,
            created_at=datetime.now()
        )
        assert record.code == '0700.HK'
        assert record.daily_return == 0.06
        assert record.is_profitable() is True
        assert record.meets_graduation_threshold() is True
    
    def test_performance_record_loss(self):
        """测试亏损记录"""
        record = PerformanceRecord(
            code='MU',
            date='2025-03-11',
            daily_return=-0.02,
            created_at=datetime.now()
        )
        assert record.is_profitable() is False
        assert record.meets_graduation_threshold() is False


class TestStrategyGraduator:
    """测试 StrategyGraduator 核心功能"""
    
    @pytest.fixture
    def test_db_path(self, tmp_path):
        """创建临时数据库路径"""
        return str(tmp_path / "test_graduator.db")
    
    @pytest.fixture
    def mock_dingtalk_webhook(self):
        """模拟钉钉 webhook"""
        return "https://oapi.dingtalk.com/robot/send?access_token=test"
    
    @pytest.fixture
    def graduator(self, test_db_path, mock_dingtalk_webhook):
        """创建测试用晋级器实例（禁用通知）"""
        return StrategyGraduator(
            db_path=test_db_path,
            dingtalk_webhook=None  # 测试时禁用通知
        )
    
    @pytest.fixture
    def graduator_with_notify(self, test_db_path, mock_dingtalk_webhook):
        """创建带通知的晋级器实例"""
        return StrategyGraduator(
            db_path=test_db_path,
            dingtalk_webhook=mock_dingtalk_webhook
        )
    
    @pytest.fixture
    def graduator_no_notify(self, test_db_path):
        """创建无通知的晋级器实例"""
        return StrategyGraduator(db_path=test_db_path)
    
    # ==================== 测试初始化 ====================
    
    def test_init(self, test_db_path, mock_dingtalk_webhook):
        """测试初始化"""
        graduator = StrategyGraduator(
            db_path=test_db_path,
            dingtalk_webhook=mock_dingtalk_webhook
        )
        assert graduator.db_path == test_db_path
        assert graduator.dingtalk_webhook == mock_dingtalk_webhook
        assert graduator.db is not None
    
    def test_init_without_webhook(self, test_db_path):
        """测试无 webhook 初始化"""
        graduator = StrategyGraduator(db_path=test_db_path)
        assert graduator.dingtalk_webhook is None
    
    # ==================== 测试绩效记录 ====================
    
    def test_record_daily_performance(self, graduator):
        """测试记录每日绩效"""
        today = datetime.now().strftime('%Y-%m-%d')
        result = graduator.record_daily_performance(
            code='0700.HK',
            date=today,
            daily_return=0.06
        )
        assert result is True
        
        # 验证记录已保存
        history = graduator.get_performance_history(code='0700.HK', days=30)
        assert len(history) == 1
        assert history[0].code == '0700.HK'
        assert history[0].daily_return == 0.06
    
    def test_record_daily_performance_duplicate(self, graduator):
        """测试重复记录同一天绩效（应更新）"""
        today = datetime.now().strftime('%Y-%m-%d')
        graduator.record_daily_performance(
            code='0700.HK',
            date=today,
            daily_return=0.06
        )
        
        # 更新同一天的记录
        result = graduator.record_daily_performance(
            code='0700.HK',
            date=today,
            daily_return=0.08
        )
        assert result is True
        
        # 验证记录已更新
        history = graduator.get_performance_history(code='0700.HK', days=30)
        assert len(history) == 1
        assert history[0].daily_return == 0.08
    
    def test_record_multiple_days(self, graduator):
        """测试记录多天绩效"""
        for i in range(5):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.03 + i * 0.01
            )
        
        history = graduator.get_performance_history(code='0700.HK', days=30)
        assert len(history) == 5
    
    # ==================== 测试晋级条件 ====================
    
    def test_check_graduation_consecutive_profit(self, graduator):
        """测试连续盈利触发晋级"""
        # 记录连续 3 天>5% 收益
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.06  # 6% > 5%
            )
        
        suggestion = graduator.check_graduation(code='0700.HK')
        
        assert suggestion is not None
        assert suggestion.code == '0700.HK'
        assert suggestion.suggestion_type == SuggestionType.GRADUATE
        assert suggestion.consecutive_days == 3
        assert suggestion.current_mode == TradingMode.PAPER
        assert suggestion.suggested_mode == TradingMode.HYBRID
    
    def test_check_graduation_not_enough_days(self, graduator):
        """测试连续盈利天数不足不触发"""
        # 记录连续 2 天>5% 收益
        for i in range(2):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.06
            )
        
        suggestion = graduator.check_graduation(code='0700.HK')
        assert suggestion is None
    
    def test_check_graduation_below_threshold(self, graduator):
        """测试收益率不足不触发"""
        # 记录连续 3 天但收益<5%
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.04  # 4% < 5%
            )
        
        suggestion = graduator.check_graduation(code='0700.HK')
        assert suggestion is None
    
    def test_check_graduation_broken_streak(self, graduator):
        """测试连续盈利中断不触发"""
        # 2 天盈利 + 1 天亏损 + 2 天盈利
        dates_returns = [
            (0, 0.06),   # 今天 6%
            (1, 0.07),   # 昨天 7%
            (2, -0.02),  # 前天 -2% (中断)
            (3, 0.08),   # 大前天 8%
            (4, 0.09),   # 大大前天 9%
        ]
        
        for days_ago, ret in dates_returns:
            date = (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=ret
            )
        
        suggestion = graduator.check_graduation(code='0700.HK')
        # 最近连续只有 2 天，不应触发
        assert suggestion is None
    
    # ==================== 测试降级条件 ====================
    
    def test_check_graduation_consecutive_loss(self, graduator):
        """测试连续亏损触发降级"""
        # 先设置为 HYBRID 模式（PAPER 无法降级）
        graduator.db.create_trading_mode('MU', TradingMode.HYBRID)
        
        # 记录连续 2 天亏损
        for i in range(2):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='MU',
                date=date,
                daily_return=-0.03  # 亏损
            )
        
        suggestion = graduator.check_graduation(code='MU')
        
        assert suggestion is not None
        assert suggestion.code == 'MU'
        assert suggestion.suggestion_type == SuggestionType.DEMOTE
        assert suggestion.consecutive_days == 2
        assert suggestion.suggested_mode == TradingMode.PAPER
    
    def test_check_graduation_single_loss(self, graduator):
        """测试单日亏损不触发降级"""
        graduator.record_daily_performance(
            code='MU',
            date=datetime.now().strftime('%Y-%m-%d'),
            daily_return=-0.03
        )
        
        suggestion = graduator.check_graduation(code='MU')
        assert suggestion is None
    
    # ==================== 测试建议模式 ====================
    
    def test_get_suggested_mode_graduate(self, graduator):
        """测试获取建议模式 - 晋级"""
        # 设置当前为 PAPER 模式
        graduator.db.create_trading_mode('0700.HK', TradingMode.PAPER)
        
        # 记录连续 3 天>5% 收益
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.06
            )
        
        mode = graduator.get_suggested_mode(code='0700.HK')
        assert mode == TradingMode.HYBRID.value
    
    def test_get_suggested_mode_demote(self, graduator):
        """测试获取建议模式 - 降级"""
        # 设置当前为 HYBRID 模式
        graduator.db.create_trading_mode('MU', TradingMode.HYBRID)
        
        # 记录连续 2 天亏损
        for i in range(2):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='MU',
                date=date,
                daily_return=-0.03
            )
        
        mode = graduator.get_suggested_mode(code='MU')
        assert mode == TradingMode.PAPER.value
    
    def test_get_suggested_mode_no_change(self, graduator):
        """测试无变化时的建议模式"""
        # 设置当前为 PAPER 模式
        graduator.db.create_trading_mode('0700.HK', TradingMode.PAPER)
        
        # 记录普通收益
        graduator.record_daily_performance(
            code='0700.HK',
            date=datetime.now().strftime('%Y-%m-%d'),
            daily_return=0.02
        )
        
        mode = graduator.get_suggested_mode(code='0700.HK')
        assert mode == TradingMode.PAPER.value
    
    # ==================== 测试人工切换 ====================
    
    def test_switch_mode(self, graduator):
        """测试手动切换模式"""
        # 初始为 PAPER
        initial_mode = graduator.db.get_trading_mode('0700.HK')
        assert initial_mode is None  # 默认无记录
        
        # 切换到 HYBRID
        result = graduator.switch_mode(
            code='0700.HK',
            new_mode=TradingMode.HYBRID.value,
            confirmed_by='user_123'
        )
        assert result is True
        
        # 验证模式已切换
        mode = graduator.db.get_trading_mode('0700.HK')
        assert mode is not None
        assert mode.mode == TradingMode.HYBRID
    
    def test_switch_mode_invalid_mode(self, graduator):
        """测试切换到无效模式"""
        with pytest.raises(ValueError, match="Invalid trading mode"):
            graduator.switch_mode(
                code='0700.HK',
                new_mode='INVALID_MODE',
                confirmed_by='user_123'
            )
    
    def test_switch_mode_records_history(self, graduator):
        """测试切换模式记录历史"""
        # 先设置为 PAPER
        graduator.db.create_trading_mode('0700.HK', TradingMode.PAPER)
        
        # 切换到 HYBRID
        graduator.switch_mode(
            code='0700.HK',
            new_mode=TradingMode.HYBRID.value,
            confirmed_by='user_123'
        )
        
        # 验证晋级历史
        history = graduator.db.get_graduation_history_by_code('0700.HK')
        assert len(history) == 1
        assert history[0].from_mode == TradingMode.PAPER
        assert history[0].to_mode == TradingMode.HYBRID
        assert 'user_123' in history[0].reason
    
    # ==================== 测试钉钉通知 ====================
    
    @patch('app.v4.core.graduator.DingTalkNotifierV4')
    def test_send_dingtalk_notification_graduate(self, mock_notifier_class, test_db_path, mock_dingtalk_webhook):
        """测试发送钉钉通知 - 晋级"""
        mock_notifier = Mock()
        mock_notifier.send.return_value = {'success': True, 'result': {'errcode': 0}}
        mock_notifier_class.return_value = mock_notifier
        
        # 创建带通知的晋级器
        graduator = StrategyGraduator(
            db_path=test_db_path,
            dingtalk_webhook=mock_dingtalk_webhook
        )
        # 替换 notifier 为 mock
        graduator.notifier = mock_notifier
        
        suggestion = GraduationSuggestion(
            code='0700.HK',
            suggestion_type=SuggestionType.GRADUATE,
            current_mode=TradingMode.PAPER,
            suggested_mode=TradingMode.HYBRID,
            reason='连续 3 天收益率>5%',
            consecutive_days=3,
            avg_return=0.06
        )
        
        result = graduator._send_dingtalk_notification(suggestion)
        
        assert result is True
        mock_notifier.send.assert_called_once()
        
        # 验证消息内容包含关键信息
        call_args = mock_notifier.send.call_args
        message = call_args[0][0]
        assert '0700.HK' in message
        assert '晋级' in message or 'HYBRID' in message
        assert '连续 3 天' in message
    
    @patch('app.v4.core.graduator.DingTalkNotifierV4')
    def test_send_dingtalk_notification_demote(self, mock_notifier_class, test_db_path, mock_dingtalk_webhook):
        """测试发送钉钉通知 - 降级"""
        mock_notifier = Mock()
        mock_notifier.send.return_value = {'success': True, 'result': {'errcode': 0}}
        mock_notifier_class.return_value = mock_notifier
        
        # 创建带通知的晋级器
        graduator = StrategyGraduator(
            db_path=test_db_path,
            dingtalk_webhook=mock_dingtalk_webhook
        )
        # 替换 notifier 为 mock
        graduator.notifier = mock_notifier
        
        suggestion = GraduationSuggestion(
            code='MU',
            suggestion_type=SuggestionType.DEMOTE,
            current_mode=TradingMode.HYBRID,
            suggested_mode=TradingMode.PAPER,
            reason='连续 2 天亏损',
            consecutive_days=2,
            avg_return=-0.03
        )
        
        result = graduator._send_dingtalk_notification(suggestion)
        
        assert result is True
        call_args = mock_notifier.send.call_args
        message = call_args[0][0]
        assert 'MU' in message
        assert '降级' in message or 'PAPER' in message
    
    @patch('app.v4.core.graduator.DingTalkNotifierV4')
    def test_send_notification_disabled(self, mock_notifier_class, graduator_no_notify):
        """测试通知禁用时不发送"""
        suggestion = GraduationSuggestion(
            code='0700.HK',
            suggestion_type=SuggestionType.GRADUATE,
            current_mode=TradingMode.PAPER,
            suggested_mode=TradingMode.HYBRID,
            reason='连续 3 天收益率>5%',
            consecutive_days=3,
            avg_return=0.06
        )
        
        result = graduator_no_notify._send_dingtalk_notification(suggestion)
        assert result is False
        mock_notifier_class.assert_not_called()
    
    # ==================== 测试绩效历史 ====================
    
    def test_get_performance_history(self, graduator):
        """测试获取绩效历史"""
        # 记录 10 天数据
        for i in range(10):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.02 + i * 0.005
            )
        
        # 获取最近 5 天
        history = graduator.get_performance_history(code='0700.HK', days=5)
        assert len(history) == 5
        
        # 获取最近 30 天
        history = graduator.get_performance_history(code='0700.HK', days=30)
        assert len(history) == 10
    
    def test_get_performance_history_empty(self, graduator):
        """测试获取空绩效历史"""
        history = graduator.get_performance_history(code='NONEXISTENT', days=30)
        assert len(history) == 0
    
    # ==================== 测试边界条件 ====================
    
    def test_exact_threshold(self, graduator):
        """测试精确阈值边界"""
        # 记录连续 3 天正好 5% 收益
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.05  # 正好 5%
            )
        
        suggestion = graduator.check_graduation(code='0700.HK')
        # 5% 应该满足>5% 的条件吗？根据需求是>5%，所以 5% 不满足
        # 但通常阈值包含边界，这里测试期望行为
        # 根据需求文档"连续 3 天收益>5%"，5% 不触发
        assert suggestion is None
    
    def test_just_above_threshold(self, graduator):
        """测试略高于阈值"""
        # 记录连续 3 天 5.01% 收益
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.0501  # 5.01% > 5%
            )
        
        suggestion = graduator.check_graduation(code='0700.HK')
        assert suggestion is not None
        assert suggestion.suggestion_type == SuggestionType.GRADUATE
    
    def test_multiple_codes_independent(self, graduator):
        """测试多只标的数据独立"""
        # 0700.HK 连续 3 天盈利
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='0700.HK',
                date=date,
                daily_return=0.06
            )
        
        # MU 设置为 HYBRID 模式后连续 2 天亏损
        graduator.db.create_trading_mode('MU', TradingMode.HYBRID)
        for i in range(2):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            graduator.record_daily_performance(
                code='MU',
                date=date,
                daily_return=-0.03
            )
        
        # 0700.HK 应触发晋级
        suggestion_0700 = graduator.check_graduation(code='0700.HK')
        assert suggestion_0700 is not None
        assert suggestion_0700.suggestion_type == SuggestionType.GRADUATE
        
        # MU 应触发降级
        suggestion_mu = graduator.check_graduation(code='MU')
        assert suggestion_mu is not None
        assert suggestion_mu.suggestion_type == SuggestionType.DEMOTE


class TestStrategyGraduatorIntegration:
    """集成测试"""
    
    @pytest.fixture
    def test_db_path(self, tmp_path):
        return str(tmp_path / "test_integration.db")
    
    def test_full_graduation_workflow(self, test_db_path):
        """测试完整晋级流程"""
        graduator = StrategyGraduator(db_path=test_db_path)
        
        # 第 1 天：盈利 6%
        graduator.record_daily_performance(
            code='0700.HK',
            date=(datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),
            daily_return=0.06
        )
        assert graduator.check_graduation('0700.HK') is None
        
        # 第 2 天：盈利 7%
        graduator.record_daily_performance(
            code='0700.HK',
            date=(datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'),
            daily_return=0.07
        )
        assert graduator.check_graduation('0700.HK') is None
        
        # 第 3 天：盈利 8%
        graduator.record_daily_performance(
            code='0700.HK',
            date=datetime.now().strftime('%Y-%m-%d'),
            daily_return=0.08
        )
        
        # 应触发晋级建议
        suggestion = graduator.check_graduation('0700.HK')
        assert suggestion is not None
        assert suggestion.suggestion_type == SuggestionType.GRADUATE
        
        # 获取建议模式
        suggested_mode = graduator.get_suggested_mode('0700.HK')
        assert suggested_mode == TradingMode.HYBRID.value
        
        # 用户手动确认切换
        graduator.switch_mode(
            code='0700.HK',
            new_mode=TradingMode.HYBRID.value,
            confirmed_by='user_test'
        )
        
        # 验证模式已切换
        mode = graduator.db.get_trading_mode('0700.HK')
        assert mode.mode == TradingMode.HYBRID
        
        # 验证晋级历史
        history = graduator.db.get_graduation_history_by_code('0700.HK')
        assert len(history) == 1
        assert history[0].from_mode == TradingMode.PAPER
        assert history[0].to_mode == TradingMode.HYBRID
    
    def test_full_demotion_workflow(self, test_db_path):
        """测试完整降级流程"""
        graduator = StrategyGraduator(db_path=test_db_path)
        
        # 先设置为 HYBRID 模式
        graduator.db.create_trading_mode('MU', TradingMode.HYBRID)
        
        # 第 1 天：亏损
        graduator.record_daily_performance(
            code='MU',
            date=(datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'),
            daily_return=-0.03
        )
        assert graduator.check_graduation('MU') is None
        
        # 第 2 天：亏损
        graduator.record_daily_performance(
            code='MU',
            date=datetime.now().strftime('%Y-%m-%d'),
            daily_return=-0.04
        )
        
        # 应触发降级建议
        suggestion = graduator.check_graduation('MU')
        assert suggestion is not None
        assert suggestion.suggestion_type == SuggestionType.DEMOTE
        
        # 获取建议模式
        suggested_mode = graduator.get_suggested_mode('MU')
        assert suggested_mode == TradingMode.PAPER.value
        
        # 用户手动确认切换
        graduator.switch_mode(
            code='MU',
            new_mode=TradingMode.PAPER.value,
            confirmed_by='user_test'
        )
        
        # 验证模式已切换
        mode = graduator.db.get_trading_mode('MU')
        assert mode.mode == TradingMode.PAPER
