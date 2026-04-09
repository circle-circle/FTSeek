"""
StateManager - 状态管理器

功能:
- 保存/加载项目状态（JSON 格式）
- 自动快照（默认 5 分钟）
- 支持手动 checkpoint
- 状态恢复功能
- 线程安全

Example:
    >>> state = StateManager()
    >>> state.set('config', {'key': 'value'})
    >>> state.save()
    >>> state.load()
    >>> value = state.get('config')
"""

import json
import os
import time
import threading
import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger("ftseek.core.state_manager")
logger.propagate = False  # 防止日志重复


class StateManager:
    """状态管理器 - 项目状态持久化"""
    
    def __init__(self, state_file: str = 'data/state_snapshot.json'):
        """
        初始化状态管理器
        
        Args:
            state_file: 状态文件路径
        """
        self.state_file = state_file
        self._state: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._auto_snapshot_thread: Optional[threading.Thread] = None
        self._stop_auto_snapshot = False
        
        # 确保目录存在
        state_dir = os.path.dirname(state_file)
        if state_dir and not os.path.exists(state_dir):
            os.makedirs(state_dir, exist_ok=True)
        
        # 自动加载
        self.load()
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取状态值"""
        with self._lock:
            return self._state.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """设置状态值"""
        with self._lock:
            self._state[key] = value
            logger.debug(f"State set: {key} = {value}")
    
    def delete(self, key: str) -> bool:
        """删除状态值"""
        with self._lock:
            if key in self._state:
                del self._state[key]
                logger.debug(f"State deleted: {key}")
                return True
            return False
    
    def save(self) -> bool:
        """保存状态到文件"""
        with self._lock:
            try:
                self._state['last_updated'] = datetime.now().isoformat()
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    json.dump(self._state, f, indent=2, ensure_ascii=False, default=str)
                logger.info(f"State saved to {self.state_file}")
                return True
            except Exception as e:
                logger.error(f"Failed to save state: {e}")
                return False
    
    def load(self) -> bool:
        """从文件加载状态"""
        with self._lock:
            try:
                if os.path.exists(self.state_file):
                    with open(self.state_file, 'r', encoding='utf-8') as f:
                        self._state = json.load(f)
                    logger.info(f"State loaded from {self.state_file}")
                    return True
                else:
                    logger.info("No state file found, starting fresh")
                    return False
            except Exception as e:
                logger.error(f"Failed to load state: {e}")
                self._state = {}
                return False
    
    def checkpoint(self, name: str = "") -> str:
        """创建检查点"""
        with self._lock:
            checkpoint_id = f"checkpoint_{uuid.uuid4().hex[:8]}"
            if not name:
                name = checkpoint_id
            
            if 'checkpoints' not in self._state:
                self._state['checkpoints'] = []
            
            checkpoint = {
                'id': checkpoint_id,
                'name': name,
                'created_at': datetime.now().isoformat(),
                'state': self._state.copy()
            }
            
            self._state['checkpoints'].append(checkpoint)
            logger.info(f"Checkpoint created: {name} ({checkpoint_id})")
            return checkpoint_id
    
    def restore(self, checkpoint_id: str) -> bool:
        """恢复到检查点"""
        with self._lock:
            if 'checkpoints' not in self._state:
                logger.warning("No checkpoints available")
                return False
            
            for checkpoint in self._state['checkpoints']:
                if checkpoint['id'] == checkpoint_id:
                    self._state = checkpoint['state'].copy()
                    self._state['checkpoints'] = self._state.get('checkpoints', [])
                    logger.info(f"Restored to checkpoint: {checkpoint_id}")
                    return True
            
            logger.warning(f"Checkpoint not found: {checkpoint_id}")
            return False
    
    def list_checkpoints(self) -> List[Dict]:
        """列出所有检查点"""
        with self._lock:
            return self._state.get('checkpoints', [])
    
    def start_auto_snapshot(self, interval_seconds: int = 300) -> None:
        """启动自动快照"""
        if self._auto_snapshot_thread and self._auto_snapshot_thread.is_alive():
            logger.warning("Auto snapshot already running")
            return
        
        self._stop_auto_snapshot = False
        
        def auto_save():
            while not self._stop_auto_snapshot:
                time.sleep(interval_seconds)
                if not self._stop_auto_snapshot:
                    self.save()
        
        self._auto_snapshot_thread = threading.Thread(target=auto_save, daemon=True)
        self._auto_snapshot_thread.start()
        logger.info(f"Auto snapshot started (interval: {interval_seconds}s)")
    
    def stop_auto_snapshot(self) -> None:
        """停止自动快照"""
        self._stop_auto_snapshot = True
        if self._auto_snapshot_thread:
            self._auto_snapshot_thread.join(timeout=5)
            logger.info("Auto snapshot stopped")
    
    def clear(self) -> None:
        """清空状态"""
        with self._lock:
            self._state = {}
            logger.info("State cleared")
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有状态"""
        with self._lock:
            return self._state.copy()
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口 - 自动保存"""
        self.save()
        return False
