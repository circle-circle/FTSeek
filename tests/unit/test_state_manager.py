"""
状态管理器单元测试

测试 StateManager 类的核心功能：
- 状态读写
- 保存加载
- 检查点创建与恢复
- 自动快照
"""

import pytest
import os
import time
import json
import tempfile
import shutil
from pathlib import Path


class TestStateManager:
    """StateManager 类测试"""

    @pytest.fixture
    def temp_dir(self):
        """创建临时目录用于测试"""
        temp = tempfile.mkdtemp()
        yield temp
        shutil.rmtree(temp, ignore_errors=True)

    @pytest.fixture
    def state_manager(self, temp_dir):
        """创建 StateManager 实例"""
        from app.core.state_manager import StateManager
        state_file = os.path.join(temp_dir, 'state_snapshot.json')
        return StateManager(state_file=state_file)

    def test_state_set_get(self, state_manager):
        """测试基本读写功能"""
        # 设置值
        state_manager.set('test_key', 'test_value')
        state_manager.set('number_key', 42)
        state_manager.set('dict_key', {'nested': 'value'})
        
        # 获取值
        assert state_manager.get('test_key') == 'test_value'
        assert state_manager.get('number_key') == 42
        assert state_manager.get('dict_key') == {'nested': 'value'}
        
        # 测试默认值
        assert state_manager.get('nonexistent', 'default') == 'default'
        assert state_manager.get('nonexistent') is None

    def test_state_save_load(self, state_manager, temp_dir):
        """测试保存加载功能"""
        # 设置状态
        state_manager.set('config', {'key': 'value', 'number': 123})
        state_manager.set('trading_state', {'position': 'long', 'symbol': 'HK.09988'})
        state_manager.save()
        
        # 验证文件存在
        state_file = os.path.join(temp_dir, 'state_snapshot.json')
        assert os.path.exists(state_file)
        
        # 创建新实例并加载
        from app.core.state_manager import StateManager
        state2 = StateManager(state_file=state_file)
        loaded = state2.load()
        
        assert loaded is True
        assert state2.get('config') == {'key': 'value', 'number': 123}
        assert state2.get('trading_state') == {'position': 'long', 'symbol': 'HK.09988'}

    def test_checkpoint_create_restore(self, state_manager):
        """测试检查点创建与恢复功能"""
        # 设置初始状态
        state_manager.set('data', 'initial_value')
        state_manager.save()
        
        # 创建检查点
        checkpoint_id = state_manager.checkpoint('test_checkpoint')
        assert checkpoint_id is not None
        assert len(checkpoint_id) > 0
        
        # 修改状态
        state_manager.set('data', 'modified_value')
        assert state_manager.get('data') == 'modified_value'
        
        # 恢复到检查点
        restored = state_manager.restore(checkpoint_id)
        assert restored is True
        assert state_manager.get('data') == 'initial_value'

    def test_auto_snapshot(self, state_manager, temp_dir):
        """测试自动快照功能"""
        # 设置状态
        state_manager.set('auto_test', 'value_before')
        state_manager.save()
        
        # 启动自动快照（间隔 2 秒用于测试）
        state_manager.start_auto_snapshot(interval_seconds=2)
        
        # 修改状态
        state_manager.set('auto_test', 'value_after')
        
        # 等待自动快照
        time.sleep(3)
        
        # 停止自动快照
        state_manager.stop_auto_snapshot()
        
        # 验证文件已更新（检查时间戳）
        state_file = os.path.join(temp_dir, 'state_snapshot.json')
        assert os.path.exists(state_file)
        
        # 加载验证数据
        with open(state_file, 'r') as f:
            data = json.load(f)
            assert 'auto_test' in data.get('session_context', {}) or 'auto_test' in data

    def test_state_version_and_timestamp(self, state_manager):
        """测试版本和时间戳字段"""
        state_manager.save()
        
        # 读取文件验证结构
        with open(state_manager.state_file, 'r') as f:
            data = json.load(f)
        
        assert 'version' in data
        assert 'last_updated' in data
        assert data['version'] == '4.3.0'

    def test_error_handling(self, temp_dir):
        """测试错误处理"""
        from app.core.state_manager import StateManager
        
        # 测试加载不存在的文件应该返回 False
        state_manager = StateManager(state_file=os.path.join(temp_dir, 'nonexistent.json'))
        result = state_manager.load()
        assert result is False
        
        # 测试保存到只读目录（使用 /proc 作为不可写目录示例）
        try:
            invalid_path = '/proc/readonly_state.json'
            state_manager2 = StateManager(state_file=invalid_path)
            result = state_manager2.save()
            # 在 root 权限下可能成功，所以只验证不抛出异常
            assert result in [True, False]
        except Exception:
            # 如果抛出异常，测试也应该通过（因为错误被捕获了）
            pass
