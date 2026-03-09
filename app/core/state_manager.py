"""
状态管理器模块

提供项目状态持久化功能，包括：
- 状态保存/加载（JSON 格式）
- 自动快照（可配置间隔）
- 手动检查点（checkpoint）
- 状态恢复功能
- 状态变更日志

使用示例:
    state = StateManager()
    state.set('config', {'key': 'value'})
    state.save()
    
    # 创建检查点
    checkpoint_id = state.checkpoint('before_trade')
    
    # 恢复检查点
    state.restore(checkpoint_id)
    
    # 启动自动快照
    state.start_auto_snapshot(interval_seconds=300)
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional, List, Dict
import uuid


class StateManager:
    """
    项目状态管理器
    
    负责管理系统状态的持久化、快照和恢复。
    支持自动定期快照和手动检查点。
    
    Attributes:
        state_file: 状态文件路径
        state: 内存中的状态数据
        auto_snapshot_thread: 自动快照线程
        auto_snapshot_interval: 自动快照间隔（秒）
    """
    
    VERSION = "4.3.0"
    
    def __init__(self, state_file: str = 'data/state_snapshot.json'):
        """
        初始化状态管理器
        
        Args:
            state_file: 状态文件路径，默认为 'data/state_snapshot.json'
        """
        self.state_file = state_file
        self.state: Dict[str, Any] = {
            "version": self.VERSION,
            "last_updated": datetime.now().isoformat(),
            "config": {},
            "trading_state": {},
            "session_context": {},
            "checkpoints": []
        }
        self._lock = threading.RLock()  # 使用可重入锁避免死锁
        self._auto_snapshot_thread: Optional[threading.Thread] = None
        self._auto_snapshot_interval: int = 300
        self._stop_auto_snapshot = threading.Event()
        
        # 确保数据目录存在
        self._ensure_directory()
        
        # 尝试加载现有状态
        self.load()
    
    def _ensure_directory(self):
        """确保状态文件所在目录存在"""
        dir_path = os.path.dirname(self.state_file)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取状态值
        
        Args:
            key: 状态键名
            default: 默认值（当键不存在时返回）
            
        Returns:
            状态值，如果键不存在则返回默认值
        """
        with self._lock:
            # 在各级状态中查找
            if key in self.state:
                return self.state[key]
            if key in self.state.get('config', {}):
                return self.state['config'][key]
            if key in self.state.get('trading_state', {}):
                return self.state['trading_state'][key]
            if key in self.state.get('session_context', {}):
                return self.state['session_context'][key]
            return default
    
    def set(self, key: str, value: Any) -> None:
        """
        设置状态值
        
        Args:
            key: 状态键名
            value: 状态值
            
        Note:
            如果 key 是顶级键（config, trading_state, session_context），直接存储
            否则存储到 session_context 中
        """
        with self._lock:
            # 检查是否是顶级键
            top_level_keys = ['config', 'trading_state', 'session_context', 'version', 'last_updated', 'checkpoints']
            if key in top_level_keys:
                self.state[key] = value
            else:
                # 默认存储到 session_context
                if 'session_context' not in self.state:
                    self.state['session_context'] = {}
                self.state['session_context'][key] = value
            self.state['last_updated'] = datetime.now().isoformat()
    
    def save(self) -> bool:
        """
        保存状态到文件
        
        Returns:
            保存成功返回 True，失败返回 False
        """
        with self._lock:
            try:
                self._ensure_directory()
                self.state['last_updated'] = datetime.now().isoformat()
                
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    json.dump(self.state, f, indent=2, ensure_ascii=False)
                
                return True
            except (IOError, OSError, PermissionError) as e:
                print(f"Error saving state: {e}")
                return False
            except Exception as e:
                print(f"Unexpected error saving state: {e}")
                return False
    
    def load(self) -> bool:
        """
        从文件加载状态
        
        Returns:
            加载成功返回 True，失败返回 False
        """
        with self._lock:
            try:
                if not os.path.exists(self.state_file):
                    return False
                
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    loaded_state = json.load(f)
                
                # 合并加载的状态
                self.state.update(loaded_state)
                return True
            except (IOError, OSError, json.JSONDecodeError) as e:
                print(f"Error loading state: {e}")
                return False
            except Exception as e:
                print(f"Unexpected error loading state: {e}")
                return False
    
    def checkpoint(self, name: str) -> str:
        """
        创建状态检查点
        
        Args:
            name: 检查点名称
            
        Returns:
            检查点 ID（UUID 格式）
        """
        with self._lock:
            checkpoint_id = str(uuid.uuid4())
            timestamp = datetime.now().isoformat()
            
            # 保存当前状态的深拷贝
            checkpoint_data = {
                "id": checkpoint_id,
                "name": name,
                "timestamp": timestamp,
                "state": json.loads(json.dumps(self.state))
            }
            
            if 'checkpoints' not in self.state:
                self.state['checkpoints'] = []
            
            self.state['checkpoints'].append(checkpoint_data)
            
            # 自动保存
            self.save()
            
            return checkpoint_id
    
    def restore(self, checkpoint_id: str) -> bool:
        """
        恢复到指定检查点
        
        Args:
            checkpoint_id: 检查点 ID
            
        Returns:
            恢复成功返回 True，失败返回 False
        """
        with self._lock:
            try:
                checkpoints = self.state.get('checkpoints', [])
                
                # 查找检查点
                target_checkpoint = None
                for cp in checkpoints:
                    if cp.get('id') == checkpoint_id:
                        target_checkpoint = cp
                        break
                
                if target_checkpoint is None:
                    print(f"Checkpoint {checkpoint_id} not found")
                    return False
                
                # 恢复状态（保留 checkpoints 列表）
                saved_state = target_checkpoint.get('state', {})
                current_checkpoints = self.state.get('checkpoints', [])
                
                self.state.clear()
                self.state.update(saved_state)
                self.state['checkpoints'] = current_checkpoints
                self.state['last_updated'] = datetime.now().isoformat()
                
                # 自动保存
                self.save()
                
                return True
            except Exception as e:
                print(f"Error restoring checkpoint: {e}")
                return False
    
    def start_auto_snapshot(self, interval_seconds: int = 300) -> None:
        """
        启动自动快照
        
        Args:
            interval_seconds: 快照间隔（秒），默认 300 秒（5 分钟）
        """
        with self._lock:
            # 如果已有线程在运行，先停止
            self.stop_auto_snapshot()
            
            self._auto_snapshot_interval = interval_seconds
            self._stop_auto_snapshot.clear()
            
            def auto_snapshot_loop():
                """自动快照循环"""
                while not self._stop_auto_snapshot.is_set():
                    # 等待间隔时间
                    if self._stop_auto_snapshot.wait(timeout=interval_seconds):
                        break
                    
                    # 执行快照
                    with self._lock:
                        try:
                            self.save()
                        except Exception as e:
                            print(f"Auto snapshot failed: {e}")
            
            self._auto_snapshot_thread = threading.Thread(
                target=auto_snapshot_loop,
                daemon=True,
                name="StateAutoSnapshot"
            )
            self._auto_snapshot_thread.start()
    
    def stop_auto_snapshot(self) -> None:
        """停止自动快照"""
        if self._auto_snapshot_thread is not None:
            self._stop_auto_snapshot.set()
            self._auto_snapshot_thread.join(timeout=2.0)
            self._auto_snapshot_thread = None
    
    def get_checkpoints(self) -> List[Dict[str, Any]]:
        """
        获取所有检查点列表
        
        Returns:
            检查点列表
        """
        with self._lock:
            return self.state.get('checkpoints', []).copy()
    
    def clear_checkpoints(self) -> None:
        """清除所有检查点"""
        with self._lock:
            self.state['checkpoints'] = []
            self.save()
    
    def get_state(self) -> Dict[str, Any]:
        """
        获取完整状态副本
        
        Returns:
            状态数据的深拷贝
        """
        with self._lock:
            return json.loads(json.dumps(self.state))
    
    def __del__(self):
        """析构函数，确保停止自动快照线程"""
        try:
            self.stop_auto_snapshot()
        except:
            pass
