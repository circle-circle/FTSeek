"""
决策日志系统模块

提供交易决策、配置变更、系统事件的记录、查询和导出功能。

功能特性:
- 记录交易决策（买入/卖出/跳过）
- 记录配置变更
- 记录系统事件和用户操作
- 支持多维度查询（时间、类型、标的）
- 支持导出为 JSON/CSV 格式
- 自动清理过期日志
- 线程安全设计

使用示例:
    from app.core.decision_log import DecisionLog
    
    log = DecisionLog()
    
    # 记录交易决策
    log.log_trade_decision(
        symbol='HK.09988',
        action='BUY',
        confidence=0.85,
        decision='manual_confirm',
        context={'market_condition': 'bullish'}
    )
    
    # 记录配置变更
    log.log_config_change(
        key='max_position_size',
        old_value=10000,
        new_value=20000,
        reason='调整仓位上限'
    )
    
    # 查询日志
    entries = log.query(symbol='HK.09988', limit=10)
    
    # 导出日志
    log.export_to_json('decision_history.json')
    log.export_to_csv('decision_history.csv')
    
    # 获取统计信息
    stats = log.get_statistics()
"""

import json
import os
import threading
import uuid
import csv
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from enum import Enum

from app.core.state_manager import StateManager


def _parse_datetime(dt_string: str) -> Optional[datetime]:
    """
    解析 ISO 格式日期字符串（兼容 Python 3.6）
    
    Args:
        dt_string: ISO 格式日期字符串
        
    Returns:
        datetime 对象，解析失败返回 None
    """
    if not dt_string:
        return None
    
    try:
        # Python 3.6 不支持 fromisoformat，手动解析
        # 处理带微秒和不带微秒的格式
        formats = [
            '%Y-%m-%dT%H:%M:%S.%f',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S.%f',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d'
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(dt_string, fmt)
            except ValueError:
                continue
        
        return None
    except Exception:
        return None


class DecisionType(str, Enum):
    """决策类型枚举"""
    TRADE_DECISION = "trade_decision"    # 交易决策
    CONFIG_CHANGE = "config_change"      # 配置变更
    SYSTEM_EVENT = "system_event"        # 系统事件
    USER_ACTION = "user_action"          # 用户操作


class DecisionLog:
    """
    决策日志类
    
    负责记录和管理所有交易决策、配置变更和系统事件。
    支持查询、过滤、导出和自动清理功能。
    
    Attributes:
        log_file: 日志文件路径
        max_entries: 最大日志条目数
        entries: 内存中的日志条目列表
        state_mgr: 状态管理器实例
        _lock: 线程锁
    """
    
    def __init__(self, log_file: str = 'data/decision_log.json', max_entries: int = 10000):
        """
        初始化决策日志
        
        Args:
            log_file: 日志文件路径，默认为 'data/decision_log.json'
            max_entries: 最大日志条目数，默认 10000 条
        """
        self.log_file = log_file
        self.max_entries = max_entries
        self.entries: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        
        # 初始化状态管理器
        self.state_mgr = StateManager()
        
        # 确保数据目录存在
        self._ensure_directory()
        
        # 加载现有日志
        self._load_entries()
    
    def _ensure_directory(self):
        """确保日志文件所在目录存在"""
        dir_path = os.path.dirname(self.log_file)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
    
    def _load_entries(self):
        """从文件加载日志条目"""
        with self._lock:
            try:
                # 尝试从状态管理器加载
                saved_entries = self.state_mgr.get('decision_log_entries', [])
                if saved_entries:
                    self.entries = saved_entries
                    return
                
                # 备用：直接从文件加载
                if os.path.exists(self.log_file):
                    with open(self.log_file, 'r', encoding='utf-8') as f:
                        self.entries = json.load(f)
            except (json.JSONDecodeError, IOError, OSError) as e:
                print(f"Warning: Could not load decision log: {e}")
                self.entries = []
            except Exception as e:
                print(f"Unexpected error loading decision log: {e}")
                self.entries = []
    
    def _save_entries(self):
        """保存日志条目到文件"""
        with self._lock:
            try:
                # 使用状态管理器保存
                self.state_mgr.set('decision_log_entries', self.entries)
                self.state_mgr.save()
                
                # 同时保存到文件作为备份
                self._ensure_directory()
                with open(self.log_file, 'w', encoding='utf-8') as f:
                    json.dump(self.entries, f, indent=2, ensure_ascii=False)
            except (IOError, OSError, PermissionError) as e:
                print(f"Error saving decision log: {e}")
            except Exception as e:
                print(f"Unexpected error saving decision log: {e}")
    
    def _create_entry(
        self,
        decision_type: DecisionType,
        **kwargs
    ) -> Dict[str, Any]:
        """
        创建日志条目
        
        Args:
            decision_type: 决策类型
            **kwargs: 其他字段
            
        Returns:
            完整的日志条目字典
        """
        entry = {
            'id': str(uuid.uuid4()),
            'timestamp': datetime.now().isoformat(),
            'type': decision_type.value,
        }
        
        # 添加其他字段
        entry.update(kwargs)
        
        # 添加元数据
        entry['metadata'] = {
            'version': '4.3.0',
            'session_id': self.state_mgr.get('session_id', 'default_session')
        }
        
        return entry
    
    def log_trade_decision(
        self,
        symbol: str,
        action: str,
        confidence: float,
        decision: str,
        context: Dict[str, Any],
        result: str = 'pending'
    ) -> Dict[str, Any]:
        """
        记录交易决策
        
        Args:
            symbol: 交易标的（如 'HK.09988'）
            action: 操作类型（'BUY', 'SELL', 'HOLD'）
            confidence: 置信度（0.0-1.0）
            decision: 决策类型（'auto', 'manual_confirm', 'manual_reject'）
            context: 上下文信息（包含四步法结果、市场条件等）
            result: 执行结果（'pending', 'executed', 'failed', 'cancelled'）
            
        Returns:
            创建的日志条目
            
        Raises:
            ValueError: 当参数无效时
        """
        # 参数验证
        if not symbol:
            raise ValueError("Symbol cannot be empty")
        if not action:
            raise ValueError("Action cannot be empty")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Confidence must be between 0.0 and 1.0")
        
        with self._lock:
            entry = self._create_entry(
                DecisionType.TRADE_DECISION,
                symbol=symbol,
                action=action,
                confidence=confidence,
                decision=decision,
                result=result,
                context=context or {}
            )
            
            self._add_entry(entry)
            return entry
    
    def log_config_change(
        self,
        key: str,
        old_value: Any,
        new_value: Any,
        reason: str
    ) -> Dict[str, Any]:
        """
        记录配置变更
        
        Args:
            key: 配置项键名
            old_value: 旧值
            new_value: 新值
            reason: 变更原因
            
        Returns:
            创建的日志条目
            
        Raises:
            ValueError: 当参数无效时
        """
        if not key:
            raise ValueError("Config key cannot be empty")
        
        with self._lock:
            entry = self._create_entry(
                DecisionType.CONFIG_CHANGE,
                key=key,
                old_value=old_value,
                new_value=new_value,
                reason=reason
            )
            
            self._add_entry(entry)
            return entry
    
    def log_system_event(
        self,
        event_name: str,
        event_data: Dict[str, Any],
        severity: str = 'info'
    ) -> Dict[str, Any]:
        """
        记录系统事件
        
        Args:
            event_name: 事件名称
            event_data: 事件数据
            severity: 严重程度（'info', 'warning', 'error', 'critical'）
            
        Returns:
            创建的日志条目
        """
        with self._lock:
            entry = self._create_entry(
                DecisionType.SYSTEM_EVENT,
                event_name=event_name,
                event_data=event_data or {},
                severity=severity
            )
            
            self._add_entry(entry)
            return entry
    
    def log_user_action(
        self,
        action_name: str,
        user_id: str,
        action_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        记录用户操作
        
        Args:
            action_name: 操作名称
            user_id: 用户 ID
            action_data: 操作数据
            
        Returns:
            创建的日志条目
        """
        with self._lock:
            entry = self._create_entry(
                DecisionType.USER_ACTION,
                action_name=action_name,
                user_id=user_id,
                action_data=action_data or {}
            )
            
            self._add_entry(entry)
            return entry
    
    def _add_entry(self, entry: Dict[str, Any]):
        """
        添加日志条目
        
        Args:
            entry: 日志条目字典
        """
        self.entries.append(entry)
        
        # 如果超过最大条目数，删除最旧的条目
        if len(self.entries) > self.max_entries:
            # 保留最新的 max_entries 条
            self.entries = self.entries[-self.max_entries:]
        
        # 保存到文件
        self._save_entries()
    
    def query(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        decision_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        查询日志条目
        
        Args:
            start_time: 开始时间（包含）
            end_time: 结束时间（包含）
            decision_type: 决策类型（'trade_decision', 'config_change', 等）
            symbol: 交易标的
            limit: 最大返回条目数，默认 100
            
        Returns:
            符合条件的日志条目列表
        """
        with self._lock:
            results = []
            
            for entry in self.entries:
                # 按类型过滤
                if decision_type and entry.get('type') != decision_type:
                    continue
                
                # 按标的过滤
                if symbol and entry.get('symbol') != symbol:
                    continue
                
                # 按时间范围过滤
                entry_time = _parse_datetime(entry.get('timestamp', ''))
                
                if entry_time:
                    if start_time and entry_time < start_time:
                        continue
                    if end_time and entry_time > end_time:
                        continue
                
                results.append(entry)
                
                # 达到限制
                if len(results) >= limit:
                    break
            
            return results
    
    def export_to_json(self, filepath: str) -> None:
        """
        导出日志到 JSON 文件
        
        Args:
            filepath: 导出文件路径
            
        Raises:
            IOError: 当文件写入失败时
        """
        with self._lock:
            try:
                self._ensure_directory()
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(self.entries, f, indent=2, ensure_ascii=False)
            except (IOError, OSError, PermissionError) as e:
                raise IOError(f"Failed to export JSON: {e}")
            except Exception as e:
                raise IOError(f"Unexpected error exporting JSON: {e}")
    
    def export_to_csv(self, filepath: str) -> None:
        """
        导出日志到 CSV 文件
        
        Args:
            filepath: 导出文件路径
            
        Raises:
            IOError: 当文件写入失败时
        """
        with self._lock:
            try:
                self._ensure_directory()
                
                if not self.entries:
                    # 如果没有数据，创建空文件
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write('')
                    return
                
                # 确定 CSV 列
                fieldnames = ['id', 'timestamp', 'type', 'symbol', 'action', 
                             'confidence', 'decision', 'result', 'key', 
                             'old_value', 'new_value', 'reason', 'event_name',
                             'severity', 'user_id', 'action_name']
                
                with open(filepath, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                    writer.writeheader()
                    
                    for entry in self.entries:
                        # 展平 context 和 metadata
                        row = dict(entry)
                        if 'context' in row and isinstance(row['context'], dict):
                            # 将 context 转为 JSON 字符串
                            row['context'] = json.dumps(row['context'], ensure_ascii=False)
                        if 'metadata' in row and isinstance(row['metadata'], dict):
                            row['metadata'] = json.dumps(row['metadata'], ensure_ascii=False)
                        if 'event_data' in row and isinstance(row['event_data'], dict):
                            row['event_data'] = json.dumps(row['event_data'], ensure_ascii=False)
                        if 'action_data' in row and isinstance(row['action_data'], dict):
                            row['action_data'] = json.dumps(row['action_data'], ensure_ascii=False)
                        
                        writer.writerow(row)
                        
            except (IOError, OSError, PermissionError) as e:
                raise IOError(f"Failed to export CSV: {e}")
            except Exception as e:
                raise IOError(f"Unexpected error exporting CSV: {e}")
    
    def clear_old_entries(self, days: int = 30) -> int:
        """
        清理指定天数之前的旧日志
        
        Args:
            days: 保留的天数，默认 30 天
            
        Returns:
            清理的条目数量
        """
        with self._lock:
            cutoff_time = datetime.now() - timedelta(days=days)
            original_count = len(self.entries)
            
            # 过滤掉旧的条目
            new_entries = []
            for entry in self.entries:
                entry_time = _parse_datetime(entry.get('timestamp', ''))
                if entry_time and entry_time >= cutoff_time:
                    new_entries.append(entry)
                elif entry_time is None:
                    # 无法解析时间的条目保留
                    new_entries.append(entry)
            
            cleared_count = original_count - len(new_entries)
            self.entries = new_entries
            
            # 保存更改
            self._save_entries()
            
            return cleared_count
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取日志统计信息
        
        Returns:
            统计信息字典，包含：
            - total_entries: 总条目数
            - by_type: 按类型统计
            - by_symbol: 按标的统计（仅交易决策）
            - date_range: 日期范围
        """
        with self._lock:
            stats = {
                'total_entries': len(self.entries),
                'by_type': {},
                'by_symbol': {},
                'date_range': {
                    'earliest': None,
                    'latest': None
                }
            }
            
            # 按类型统计
            for entry in self.entries:
                entry_type = entry.get('type', 'unknown')
                stats['by_type'][entry_type] = stats['by_type'].get(entry_type, 0) + 1
                
                # 按标的统计（仅交易决策）
                if entry_type == 'trade_decision':
                    symbol = entry.get('symbol', 'unknown')
                    stats['by_symbol'][symbol] = stats['by_symbol'].get(symbol, 0) + 1
            
            # 计算日期范围
            if self.entries:
                timestamps = []
                for entry in self.entries:
                    ts = _parse_datetime(entry.get('timestamp', ''))
                    if ts:
                        timestamps.append(ts)
                
                if timestamps:
                    stats['date_range']['earliest'] = min(timestamps).isoformat()
                    stats['date_range']['latest'] = max(timestamps).isoformat()
            
            return stats
    
    def get_entry(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """
        根据 ID 获取单个日志条目
        
        Args:
            entry_id: 日志条目 ID
            
        Returns:
            日志条目，如果不存在则返回 None
        """
        with self._lock:
            for entry in self.entries:
                if entry.get('id') == entry_id:
                    return entry.copy()
            return None
    
    def update_entry_result(self, entry_id: str, result: str) -> bool:
        """
        更新日志条目的执行结果
        
        Args:
            entry_id: 日志条目 ID
            result: 新的结果（'executed', 'failed', 'cancelled'）
            
        Returns:
            更新成功返回 True，失败返回 False
        """
        with self._lock:
            for entry in self.entries:
                if entry.get('id') == entry_id:
                    entry['result'] = result
                    self._save_entries()
                    return True
            return False
    
    def __len__(self) -> int:
        """返回日志条目数量"""
        with self._lock:
            return len(self.entries)
    
    def __repr__(self) -> str:
        """返回对象的字符串表示"""
        with self._lock:
            return f"DecisionLog(entries={len(self.entries)}, max_entries={self.max_entries})"
