"""
FTSeek Core Module Tests

测试所有核心模块：
- StateManager
- DecisionLog
- SessionContext
- ConfigContainer
- VersionManager
"""

import pytest
import os
import sys
import tempfile
import shutil

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog, DecisionType
from app.core.session_context import SessionContext
from app.core.config_container import ConfigContainer
from app.core.version_manager import VersionManager


class TestStateManager:
    """测试 StateManager"""
    
    def setup_method(self):
        """每个测试前执行"""
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, 'test_state.json')
        self.state = StateManager(self.test_file)
    
    def teardown_method(self):
        """每个测试后执行"""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_state_set_get(self):
        """测试基本读写"""
        self.state.set('test_key', 'test_value')
        assert self.state.get('test_key') == 'test_value'
        assert self.state.get('nonexistent', 'default') == 'default'
    
    def test_state_save_load(self):
        """测试保存加载"""
        self.state.set('config', {'key': 'value'})
        assert self.state.save()
        
        state2 = StateManager(self.test_file)
        assert state2.get('config') == {'key': 'value'}
    
    def test_checkpoint_create_restore(self):
        """测试检查点"""
        self.state.set('data', 'original')
        checkpoint_id = self.state.checkpoint('test_checkpoint')
        
        self.state.set('data', 'modified')
        assert self.state.restore(checkpoint_id)
        assert self.state.get('data') == 'original'
    
    def test_auto_snapshot(self):
        """测试自动快照"""
        self.state.start_auto_snapshot(interval_seconds=1)
        self.state.set('auto_test', 'value')
        self.state.save()
        self.state.stop_auto_snapshot()
        assert True  # 不崩溃即通过


class TestDecisionLog:
    """测试 DecisionLog"""
    
    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, 'test_log.json')
        self.log = DecisionLog(self.test_file)
    
    def teardown_method(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_log_trade_decision(self):
        """测试记录交易决策"""
        entry = self.log.log_trade_decision('HK.00700', 'BUY', 0.85, 'manual_confirm')
        assert entry['symbol'] == 'HK.00700'
        assert entry['type'] == 'trade_decision'
    
    def test_log_config_change(self):
        """测试记录配置变更"""
        entry = self.log.log_config_change('test_key', 'old', 'new', 'test reason')
        assert entry['key'] == 'test_key'
        assert entry['type'] == 'config_change'
    
    def test_query_by_type(self):
        """测试按类型查询"""
        self.log.log_trade_decision('HK.00700', 'BUY', 0.85, 'manual_confirm')
        results = self.log.query(decision_type='trade_decision')
        assert len(results) > 0
    
    def test_query_by_symbol(self):
        """测试按标的查询"""
        self.log.log_trade_decision('HK.00700', 'BUY', 0.85, 'manual_confirm')
        results = self.log.query(symbol='HK.00700')
        assert len(results) > 0
    
    def test_export_json(self):
        """测试导出 JSON"""
        self.log.log_trade_decision('HK.00700', 'BUY', 0.85, 'manual_confirm')
        export_file = os.path.join(self.test_dir, 'export.json')
        assert self.log.export_to_json(export_file)
        assert os.path.exists(export_file)
    
    def test_get_statistics(self):
        """测试统计信息"""
        self.log.log_trade_decision('HK.00700', 'BUY', 0.85, 'manual_confirm')
        stats = self.log.get_statistics()
        assert stats['total_entries'] >= 1


class TestSessionContext:
    """测试 SessionContext"""
    
    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.session = SessionContext(data_dir=self.test_dir)
    
    def teardown_method(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_session_start_end(self):
        """测试会话启动和结束"""
        session_id = self.session.start_session()
        assert session_id is not None
        assert self.session.get_current_session() == session_id
    
    def test_session_save_restore(self):
        """测试会话保存和恢复"""
        self.session.start_session()
        self.session.set_context('test_key', 'test_value')
        self.session.save()
        
        session2 = SessionContext(data_dir=self.test_dir)
        session2.restore()
        assert session2.get_context('test_key') == 'test_value'
    
    def test_context_set_get(self):
        """测试上下文读写"""
        self.session.start_session()
        self.session.set_context('key1', 'value1')
        assert self.session.get_context('key1') == 'value1'
    
    def test_multi_session_management(self):
        """测试多会话管理"""
        session1 = self.session.start_session()
        self.session.set_context('session', 1)
        self.session.save()
        
        session2 = SessionContext(data_dir=self.test_dir)
        session2.start_session()
        session2.set_context('session', 2)
        session2.save()
        
        sessions = session2.list_sessions()
        assert len(sessions) >= 2


class TestConfigContainer:
    """测试 ConfigContainer"""

    def test_config_container_init(self):
        """测试 ConfigContainer 初始化"""
        config = ConfigContainer()
        assert config.paths is not None
        assert config.opend is not None

    def test_config_container_get(self):
        """测试从配置中心获取配置"""
        config = ConfigContainer()
        # 测试从 config.yaml 获取配置
        mode = config.get('trading.mode')
        assert mode is not None

    def test_config_container_paths(self):
        """测试路径配置"""
        config = ConfigContainer()
        assert config.paths.data_dir is not None
        assert config.paths.log_dir is not None


class TestVersionManager:
    """测试 VersionManager"""
    
    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.version_mgr = VersionManager(self.test_dir)
    
    def teardown_method(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_get_current_version(self):
        """测试获取当前版本"""
        version = self.version_mgr.get_current_version()
        assert version == '4.3.0'
    
    def test_list_versions(self):
        """测试版本列表"""
        versions = self.version_mgr.list_versions()
        assert isinstance(versions, list)
    
    def test_compatibility_check(self):
        """测试兼容性检查"""
        is_compatible, warnings = self.version_mgr.check_compatibility('4.2.0', '4.3.0')
        assert is_compatible  # 次版本兼容


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
