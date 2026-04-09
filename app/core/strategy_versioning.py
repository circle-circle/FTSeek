"""
策略版本管理模块

追踪策略演进，支持回滚
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger("ftseek.core.strategy_versioning")
logger.propagate = False  # 防止日志重复

# 版本历史存储
VERSIONS_FILE = Path('/opt/ftseek/FTSeek/data/strategy_versions.json')


@dataclass
class StrategyVersion:
    """策略版本"""
    version: str
    date: str
    changes: List[str]
    performance: Dict
    config_snapshot: Dict
    
    def to_dict(self) -> Dict:
        return asdict(self)


class StrategyVersionManager:
    """策略版本管理器"""
    
    def __init__(self):
        self.versions: List[StrategyVersion] = []
        self._load_versions()
    
    def _load_versions(self):
        """加载版本历史"""
        if VERSIONS_FILE.exists():
            try:
                with open(VERSIONS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.versions = [StrategyVersion(**v) for v in data]
                    logger.info(f"加载策略版本：{len(self.versions)} 个")
            except Exception as e:
                logger.error(f"加载版本失败：{e}")
    
    def create_version(self,
                      version: str,
                      changes: List[str],
                      performance: Dict,
                      config_snapshot: Dict) -> StrategyVersion:
        """
        创建新版本
        
        Args:
            version: 版本号 (如 v1.0, v2.1)
            changes: 变更列表
            performance: 性能指标
            config_snapshot: 配置快照
        
        Returns:
            StrategyVersion: 版本对象
        """
        v = StrategyVersion(
            version=version,
            date=datetime.now().strftime('%Y-%m-%d'),
            changes=changes,
            performance=performance,
            config_snapshot=config_snapshot
        )
        
        self.versions.append(v)
        self._save_versions()
        
        logger.info(f"创建策略版本：{version}")
        return v
    
    def get_current_version(self) -> Optional[StrategyVersion]:
        """获取当前版本"""
        if self.versions:
            return self.versions[-1]
        return None
    
    def get_version(self, version: str) -> Optional[StrategyVersion]:
        """获取指定版本"""
        for v in self.versions:
            if v.version == version:
                return v
        return None
    
    def list_versions(self) -> List[Dict]:
        """列出所有版本"""
        return [
            {
                'version': v.version,
                'date': v.date,
                'performance': v.performance
            }
            for v in self.versions
        ]
    
    def compare_versions(self, v1_version: str, v2_version: str) -> Dict:
        """比较两个版本"""
        v1 = self.get_version(v1_version)
        v2 = self.get_version(v2_version)
        
        if not v1 or not v2:
            return {'error': '版本不存在'}
        
        comparison = {
            'v1': {
                'version': v1.version,
                'date': v1.date,
                'performance': v1.performance
            },
            'v2': {
                'version': v2.version,
                'date': v2.date,
                'performance': v2.performance
            },
            'improvements': {}
        }
        
        # 比较性能指标
        if 'sharpe' in v1.performance and 'sharpe' in v2.performance:
            comparison['improvements']['sharpe'] = v2.performance['sharpe'] - v1.performance['sharpe']
        
        if 'win_rate' in v1.performance and 'win_rate' in v2.performance:
            comparison['improvements']['win_rate'] = v2.performance['win_rate'] - v1.performance['win_rate']
        
        return comparison
    
    def rollback_to(self, version: str) -> bool:
        """回滚到指定版本"""
        target = self.get_version(version)
        
        if not target:
            logger.error(f"版本不存在：{version}")
            return False
        
        # 创建回滚版本
        rollback_version = f"rollback_{version}"
        self.create_version(
            version=rollback_version,
            changes=[f'回滚到 {version}', '恢复配置'],
            performance=target.performance,
            config_snapshot=target.config_snapshot
        )
        
        logger.info(f"回滚到版本：{version}")
        return True
    
    def _save_versions(self):
        """保存版本历史"""
        data = [v.to_dict() for v in self.versions]
        
        with open(VERSIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# 全局单例
_version_manager = None

def get_strategy_version_manager() -> StrategyVersionManager:
    """获取策略版本管理器单例"""
    global _version_manager
    if _version_manager is None:
        _version_manager = StrategyVersionManager()
    return _version_manager
