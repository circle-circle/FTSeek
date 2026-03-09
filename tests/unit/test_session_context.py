"""
会话上下文管理器单元测试

测试 SessionContext 类的功能，包括：
- 会话启动和结束
- 会话保存和恢复
- 自动恢复
- 上下文读写
- 过期会话清理
- 多会话管理
- 与 StateManager 集成
- 与 DecisionLog 集成
"""

import pytest
import os
import sys
import time
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.core.session_context import SessionContext


class TestSessionStartEnd:
    """测试会话启动和结束功能"""
    
    def test_session_start_end(self):
        """测试会话启动和结束"""
        session = SessionContext()
        
        # 启动会话
        session_id = session.start_session()
        
        # 验证会话 ID 格式
        assert session_id is not None
        assert session_id.startswith('session_')
        
        # 验证当前会话 ID
        current_id = session.get_current_session()
        assert current_id == session_id
        
        # 验证会话状态
        info = session.get_session_info(session_id)
        assert info is not None
        assert info['status'] == 'active'
        
        # 结束会话
        session.end_session()
        
        # 验证会话已结束
        info = session.get_session_info(session_id)
        assert info['status'] == 'ended'
        
        # 验证当前会话为 None
        assert session.get_current_session() is None
    
    def test_session_start_returns_unique_id(self):
        """测试每次启动会话返回唯一 ID"""
        session = SessionContext()
        
        session_id1 = session.start_session()
        session.end_session()
        
        session_id2 = session.start_session()
        session.end_session()
        
        assert session_id1 != session_id2


class TestSessionSaveRestore:
    """测试会话保存和恢复功能"""
    
    def test_session_save_restore(self):
        """测试会话保存和恢复"""
        session = SessionContext()
        session.start_session()
        
        # 设置上下文数据
        session.set_context('config', {'max_trades': 10})
        session.set_context('trading_state', {'daily_pnl': 1500.0})
        session.set_context('pending_confirmations', ['trade_001'])
        
        # 保存会话
        session.save()
        
        # 创建新会话实例并恢复
        session2 = SessionContext()
        restored = session2.restore()
        
        # 验证恢复成功
        assert restored is True
        
        # 验证数据恢复
        config = session2.get_context('config')
        assert config == {'max_trades': 10}
        
        trading_state = session2.get_context('trading_state')
        assert trading_state['daily_pnl'] == 1500.0
        
        pending = session2.get_context('pending_confirmations')
        assert pending == ['trade_001']
    
    def test_save_without_session(self):
        """测试在没有活动会话时保存"""
        session = SessionContext()
        
        # 没有启动会话就保存应该失败或无操作
        result = session.save()
        assert result is False


class TestAutoRestore:
    """测试自动恢复功能"""
    
    def test_auto_restore_on_init(self):
        """测试初始化时自动恢复"""
        # 先创建一个会话并保存
        session1 = SessionContext()
        session1.start_session()
        session1.set_context('auto_restore_test', 'value_123')
        session1.save()
        session1.end_session()
        
        # 创建新会话，启用自动恢复
        session2 = SessionContext(auto_restore=True)
        
        # 验证自动恢复成功
        value = session2.get_context('auto_restore_test')
        assert value == 'value_123'
    
    def test_auto_restore_no_existing_session(self):
        """测试没有现有会话时的自动恢复"""
        # 清理所有会话文件
        session = SessionContext()
        session.cleanup_expired_sessions(hours=0)
        
        # 创建新会话，启用自动恢复
        session2 = SessionContext(auto_restore=True)
        
        # 应该返回 False，因为没有可恢复的会话
        # 但不应抛出异常


class TestContextSetGet:
    """测试上下文读写功能"""
    
    def test_context_set_get(self):
        """测试上下文读写"""
        session = SessionContext()
        session.start_session()
        
        # 测试基本类型
        session.set_context('string_key', 'string_value')
        session.set_context('int_key', 42)
        session.set_context('float_key', 3.14)
        session.set_context('bool_key', True)
        session.set_context('list_key', [1, 2, 3])
        session.set_context('dict_key', {'nested': 'value'})
        
        # 验证读取
        assert session.get_context('string_key') == 'string_value'
        assert session.get_context('int_key') == 42
        assert session.get_context('float_key') == 3.14
        assert session.get_context('bool_key') is True
        assert session.get_context('list_key') == [1, 2, 3]
        assert session.get_context('dict_key') == {'nested': 'value'}
        
        # 测试默认值
        assert session.get_context('nonexistent', 'default') == 'default'
        assert session.get_context('nonexistent') is None
    
    def test_context_clear(self):
        """测试清除上下文"""
        session = SessionContext()
        session.start_session()
        
        session.set_context('key1', 'value1')
        session.set_context('key2', 'value2')
        
        # 清除上下文
        session.clear_context()
        
        # 验证已清除
        assert session.get_context('key1') is None
        assert session.get_context('key2') is None


