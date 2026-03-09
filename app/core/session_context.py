"""
会话上下文管理器模块

提供跨会话的状态保持和自动恢复功能，解决长对话遗忘问题。

功能特性:
- 保存会话状态（配置、持仓、待确认等）
- 自动恢复上次会话状态
- 上下文过期清理（默认 24 小时）
- 支持多会话管理
- 与 StateManager 和 DecisionLog 集成
- 会话启动/结束事件记录

使用示例:
    from app.core.session_context import SessionContext
    
    # 创建会话（自动恢复最近会话）
    session = SessionContext(auto_restore=True)
    
    # 启动新会话
    session_id = session.start_session()
    
    # 设置上下文数据
    session.set_context('config', {'max_trades': 10})
    session.set_context('trading_state', {'daily_pnl': 1500.0})
    
    # 保存会话
    session.save()
    
    # 恢复会话
    session.restore()
    
    # 清理过期会话
    session.cleanup_expired_sessions(hours=24)
"""

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog


class SessionContext:
    """
    会话上下文管理器
    
    负责管理会话的生命周期、状态持久化和自动恢复。
    支持多会话并发管理，线程安全设计。
    
    Attributes:
        session_id: 当前会话 ID
        sessions_dir: 会话数据目录
        state_mgr: 状态管理器实例
        decision_log: 决策日志实例
        _lock: 线程锁
        _sessions: 内存中的会话缓存
    """
    
    VERSION = "4.3.0"
    
    def __init__(self, session_id: Optional[str] = None, auto_restore: bool = False):
        """
        初始化会话上下文管理器
        
        Args:
            session_id: 可选的会话 ID，如果提供则直接加载该会话
            auto_restore: 是否在初始化时自动恢复最近的会话，默认 False
        """
        self.sessions_dir = 'data/sessions'
        self.state_mgr = StateManager()
        self.decision_log = DecisionLog()
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        
        # 当前会话 ID
        self._current_session_id: Optional[str] = session_id
        
        # 确保会话目录存在
        self._ensure_directory()
        
        # 加载会话索引
        self._load_session_index()
        
        # 如果提供了 session_id，加载该会话
        if session_id:
            self.load_session(session_id)
        # 如果启用自动恢复，尝试恢复最近的会话
        elif auto_restore:
            self.auto_restore()
    
    def _ensure_directory(self):
        """确保会话数据目录存在"""
        if not os.path.exists(self.sessions_dir):
            os.makedirs(self.sessions_dir, exist_ok=True)
    
    def _get_session_file(self, session_id: str) -> str:
        """
        获取会话文件路径
        
        Args:
            session_id: 会话 ID
            
        Returns:
            会话文件路径
        """
        return os.path.join(self.sessions_dir, f'{session_id}.json')
    
    def _get_session_index_file(self) -> str:
        """获取会话索引文件路径"""
        return os.path.join(self.sessions_dir, 'session_index.json')
    
    def _load_session_index(self):
        """加载会话索引"""
        with self._lock:
            try:
                index_file = self._get_session_index_file()
                if os.path.exists(index_file):
                    with open(index_file, 'r', encoding='utf-8') as f:
                        self._sessions = json.load(f)
                else:
                    self._sessions = {}
            except (json.JSONDecodeError, IOError, OSError) as e:
                print(f"Warning: Could not load session index: {e}")
                self._sessions = {}
    
    def _save_session_index(self):
        """保存会话索引"""
        with self._lock:
            try:
                self._ensure_directory()
                with open(self._get_session_index_file(), 'w', encoding='utf-8') as f:
                    json.dump(self._sessions, f, indent=2, ensure_ascii=False)
            except (IOError, OSError, PermissionError) as e:
                print(f"Error saving session index: {e}")
            except Exception as e:
                print(f"Unexpected error saving session index: {e}")
    
    def _create_session_data(self, session_id: str) -> Dict[str, Any]:
        """
        创建新的会话数据结构
        
        Args:
            session_id: 会话 ID
            
        Returns:
            会话数据字典
        """
        now = datetime.now().isoformat()
        return {
            "session_id": session_id,
            "created_at": now,
            "last_active": now,
            "status": "active",
            "context": {
                "config": {},
                "trading_state": {
                    "daily_trades": 0,
                    "daily_pnl": 0.0,
                    "positions": []
                },
                "pending_confirmations": [],
                "market_condition": "unknown"
            },
            "metadata": {
                "version": self.VERSION,
                "user_id": self.state_mgr.get('user_id', 'default_user')
            }
        }
    
    def start_session(self) -> str:
        """
        启动新会话
        
        Returns:
            新会话的 ID
            
        Raises:
            RuntimeError: 当已有活动会话时
        """
        with self._lock:
            # 如果有活动会话，先结束
            if self._current_session_id and self._current_session_id in self._sessions:
                if self._sessions[self._current_session_id].get('status') == 'active':
                    self.end_session()
            
            # 生成新会话 ID
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            session_id = f"session_{timestamp}_{uuid.uuid4().hex[:8]}"
            
            # 创建会话数据
            session_data = self._create_session_data(session_id)
            
            # 保存到索引
            self._sessions[session_id] = session_data
            self._save_session_index()
            
            # 设置当前会话
            self._current_session_id = session_id
            
            # 记录会话启动事件
            try:
                self.decision_log.log_system_event(
                    event_name='session_started',
                    event_data={
                        'session_id': session_id,
                        'timestamp': session_data['created_at']
                    },
                    severity='info'
                )
            except Exception as e:
                print(f"Warning: Failed to log session start event: {e}")
            
            return session_id
    
    def end_session(self) -> None:
        """
        结束当前会话
        
        将会话状态标记为 ended，并保存最终状态。
        """
        with self._lock:
            if not self._current_session_id:
                return
            
            session_id = self._current_session_id
            
            if session_id in self._sessions:
                # 更新会话状态
                self._sessions[session_id]['status'] = 'ended'
                self._sessions[session_id]['last_active'] = datetime.now().isoformat()
                self._save_session_index()
                
                # 保存最终状态到文件
                self._save_session_to_file(session_id)
                
                # 记录会话结束事件
                try:
                    self.decision_log.log_system_event(
                        event_name='session_ended',
                        event_data={
                            'session_id': session_id,
                            'timestamp': self._sessions[session_id]['last_active']
                        },
                        severity='info'
                    )
                except Exception as e:
                    print(f"Warning: Failed to log session end event: {e}")
            
            # 清除当前会话
            self._current_session_id = None
    
    def load_session(self, session_id: str) -> bool:
        """
        加载指定会话
        
        Args:
            session_id: 要加载的会话 ID
            
        Returns:
            加载成功返回 True，失败返回 False
        """
        with self._lock:
            try:
                # 检查索引中是否存在
                if session_id not in self._sessions:
                    # 尝试从文件加载
                    session_file = self._get_session_file(session_id)
                    if not os.path.exists(session_file):
                        return False
                    
                    with open(session_file, 'r', encoding='utf-8') as f:
                        session_data = json.load(f)
                    
                    self._sessions[session_id] = session_data
                    self._save_session_index()
                else:
                    # 从文件重新加载最新数据
                    self._load_session_from_file(session_id)
                
                # 设置当前会话
                self._current_session_id = session_id
                
                # 更新最后活跃时间
                self._sessions[session_id]['last_active'] = datetime.now().isoformat()
                self._sessions[session_id]['status'] = 'active'
                self._save_session_index()
                
                return True
            except (json.JSONDecodeError, IOError, OSError) as e:
                print(f"Error loading session {session_id}: {e}")
                return False
            except Exception as e:
                print(f"Unexpected error loading session {session_id}: {e}")
                return False
    
    def _load_session_from_file(self, session_id: str):
        """从文件加载会话数据"""
        session_file = self._get_session_file(session_id)
        if os.path.exists(session_file):
            with open(session_file, 'r', encoding='utf-8') as f:
                self._sessions[session_id] = json.load(f)
    
    def _save_session_to_file(self, session_id: str):
        """保存会话数据到文件"""
        if session_id in self._sessions:
            session_file = self._get_session_file(session_id)
            try:
                with open(session_file, 'w', encoding='utf-8') as f:
                    json.dump(self._sessions[session_id], f, indent=2, ensure_ascii=False)
            except (IOError, OSError, PermissionError) as e:
                print(f"Error saving session {session_id}: {e}")
    
    def get_current_session(self) -> Optional[str]:
        """
        获取当前会话 ID
        
        Returns:
            当前会话 ID，如果没有活动会话则返回 None
        """
        with self._lock:
            return self._current_session_id
    
    def set_context(self, key: str, value: Any) -> None:
        """
        设置上下文值
        
        Args:
            key: 上下文键名
            value: 上下文值
            
        Raises:
            RuntimeError: 当没有活动会话时
        """
        with self._lock:
            if not self._current_session_id:
                raise RuntimeError("No active session. Call start_session() first.")
            
            session_data = self._sessions.get(self._current_session_id)
            if not session_data:
                raise RuntimeError(f"Session {self._current_session_id} not found.")
            
            # 确定存储位置
            if key in ['config', 'trading_state', 'pending_confirmations', 'market_condition']:
                session_data['context'][key] = value
            else:
                # 其他键存储到 context 的根级别
                session_data['context'][key] = value
            
            # 更新最后活跃时间
            session_data['last_active'] = datetime.now().isoformat()
    
    def get_context(self, key: str, default: Any = None) -> Any:
        """
        获取上下文值
        
        Args:
            key: 上下文键名
            default: 默认值（当键不存在时返回）
            
        Returns:
            上下文值，如果键不存在则返回默认值
            
        Raises:
            RuntimeError: 当没有活动会话时
        """
        with self._lock:
            if not self._current_session_id:
                return default
            
            session_data = self._sessions.get(self._current_session_id)
            if not session_data:
                return default
            
            context = session_data.get('context', {})
            return context.get(key, default)
    
    def clear_context(self) -> None:
        """
        清除当前会话的上下文
        
        Raises:
            RuntimeError: 当没有活动会话时
        """
        with self._lock:
            if not self._current_session_id:
                raise RuntimeError("No active session. Call start_session() first.")
            
            session_data = self._sessions.get(self._current_session_id)
            if session_data:
                # 重置为默认结构
                session_data['context'] = {
                    "config": {},
                    "trading_state": {
                        "daily_trades": 0,
                        "daily_pnl": 0.0,
                        "positions": []
                    },
                    "pending_confirmations": [],
                    "market_condition": "unknown"
                }
                session_data['last_active'] = datetime.now().isoformat()
    
    def save(self) -> bool:
        """
        保存当前会话状态
        
        Returns:
            保存成功返回 True，失败返回 False
        """
        with self._lock:
            if not self._current_session_id:
                return False
            
            try:
                session_id = self._current_session_id
                session_data = self._sessions.get(session_id)
                
                if not session_data:
                    return False
                
                # 更新最后活跃时间
                session_data['last_active'] = datetime.now().isoformat()
                
                # 保存到索引
                self._save_session_index()
                
                # 保存到文件
                self._save_session_to_file(session_id)
                
                # 同步到状态管理器
                self.state_mgr.set('current_session_id', session_id)
                self.state_mgr.set('session_context', session_data.get('context', {}))
                self.state_mgr.save()
                
                return True
            except Exception as e:
                print(f"Error saving session: {e}")
                return False
    
    def restore(self) -> bool:
        """
        恢复最近一次会话
        
        Returns:
            恢复成功返回 True，失败返回 False
        """
        with self._lock:
            try:
                # 查找最近的会话（包括已结束的）
                recent_session = None
                recent_time = None
                
                for session_id, session_data in self._sessions.items():
                    last_active = session_data.get('last_active', '')
                    if last_active:
                        if recent_time is None or last_active > recent_time:
                            recent_time = last_active
                            recent_session = session_id
                
                if recent_session:
                    return self.load_session(recent_session)
                
                # 如果没有会话，尝试从状态管理器恢复
                saved_session_id = self.state_mgr.get('current_session_id')
                if saved_session_id:
                    return self.load_session(saved_session_id)
                
                return False
            except Exception as e:
                print(f"Error restoring session: {e}")
                return False
    
    def auto_restore(self) -> bool:
        """
        启动时自动恢复最近一次会话
        
        Returns:
            恢复成功返回 True，失败返回 False
        """
        return self.restore()
    
    def cleanup_expired_sessions(self, hours: int = 24) -> int:
        """
        清理过期会话
        
        Args:
            hours: 过期时间阈值（小时），默认 24 小时
            
        Returns:
            清理的会话数量
        """
        with self._lock:
            try:
                cutoff_time = datetime.now() - timedelta(hours=hours)
                cleaned_count = 0
                sessions_to_remove = []
                
                for session_id, session_data in self._sessions.items():
                    # 跳过当前会话
                    if session_id == self._current_session_id:
                        continue
                    
                    # 跳过活跃会话
                    if session_data.get('status') == 'active':
                        continue
                    
                    # 检查最后活跃时间
                    last_active_str = session_data.get('last_active', '')
                    if last_active_str:
                        try:
                            last_active = datetime.fromisoformat(last_active_str)
                            if last_active < cutoff_time:
                                sessions_to_remove.append(session_id)
                        except (ValueError, TypeError):
                            # 无法解析时间的会话，保留
                            pass
                
                # 删除过期会话
                for session_id in sessions_to_remove:
                    # 从索引中移除
                    del self._sessions[session_id]
                    
                    # 删除文件
                    session_file = self._get_session_file(session_id)
                    if os.path.exists(session_file):
                        try:
                            os.remove(session_file)
                        except OSError:
                            pass
                    
                    cleaned_count += 1
                
                # 保存索引
                if cleaned_count > 0:
                    self._save_session_index()
                
                return cleaned_count
            except Exception as e:
                print(f"Error cleaning up expired sessions: {e}")
                return 0
    
    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        获取会话信息
        
        Args:
            session_id: 会话 ID
            
        Returns:
            会话信息字典，如果不存在则返回 None
        """
        with self._lock:
            if session_id not in self._sessions:
                # 尝试从文件加载
                session_file = self._get_session_file(session_id)
                if os.path.exists(session_file):
                    try:
                        with open(session_file, 'r', encoding='utf-8') as f:
                            session_data = json.load(f)
                        return session_data.copy()
                    except (json.JSONDecodeError, IOError, OSError):
                        return None
                return None
            
            return self._sessions[session_id].copy()
    
    def get_all_sessions(self) -> List[Dict[str, Any]]:
        """
        获取所有会话列表
        
        Returns:
            会话信息列表
        """
        with self._lock:
            return [
                {
                    'session_id': sid,
                    'created_at': data.get('created_at'),
                    'last_active': data.get('last_active'),
                    'status': data.get('status')
                }
                for sid, data in self._sessions.items()
            ]
    
    def __del__(self):
        """析构函数，确保保存当前会话"""
        try:
            if self._current_session_id:
                self.save()
        except:
            pass
