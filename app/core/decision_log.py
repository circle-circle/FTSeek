"""
DecisionLog - 决策日志系统

功能:
- 记录交易决策（买入/卖出/跳过）
- 记录配置变更
- 记录系统事件
- 支持查询和过滤
- 支持导出（JSON/CSV）
- 自动清理过期日志
"""

import json
import os
import csv
import uuid
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from enum import Enum
import logging

logger = logging.getLogger("ftseek.core.decision_log")


class DecisionType(str, Enum):
    """决策类型"""
    TRADE_DECISION = "trade_decision"
    CONFIG_CHANGE = "config_change"
    SYSTEM_EVENT = "system_event"
    USER_ACTION = "user_action"


class DecisionLog:
    """决策日志系统"""
    
    def __init__(self, log_file: str = 'data/decision_log.json', max_entries: int = 10000):
        """
        初始化决策日志
        
        Args:
            log_file: 日志文件路径
            max_entries: 最大条目数
        """
        self.log_file = log_file
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self._entries: List[Dict] = []
        
        # 确保目录存在
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        # 自动加载
        self._load()
    
    def _load(self) -> None:
        """加载日志"""
        with self._lock:
            try:
                if os.path.exists(self.log_file):
                    with open(self.log_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self._entries = data.get('entries', [])
                    logger.info(f"Loaded {len(self._entries)} log entries")
            except Exception as e:
                logger.error(f"Failed to load log: {e}")
                self._entries = []
    
    def _save(self) -> None:
        """保存日志"""
        with self._lock:
            try:
                data = {
                    'version': '4.3.0',
                    'last_updated': datetime.now().isoformat(),
                    'entry_count': len(self._entries),
                    'entries': self._entries[-self.max_entries:]  # 保留最新条目
                }
                with open(self.log_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            except Exception as e:
                logger.error(f"Failed to save log: {e}")
    
    def _create_entry(self, decision_type: DecisionType, **kwargs) -> Dict:
        """创建日志条目"""
        return {
            'id': str(uuid.uuid4()),
            'timestamp': datetime.now().isoformat(),
            'type': decision_type.value,
            **kwargs
        }
    
    def log_trade_decision(
        self,
        symbol: str,
        action: str,
        confidence: float,
        decision: str,
        context: Optional[Dict] = None,
        result: Optional[str] = None
    ) -> Dict:
        """记录交易决策"""
        entry = self._create_entry(
            DecisionType.TRADE_DECISION,
            symbol=symbol,
            action=action,
            confidence=confidence,
            decision=decision,
            result=result or 'pending',
            context=context or {}
        )
        
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                self._entries = self._entries[-self.max_entries:]
            self._save()
        
        logger.info(f"Trade decision logged: {symbol} {action} - {decision}")
        return entry
    
    def log_config_change(
        self,
        key: str,
        old_value: Any,
        new_value: Any,
        reason: str = ""
    ) -> Dict:
        """记录配置变更"""
        entry = self._create_entry(
            DecisionType.CONFIG_CHANGE,
            key=key,
            old_value=old_value,
            new_value=new_value,
            reason=reason
        )
        
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                self._entries = self._entries[-self.max_entries:]
            self._save()
        
        logger.info(f"Config change logged: {key} changed")
        return entry
    
    def log_system_event(
        self,
        event: str,
        message: str,
        data: Optional[Dict] = None
    ) -> Dict:
        """记录系统事件"""
        entry = self._create_entry(
            DecisionType.SYSTEM_EVENT,
            event=event,
            message=message,
            data=data or {}
        )
        
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                self._entries = self._entries[-self.max_entries:]
            self._save()
        
        return entry
    
    def log_user_action(
        self,
        user_id: str,
        action: str,
        details: Optional[Dict] = None
    ) -> Dict:
        """记录用户操作"""
        entry = self._create_entry(
            DecisionType.USER_ACTION,
            user_id=user_id,
            action=action,
            details=details or {}
        )
        
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                self._entries = self._entries[-self.max_entries:]
            self._save()
        
        return entry
    
    def query(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        decision_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict]:
        """查询日志"""
        with self._lock:
            results = []
            
            for entry in reversed(self._entries):  # 从新到旧
                # 时间过滤 (Python 3.6 兼容)
                if start_time:
                    entry_time = datetime.strptime(entry['timestamp'][:19], '%Y-%m-%dT%H:%M:%S')
                    if entry_time < start_time:
                        continue
                
                if end_time:
                    entry_time = datetime.strptime(entry['timestamp'][:19], '%Y-%m-%dT%H:%M:%S')
                    if entry_time > end_time:
                        continue
                
                # 类型过滤
                if decision_type and entry.get('type') != decision_type:
                    continue
                
                # 标的过滤
                if symbol and entry.get('symbol') != symbol:
                    continue
                
                results.append(entry)
                
                if len(results) >= limit:
                    break
            
            return results
    
    def export_to_json(self, filepath: str) -> bool:
        """导出为 JSON"""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump({'entries': self._entries}, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f"Exported {len(self._entries)} entries to {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to export JSON: {e}")
            return False
    
    def export_to_csv(self, filepath: str) -> bool:
        """导出为 CSV"""
        try:
            if not self._entries:
                return False
            
            fieldnames = ['id', 'timestamp', 'type', 'symbol', 'action', 'decision', 'confidence']
            
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(self._entries)
            
            logger.info(f"Exported {len(self._entries)} entries to CSV")
            return True
        except Exception as e:
            logger.error(f"Failed to export CSV: {e}")
            return False
    
    def clear_old_entries(self, days: int = 30) -> int:
        """清理旧日志"""
        with self._lock:
            cutoff = datetime.now() - timedelta(days=days)
            original_count = len(self._entries)
            
            self._entries = [
                e for e in self._entries
                if datetime.fromisoformat(e['timestamp']) > cutoff
            ]
            
            removed = original_count - len(self._entries)
            self._save()
            
            logger.info(f"Cleared {removed} entries older than {days} days")
            return removed
    
    def get_statistics(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            stats = {
                'total_entries': len(self._entries),
                'by_type': {},
                'by_symbol': {},
                'date_range': {
                    'earliest': None,
                    'latest': None
                }
            }
            
            if self._entries:
                timestamps = [e['timestamp'] for e in self._entries]
                stats['date_range']['earliest'] = min(timestamps)
                stats['date_range']['latest'] = max(timestamps)
                
                for entry in self._entries:
                    # 按类型统计
                    entry_type = entry.get('type', 'unknown')
                    stats['by_type'][entry_type] = stats['by_type'].get(entry_type, 0) + 1
                    
                    # 按标的统计
                    if 'symbol' in entry:
                        symbol = entry['symbol']
                        stats['by_symbol'][symbol] = stats['by_symbol'].get(symbol, 0) + 1
            
            return stats
    
    def get_entry_count(self) -> int:
        """获取条目数"""
        return len(self._entries)
