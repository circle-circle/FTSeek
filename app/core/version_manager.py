"""
VersionManager - 版本管理器

功能:
- 版本号管理
- 版本历史记录
- 版本兼容性检查
- 支持回滚
"""

import os
import json
import shutil
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import logging

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog

logger = logging.getLogger("ftseek.core.version_manager")
logger.propagate = False  # 防止日志重复


class VersionManager:
    """版本管理器"""
    
    def __init__(self, versions_dir: str = 'config/versions'):
        """
        初始化版本管理器
        
        Args:
            versions_dir: 版本目录
        """
        self.versions_dir = versions_dir
        self._state_mgr = StateManager()
        self._decision_log = DecisionLog()
        
        # 确保目录存在
        os.makedirs(versions_dir, exist_ok=True)
        
        # 当前版本
        self.current_version = self._state_mgr.get('current_version', '4.3.0')
    
    def get_current_version(self) -> str:
        """获取当前版本"""
        return self.current_version
    
    def set_current_version(self, version: str) -> None:
        """设置当前版本"""
        old_version = self.current_version
        self.current_version = version
        self._state_mgr.set('current_version', version)
        
        self._decision_log.log_config_change(
            key='current_version',
            old_value=old_version,
            new_value=version,
            reason='Version update'
        )
        
        logger.info(f"Current version set to: {version}")
    
    def create_version_snapshot(self, version: str, config: Dict[str, Any]) -> str:
        """创建版本快照"""
        snapshot_file = os.path.join(self.versions_dir, f"v{version}.json")
        
        snapshot = {
            'version': version,
            'created_at': datetime.now().isoformat(),
            'config': config,
            'metadata': {
                'python_version': '3.8',
                'ftseek_version': version,
            }
        }
        
        with open(snapshot_file, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Version snapshot created: v{version}")
        return snapshot_file
    
    def list_versions(self) -> List[Dict]:
        """列出所有可用版本"""
        versions = []
        
        try:
            for filename in os.listdir(self.versions_dir):
                if filename.endswith('.json'):
                    filepath = os.path.join(self.versions_dir, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            versions.append({
                                'version': data.get('version', filename.replace('.json', '')),
                                'created_at': data.get('created_at', ''),
                                'file': filename
                            })
                    except (json.JSONDecodeError, IOError) as e:
                        logger.debug(f"读取版本文件 {filename} 失败：{e}")
                        pass
        except OSError as e:
            logger.debug(f"扫描版本目录失败：{e}")
            pass
        
        versions.sort(key=lambda x: x['version'], reverse=True)
        return versions
    
    def get_version(self, version: str) -> Optional[Dict]:
        """获取指定版本配置"""
        snapshot_file = os.path.join(self.versions_dir, f"v{version}.json")
        
        if os.path.exists(snapshot_file):
            try:
                with open(snapshot_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load version: {e}")
                return None
        
        return None
    
    def rollback_to_version(self, version: str) -> bool:
        """回滚到指定版本"""
        version_data = self.get_version(version)
        
        if not version_data:
            logger.error(f"Version not found: {version}")
            return False
        
        # 保存当前版本
        self.create_version_snapshot(
            f"{self.current_version}_pre_rollback",
            {'rollback_from': self.current_version}
        )
        
        # 回滚
        old_version = self.current_version
        self.set_current_version(version)
        
        self._decision_log.log_system_event(
            event='version_rollback',
            message=f"Rolled back from {old_version} to {version}"
        )
        
        logger.info(f"Rolled back to version: {version}")
        return True
    
    def check_compatibility(self, from_version: str, to_version: str) -> Tuple[bool, List[str]]:
        """检查版本兼容性"""
        warnings = []
        
        # 简单的版本兼容性检查
        from_parts = from_version.split('.')
        to_parts = to_version.split('.')
        
        # 主版本号变化 - 重大变更
        if from_parts[0] != to_parts[0]:
            warnings.append(f"Major version change: {from_version} → {to_version}")
            warnings.append("Breaking changes may exist")
        
        # 次版本号变化 - 新功能
        if from_parts[1] != to_parts[1]:
            warnings.append(f"Minor version change: {from_version} → {to_version}")
            warnings.append("New features added, backward compatible")
        
        is_compatible = len(warnings) == 0 or from_parts[0] == to_parts[0]
        
        return is_compatible, warnings
    
    def get_migration_guide(self, from_version: str, to_version: str) -> str:
        """获取迁移指南"""
        guide = f"# Migration Guide: {from_version} → {to_version}\n\n"
        
        is_compatible, warnings = self.check_compatibility(from_version, to_version)
        
        if warnings:
            guide += "## ⚠️ Warnings\n\n"
            for warning in warnings:
                guide += f"- {warning}\n"
            guide += "\n"
        
        guide += "## Steps\n\n"
        guide += "1. Backup current configuration\n"
        guide += "2. Review changes\n"
        guide += "3. Apply migration\n"
        guide += "4. Test functionality\n"
        
        return guide