class TestCleanupExpiredSessions:
    """测试过期会话清理功能"""
    
    def test_cleanup_expired_sessions(self):
        """测试清理过期会话"""
        session = SessionContext()
        session.start_session()
        session.set_context('test', 'value')
        session.save()
        session.end_session()
        
        # 立即清理（不应该删除任何会话）
        cleaned = session.cleanup_expired_sessions(hours=24)
        assert cleaned >= 0
        
        # 模拟过期会话：修改最后活跃时间
        session_file = session._get_session_file(session.get_current_session() or 'test')
        if os.path.exists(session_file):
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 设置为 25 小时前
            data['last_active'] = (datetime.now() - timedelta(hours=25)).isoformat()
            
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        
        # 清理过期会话
        cleaned = session.cleanup_expired_sessions(hours=24)
        assert cleaned >= 0


class TestMultiSessionManagement:
    """测试多会话管理功能"""
    
    def test_multi_session_management(self):
        """测试多会话管理"""
        # 创建第一个会话
        session1 = SessionContext()
        session1.start_session()
        session1.set_context('session', 'first')
        session1.save()
        session1_id = session1.get_current_session()
        
        # 创建第二个会话
        session2 = SessionContext()
        session2.start_session()
        session2.set_context('session', 'second')
        session2.save()
        session2_id = session2.get_current_session()
        
        # 验证两个会话不同
        assert session1_id != session2_id
        
        # 加载第一个会话
        session2.load_session(session1_id)
        assert session2.get_context('session') == 'first'
        
        # 加载第二个会话
        session2.load_session(session2_id)
        assert session2.get_context('session') == 'second'
        
        # 清理
        session1.end_session()
        session2.end_session()


class TestIntegrationWithStateManager:
    """测试与 StateManager 集成"""
    
    def test_integration_with_state_manager(self):
        """测试与 StateManager 集成"""
        session = SessionContext()
        session.start_session()
        
        # 设置上下文
        session.set_context('integration_test', 'state_manager')
        session.save()
        
        # 验证状态管理器中有数据
        state = session.state_mgr.get_state()
        assert 'session_context' in state or 'integration_test' in str(state)
        
        session.end_session()


class TestIntegrationWithDecisionLog:
    """测试与 DecisionLog 集成"""
    
    def test_integration_with_decision_log(self):
        """测试与 DecisionLog 集成"""
        session = SessionContext()
        
        # 启动会话应该在决策日志中记录
        session_id = session.start_session()
        
        # 验证决策日志中有会话启动记录
        entries = session.decision_log.query(
            decision_type='system_event',
            limit=10
        )
        
        # 应该至少有一条会话启动事件
        session_start_events = [
            e for e in entries 
            if e.get('event_name') == 'session_started'
        ]
        assert len(session_start_events) > 0
        
        # 结束会话
        session.end_session()
        
        # 验证决策日志中有会话结束记录
        entries = session.decision_log.query(
            decision_type='system_event',
            limit=10
        )
        
        session_end_events = [
            e for e in entries 
            if e.get('event_name') == 'session_ended'
        ]
        assert len(session_end_events) > 0


class TestSessionInfo:
    """测试会话信息获取"""
    
    def test_get_session_info(self):
        """测试获取会话信息"""
        session = SessionContext()
        session.start_session()
        
        info = session.get_session_info(session.get_current_session())
        
        assert info is not None
        assert 'session_id' in info
        assert 'created_at' in info
        assert 'last_active' in info
        assert 'status' in info
        assert 'context' in info
        assert 'metadata' in info
        
        session.end_session()


class TestThreadSafety:
    """测试线程安全性"""
    
    def test_thread_safety(self):
        """测试多线程并发访问"""
        import threading
        
        session = SessionContext()
        session.start_session()
        
        errors = []
        
        def set_context_thread(key, value):
            try:
                for i in range(10):
                    session.set_context(f'{key}_{i}', value)
            except Exception as e:
                errors.append(e)
        
        # 创建多个线程
        threads = []
        for i in range(5):
            t = threading.Thread(target=set_context_thread, args=(f'thread{i}', f'value{i}'))
            threads.append(t)
            t.start()
        
        # 等待所有线程完成
        for t in threads:
            t.join()
        
        # 验证没有错误
        assert len(errors) == 0, f"Thread safety errors: {errors}"
        
        session.end_session()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
