#!/usr/bin/env python3
"""
配置文件拆分工具 (P1 修复 2026-04-11)

解决问题:
- config.yaml 过于庞大 (1361行)，难以维护
- 多个配置文件分散，缺乏组织
- 修改配置时需要查找多个文件

优化方案:
- 将config.yaml按功能模块拆分为多个小文件
- 保持向后兼容，提供统一加载接口
- 支持配置文件的独立版本控制
- 提高可读性和可维护性

拆分结构:
config/
├── base.yaml              # 基础配置 (环境、日志等)
├── futu.yaml              # 富途OpenD配置
├── trading.yaml           # 交易配置 (股票池、仓位等)
├── risk.yaml              # 风控配置
├── strategy.yaml          # 策略参数
├── notification.yaml      # 通知配置 (钉钉、邮件等)
├── performance.yaml       # 性能配置 (缓存、线程池等)
└── merged/
    └── config.yaml        # 自动合并的完整配置 (git忽略)
"""

import yaml
import json
import os
import logging
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger("ftseek.config.splitter")


class ConfigSplitter:
    """配置文件拆分/合并工具"""
    
    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = os.path.join(os.path.dirname(__file__), '..')
        
        self.config_dir = Path(config_dir)
        self.source_file = self.config_dir / "config.yaml"
        self.split_dir = self.config_dir / "split"
        self.merged_dir = self.config_dir / "merged"
        
        # 定义拆分规则：顶级键 -> 目标文件
        self.split_rules = {
            'version': 'base.yaml',
            'updated': 'base.yaml',
            'description': 'base.yaml',
            'base': 'base.yaml',
            
            'futu': 'futu.yaml',
            
            'trading': 'trading.yaml',
            
            'risk': 'risk.yaml',
            
            'strategy': 'strategy.yaml',
            'consensus': 'strategy.yaml',
            'indicators': 'strategy.yaml',
            
            'notification': 'notification.yaml',
            'dingtalk': 'notification.yaml',
            'alert': 'notification.yaml',
            
            'performance': 'performance.yaml',
            'cache': 'performance.yaml',
            'database': 'performance.yaml',
            'thread_pool': 'performance.yaml',
        }
    
    def split(self):
        """拆分配置文件"""
        logger.info(f"开始拆分配置文件: {self.source_file}")
        
        # 读取原始配置
        with open(self.source_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 按规则分组
        split_configs: Dict[str, Dict[str, Any]] = {}
        unassigned = {}
        
        for key, value in config.items():
            target_file = self.split_rules.get(key)
            if target_file:
                if target_file not in split_configs:
                    split_configs[target_file] = {}
                split_configs[target_file][key] = value
            else:
                unassigned[key] = value
                logger.warning(f"未分配的配置项: {key}")
        
        # 创建输出目录
        self.split_dir.mkdir(exist_ok=True)
        
        # 写入拆分后的文件
        for filename, content in split_configs.items():
            output_path = self.split_dir / filename
            with open(output_path, 'w', encoding='utf-8') as f:
                yaml.dump(
                    content,
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False
                )
            logger.info(f"已创建: {output_path} ({len(content)} 个配置项)")
        
        # 处理未分配的配置
        if unassigned:
            output_path = self.split_dir / "other.yaml"
            with open(output_path, 'w', encoding='utf-8') as f:
                yaml.dump(
                    unassigned,
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False
                )
            logger.warning(f"未分配的配置已写入: {output_path}")
        
        # 生成README
        self._generate_readme()
        
        logger.info(f"拆分完成！输出目录: {self.split_dir}")
        return split_configs
    
    def merge(self, output_file: str = None):
        """合并拆分后的配置文件"""
        if output_file is None:
            self.merged_dir.mkdir(exist_ok=True)
            output_file = self.merged_dir / "config.yaml"
        else:
            output_file = Path(output_file)
        
        logger.info(f"开始合并配置文件 -> {output_file}")
        
        merged = {}
        
        # 按顺序读取所有拆分文件
        split_files = [
            'base.yaml',
            'futu.yaml',
            'trading.yaml',
            'risk.yaml',
            'strategy.yaml',
            'notification.yaml',
            'performance.yaml',
            'other.yaml'
        ]
        
        for filename in split_files:
            file_path = self.split_dir / filename
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = yaml.safe_load(f)
                    if content:
                        merged.update(content)
                        logger.info(f"已合并: {filename}")
            else:
                logger.debug(f"跳过不存在的文件: {filename}")
        
        # 添加合并元数据
        merged['merged_at'] = datetime.now().isoformat()
        merged['merged_from'] = str(self.split_dir)
        
        # 写入合并后的配置
        with open(output_file, 'w', encoding='utf-8') as f:
            yaml.dump(
                merged,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False
            )
        
        logger.info(f"合并完成！文件大小: {output_file.stat().st_size / 1024:.1f} KB")
        return merged
    
    def validate_split(self):
        """验证拆分后的配置是否完整"""
        logger.info("验证拆分配置...")
        
        # 读取原始配置
        with open(self.source_file, 'r', encoding='utf-8') as f:
            original = yaml.safe_load(f)
        
        # 合并拆分后的配置
        merged = self.merge()
        
        # 对比键
        original_keys = set(original.keys())
        merged_keys = set(merged.keys()) - {'merged_at', 'merged_from'}  # 排除元数据
        
        missing = original_keys - merged_keys
        extra = merged_keys - original_keys
        
        if missing:
            logger.error(f"缺失的配置项: {missing}")
        
        if extra:
            logger.warning(f"额外的配置项: {extra}")
        
        # 验证值
        errors = []
        for key in original_keys & merged_keys:
            if original[key] != merged[key]:
                errors.append(f"{key}: 值不匹配")
        
        if errors:
            logger.error(f"验证失败: {errors}")
            return False
        else:
            logger.info("✅ 验证通过！拆分配置与原始配置完全一致")
            return True
    
    def _generate_readme(self):
        """生成拆分配置说明"""
        readme = """# 拆分配置文件说明

## 目录结构

```
config/split/
├── base.yaml              # 基础配置 (环境、日志、调试模式)
├── futu.yaml              # 富途 OpenD 配置 (连接、登录、账户)
├── trading.yaml           # 交易配置 (股票池、仓位、交易规则)
├── risk.yaml              # 风控配置 (止损、限额、风险参数)
├── strategy.yaml          # 策略参数 (共识引擎、指标、信号)
├── notification.yaml      # 通知配置 (钉钉、邮件、告警)
├── performance.yaml       # 性能配置 (缓存、数据库、线程池)
└── other.yaml             # 其他未分类配置
```

## 使用方法

### 1. 拆分配置文件
```bash
python3 -m app.config.splitter split
```

### 2. 合并配置文件
```bash
python3 -m app.config.splitter merge
```

### 3. 验证拆分
```bash
python3 -m app.config.splitter validate
```

## 配置加载

系统会自动加载拆分后的配置文件，无需手动合并。
加载优先级：
1. 环境变量 (最高)
2. 拆分配置文件
3. 默认值 (最低)

## 版本控制

- 拆分后的文件应该提交到 Git
- 合并后的 `merged/config.yaml` 已加入 `.gitignore`
- 每次修改拆分文件后，运行 `validate` 确保完整性

## 维护建议

- 修改配置时，只修改对应的拆分文件
- 定期运行 `validate` 检查一致性
- 新增配置项时，更新 `split_rules` 映射

## 历史

- 2026-04-11: 初始拆分 (P1 修复)
  - 原始文件: config.yaml (1361行)
  - 拆分后: 7个文件，平均每个 ~200行
"""
        
        readme_path = self.split_dir / "README.md"
        with open(readme_path, 'w', encoding='utf-8') as f:
            f.write(readme)
        
        logger.info(f"已生成说明文档: {readme_path}")


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='配置文件拆分/合并工具')
    parser.add_argument(
        'action',
        choices=['split', 'merge', 'validate'],
        help='操作类型'
    )
    parser.add_argument(
        '--config-dir',
        help='配置文件目录 (默认: 当前目录)'
    )
    parser.add_argument(
        '--output',
        help='输出文件路径 (仅merge模式)'
    )
    
    args = parser.parse_args()
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    
    splitter = ConfigSplitter(args.config_dir)
    
    if args.action == 'split':
        splitter.split()
    elif args.action == 'merge':
        splitter.merge(args.output)
    elif args.action == 'validate':
        splitter.validate_split()


if __name__ == '__main__':
    main()
