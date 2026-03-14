"""
SessionContext - 会话上下文管理

功能:
- 保存会话状态
- 自动恢复上次会话
- 上下文过期清理
- 多会话管理
- 与 StateManager 和 DecisionLog 集成
"""

import os
import json
import uuid
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import logging

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog

logger = logging.getLogger("ftseek.core.session_context")


class SessionContext:
    """会话上下文管理器"""
    
    def __init__(self, session_id: Optional[str] = None, data_dir: str = 'data/sessions'):
        """
        初始化会话上下文
        
        Args:
            session_id: 会话 ID（不提供则自动创建）
            data_dir: 数据目录
        """
        self.data_dir = data_dir
        self._session_id = session_id
        self._context: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._state_mgr = StateManager()
        self._decision_log = DecisionLog()
        
        # 确保目录存在
        os.makedirs(data_dir, exist_ok=True)
        
        # 如果没有提供 session_id，尝试恢复最近的会话
        if not session_id:
            if not self.auto_restore():
                self.start_session()
        else:
            self.load_session(session_id)
    
    @property
    def session_id(self) -> Optional[str]:
        """获取当前会话 ID"""
        return self._session_id
    
    def start_session(self) -> str:
        """启动新会话"""
        with self._lock:
            self._session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            self._context = {
                'created_at': datetime.now().isoformat(),
                'last_active': datetime.now().isoformat(),
                'status': 'active',
                'data': {}
            }
            self.save()
            
            self._decision_log.log_system_event(
                event='session_started',
                message=f"New session started: {self._session_id}"
            )
            
            logger.info(f"Session started: {self._session_id}")
            return self._session_id
    
    def end_session(self) -> None:
        """结束当前会话"""
        with self._lock:
            if self._session_id:
                self._context['status'] = 'ended'
                self._context['ended_at'] = datetime.now().isoformat()
                self.save()
                
                self._decision_log.log_system_event(
                    event='session_ended',
                    message=f"Session ended: {self._session_id}"
                )
                
                logger.info(f"Session ended: {self._session_id}")
                self._session_id = None
    
    def load_session(self, session_id: str) -> bool:
        """加载指定会话"""
        with self._lock:
            session_file = self._get_session_file(session_id)
            
            if os.path.exists(session_file):
                try:
                    with open(session_file, 'r', encoding='utf-8') as f:
                        self._context = json.load(f)
                    self._session_id = session_id
                    logger.info(f"Session loaded: {session_id}")
                    return True
                except Exception as e:
                    logger.error(f"Failed to load session: {e}")
                    return False
            else:
                logger.warning(f"Session not found: {session_id}")
                return False
    
    def get_current_session(self) -> Optional[str]:
        """获取当前会话 ID"""
        return self._session_id
    
    def set_context(self, key: str, value: Any) -> None:
        """设置上下文值"""
        with self._lock:
            if 'data' not in self._context:
                self._context['data'] = {}
            self._context['data'][key] = value
            self._context['last_active'] = datetime.now().isoformat()
    
    def get_context(self, key: str, default: Any = None) -> Any:
        """获取上下文值"""
        with self._lock:
            return self._context.get('data', {}).get(key, default)
    
    def clear_context(self) -> None:
        """清空上下文"""
        with self._lock:
            self._context['data'] = {}
            self._context['last_active'] = datetime.now().isoformat()
    
    def save(self) -> bool:
        """保存当前会话"""
        with self._lock:
            if not self._session_id:
                return False
            
            session_file = self._get_session_file(self._session_id)
            try:
                with open(session_file, 'w', encoding='utf-8') as f:
                    json.dump(self._context, f, indent=2, ensure_ascii=False, default=str)
                
                # 同时保存到 StateManager
                self._state_mgr.set('current_session', self._session_id)
                self._state_mgr.set(f'session_{self._session_id}', self._context)
                self._state_mgr.save()
                
                return True
            except Exception as e:
                logger.error(f"Failed to save session: {e}")
                return False
    
    def restore(self) -> bool:
        """恢复最近一次会话"""
        with self._lock:
            sessions = self.list_sessions()
            if sessions:
                # 恢复最新的会话
                latest = sessions[0]
                return self.load_session(latest['session_id'])
            return False
    
    def auto_restore(self) -> bool:
        """启动时自动恢复"""
        # 尝试从 StateManager 恢复
        last_session = self._state_mgr.get('current_session')
        if last_session:
            return self.load_session(last_session)
        
        # 否则尝试恢复最近的会话文件
        return self.restore()
    
    def cleanup_expired_sessions(self, hours: int = 24) -> int:
        """清理过期会话"""
        with self._lock:
            cutoff = datetime.now() - timedelta(hours=hours)
            removed = 0
            
            for session_info in self.list_sessions():
                session_time = datetime.fromisoformat(session_info['last_active'])
                if session_time < cutoff:
                    session_file = self._get_session_file(session_info['session_id'])
                    if os.path.exists(session_file):
                        os.remove(session_file)
                        removed += 1
            
            logger.info(f"Cleaned up {removed} expired sessions")
            return removed
    
    def get_session_info(self, session_id: str) -> Optional[Dict]:
        """获取会话信息"""
        session_file = self._get_session_file(session_id)
        if os.path.exists(session_file):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return None
    
    def list_sessions(self) -> List[Dict]:
        """列出所有会话"""
        sessions = []
        try:
            for filename in os.listdir(self.data_dir):
                if filename.endswith('.json'):
                    filepath = os.path.join(self.data_dir, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            sessions.append({
                                'session_id': filename.replace('.json', ''),
                                'created_at': data.get('created_at', ''),
                                'last_active': data.get('last_active', ''),
                                'status': data.get('status', 'unknown')
                            })
                    except:
                        pass
        except:
            pass
        
        # 按最后活跃时间排序
        sessions.sort(key=lambda x: x['last_active'], reverse=True)
        return sessions
    
    def _get_session_file(self, session_id: str) -> str:
        """获取会话文件路径"""
        return os.path.join(self.data_dir, f"{session_id}.json")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口 - 自动保存"""
        self.save()
        return False
