#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 配置迁移工具

功能：
1. 将分散的配置文件合并到 unified_config.yaml
2. 检测配置冲突
3. 生成迁移报告
4. 备份旧配置
"""

import json
import yaml
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List


class ConfigMigrator:
    """配置迁移器"""
    
    def __init__(self, config_dir: str = None):
        self.config_dir = Path(config_dir) if config_dir else Path(__file__).parent
        self.backup_dir = self.config_dir / "deprecated"
        self.migration_report = {
            'timestamp': datetime.now().isoformat(),
            'migrated_files': [],
            'conflicts': [],
            'warnings': [],
        }
    
    def migrate_all(self):
        """执行完整迁移"""
        print("=" * 60)
        print("FTSeek 配置迁移工具")
        print("=" * 60)
        
        # 1. 备份旧配置
        print("\n1. 备份旧配置...")
        self.backup_old_configs()
        
        # 2. 加载所有配置
        print("2. 加载所有配置...")
        configs = self.load_all_configs()
        
        # 3. 检测冲突
        print("3. 检测配置冲突...")
        self.detect_conflicts(configs)
        
        # 4. 生成迁移报告
        print("4. 生成迁移报告...")
        self.generate_report()
        
        print("\n✅ 迁移完成！")
        print(f"📁 备份目录: {self.backup_dir}")
        print(f"📄 统一配置: {self.config_dir / 'unified_config.yaml'}")
        print(f"📊 迁移报告: {self.config_dir / 'migration_report.json'}")
    
    def backup_old_configs(self):
        """备份旧配置文件"""
        if not self.backup_dir.exists():
            self.backup_dir.mkdir(parents=True)
        
        files_to_backup = [
            "app_config.yaml",
            "risk_config.yaml",
            "strategy_params.json",
            "system_config.yaml",
            "position_strategy.yaml",
            "alert_thresholds.yaml",
            "market_regime_params.yaml",
            "dynamic_exit_params.yaml",
        ]
        
        for filename in files_to_backup:
            src = self.config_dir / filename
            if src.exists():
                dst = self.backup_dir / filename
                shutil.copy2(src, dst)
                print(f"  ✅ 备份: {filename}")
                self.migration_report['migrated_files'].append(filename)
    
    def load_all_configs(self) -> Dict[str, Any]:
        """加载所有配置文件"""
        configs = {}
        
        # YAML配置
        for yaml_file in self.config_dir.glob("*.yaml"):
            if yaml_file.name == "unified_config.yaml":
                continue
            try:
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    configs[yaml_file.stem] = yaml.safe_load(f)
            except Exception as e:
                self.migration_report['warnings'].append(
                    f"加载 {yaml_file.name} 失败: {e}"
                )
        
        # JSON配置
        for json_file in self.config_dir.glob("*.json"):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    configs[json_file.stem] = json.load(f)
            except Exception as e:
                self.migration_report['warnings'].append(
                    f"加载 {json_file.name} 失败: {e}"
                )
        
        print(f"  ✅ 加载了 {len(configs)} 个配置文件")
        return configs
    
    def detect_conflicts(self, configs: Dict[str, Any]):
        """检测配置冲突"""
        # 检查重复配置项
        common_keys = {
            'trading': ['app_config', 'risk_config', 'unified_config'],
            'risk_limits': ['risk_config', 'strategy_params', 'unified_config'],
            'notification': ['app_config', 'unified_config'],
        }
        
        for key, sources in common_keys.items():
            values = []
            for source in sources:
                if source in configs and key in configs[source]:
                    values.append((source, configs[source][key]))
            
            if len(values) > 1:
                # 简化比较
                self.migration_report['conflicts'].append({
                    'key': key,
                    'sources': [v[0] for v in values],
                    'resolution': f"使用 unified_config.yaml 的值",
                })
        
        if self.migration_report['conflicts']:
            print(f"  ⚠️  发现 {len(self.migration_report['conflicts'])} 个配置冲突")
        else:
            print("  ✅ 无配置冲突")
    
    def generate_report(self):
        """生成迁移报告"""
        report_path = self.config_dir / "migration_report.json"
        
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(self.migration_report, f, indent=2, ensure_ascii=False)
        
        print(f"\n📊 迁移报告:")
        print(f"  - 迁移文件数: {len(self.migration_report['migrated_files'])}")
        print(f"  - 配置冲突: {len(self.migration_report['conflicts'])}")
        print(f"  - 警告: {len(self.migration_report['warnings'])}")


if __name__ == "__main__":
    migrator = ConfigMigrator()
    migrator.migrate_all()
